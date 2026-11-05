#!/usr/bin/env python3
"""Sincroniza la contraseña del rol existente con Compose sin borrar datos.

Uso: python3 scripts/reparar_postgres.py
No cambia .env, no publica secretos y no elimina volúmenes.
"""
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC_PASSWORD_SQL = r"""\getenv role POSTGRES_USER
\getenv password POSTGRES_PASSWORD
ALTER ROLE :"role" WITH PASSWORD :'password';
"""
LOCAL_PSQL = ('exec psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" '
              '--set ON_ERROR_STOP=1')
TCP_CHECK = ('PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -h 127.0.0.1 '
             '-U "$POSTGRES_USER" -d "$POSTGRES_DB" '
             '--set ON_ERROR_STOP=1 -Atqc "SELECT 1"')


class RepairError(RuntimeError):
    pass


def compose(*args, input=None):
    try:
        # Nunca imprimir stdout/stderr de comandos fallidos: pueden contener SQL
        # interpolado con credenciales. Los diagnósticos públicos son propios.
        return subprocess.run(['docker', 'compose', *args], cwd=ROOT,
                              input=input, text=True, capture_output=True, check=True)
    except FileNotFoundError:
        raise RepairError('No se encontró Docker en el PATH.') from None


def esperar_postgres(timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        try:
            # Solo readiness: todavía debe poder entrar con la clave desincronizada.
            compose('exec', '-T', 'matriculas-db', 'sh', '-eu', '-c',
                    'exec pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"')
            return
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise RepairError('PostgreSQL no aceptó conexiones en 60 segundos.') from None
            time.sleep(1)


def esperar_healthcheck(timeout=60):
    # Tras cambiar la contraseña, Docker puede conservar "unhealthy" hasta
    # su siguiente chequeo. No arrancar dependientes con ese estado antiguo.
    deadline = time.monotonic() + timeout
    while True:
        output = compose('ps', '--format', 'json', 'matriculas-db').stdout.strip()
        if output:
            try:
                rows = json.loads(output)
            except json.JSONDecodeError:
                rows = [json.loads(line) for line in output.splitlines()]
            if isinstance(rows, dict):
                rows = [rows]
            if any(row.get('Service') == 'matriculas-db' and row.get('Health') == 'healthy' for row in rows):
                return
        if time.monotonic() >= deadline:
            raise RepairError('La contraseña ya se verificó por TCP, pero el '
                              'healthcheck de PostgreSQL no llegó a healthy.')
        time.sleep(1)


def reparar():
    try:
        compose('config', '--quiet')
        # Detectar permisos insuficientes antes de detener o recrear servicios.
        compose('ps', '-a')
    except subprocess.CalledProcessError:
        raise RepairError('No se pudo acceder a Docker o resolver Compose. '
                          'Comprueba docker compose ps -a desde tu terminal.') from None
    try:
        compose('up', '-d', 'matriculas-db')
        esperar_postgres()
        # Socket local de la imagen oficial; no se modifica pg_hba.conf ni se
        # habilita trust para conexiones de red. psql escapa rol y contraseña.
        compose('exec', '-T', '--user', 'postgres', 'matriculas-db',
                'sh', '-eu', '-c', LOCAL_PSQL, input=SYNC_PASSWORD_SQL)
        compose('exec', '-T', 'matriculas-db', 'sh', '-eu', '-c', TCP_CHECK)
    except subprocess.CalledProcessError:
        raise RepairError('No se pudo sincronizar/verificar la contraseña. '
                          'Revisa los logs de matriculas-db. El rol y la base '
                          'deben existir; no se han borrado datos.') from None
    esperar_healthcheck()
    print('Contraseña sincronizada y autenticación TCP verificada.', flush=True)
    try:
        # Una recreación aplica el entorno actual y descarta el healthcheck viejo.
        compose('up', '-d', '--force-recreate', '--wait', '--wait-timeout', '120', 'matriculas-api')
        compose('up', '-d', '--wait', '--wait-timeout', '120', 'orquestador', 'gateway')
    except subprocess.CalledProcessError:
        raise RepairError('PostgreSQL ya autenticó correctamente, pero un servicio '
                          'no arrancó. Ejecuta docker compose logs --tail=100 '
                          'matriculas-api orquestador gateway.') from None
    print(compose('ps', '-a').stdout, end='')


if __name__ == '__main__':
    try:
        reparar()
    except RepairError as exc:
        raise SystemExit(str(exc)) from None
