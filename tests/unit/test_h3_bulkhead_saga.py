"""Regresiones H3: sobrecarga local sin efectos inciertos ni recursos huérfanos.

API, Orchestrator, Client, breakers y SQLite reales en proceso. Los callbacks de
Cupos/Pagos/Inscripción son dobles idempotentes sanos con estado verificable.
Events y futures producen saturación reproducible sin sleeps ni servicios externos.
Los plazos de espera solo evitan que una prueba bloqueada quede colgada; no son
mediciones de latencia. No usar xfail ni debilitar los invariantes para pasar H3.
"""
import importlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.test_api_unidad import m  # noqa: F401
from tests.unit.test_saga import setup_saga  # noqa: F401


def _payload(sid):
    return {'id': sid, 'alumno_id': sid, 'curso_id': 1, 'monto': 100}


@pytest.fixture(autouse=True)
def _capacidad_de_prueba(monkeypatch):
    # Configurar antes de construir Orchestrator y Client, también para la
    # admisión global. Usar la configuración productiva ya existente.
    monkeypatch.setenv('SAGA_BULKHEAD', '1')


@pytest.fixture
def entorno(setup_saga):
    sg, obj, sessions, _, _ = setup_saga
    sc = importlib.import_module('app.saga_clients')
    env = SimpleNamespace(sg=sg, sc=sc, obj=obj, sessions=sessions, lock=threading.Lock(),
                          history=[], reservas=set(), pagos={}, inscripciones=set(),
                          block_step=None, block_sid=None, timeout_step=None,
                          started=threading.Event(), release=threading.Event())

    def callback(kind, step):
        def apply(saga, key, *args, timeout):
            sid = saga['id']
            with env.lock:
                env.history.append((kind, step, sid))
            if kind == 'forward':
                assert key == f'{sid}:{step}'
                if env.block_step == step and env.block_sid == sid:
                    env.started.set()
                    assert env.release.wait(8), 'La prueba no liberó la operación sana'
                if env.timeout_step == step:
                    raise TimeoutError('Respuesta realmente incierta')
                with env.lock:
                    if step == 'RESERVAR':
                        env.reservas.add(sid)
                    elif step == 'COBRAR':
                        env.pagos.setdefault(sid, 'COBRADO')
                    else:
                        env.inscripciones.add(sid)
                return {'estado': 'COBRADO'} if step == 'COBRAR' else None
            assert args == (f'{sid}:{step}',)
            with env.lock:
                if step == 'RESERVAR':
                    env.reservas.discard(sid)
                elif step == 'COBRAR' and sid in env.pagos:
                    env.pagos[sid] = 'REEMBOLSADO'
                elif step == 'INSCRIBIR':
                    env.inscripciones.discard(sid)
        return apply

    env.clients = {step: sc.Client(callback('forward', step), callback('compensacion', step),
                                  capacity=1, threshold=100, timeout=2)
                   for step in sg.STEPS}
    obj.clients, obj.retries, obj.backoff, obj.stale_seconds = env.clients, 1, 0, 3600
    return env


@contextmanager
def _ocupar(env, step, sid='holder'):
    env.block_step, env.block_sid = step, sid
    env.started.clear()
    env.release.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(env.clients[step].forward, _payload(sid), f'{sid}:{step}')
        try:
            assert env.started.wait(3), 'No se alcanzó la saturación controlada'
            yield future
        finally:
            env.release.set()
        future.result(timeout=3)
    env.block_step = env.block_sid = None


def _resources(env, sid):
    with env.lock:
        return (sid in env.reservas, env.pagos.get(sid), sid in env.inscripciones)


def test_h3_rechazo_local_informa_no_enviado_y_no_abre_breaker(entorno):
    e = entorno
    with _ocupar(e, 'COBRAR'):
        with pytest.raises(Exception) as error:
            e.clients['COBRAR'].forward(_payload('rechazada'), 'rechazada:COBRAR')
        assert ('forward', 'COBRAR', 'rechazada') not in e.history
        assert e.clients['COBRAR'].breaker.state.value == 'CLOSED'
        assert e.clients['COBRAR'].rejected == 1
        assert getattr(error.value, 'enviado', True) is False, (
            'Un rechazo local debe distinguirse de un timeout: la dependencia no fue llamada')


def test_h3_control_timeout_puede_haber_sido_enviado(entorno):
    e = entorno
    e.timeout_step = 'COBRAR'
    with pytest.raises(e.sc.DependencyFailure) as error:
        e.clients['COBRAR'].forward(_payload('timeout'), 'timeout:COBRAR')
    assert ('forward', 'COBRAR', 'timeout') in e.history
    assert getattr(error.value, 'enviado', True) is True


@pytest.mark.parametrize('step', ['RESERVAR', 'COBRAR', 'INSCRIBIR'])
def test_h3_saga_ya_admitida_no_compensa_un_paso_rechazado_localmente(entorno, step):
    e = entorno
    sid = str(uuid.uuid4())
    # Probar la máquina de estados de una saga ya admitida/reanudada. La
    # admisión HTTP no reemplaza esta protección frente a rechazos locales tardíos.
    with e.sessions() as db:
        saga = e.sg.Saga(id=sid, idempotency_key=sid, request_hash='h3',
                         alumno_id=sid, curso_id=1, monto=100)
        db.add(saga)
        db.commit()
        for previous in e.sg.STEPS[:e.sg.STEPS.index(step)]:
            key = f'{sid}:{previous}'
            e.clients[previous].forward(_payload(sid), key)
            db.add(e.sg.SagaStep(saga_id=sid, paso=previous, tipo='forward',
                                estado='OK', clave=key, intentos=1))
        db.commit()
        with _ocupar(e, step):
            result = e.obj._avanzar(db, saga, e.sg.STEPS.index(step))
        rows = db.scalars(select(e.sg.SagaStep).where(e.sg.SagaStep.saga_id == sid)).all()
        forward = next(row for row in rows if row.paso == step and row.tipo == 'forward')
        assert forward.estado == 'FALLIDO', (
            f'{step} nunca se llamó; quedó {forward.estado} y saga {result["estado"]}')
        assert not any(row.paso == step and row.tipo == 'compensacion' for row in rows)
        assert result['estado'] == 'COMPENSADA'
        for previous in e.sg.STEPS[:e.sg.STEPS.index(step)]:
            assert ('compensacion', previous, sid) in e.history
        assert _resources(e, sid) in ((False, None, False), (False, 'REEMBOLSADO', False))


def test_h3_reembolso_sano_progresa_con_forward_saturado(entorno):
    e = entorno
    e.clients['COBRAR'].forward(_payload('victima'), 'victima:COBRAR')
    failure = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        with _ocupar(e, 'COBRAR'):
            future = pool.submit(e.clients['COBRAR'].compensate,
                                 _payload('victima'), 'victima:comp:COBRAR', 'victima:COBRAR')
            try:
                future.result(timeout=.2)
            except FutureTimeout:
                pass  # Una cola priorizada puede esperar a que termine el forward sano.
            except Exception as exc:
                failure = type(exc).__name__
        try:
            future.result(timeout=3)
        except Exception as exc:
            failure = type(exc).__name__
    assert failure is None, f'La saturación del forward impidió reembolsar: {failure}'
    assert e.pagos['victima'] == 'REEMBOLSADO'
    assert e.history.count(('compensacion', 'COBRAR', 'victima')) == 1


@pytest.fixture
def api(entorno, monkeypatch):
    e = entorno
    module = importlib.import_module('app.orchestrator_main')
    key = 'h3-orchestrator-test-key-at-least-32-characters'
    monkeypatch.setenv('ORCHESTRATOR_API_KEY', key)
    monkeypatch.setattr(module, 'engine', e.sessions.kw['bind'])
    monkeypatch.setattr(module, 'sessions', e.sessions)
    monkeypatch.setattr(module, 'orchestrator', e.obj)
    with TestClient(module.app) as client:
        yield SimpleNamespace(client=client, key=key, env=e)


def _matricular(api, key=None, alumno=None):
    return api.client.post('/v1/matriculas',
        headers={'X-API-Key': api.key, 'Idempotency-Key': key or str(uuid.uuid4())},
        json={'alumno_id': alumno or str(uuid.uuid4()), 'curso_id': 1, 'monto': 100})


def _bloquear_cobro_de(e, alumno):
    # El id lo asigna la saga: seleccionar por alumno evita inspeccionar ids
    # internos o depender del orden de scheduling de los threads.
    original = e.clients['COBRAR'].operation
    def charge(saga, key, timeout):
        if saga['alumno_id'] == alumno:
            e.block_step, e.block_sid = 'COBRAR', saga['id']
        return original(saga, key, timeout=timeout)
    e.clients['COBRAR'].operation = charge


def _rafaga(api, size):
    e = api.env
    first_key, first_alumno = str(uuid.uuid4()), str(uuid.uuid4())
    _bloquear_cobro_de(e, first_alumno)
    with ThreadPoolExecutor(max_workers=size) as pool:
        first = pool.submit(_matricular, api, first_key, first_alumno)
        try:
            assert e.started.wait(3)
            responses = [pool.submit(_matricular, api) for _ in range(size-1)]
            rejected = [future.result(timeout=3) for future in responses]
            with e.lock:
                before_release = list(e.history)
        finally:
            e.release.set()
        accepted = first.result(timeout=3)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()['estado'] == 'COMPLETADA'
    e.block_step = e.block_sid = None
    with e.sessions() as db:
        sagas = [(s.id, s.estado) for s in db.scalars(select(e.sg.Saga)).all()]
    return SimpleNamespace(accepted=accepted, rejected=rejected,
                           before_release=before_release, sagas=sagas)


@pytest.mark.parametrize('size', [2, 4], ids=['2-simultaneas', '4-simultaneas'])
def test_h3_api_rechaza_sobrecarga_antes_de_reservar_con_retry_after(api, size):
    r = _rafaga(api, size)
    first_id = r.accepted.json()['id']
    evidence = [(response.status_code, response.headers.get('Retry-After'),
                 response.json().get('estado')) for response in r.rejected]
    assert all(response.status_code in (429, 503) and response.headers.get('Retry-After')
               for response in r.rejected), evidence
    assert all(sid == first_id for _, _, sid in r.before_release), r.before_release
    before = list(api.env.history)
    recovered = api.client.post('/v1/matriculas',
        headers={'X-API-Key': api.key,
                 'Idempotency-Key': 'recuperacion-'+str(uuid.uuid4())},
        json={'alumno_id': str(uuid.uuid4()), 'curso_id': 1, 'monto': 100})
    assert recovered.status_code == 201, recovered.text  # sin saturación se admite trabajo nuevamente
    assert len(api.env.history) == len(before) + 3


@pytest.mark.parametrize('size', [2, 4], ids=['2-simultaneas', '4-simultaneas'])
def test_h3_rafaga_sana_no_deja_compensaciones_pendientes_ni_huerfanos(api, size):
    e = api.env
    r = _rafaga(api, size)
    pending = [(sid, state) for sid, state in r.sagas
               if state not in ('COMPLETADA', 'COMPENSADA')]
    assert pending == [], f'Dependencias sanas; sagas sin cerrar: {pending}'
    completed = {sid for sid, state in r.sagas if state == 'COMPLETADA'}
    assert completed == {r.accepted.json()['id']}
    assert e.reservas == e.inscripciones == completed
    assert {sid for sid, state in e.pagos.items() if state == 'COBRADO'} == completed
    for sid, state in r.sagas:
        if state == 'COMPENSADA':
            assert _resources(e, sid) in ((False, None, False), (False, 'REEMBOLSADO', False))


def test_h3_control_sagas_sanas_secuenciales_e_idempotentes(api):
    for _ in range(3):
        key, alumno = str(uuid.uuid4()), str(uuid.uuid4())
        first = _matricular(api, key, alumno)
        assert first.status_code == 201, first.text
        assert first.json()['estado'] == 'COMPLETADA'
        history = list(api.env.history)
        replay = _matricular(api, key, alumno)
        assert replay.status_code == 200 and replay.json()['id'] == first.json()['id']
        assert api.env.history == history
    assert len(api.env.reservas) == len(api.env.pagos) == len(api.env.inscripciones) == 3


def test_h3_control_timeout_real_se_compensa_como_incierto(entorno):
    e = entorno
    e.timeout_step = 'COBRAR'
    result, _ = e.obj.matricular(str(uuid.uuid4()), str(uuid.uuid4()), 1, 100)
    assert result['estado'] == 'COMPENSADA'
    with e.sessions() as db:
        step = db.scalars(select(e.sg.SagaStep).where(e.sg.SagaStep.saga_id == result['id'],
            e.sg.SagaStep.paso == 'COBRAR', e.sg.SagaStep.tipo == 'forward')).one()
        assert step.estado == 'INCIERTO'
    assert ('compensacion', 'COBRAR', result['id']) in e.history
    assert _resources(e, result['id']) == (False, None, False)



def test_h3_control_replay_en_curso_no_duplica_reservas_ni_cobros(api):
    e = api.env
    key, alumno = str(uuid.uuid4()), str(uuid.uuid4())
    _bloquear_cobro_de(e, alumno)
    with ThreadPoolExecutor(max_workers=1) as pool:
        original = pool.submit(_matricular, api, key, alumno)
        try:
            assert e.started.wait(3)
            before = list(e.history)
            replay = _matricular(api, key, alumno)
            assert replay.status_code == 202, replay.text
            assert replay.headers.get('Idempotent-Replayed') == 'true'
            assert e.history == before
        finally:
            e.release.set()
        completed = original.result(timeout=3)
    assert completed.status_code == 201, completed.text
    assert replay.json()['id'] == completed.json()['id']
    assert len(e.reservas) == len(e.pagos) == len(e.inscripciones) == 1
    assert len(e.history) == 3


def test_h3_sobrecarga_no_registra_saga_y_permite_reintentar_la_misma_clave(api):
    from tests.contract.test_openapi_contract import validar

    e = api.env
    first_key, first_alumno = str(uuid.uuid4()), str(uuid.uuid4())
    retry_key, retry_alumno = str(uuid.uuid4()), str(uuid.uuid4())
    _bloquear_cobro_de(e, first_alumno)
    with ThreadPoolExecutor(max_workers=1) as pool:
        original = pool.submit(_matricular, api, first_key, first_alumno)
        try:
            assert e.started.wait(3)
            rejected = _matricular(api, retry_key, retry_alumno)
            assert rejected.status_code == 429
            assert int(rejected.headers['Retry-After']) >= 1
            assert rejected.json()['error']['code'] == 'TOO_MANY_REQUESTS'
            validar(api.client.get('/openapi.json').json(), 'post', '/v1/matriculas', rejected)
            with e.sessions() as db:
                assert db.scalar(select(e.sg.Saga).where(e.sg.Saga.idempotency_key == retry_key)) is None
            # La saturación tampoco oculta el uso incorrecto de una clave registrada.
            assert _matricular(api, first_key, retry_alumno).status_code == 422
        finally:
            e.release.set()
        assert original.result(timeout=3).status_code == 201
    recovered = _matricular(api, retry_key, retry_alumno)
    assert recovered.status_code == 201
    assert recovered.headers.get('Idempotent-Replayed') is None
    assert len(e.reservas) == len(e.pagos) == len(e.inscripciones) == 2


def test_h3_conflicto_de_claim_no_pierde_el_permiso_de_admision(api):
    alumno = str(uuid.uuid4())
    assert _matricular(api, alumno=alumno).status_code == 201
    assert _matricular(api, alumno=alumno).status_code == 409
    assert _matricular(api).status_code == 201


def test_h3_error_inesperado_libera_admision_para_otras_sagas(entorno, monkeypatch):
    e = entorno
    def crash(*args):
        raise RuntimeError('Caída antes del primer efecto')
    with monkeypatch.context() as patch:
        patch.setattr(e.obj, '_avanzar', crash)
        with pytest.raises(RuntimeError):
            e.obj.matricular(str(uuid.uuid4()), str(uuid.uuid4()), 1, 100)
    result, _ = e.obj.matricular(str(uuid.uuid4()), str(uuid.uuid4()), 1, 100)
    assert result['estado'] == 'COMPLETADA'
    assert len(e.history) == 3


def test_h3_espera_webpay_libera_admision_y_reanudacion_saturada_se_pospone(entorno):
    e = entorno
    charge = e.clients['COBRAR'].operation
    def pendiente(saga, key, timeout):
        charge(saga, key, timeout=timeout)
        return {'estado': 'PENDIENTE', 'redirect_url': 'http://webpay.test/pagar'}
    e.clients['COBRAR'].operation = pendiente
    waiting, _ = e.obj.matricular('esperando', 'comprador', 1, 100)
    assert waiting['estado'] == 'ESPERANDO_PAGO'
    # Simular otra saga sana ocupando el cupo de ejecución.
    e.clients['COBRAR'].operation = charge
    _bloquear_cobro_de(e, 'otro-comprador')
    with ThreadPoolExecutor(max_workers=1) as pool:
        other = pool.submit(e.obj.matricular, 'otra-clave', 'otro-comprador', 1, 100)
        try:
            assert e.started.wait(3), 'La espera del comprador retuvo el permiso de admisión'
            history = list(e.history)
            deferred = e.obj.pago_resuelto(waiting['id'], 'COBRADO')
            assert deferred['estado'] == 'ESPERANDO_PAGO'
            assert e.history == history
            assert _resources(e, waiting['id']) == (True, 'COBRADO', False)
        finally:
            e.release.set()
        assert other.result(timeout=3)[0]['estado'] == 'COMPLETADA'
    e.clients['COBRAR'].inquiry = lambda *args, **kwargs: {'estado': 'COBRADO'}
    completed = e.obj.sondear_pago(waiting['id'])
    assert completed['estado'] == 'COMPLETADA'
    assert e.obj.pago_resuelto(waiting['id'], 'COBRADO')['estado'] == 'COMPLETADA'
    assert e.history.count(('forward', 'INSCRIBIR', waiting['id'])) == 1
    assert not any(kind == 'compensacion' for kind, _, _ in e.history)


def test_h3_consulta_saturada_no_desplaza_forward_ni_compensacion(entorno):
    e = entorno
    started, release = threading.Event(), threading.Event()
    def inquiry(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return {'estado': 'COBRADO'}
    client = e.clients['COBRAR']
    client.inquiry = inquiry
    with ThreadPoolExecutor(max_workers=1) as pool:
        query = pool.submit(client.query, _payload('consulta'), 'consulta:COBRAR')
        try:
            assert started.wait(3)
            client.forward(_payload('victima'), 'victima:COBRAR')
            client.compensate(_payload('victima'), 'victima:comp:COBRAR', 'victima:COBRAR')
            assert e.pagos['victima'] == 'REEMBOLSADO'
            with pytest.raises(e.sc.LocalRejection) as rejected:
                client.query(_payload('consulta-2'), 'consulta-2:COBRAR')
            assert rejected.value.enviado is False
        finally:
            release.set()
        assert query.result(timeout=3) == {'estado': 'COBRADO'}


def test_h3_capacidad_de_compensacion_tambien_es_acotada(entorno):
    e = entorno
    started, release = threading.Event(), threading.Event()
    client = e.clients['COBRAR']
    compensate = client.compensation
    def gated(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return compensate(*args, **kwargs)
    client.compensation = gated
    client.forward(_payload('victima'), 'victima:COBRAR')
    with ThreadPoolExecutor(max_workers=1) as pool:
        refund = pool.submit(client.compensate, _payload('victima'),
                             'victima:comp:COBRAR', 'victima:COBRAR')
        try:
            assert started.wait(3)
            with pytest.raises(e.sc.LocalRejection) as rejected:
                client.compensate(_payload('otra'), 'otra:comp:COBRAR', 'otra:COBRAR')
            assert rejected.value.enviado is False
            client.forward(_payload('sana'), 'sana:COBRAR')
            assert client.breakers['compensacion'].state.value == 'CLOSED'
        finally:
            release.set()
        refund.result(timeout=3)
    assert e.pagos['victima'] == 'REEMBOLSADO'


def test_h3_circuito_abierto_es_rechazo_conocido_sin_llamar_al_destino(entorno):
    e = entorno
    existing = e.clients['COBRAR']
    client = e.sc.Client(existing.operation, existing.compensation, threshold=1, capacity=1)
    # Abrir el circuito con una llamada real que no respondió.
    e.timeout_step = 'COBRAR'
    with pytest.raises(e.sc.DependencyFailure):
        client.forward(_payload('incierta'), 'incierta:COBRAR')
    history = list(e.history)
    with pytest.raises(e.sc.LocalRejection) as rejected:
        client.forward(_payload('no-enviada'), 'no-enviada:COBRAR')
    assert rejected.value.enviado is False
    assert e.history == history
