#!/usr/bin/env python3
"""Sincroniza la contraseña del rol existente con Compose sin borrar datos.

Uso: python3 scripts/reparar_postgres.py [--comprobar]
No cambia .env, no publica secretos y no elimina volúmenes.
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC_PASSWORD_SQL = r"""\getenv role POSTGRES_USER
\getenv password POSTGRES_PASSWORD
ALTER ROLE :"role" WITH PASSWORD :'password';
"""
PASSWORD_ENV = ('if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then '
                'POSTGRES_PASSWORD="$(cat "$POSTGRES_PASSWORD_FILE")"; export POSTGRES_PASSWORD; fi; ')
LOCAL_PSQL = (PASSWORD_ENV + 'exec psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" '
              '--set ON_ERROR_STOP=1')
TCP_CHECK = (PASSWORD_ENV + 'PGCONNECT_TIMEOUT=3 PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -h 127.0.0.1 '
             '-U "$POSTGRES_USER" -d "$POSTGRES_DB" '
             '--set ON_ERROR_STOP=1 -Atqc "SELECT 1"')


class RepairError(RuntimeError):
    pass


def compose(*args, input=None):
    return docker('compose', *args, input=input)


def docker(*args, input=None):
    try:
        # Nunca imprimir stdout/stderr de comandos fallidos: pueden contener SQL
        # interpolado con credenciales. Los diagnósticos públicos son propios.
        return subprocess.run(['docker', *args], cwd=ROOT,
                              input=input, text=True, capture_output=True, check=True)
    except FileNotFoundError:
        raise RepairError('No se encontró Docker en el PATH.') from None


def comprobar():
    """Solo lectura: modelo actual, contenedor, imagen local y autenticación real."""
    try:
        config = json.loads(compose('config', '--format', 'json').stdout)
        expected = config['services']['matriculas-db']
        cid = compose('ps', '-a', '-q', 'matriculas-db').stdout.strip()
        if not cid:
            raise RepairError('PostgreSQL no tiene un contenedor creado.')
        running = json.loads(docker('inspect', cid).stdout)[0]
        if not running['State'].get('Running'):
            raise RepairError('PostgreSQL no está en ejecución; revisa la versión del volumen y sus logs.')
        image = json.loads(docker('image', 'inspect', expected['image']).stdout)[0]
        if running['Image'] != image['Id']:
            raise RepairError('La imagen de PostgreSQL en ejecución difiere de la imagen local declarada; requiere recreación.')
        actual = dict(item.split('=', 1) for item in running['Config'].get('Env', []) if '=' in item)
        wanted = expected['environment']
        for name in ('POSTGRES_USER', 'POSTGRES_DB'):
            if str(wanted.get(name, '')) != actual.get(name, ''):
                raise RepairError('El usuario o base del contenedor difiere de Compose; requiere revisión manual.')
        password = wanted.get('POSTGRES_PASSWORD') or config['services'].get('inicializar', {}).get(
            'environment', {}).get('MATRICULAS_DB_PASSWORD')
        actual_password = actual.get('POSTGRES_PASSWORD')
        if wanted.get('POSTGRES_PASSWORD_FILE'):
            if wanted['POSTGRES_PASSWORD_FILE'] != actual.get('POSTGRES_PASSWORD_FILE'):
                raise RepairError('El origen de las credenciales del contenedor difiere de Compose; requiere recreación.')
            actual_password = compose('exec', '-T', 'matriculas-db', 'sh', '-eu', '-c',
                                      'cat "$POSTGRES_PASSWORD_FILE"').stdout
        if password and password != actual_password:
            raise RepairError('Las credenciales configuradas difieren de las del contenedor; requiere recreación y comprobación.')
        compose('exec', '-T', 'matriculas-db', 'sh', '-eu', '-c', TCP_CHECK)
        version = compose('exec', '-T', 'matriculas-db', 'sh', '-eu', '-c',
                          TCP_CHECK.replace('SELECT 1', 'SHOW server_version_num')).stdout.strip()
        major = re.search(r'postgres:(\d+)', expected['image'])
        if major and int(version) // 10000 != int(major[1]):
            raise RepairError('La versión mayor de PostgreSQL no coincide con la declarada; requiere migración manual.')
    except (subprocess.CalledProcessError, ValueError, KeyError, IndexError):
        raise RepairError('No se pudo comprobar PostgreSQL o la autenticación TCP. No se modificó la instalación.') from None
    print('PostgreSQL comprobado: configuración, imagen, versión y autenticación TCP coinciden. Sin modificaciones.')


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
        model = json.loads(compose('config', '--format', 'json').stdout)['services']['matriculas-db']
        cid = compose('ps', '-a', '-q', 'matriculas-db').stdout.strip()
        if cid:
            running = json.loads(docker('inspect', cid).stdout)[0]
            if running['State'].get('Running'):
                version = compose('exec', '-T', '--user', 'postgres', 'matriculas-db', 'sh', '-eu', '-c',
                                  LOCAL_PSQL + ' -Atqc "SHOW server_version_num"').stdout.strip()
                major = re.search(r'postgres:(\d+)', model['image'])
                if major and int(version) // 10000 != int(major[1]):
                    raise RepairError('La versión mayor del volumen requiere migración manual; no se recreó PostgreSQL.')
    except (subprocess.CalledProcessError, ValueError, KeyError, IndexError):
        raise RepairError('No se pudo acceder a Docker o resolver Compose. '
                          'Comprueba docker compose ps -a desde tu terminal.') from None
    try:
        compose('up', '-d', 'matriculas-db')
        esperar_postgres()
        # Socket local de la imagen oficial; no se modifica pg_hba.conf ni se
        # habilita trust para conexiones de red. psql escapa rol y contraseña.
        try:
            compose('exec', '-T', 'matriculas-db', 'sh', '-eu', '-c', TCP_CHECK)
        except subprocess.CalledProcessError:
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
        compose('up', '-d', '--build', '--force-recreate', '--wait', '--wait-timeout', '120', 'matriculas-api', 'pagos')
        compose('up', '-d', '--build', '--wait', '--wait-timeout', '120', 'orquestador', 'gateway')
    except subprocess.CalledProcessError:
        raise RepairError('PostgreSQL ya autenticó correctamente, pero un servicio '
                          'no arrancó. Ejecuta docker compose logs --tail=100 '
                          'matriculas-api orquestador gateway.') from None
    print(compose('ps', '-a').stdout, end='')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comprobar', action='store_true', help='solo diagnostica; no cambia roles, contenedores ni datos')
    args = parser.parse_args()
    try:
        comprobar() if args.comprobar else reparar()
    except RepairError as exc:
        raise SystemExit(str(exc)) from None
