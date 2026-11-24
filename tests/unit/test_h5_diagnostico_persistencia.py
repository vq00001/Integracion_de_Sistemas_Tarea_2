"""H5 RED, adicional: comprobar PostgreSQL y datos persistidos sin modificarlos.

El CLI de reparación se ejecuta completo sobre Docker simulado: nunca alcanza
los contenedores reales. Se separa el diagnóstico --comprobar de la reparación.
El desfase de sagas se reproduce con una base SQLite temporal con datos.
"""
import importlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import ROOT
from tests.unit.test_api_unidad import m  # noqa: F401


class Docker:
    """Estado observable del daemon; solo ALTER ROLE cambia la autenticación."""
    def __init__(self, auth_ok=True, drift=None):
        self.auth_ok, self.drift = auth_ok, drift
        self.password = 'h5-fake-private-db-password-not-for-output'
        self.mutations = []

    def run(self, command, *, input=None, **kwargs):
        assert command[0] == 'docker', f'Comando externo no simulado: {command[0]}'
        out = ''
        current_image = 'sha256:current-postgres-image'
        runtime_image = 'sha256:old-postgres-image' if self.drift == 'imagen' else current_image
        env = {'POSTGRES_USER': 'matriculas_user', 'POSTGRES_DB': 'matriculas_db',
               'POSTGRES_PASSWORD': self.password}
        if command[1] == 'compose':
            verb = command[2]
            if verb == 'config':
                out = json.dumps({'name': 'h5-diagnostico', 'services': {
                    'matriculas-db': {'image': 'postgres:16-alpine', 'environment': env}}})
            elif verb == 'ps':
                row = {'ID': 'h5-db-container', 'Service': 'matriculas-db', 'State': 'running',
                       'Health': 'healthy', 'Image': 'postgres:16-alpine'}
                out = 'h5-db-container' if '-q' in command else json.dumps(row)
            elif verb == 'exec':
                statement = (' '.join(command) + '\n' + (input or '')).lower()
                if 'alter role' in statement or 'alter user' in statement:
                    self.mutations.append(('sql', input))
                    self.auth_ok = True
                elif 'server_version_num' in statement:
                    out = '150015\n' if self.drift == 'version' else '160015\n'
                elif 'psql' in statement and ('127.0.0.1' in statement or 'pgpassword' in statement):
                    if not self.auth_ok:
                        raise subprocess.CalledProcessError(1, command, stderr=self.password)
                    out = '1\n'
            elif verb in ('up', 'restart', 'down', 'rm', 'stop'):
                self.mutations.append((verb, command))
            else:
                raise AssertionError(f'Operación Compose sin simular: {verb}')
        elif command[1:3] == ['image', 'inspect']:
            out = json.dumps([{'Id': current_image, 'RepoTags': ['postgres:16-alpine']}])
        elif command[1] == 'inspect':
            runtime_env = dict(env)
            if self.drift == 'credenciales':
                runtime_env['POSTGRES_PASSWORD'] = 'h5-fake-password-from-old-container'
            out = json.dumps([{'Id': 'h5-db-container', 'Image': runtime_image,
                               'Config': {'Image': 'postgres:16-alpine',
                                          'Env': [f'{key}={value}' for key, value in runtime_env.items()]},
                               'State': {'Running': True, 'Health': {'Status': 'healthy'}}}])
        else:
            raise AssertionError(f'Operación Docker sin simular: {command[1:3]}')
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr='')


def _cli(tmp_path, monkeypatch, capsys, docker, *args):
    folder = tmp_path / 'repo'
    shutil.copytree(ROOT / 'scripts', folder / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT / 'docker-compose.yml', folder / 'docker-compose.yml')
    shutil.copy2(ROOT / '.env.example', folder / '.env.example')
    env_file = folder / '.env'
    env_file.write_text(f'MATRICULAS_DB_PASSWORD={docker.password}\n', encoding='utf-8')
    before = env_file.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, 'run', docker.run)
        patch.setattr(sys, 'argv', ['reparar_postgres.py', *args])
        patch.syspath_prepend(str(folder))
        try:
            runpy.run_path(str(folder / 'scripts/reparar_postgres.py'), run_name='__main__')
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1 if exc.code else 0
            if isinstance(exc.code, str):
                print(exc.code, file=sys.stderr)
    captured = capsys.readouterr()
    assert env_file.read_bytes() == before, 'El diagnóstico o reparación reescribió .env'
    assert docker.password not in captured.out + captured.err, 'Se publicó una credencial'
    return SimpleNamespace(code=code, output=captured.out + captured.err)


def test_h5_comprobar_postgres_sano_no_altera_roles_ni_recrea_servicios(tmp_path, monkeypatch, capsys):
    docker = Docker()
    result = _cli(tmp_path, monkeypatch, capsys, docker, '--comprobar')
    assert result.code == 0, result.output
    assert docker.mutations == [], 'El modo de comprobación modificó la instalación'


def test_h5_comprobar_password_desfasada_falla_sin_reparar_automaticamente(tmp_path, monkeypatch, capsys):
    docker = Docker(auth_ok=False)
    result = _cli(tmp_path, monkeypatch, capsys, docker, '--comprobar')
    assert result.code != 0, 'El diagnóstico ocultó una autenticación TCP fallida'
    assert docker.mutations == [], 'La comprobación alteró el rol antes de diagnosticar el desfase'


@pytest.mark.parametrize('drift', ['credenciales', 'imagen', 'version'])
def test_h5_comprobar_detecta_diferencias_entre_repo_y_postgres_en_ejecucion(tmp_path, monkeypatch, capsys, drift):
    docker = Docker(drift=drift)
    result = _cli(tmp_path, monkeypatch, capsys, docker, '--comprobar')
    assert result.code != 0, f'No se detectó el desfase de {drift} respecto del repositorio'
    assert docker.mutations == []


def test_h5_control_reparacion_explicita_sin_borrar_volumenes(tmp_path, monkeypatch, capsys):
    docker = Docker(auth_ok=False)
    result = _cli(tmp_path, monkeypatch, capsys, docker)
    assert result.code == 0, result.output
    assert docker.auth_ok
    assert sum(kind == 'sql' for kind, _ in docker.mutations) == 1
    assert not any(kind in ('down', 'rm') for kind, _ in docker.mutations)


def test_h5_esquema_sagas_antiguo_orienta_a_migrar_sin_recomendar_borrar_datos(m, tmp_path):
    sg = importlib.import_module('app.saga')
    path = tmp_path / 'sagas-anteriores.db'
    engine = create_engine(f'sqlite:///{path}')
    try:
        with engine.begin() as db:
            db.execute(text("CREATE TABLE sagas (id TEXT PRIMARY KEY, estado TEXT, "
                            "CHECK (estado IN ('EN_CURSO','COMPLETADA','COMPENSADA')) )"))
            db.execute(text("INSERT INTO sagas VALUES ('conservar', 'COMPLETADA')"))
        before = path.read_bytes()
        with pytest.raises(RuntimeError) as error:
            sg.verificar_esquema(engine)
        assert path.read_bytes() == before
        with engine.connect() as db:
            assert db.execute(text('SELECT id, estado FROM sagas')).all() == [('conservar', 'COMPLETADA')]
        message = str(error.value).lower()
        assert 'down -v' not in message and 'borra' not in message, (
            'El desfase de una base no debe recomendar eliminar todos los volúmenes')
        assert 'migr' in message, 'El diagnóstico debe orientar a una migración que conserve los datos'
    finally:
        engine.dispose()


def test_h5_control_esquema_sagas_actual_es_aceptado(m, tmp_path):
    sg = importlib.import_module('app.saga')
    engine = create_engine(f'sqlite:///{tmp_path / "sagas-actuales.db"}')
    try:
        sg.Base.metadata.create_all(engine, tables=[sg.Saga.__table__, sg.SagaStep.__table__])
        sg.verificar_esquema(engine)
    finally:
        engine.dispose()
