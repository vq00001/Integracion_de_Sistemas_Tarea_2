"""Secretos persistentes y migración real con datos, índices e idempotencia."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.schema import CreateIndex, CreateTable

from tests.conftest import ROOT
from tests.unit.test_api_unidad import m  # noqa: F401


def _bootstrap():
    spec = importlib.util.spec_from_file_location('h5_bootstrap', ROOT / 'scripts/inicializar_secretos.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inicializador_persiste_secretos_y_limita_su_distribucion(tmp_path):
    module = _bootstrap()
    state, config = tmp_path / 'state', tmp_path / 'config'
    module.initialize(state, config, {})
    original = (state / 'secrets.json').read_bytes()
    module.initialize(state, config, {})
    assert (state / 'secrets.json').read_bytes() == original
    master = json.loads(original)
    assert len(set(master.values())) == len(master) == 8
    assert all(len(value) >= 24 for value in master.values())
    bundles = {name: json.loads((config / name / 'env.json').read_text())
               for name in ('api', 'orquestador', 'gateway', 'pagos', 'db', 'runner', 'legacy')}
    assert bundles['api']['INSCRIPCION_WRITER_KEY'] == bundles['orquestador']['INSCRIPCION_WRITER_KEY']
    for name in ('gateway', 'pagos', 'db', 'legacy'):
        assert master['INSCRIPCION_WRITER_KEY'] not in bundles[name].values()
    assert master['MATRICULAS_DB_PASSWORD'] not in bundles['gateway'].values()
    assert bundles['pagos']['PAGOS_API_KEY'] == bundles['orquestador']['PAGOS_API_KEY']
    assert (config / 'db/password').read_text() == master['MATRICULAS_DB_PASSWORD']
    assert (state / 'secrets.json').stat().st_mode & 0o777 == 0o600


def test_inicializador_adopta_valores_explicitos_y_codifica_password_en_url(tmp_path):
    module = _bootstrap()
    password = 'guardar-$"@:/?%#-no-ejecutar'
    module.initialize(tmp_path / 'state', tmp_path / 'config', {'MATRICULAS_DB_PASSWORD': password})
    bundle = json.loads((tmp_path / 'config/api/env.json').read_text())
    from sqlalchemy.engine import make_url
    assert make_url(bundle['DATABASE_URL']).password == password
    assert json.loads((tmp_path / 'state/secrets.json').read_text())['MATRICULAS_DB_PASSWORD'] == password


def test_inicializadores_concurrentes_no_generan_claves_distintas(tmp_path):
    module = _bootstrap()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: module.initialize(tmp_path / 'state', tmp_path / 'config', {}), range(4)))
    master = json.loads((tmp_path / 'state/secrets.json').read_text())
    gateway = json.loads((tmp_path / 'config/gateway/env.json').read_text())
    api = json.loads((tmp_path / 'config/api/env.json').read_text())
    assert gateway['MATRICULAS_API_KEYS'] == api['API_KEYS'] == master['MATRICULAS_API_KEYS']
    assert gateway['MATRICULAS_JWT_SECRET'] == api['JWT_SECRET'] == master['MATRICULAS_JWT_SECRET']


@pytest.mark.parametrize('content', ['archivo interrumpido', '{}', '[]'])
def test_almacen_corrupto_no_se_regenera_silenciosamente(tmp_path, content):
    state = tmp_path / 'state'
    state.mkdir()
    path = state / 'secrets.json'
    path.write_text(content)
    with pytest.raises(ValueError):
        _bootstrap().initialize(state, tmp_path / 'config', {})
    assert path.read_text() == content


def test_gateway_y_bff_no_montan_secretos_exclusivos_de_otros_servicios():
    import yaml
    services = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']
    for name in ('gateway', 'bff', 'pagos'):
        mounts = {volume.split(':')[0] for volume in services[name].get('volumes', [])}
        assert not mounts & {'secretos_data', 'config_api', 'config_orquestador', 'config_db', 'config_runner'}


@pytest.fixture
def antiguo(m, tmp_path):
    sg = importlib.import_module('app.saga')
    migration = importlib.import_module('app.saga_schema')
    path = tmp_path / 'sagas.db'
    engine = create_engine(f'sqlite:///{path}')
    ddl = str(CreateTable(sg.Saga.__table__).compile(engine))
    ddl = ddl.replace('\tpago_url VARCHAR(256), \n', '').replace('\tpago_expira_at DATETIME, \n', '')
    ddl = ddl.replace("'ESPERANDO_PAGO',", '')
    payload = {'alumno_id': 'alumno', 'curso_id': 1, 'monto': 100}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with engine.begin() as db:
        db.execute(text(ddl))
        for index in sg.Saga.__table__.indexes:
            db.execute(text(str(CreateIndex(index).compile(engine)).replace("'ESPERANDO_PAGO',", '')))
        for sid, state in [('uno', 'COMPLETADA'), ('dos', 'COMPENSADA')]:
            db.execute(text('INSERT INTO sagas (id,idempotency_key,request_hash,alumno_id,curso_id,monto,'
                            'estado,created_at,updated_at) VALUES (:id,:key,:hash,:alumno_id,:curso_id,'
                            ':monto,:estado,\'2026-11-01 00:00:00\',\'2026-11-01 00:00:00\')'),
                       dict(id=sid, key='key-' + sid, hash=digest, estado=state, **payload))
    sg.SagaStep.__table__.create(engine)
    with engine.begin() as db:
        db.execute(text("INSERT INTO saga_steps (id,saga_id,paso,tipo,estado,intentos,clave,created_at,updated_at) "
                        "VALUES (1,'uno','INSCRIBIR','forward','OK',1,'uno:INSCRIBIR','2026-11-01','2026-11-01')"))
        db.execute(text('CREATE INDEX idx_monto ON sagas(monto)'))
        db.execute(text('CREATE TABLE audit (id TEXT)'))
        db.execute(text('CREATE TRIGGER audit_sagas AFTER UPDATE ON sagas BEGIN INSERT INTO audit VALUES (NEW.id); END'))
    try:
        yield sg, migration, engine, path, payload
    finally:
        engine.dispose()


def test_migracion_conserva_filas_pasos_indices_trigger_y_replay(antiguo):
    sg, migration, engine, path, payload = antiguo
    with engine.connect() as db:
        before = db.execute(text('SELECT * FROM sagas ORDER BY id')).all()
        steps = db.execute(text('SELECT * FROM saga_steps')).all()
    backup = migration.migrar_esquema(engine)
    assert backup.exists() and backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as old:
        assert old.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert 'ESPERANDO_PAGO' not in old.execute("SELECT sql FROM sqlite_master WHERE name='sagas'").fetchone()[0]
        assert old.execute('SELECT count(*) FROM sagas').fetchone() == (2,)
    sg.verificar_esquema(engine)
    names = 'id,idempotency_key,request_hash,alumno_id,curso_id,monto,estado,motivo_fallo,created_at,updated_at'
    with engine.connect() as db:
        assert db.execute(text(f'SELECT {names} FROM sagas ORDER BY id')).all() == before
        assert db.execute(text('SELECT * FROM saga_steps')).all() == steps
        assert db.execute(text('SELECT * FROM audit')).all() == []
        assert db.execute(text("SELECT count(*) FROM sqlite_master WHERE name IN ('idx_monto','audit_sagas')")).scalar() == 2
        assert db.execute(text('PRAGMA foreign_key_check')).all() == []
    from sqlalchemy.orm import sessionmaker
    class NoEffects:
        def forward(self, *args):
            raise AssertionError('Se repitió un efecto durante el replay')
    obj = sg.Orchestrator(sessionmaker(engine), {name: NoEffects() for name in sg.STEPS})
    result, replay = obj.matricular('key-uno', **payload)
    assert replay and result['id'] == 'uno' and result['estado'] == 'COMPLETADA'
    assert migration.migrar_esquema(engine) is None
    assert len(list(path.parent.glob('*.bak'))) == 1


def test_migracion_revierte_si_hay_referencias_invalidas(antiguo):
    sg, migration, engine, path, _ = antiguo
    with engine.begin() as db:
        db.execute(text("UPDATE saga_steps SET saga_id='huerfana'"))
    with pytest.raises(RuntimeError, match='referencias'):
        migration.migrar_esquema(engine)
    with pytest.raises(RuntimeError, match='migración'):
        sg.verificar_esquema(engine)
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM sagas')).scalar() == 2
        assert db.execute(text('SELECT saga_id FROM saga_steps')).scalar() == 'huerfana'
    assert len(list(path.parent.glob('*.bak'))) == 1


def test_migracion_no_descarta_columnas_de_un_esquema_desconocido(antiguo):
    _, migration, engine, path, _ = antiguo
    with engine.begin() as db:
        db.execute(text("ALTER TABLE sagas ADD COLUMN extra TEXT DEFAULT 'conservar'"))
    with pytest.raises(RuntimeError, match='desconocido'):
        migration.migrar_esquema(engine)
    with engine.connect() as db:
        assert db.execute(text('SELECT extra FROM sagas')).all() == [('conservar',), ('conservar',)]
    assert list(path.parent.glob('*.bak')) == []
