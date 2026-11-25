"""H7 RED: la baja pública no puede descoordinar una Saga ya completada.

Gateway, API de Inscripción y orquestador reales en proceso; SQLite temporal.
Solo Cupos/Pagos y el transporte HTTP son dobles. No requiere Docker, servicios
levantados ni credenciales del despliegue. La compensación usa la ruta interna
real de Inscripción, incluida su autenticación y cancelación anticipada.
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
from tests.unit.test_api_unidad import ADMIN, _admin, _alumno, cliente, m  # noqa: F401


@pytest.fixture
def sistema_h7(m, cliente, monkeypatch, tmp_path):
    alumno, clave = _alumno(cliente)
    password = 'h7-password-local-para-las-pruebas'
    provision = cliente.put(f"/v1/students/{alumno['id']}/password", headers=_admin(),
                            json={'password': password})
    assert provision.status_code == 204, provision.text
    login = cliente.post('/v1/auth/token', data={'username': alumno['email'], 'password': password})
    assert login.status_code == 200, login.text

    internal = 'h7-internal-test-key-at-least-32-characters'
    env = {
        'INTERNAL_KEY': internal,
        'ORCHESTRATOR_API_KEY': 'h7-orchestrator-test-key-at-least-32-characters',
        'MATRICULAS_API_KEYS': ADMIN,
        'MATRICULAS_JWT_SECRET': m.config.settings.jwt_secret,
        'JWT_ISSUER': m.security.JWT_ISSUER,
        'JWT_AUDIENCE': m.security.JWT_AUDIENCE,
        'INSCRIPCION_SERVICE_URL': 'http://inscripcion.h7',
        'ORCHESTRATOR_SERVICE_URL': 'http://orquestador.h7',
        'GATEWAY_RATE_LIMIT': '1000',
        'SAGA_DATABASE_URL': f"sqlite:///{tmp_path / 'sagas.db'}",
        'SAGA_RECONCILE_SECONDS': '3600',
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    spec = importlib.util.spec_from_file_location('gateway_h7', ROOT / 'api-gateway/app.py')
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    gateway.app.config['TESTING'] = True
    sg = importlib.import_module('app.saga')
    api_orq = importlib.import_module('app.orchestrator_main')
    sc = importlib.import_module('app.saga_clients')
    engine = create_engine(env['SAGA_DATABASE_URL'], connect_args={'check_same_thread': False})
    sg.Base.metadata.create_all(engine, tables=[sg.Saga.__table__, sg.SagaStep.__table__])
    sessions = sessionmaker(engine)
    reservas, pagos, llamadas = set(), {}, []
    control = SimpleNamespace(perder_respuesta_inscripcion=False)

    def reservar(saga, key):
        llamadas.append('RESERVAR')
        reservas.add(key)

    def cobrar(saga, key):
        llamadas.append('COBRAR')
        pagos.setdefault(key, 'COBRADO')
        return {'estado': 'COBRADO'}

    def inscribir(saga, key):
        assert f"{saga['id']}:RESERVAR" in reservas
        assert pagos[f"{saga['id']}:COBRAR"] == 'COBRADO'
        llamadas.append('INSCRIBIR')
        response = cliente.post('/v1/enrollments', headers={**_admin(), 'Idempotency-Key': key},
                                json={'student_id': saga['alumno_id'], 'curso_id': saga['curso_id']})
        assert response.status_code == 201, response.text
        assert response.json()['estado'] == 'CONFIRMADA'
        if control.perder_respuesta_inscripcion:
            # El alta ocurrió: la Saga debe deshacerla por su operación interna.
            raise sc.DependencyFailure('Respuesta de Inscripción perdida')

    def cancelar_inscripcion(saga, key, original):
        llamadas.append('comp:INSCRIBIR')
        assert key == f"{saga['id']}:comp:INSCRIBIR"
        response = cliente.post('/internal/enrollments/cancel',
            headers={'X-Internal-Key': internal, 'Idempotency-Key': original},
            json={'student_id': saga['alumno_id'], 'curso_id': saga['curso_id']})
        assert response.status_code == 200, response.text
        assert response.json()['estado'] == 'ANULADA'

    def reembolsar(saga, key, original):
        llamadas.append('comp:COBRAR')
        assert original == f"{saga['id']}:COBRAR"
        pagos[original] = 'REEMBOLSADO'

    def liberar(saga, key, original):
        llamadas.append('comp:RESERVAR')
        assert original == f"{saga['id']}:RESERVAR"
        reservas.discard(original)

    clients = {
        name: SimpleNamespace(available=True, forward=forward, compensate=compensate)
        for name, forward, compensate in [
            ('RESERVAR', reservar, liberar), ('COBRAR', cobrar, reembolsar),
            ('INSCRIBIR', inscribir, cancelar_inscripcion),
        ]
    }
    obj = sg.Orchestrator(sessions, clients, retries=1, backoff=0, stale_seconds=3600)
    monkeypatch.setattr(api_orq, 'engine', engine)
    monkeypatch.setattr(api_orq, 'sessions', sessions)
    monkeypatch.setattr(api_orq, 'orchestrator', obj)

    def snapshot():
        with m.database.SessionLocal() as db:
            enrollments = db.scalars(select(m.models.Enrollment).where(
                m.models.Enrollment.student_id == alumno['id']).order_by(m.models.Enrollment.id)).all()
            inscripciones = [(e.id, e.curso_id, e.estado.value, e.client_key) for e in enrollments]
        with sessions() as db:
            sagas = [(s.id, s.estado, s.motivo_fallo, s.idempotency_key)
                     for s in db.scalars(select(sg.Saga).order_by(sg.Saga.id))]
            pasos = [(p.saga_id, p.paso, p.tipo, p.estado, p.intentos, p.clave)
                     for p in db.scalars(select(sg.SagaStep).order_by(sg.SagaStep.id))]
        return dict(inscripciones=inscripciones, sagas=sagas, pasos=pasos,
                    reservas=sorted(reservas), pagos=dict(pagos), llamadas=list(llamadas))

    headers = {
        'jwt': {'Authorization': 'Bearer ' + login.json()['access_token']},
        'api_key_admin': {'X-API-Key': ADMIN},
        'api_key_estudiante': {'X-API-Key': clave['api_key']},
    }
    try:
        with TestClient(api_orq.app) as orq_client:
            def transporte(_session, method, url, **kwargs):
                # Intercepta Session.request: cubre tanto requests.request como get/post.
                parsed = urlsplit(url)
                target = {'inscripcion.h7': cliente, 'orquestador.h7': orq_client}.get(parsed.netloc)
                assert target is not None, f'Destino externo inesperado: {parsed.netloc}'
                path = parsed.path + ('?' + parsed.query if parsed.query else '')
                request_headers = dict(requests.structures.CaseInsensitiveDict(kwargs.get('headers') or {}))
                options = {'headers': request_headers}
                if kwargs.get('json') is not None:
                    options['json'] = kwargs['json']
                if kwargs.get('data') is not None:
                    options['content'] = kwargs['data']
                if kwargs.get('params') is not None:
                    options['params'] = kwargs['params']
                upstream = target.request(method, path, **options)
                response = requests.Response()
                response.status_code = upstream.status_code
                response._content = upstream.content
                response.headers.update(upstream.headers)
                return response

            monkeypatch.setattr(requests.Session, 'request', transporte)
            yield SimpleNamespace(public=gateway.app.test_client(), api=cliente,
                alumno_id=alumno['id'], headers=headers, internal=internal,
                snapshot=snapshot, control=control)
    finally:
        engine.dispose()


def _resumen(snap):
    return dict(inscripciones=[e[2] for e in snap['inscripciones']],
                sagas=[s[1] for s in snap['sagas']], reservas=len(snap['reservas']),
                pagos=list(snap['pagos'].values()))


def _iniciar(sistema, key=None):
    return sistema.public.post('/v1/matriculas',
        headers={**sistema.headers['jwt'], 'Idempotency-Key': key or str(uuid.uuid4())},
        json={'alumno_id': sistema.alumno_id, 'curso_id': 1, 'monto': 15000})


def _completar(sistema):
    response = _iniciar(sistema)
    assert response.status_code == 201, response.get_json()
    saga = response.get_json()
    assert saga['estado'] == 'COMPLETADA'
    snap = sistema.snapshot()
    assert len(snap['inscripciones']) == 1
    assert snap['inscripciones'][0][2] == 'CONFIRMADA'
    assert snap['reservas'] == [f"{saga['id']}:RESERVAR"]
    assert snap['pagos'] == {f"{saga['id']}:COBRAR": 'COBRADO'}
    return saga, snap['inscripciones'][0][0]


@pytest.mark.parametrize('credencial', ['jwt', 'api_key_admin'])
def test_h7_baja_por_gateway_rechazada_sin_alterar_saga_completada(sistema_h7, credencial):
    sistema = sistema_h7
    _, enrollment_id = _completar(sistema)
    headers = sistema.headers[credencial]
    assert sistema.public.get(f'/v1/students/{sistema.alumno_id}', headers=headers).status_code == 200
    before = sistema.snapshot()
    response = sistema.public.post(f'/v1/enrollments/{enrollment_id}/revert', headers=headers)
    after = sistema.snapshot()
    # No se exige bloquear en el Gateway: Inscripción puede rechazar el proxy.
    assert response.status_code in (403, 404, 405), (
        f'H7: baja pública con {credencial}: HTTP {response.status_code}; estado={_resumen(after)}')
    assert after == before, 'El rechazo público debe conservar inscripción, cupo, cobro y Saga'


@pytest.mark.parametrize('credencial', ['jwt', 'api_key_admin', 'api_key_estudiante'])
def test_h7_baja_directa_sin_credencial_interna_rechazada(sistema_h7, credencial):
    sistema = sistema_h7
    _, enrollment_id = _completar(sistema)
    headers = sistema.headers[credencial]
    # Control de autenticación: las tres credenciales sirven para leer su ficha.
    assert sistema.api.get(f'/v1/students/{sistema.alumno_id}', headers=headers).status_code == 200
    before = sistema.snapshot()
    response = sistema.api.post(f'/v1/enrollments/{enrollment_id}/revert', headers=headers)
    after = sistema.snapshot()
    assert response.status_code in (403, 404, 405), (
        f'H7: Inscripción admite baja con {credencial}: HTTP {response.status_code}; '
        f'estado={_resumen(after)}')
    assert after == before, 'La protección debe estar en Inscripción, también fuera del Gateway'


def test_h7_control_saga_completada_y_replay_no_duplican_efectos(sistema_h7):
    sistema = sistema_h7
    key = str(uuid.uuid4())
    first = _iniciar(sistema, key)
    assert first.status_code == 201 and first.get_json()['estado'] == 'COMPLETADA'
    before = sistema.snapshot()
    replay = _iniciar(sistema, key)
    assert replay.status_code == 200 and replay.get_json()['id'] == first.get_json()['id']
    assert sistema.snapshot() == before
    duplicate = _iniciar(sistema)
    assert duplicate.status_code == 409
    assert sistema.snapshot() == before


@pytest.mark.parametrize('internal_key', [None, 'h7-clave-interna-incorrecta'], ids=['sin-clave', 'clave-incorrecta'])
def test_h7_cancelacion_interna_sin_autorizacion_no_modifica_datos(sistema_h7, internal_key):
    sistema = sistema_h7
    saga, _ = _completar(sistema)
    before = sistema.snapshot()
    headers = {'Idempotency-Key': f"{saga['id']}:INSCRIBIR"}
    if internal_key:
        headers['X-Internal-Key'] = internal_key
    response = sistema.api.post('/internal/enrollments/cancel', headers=headers,
        json={'student_id': sistema.alumno_id, 'curso_id': 1})
    assert response.status_code == 403, response.text
    assert sistema.snapshot() == before


def test_h7_gateway_no_expone_cancelacion_interna_aunque_cliente_envie_clave(sistema_h7):
    sistema = sistema_h7
    saga, _ = _completar(sistema)
    before = sistema.snapshot()
    response = sistema.public.post('/internal/enrollments/cancel',
        headers={**sistema.headers['api_key_admin'], 'X-Internal-Key': sistema.internal,
                 'Idempotency-Key': f"{saga['id']}:INSCRIBIR"},
        json={'student_id': sistema.alumno_id, 'curso_id': 1})
    assert response.status_code == 404
    assert sistema.snapshot() == before


def test_h7_compensacion_interna_autenticada_conserva_orden_e_idempotencia(sistema_h7):
    sistema = sistema_h7
    sistema.control.perder_respuesta_inscripcion = True
    response = _iniciar(sistema)
    assert response.status_code == 503, response.get_json()
    saga = response.get_json()
    assert saga['estado'] == 'COMPENSADA'
    snap = sistema.snapshot()
    assert snap['inscripciones'][0][2] == 'ANULADA'
    assert snap['reservas'] == []
    assert snap['pagos'] == {f"{saga['id']}:COBRAR": 'REEMBOLSADO'}
    assert snap['llamadas'] == ['RESERVAR', 'COBRAR', 'INSCRIBIR',
                                'comp:INSCRIBIR', 'comp:COBRAR', 'comp:RESERVAR']
    replay = sistema.api.post('/internal/enrollments/cancel',
        headers={'X-Internal-Key': sistema.internal, 'Idempotency-Key': f"{saga['id']}:INSCRIBIR"},
        json={'student_id': sistema.alumno_id, 'curso_id': 1})
    assert replay.status_code == 200 and replay.json()['estado'] == 'ANULADA'
    assert sistema.snapshot() == snap


def test_h7_cancelacion_anticipada_impide_inscripcion_tardia(sistema_h7):
    sistema = sistema_h7
    key = f'{uuid.uuid4()}:INSCRIBIR'
    payload = {'student_id': sistema.alumno_id, 'curso_id': 1}
    cancelled = sistema.api.post('/internal/enrollments/cancel',
        headers={'X-Internal-Key': sistema.internal, 'Idempotency-Key': key}, json=payload)
    assert cancelled.status_code == 200 and cancelled.json()['estado'] == 'ANULADA'
    before = sistema.snapshot()
    late = sistema.api.post('/v1/enrollments', headers={**_admin(), 'Idempotency-Key': key}, json=payload)
    assert late.status_code in (200, 201), late.text
    assert late.json()['estado'] == 'ANULADA'
    assert late.json()['id'] == cancelled.json()['id']
    assert sistema.snapshot() == before
