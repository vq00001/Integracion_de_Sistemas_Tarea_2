"""H1 (regresión): altas públicas deben pasar por la saga.

Gateway Flask, API de Inscripción y máquina de estados reales en proceso, con
SQLite temporal. Solo el transporte HTTP y los clientes de Cupos/Pagos son dobles;
no se accede a Docker, Webpay ni a bases del despliegue. Los casos de alta directa deben rechazarse sin alterar el estado.
"""
import importlib
import importlib.util
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.conftest import ROOT
from tests.unit.test_api_unidad import m, cliente, _admin, _alumno, ADMIN


@pytest.fixture
def sistema(m, cliente, monkeypatch, tmp_path):
    alumno, _ = _alumno(cliente)
    password = 'h1-local-student-password-32-chars'
    provision = cliente.put(f"/v1/students/{alumno['id']}/password",
                            headers=_admin(), json={'password': password})
    assert provision.status_code == 204
    login = cliente.post('/v1/auth/token', data={'username': alumno['email'], 'password': password})
    assert login.status_code == 200, login.text
    jwt = login.json()['access_token']
    orchestrator_key = 'h1-orchestrator-test-secret-at-least-32'
    monkeypatch.setenv('ORCHESTRATOR_API_KEY', orchestrator_key)
    monkeypatch.setenv('MATRICULAS_API_KEYS', ADMIN)
    monkeypatch.setenv('MATRICULAS_JWT_SECRET', m.config.settings.jwt_secret)
    monkeypatch.setenv('JWT_ISSUER', m.security.JWT_ISSUER)
    monkeypatch.setenv('JWT_AUDIENCE', m.security.JWT_AUDIENCE)
    monkeypatch.setenv('GATEWAY_RATE_LIMIT', '1000')
    monkeypatch.setenv('INSCRIPCION_SERVICE_URL', 'http://inscripcion.h1')
    monkeypatch.setenv('ORCHESTRATOR_SERVICE_URL', 'http://orquestador.h1')
    spec = importlib.util.spec_from_file_location('gateway_h1', ROOT/'api-gateway/app.py')
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    gateway.app.config['TESTING'] = True

    sg = importlib.import_module('app.saga')
    orch_api = importlib.import_module('app.orchestrator_main')
    engine = create_engine(f'sqlite:///{tmp_path / "sagas.db"}',
                           connect_args={'check_same_thread': False})
    sg.Base.metadata.create_all(engine, tables=[sg.Saga.__table__, sg.SagaStep.__table__])
    sessions = sessionmaker(engine)
    reservas, pagos, orden = set(), {}, []
    control = SimpleNamespace(pago_pendiente=False)

    def reservar(saga, key):
        orden.append('RESERVAR')
        reservas.add(key)

    def cobrar(saga, key):
        orden.append('COBRAR')
        estado = 'PENDIENTE' if control.pago_pendiente else 'COBRADO'
        pagos.setdefault(key, estado)
        return {'estado': estado}

    def inscribir(saga, key):
        # Invariante del flujo legítimo: antes del alta debe haber reserva y cobro.
        assert f'{saga["id"]}:RESERVAR' in reservas
        assert pagos[f'{saga["id"]}:COBRAR'] == 'COBRADO'
        orden.append('INSCRIBIR')
        response = cliente.post('/v1/enrollments', headers={**_admin(), 'Idempotency-Key': key},
                                json={'student_id': saga['alumno_id'], 'curso_id': saga['curso_id']})
        assert response.status_code == 201, response.text
        assert response.json()['estado'] == 'CONFIRMADA'

    def compensacion_inesperada(*args):
        raise AssertionError('El camino feliz no debe compensar')

    clients = {name: SimpleNamespace(available=True, forward=forward,
                                    compensate=compensacion_inesperada)
               for name, forward in [('RESERVAR', reservar), ('COBRAR', cobrar), ('INSCRIBIR', inscribir)]}
    obj = sg.Orchestrator(sessions, clients, retries=1, backoff=0, stale_seconds=3600)
    monkeypatch.setattr(orch_api, 'engine', engine)
    monkeypatch.setattr(orch_api, 'sessions', sessions)
    monkeypatch.setattr(orch_api, 'orchestrator', obj)

    proxy_calls = []
    with TestClient(orch_api.app) as orch_client:
        def transporte(method, url, *, headers, data, timeout, allow_redirects):
            parsed = urlsplit(url)
            proxy_calls.append((method, parsed.netloc, parsed.path))
            target = {'inscripcion.h1': cliente, 'orquestador.h1': orch_client}.get(parsed.netloc)
            assert target is not None, f'Destino inesperado: {parsed.netloc}'
            path = parsed.path + ('?' + parsed.query if parsed.query else '')
            # requests prepara headers sin distinguir mayúsculas: la credencial
            # inyectada reemplaza la original aunque Flask cambie su capitalización.
            normalized = dict(requests.structures.CaseInsensitiveDict(headers))
            upstream = target.request(method, path, headers=normalized, content=data)
            response = requests.Response()
            response.status_code = upstream.status_code
            response._content = upstream.content
            response.headers.update(upstream.headers)
            return response
        monkeypatch.setattr(gateway.requests, 'request', transporte)

        def inscripciones():
            with m.database.SessionLocal() as db:
                rows = db.scalars(select(m.models.Enrollment).where(
                    m.models.Enrollment.student_id == alumno['id'])).all()
                return [(row.curso_id, row.estado.value) for row in rows]

        def sagas():
            with sessions() as db:
                return [saga.estado for saga in db.scalars(select(sg.Saga)).all()]

        yield SimpleNamespace(
            public=gateway.app.test_client(), alumno_id=alumno['id'],
            jwt=jwt, reservas=reservas,
            pagos=pagos, orden=orden, inscripciones=inscripciones, sagas=sagas,
            control=control, proxy_calls=proxy_calls,
        )
    engine.dispose()


def credenciales(sistema, tipo):
    return {'Authorization': f'Bearer {sistema.jwt}'} if tipo == 'jwt' else {'X-API-Key': ADMIN}


@pytest.mark.parametrize('tipo', ['jwt', 'api_key'])
def test_h1_credencial_valida_conserva_lecturas_publicas(sistema, tipo):
    headers = credenciales(sistema, tipo)
    ficha = sistema.public.get(f'/v1/students/{sistema.alumno_id}', headers=headers)
    assert ficha.status_code == 200
    assert ficha.json['id'] == sistema.alumno_id
    lista = sistema.public.get('/v1/enrollments', headers=headers,
                               query_string={'student_id': sistema.alumno_id})
    assert lista.status_code == 200 and lista.json['total'] == 0


@pytest.mark.parametrize('tipo', ['jwt', 'api_key'])
@pytest.mark.parametrize('curso_id', [1, 999], ids=['curso-normal', 'curso-inexistente'])
def test_h1_alta_directa_rechazada_sin_crear_inscripcion(sistema, tipo, curso_id):
    assert sistema.inscripciones() == [] and sistema.sagas() == []
    response = sistema.public.post('/v1/enrollments', headers={
        **credenciales(sistema, tipo), 'Idempotency-Key': str(uuid.uuid4())},
        json={'student_id': sistema.alumno_id, 'curso_id': curso_id})
    estado = {'inscripciones': sistema.inscripciones(), 'sagas': sistema.sagas(),
              'reservas': len(sistema.reservas), 'pagos': len(sistema.pagos)}
    assert response.status_code in (403, 404, 405), (
        f'H1: alta directa con {tipo}: HTTP {response.status_code}; estado={estado}')
    assert estado == {'inscripciones': [], 'sagas': [], 'reservas': 0, 'pagos': 0}
    assert sistema.orden == []


@pytest.mark.parametrize('tipo', ['jwt', 'api_key'])
def test_h1_saga_completada_confirma_tras_reservar_y_cobrar_sin_duplicar(sistema, tipo):
    headers = {**credenciales(sistema, tipo), 'Idempotency-Key': str(uuid.uuid4())}
    payload = {'alumno_id': sistema.alumno_id, 'curso_id': 1, 'monto': 100}
    assert sistema.inscripciones() == [] and not sistema.reservas and not sistema.pagos
    response = sistema.public.post('/v1/matriculas', headers=headers, json=payload)
    assert response.status_code == 201, response.data
    assert response.json['estado'] == 'COMPLETADA'
    assert sistema.orden == ['RESERVAR', 'COBRAR', 'INSCRIBIR']
    assert sistema.inscripciones() == [(1, 'CONFIRMADA')]
    assert len(sistema.reservas) == 1 and list(sistema.pagos.values()) == ['COBRADO']
    replay = sistema.public.post('/v1/matriculas', headers=headers, json=payload)
    assert replay.status_code == 200 and replay.json['id'] == response.json['id']
    assert sistema.orden == ['RESERVAR', 'COBRAR', 'INSCRIBIR']
    assert len(sistema.reservas) == len(sistema.pagos) == len(sistema.inscripciones()) == 1


@pytest.mark.parametrize('tipo', ['jwt', 'api_key'])
def test_h1_saga_con_pago_pendiente_no_confirma_inscripcion(sistema, tipo):
    sistema.control.pago_pendiente = True
    response = sistema.public.post('/v1/matriculas',
        headers={**credenciales(sistema, tipo), 'Idempotency-Key': str(uuid.uuid4())},
        json={'alumno_id': sistema.alumno_id, 'curso_id': 1, 'monto': 100})
    assert response.status_code == 202, response.data
    assert response.json['estado'] == 'ESPERANDO_PAGO'
    assert sistema.orden == ['RESERVAR', 'COBRAR']
    assert len(sistema.reservas) == 1 and list(sistema.pagos.values()) == ['PENDIENTE']
    assert sistema.inscripciones() == []


@pytest.mark.parametrize('tipo', ['jwt', 'api_key'])
@pytest.mark.parametrize('writer', [None, 'clave-falsa'])
def test_h1_api_interna_tambien_rechaza_escritura_sin_clave_exclusiva(sistema, cliente, tipo, writer):
    headers = credenciales(sistema, tipo)
    if writer:
        headers['X-Enrollment-Writer-Key'] = writer
    response = cliente.post('/v1/enrollments', headers=headers,
                            json={'student_id': sistema.alumno_id, 'curso_id': 1})
    assert response.status_code == 403
    assert sistema.inscripciones() == []


@pytest.mark.parametrize('path', ['/v1/enrollments', '/v1/enrollments/'])
def test_h1_gateway_no_reenvia_alta_ni_con_clave_escritora(sistema, path):
    from tests.unit.test_api_unidad import WRITER
    response = sistema.public.post(path, headers={**_admin(), 'X-Enrollment-Writer-Key': WRITER},
                                   json={'student_id': sistema.alumno_id, 'curso_id': 1})
    assert response.status_code == 403
    assert sistema.inscripciones() == []
    assert sistema.proxy_calls == []


def test_h1_sin_configurar_clave_escritora_falla_cerrado(sistema, cliente, monkeypatch):
    monkeypatch.delenv('INSCRIPCION_WRITER_KEY', raising=False)
    response = cliente.post('/v1/enrollments', headers=_admin(),
                            json={'student_id': sistema.alumno_id, 'curso_id': 1})
    assert response.status_code == 403
    assert sistema.inscripciones() == []


def test_h1_adaptador_de_inscripcion_envia_credencial_exclusiva(m, monkeypatch):
    import io
    import json
    adapter = importlib.import_module('app.saga_clients')
    monkeypatch.setenv('INSCRIPCION_WRITER_KEY', 'clave-escritora-de-prueba')
    sent = []
    def urlopen(request, timeout):
        sent.append(request)
        estado = 'CONFIRMADA' if request.full_url.endswith('/v1/enrollments') else 'COBRADO'
        return io.BytesIO(json.dumps({'estado': estado}).encode())
    monkeypatch.setattr(adapter.urllib.request, 'urlopen', urlopen)
    adapter.clients_from_env()['INSCRIBIR'].forward({'alumno_id': 'alumno', 'curso_id': 1}, 'saga:INSCRIBIR')
    assert sent[0].get_header('X-enrollment-writer-key') == 'clave-escritora-de-prueba'
    adapter.clients_from_env()['COBRAR'].forward({'alumno_id': 'alumno', 'curso_id': 1, 'monto': 100}, 'saga:COBRAR')
    assert sent[1].get_header('X-enrollment-writer-key') is None


def test_h1_openapi_declara_autenticacion_y_escritor_como_requisitos_conjuntos(cliente):
    spec = cliente.get('/openapi.json').json()
    security = spec['paths']['/v1/enrollments']['post']['security']
    assert security and all('EnrollmentWriterAuth' in alternative for alternative in security)
    assert all('ApiKeyAuth' in alternative or 'BearerAuth' in alternative for alternative in security)
    assert all('EnrollmentWriterAuth' not in alternative
               for alternative in spec['paths']['/v1/enrollments']['get']['security'])


def test_h1_generar_env_completa_clave_sin_rotar_credenciales(tmp_path, monkeypatch):
    from dotenv import dotenv_values
    spec = importlib.util.spec_from_file_location('h1_generar_env', ROOT/'scripts/generar_env.py')
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    env = tmp_path/'.env'
    env.write_text('MATRICULAS_DB_PASSWORD=conservar-db\nMATRICULAS_API_KEYS=conservar-admin\n')
    monkeypatch.setattr(gen, 'ROOT', tmp_path)
    monkeypatch.setattr(gen.sys, 'argv', ['generar_env.py'])
    assert gen.main() == 0
    first = dotenv_values(env)
    assert len(first['INSCRIPCION_WRITER_KEY']) >= 32
    assert first['MATRICULAS_DB_PASSWORD'] == 'conservar-db'
    assert first['MATRICULAS_API_KEYS'] == 'conservar-admin'
    assert gen.main() == 0
    assert dotenv_values(env) == first
