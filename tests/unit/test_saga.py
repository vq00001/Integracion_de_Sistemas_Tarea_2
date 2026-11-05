"""Máquina de estados, persistencia, concurrencia y contratos sin red ni sleeps."""
import importlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from tests.unit.test_api_unidad import m, cliente, _alumno, _admin
from tests.contract.test_openapi_contract import validar


class FakeClient:
    available = True
    def __init__(self, name, events, failure=None, compensation_failure=False):
        self.name, self.events = name, events
        self.failure, self.compensation_failure = failure, compensation_failure

    def forward(self, saga, key):
        self.events.append(self.name)
        if self.failure:
            raise self.failure()

    def compensate(self, saga, key, original):
        self.events.append("comp:"+self.name)
        assert key == f"{saga['id']}:comp:{self.name}"
        assert original == f"{saga['id']}:{self.name}"
        if self.compensation_failure:
            raise TimeoutError()


@pytest.fixture
def setup_saga(m, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    sg = importlib.import_module("app.saga")
    engine = create_engine(f"sqlite:///{tmp_path / 'saga.db'}", connect_args={"check_same_thread": False})
    sg.Base.metadata.create_all(engine, tables=[sg.Saga.__table__, sg.SagaStep.__table__])
    sessions = sessionmaker(engine)
    events = []
    clients = {n: FakeClient(n, events) for n in sg.STEPS}
    obj = sg.Orchestrator(sessions, clients, retries=2, backoff=0, stale_seconds=0)
    return sg, obj, sessions, events, clients


def run(obj, key=None, alumno=None, curso=1, monto=100):
    return obj.matricular(key or str(uuid.uuid4()), alumno or str(uuid.uuid4()), curso, monto)


def test_happy_path_y_replay_sin_efectos(setup_saga):
    sg, obj, sessions, events, clients = setup_saga
    key, alumno = str(uuid.uuid4()), str(uuid.uuid4())
    result, replay = run(obj, key, alumno)
    assert result['estado'] == 'COMPLETADA' and not replay
    assert events == list(sg.STEPS)
    second, replay = run(obj, key, alumno)
    assert second == result and replay and events == list(sg.STEPS)
    from app.errors import IdempotencyKeyReusedError
    with pytest.raises(IdempotencyKeyReusedError):
        run(obj, key, alumno, monto=200)


@pytest.mark.parametrize('business', [True, False])
def test_falla_ultimo_paso_compensa_en_orden_inverso(setup_saga, business):
    sg, obj, sessions, events, clients = setup_saga
    from app.saga_clients import BusinessError
    clients['INSCRIBIR'].failure = BusinessError if business else TimeoutError
    result, _ = run(obj)
    assert result['estado'] == 'COMPENSADA'
    expected = ['RESERVAR','COBRAR','INSCRIBIR']
    if not business:
        expected += ['comp:INSCRIBIR']
    assert events == expected+['comp:COBRAR','comp:RESERVAR']


def test_compensacion_pendiente_se_recupera_sin_repetir_lo_cerrado(setup_saga):
    sg, obj, sessions, events, clients = setup_saga
    clients['INSCRIBIR'].failure = TimeoutError
    clients['COBRAR'].compensation_failure = True
    result, _ = run(obj)
    assert result['estado'] == 'COMPENSACION_PENDIENTE'
    assert events[-2:] == ['comp:COBRAR']*2
    clients['COBRAR'].compensation_failure = False
    obj.reconcile()
    with sessions() as db:
        assert db.get(sg.Saga,result['id']).estado == 'COMPENSADA'
    assert events.count('comp:INSCRIBIR') == 1
    assert events[-2:] == ['comp:COBRAR','comp:RESERVAR']


def test_fail_fast_pagos_no_reserva(setup_saga):
    sg, obj, sessions, events, clients = setup_saga
    clients['COBRAR'].available = False
    result, _ = run(obj)
    assert result['estado'] == 'COMPENSADA' and events == []


def test_recuperar_intencion_persistida_tras_caida(setup_saga):
    sg, obj, sessions, events, clients = setup_saga
    from app.time import utc_now
    with sessions() as db:
        saga = sg.Saga(idempotency_key='crash',request_hash='x',alumno_id='id',curso_id=1,monto=100,
                       updated_at=utc_now()-timedelta(minutes=5))
        db.add(saga); db.commit()
        sid = saga.id
        db.add(sg.SagaStep(saga_id=sid,paso='RESERVAR',tipo='forward',estado='PENDIENTE',
                          clave=f'{sid}:RESERVAR',intentos=1))
        db.commit()
    restarted = sg.Orchestrator(sessions, clients, backoff=0, stale_seconds=0)
    restarted.reconcile()
    assert events == ['comp:RESERVAR']
    with sessions() as db:
        assert db.get(sg.Saga,sid).estado == 'COMPENSADA'


def test_concurrencia_indice_parcial_y_replay(setup_saga):
    sg, obj, sessions, events, clients = setup_saga
    from app.errors import ConflictError
    alumno = str(uuid.uuid4())
    def attempt(i):
        try:
            return run(obj, alumno=alumno)[0]['estado']
        except ConflictError:
            return '409'
    with ThreadPoolExecutor(6) as pool:
        states = list(pool.map(attempt, range(6)))
    assert states.count('COMPLETADA') == 1 and states.count('409') == 5
    assert events == list(sg.STEPS)


def test_client_clasifica_negocio_sin_abrir_breaker_y_bulkhead_aislado(m):
    from app.saga_clients import Client, BusinessError, DependencyFailure
    def reject(*args, **kwargs): raise BusinessError()
    client = Client(reject, reject, threshold=1)
    with pytest.raises(BusinessError): client.forward({},'k')
    assert client.breaker.state.value == 'CLOSED'
    def fail(*args, **kwargs): raise TimeoutError()
    broken = Client(fail, fail, threshold=1)
    with pytest.raises(DependencyFailure): broken.forward({},'k')
    assert not broken.available
    client.semaphore.acquire()
    other = Client(lambda *a,**k: None, reject, capacity=1)
    other.semaphore.acquire()
    with pytest.raises(DependencyFailure): other.forward({},'k')
    assert other.rejected == 1 and client.available
    other.semaphore.release(); client.semaphore.release()


def test_inscripcion_idempotencia_bajas_y_reinscripcion(cliente,m,monkeypatch):
    est,_ = _alumno(cliente)
    body = dict(student_id=est['id'],curso_id=1)
    h = {**_admin(),'Idempotency-Key':str(uuid.uuid4())}
    # Una llamada a Cupos en este flujo debe hacer fallar la prueba.
    import app.grpc_client as grpc_client
    def forbidden(*a,**kw): raise AssertionError('Inscripción llamó Cupos')
    monkeypatch.setattr(grpc_client.cupos_client,'ocupar_cupo',forbidden)
    monkeypatch.setattr(grpc_client.cupos_client,'liberar_cupo',forbidden)
    first = cliente.post('/v1/enrollments',json=body,headers=h)
    assert first.status_code == 201
    assert cliente.post('/v1/enrollments',json=body,headers=h).json() == first.json()
    spec = cliente.get('/openapi.json').json()
    validar(spec,'post','/v1/enrollments',first)
    for _ in range(2):
        baja=cliente.post(f"/v1/enrollments/{first.json()['id']}/revert",headers=_admin())
        assert baja.status_code == 200
        validar(spec,'post','/v1/enrollments/{enrollment_id}/revert',baja)
    replay = cliente.post('/v1/enrollments',json=body,headers=h)
    assert replay.json()['id'] == first.json()['id'] and replay.json()['estado'] == 'ANULADA'
    h['Idempotency-Key'] = str(uuid.uuid4())
    again=cliente.post('/v1/enrollments',json=body,headers=h)
    assert again.status_code == 201 and again.json()['id'] != first.json()['id']


def test_interruptor_protegido_y_cancelacion_anticipada(cliente,m,monkeypatch):
    monkeypatch.setenv('INTERNAL_KEY','test-internal-secret')
    assert cliente.put('/internal/fault',json={'falla':True}).status_code == 403
    h = {'X-Internal-Key':'test-internal-secret'}
    est,_ = _alumno(cliente)
    body = dict(student_id=est['id'],curso_id=1)
    assert cliente.put('/internal/fault',json={'falla':True},headers=h).status_code == 200
    try:
        failure = cliente.post('/v1/enrollments',json=body,headers=_admin())
        assert failure.status_code == 503
        validar(cliente.get('/openapi.json').json(),'post','/v1/enrollments',failure)
    finally:
        cliente.put('/internal/fault',json={},headers=h)
    key=str(uuid.uuid4())
    cancelled=cliente.post('/internal/enrollments/cancel',json=body,headers={**h,'Idempotency-Key':key})
    assert cancelled.status_code == 200
    late=cliente.post('/v1/enrollments',json=body,headers={**_admin(),'Idempotency-Key':key})
    assert late.json()['estado'] == 'ANULADA'
    assert late.json()['id'] == cancelled.json()['id']
    assert '/internal/fault' not in cliente.get('/openapi.json').json()['paths']


def test_openapi_orquestador_respuestas_y_metricas(setup_saga,monkeypatch):
    sg, obj, sessions, events, clients=setup_saga
    obj.stale_seconds=60
    api=importlib.import_module('app.orchestrator_main')
    monkeypatch.setenv('ORCHESTRATOR_API_KEY','test-orchestrator-key-with-32-characters')
    monkeypatch.setattr(api,'orchestrator',obj)
    monkeypatch.setattr(api,'sessions',sessions)
    monkeypatch.setattr(api,'engine',sessions.kw['bind'])
    # Métricas de los adaptadores reales se prueban con el cliente de resiliencia.
    from fastapi.testclient import TestClient
    h={'X-API-Key':'test-orchestrator-key-with-32-characters','Idempotency-Key':str(uuid.uuid4())}
    body={'alumno_id':str(uuid.uuid4()),'curso_id':1,'monto':100}
    with TestClient(api.app) as cli:
        spec=cli.get('/openapi.json').json()
        import json
        from tests.conftest import ROOT
        assert json.loads((ROOT/'matriculas-api/docs/orchestrator.openapi.json').read_text())==spec
        first=cli.post('/v1/matriculas',json=body,headers=h)
        assert first.status_code==201
        validar(spec,'post','/v1/matriculas',first)
        replay=cli.post('/v1/matriculas',json=body,headers=h)
        assert replay.status_code==200 and replay.headers['Idempotent-Replayed']=='true'
        validar(spec,'post','/v1/matriculas',replay)
        for response in (
            cli.post('/v1/matriculas',json={**body,'monto':200},headers=h),
            cli.post('/v1/matriculas',json=body,headers={**h,'Idempotency-Key':'new'}),
            cli.post('/v1/matriculas',json=body),
            cli.post('/v1/matriculas',json={},headers=h)):
            validar(spec,'post','/v1/matriculas',response)
        assert cli.get('/v1/sagas/'+first.json()['id'],headers=h).status_code==200
        clients['COBRAR'].available=False
        failure=cli.post('/v1/matriculas',json={**body,'alumno_id':str(uuid.uuid4())},
                         headers={**h,'Idempotency-Key':str(uuid.uuid4())})
        assert failure.status_code==503
        validar(spec,'post','/v1/matriculas',failure)
        from app.saga_clients import Client
        monkeypatch.setattr(obj,'clients',{'RESERVAR':Client(lambda *a,**k:None,lambda *a,**k:None)})
        metrics=cli.get('/metrics')
        assert metrics.status_code==200
        assert 'sagas_total{estado="COMPLETADA"} 1' in metrics.text
        assert 'circuito_estado' in metrics.text and 'paso_duracion_seconds_sum' in metrics.text


def test_lentitud_no_mantiene_candado_del_interruptor(cliente,m,monkeypatch):
    import app.faults as faults
    monkeypatch.setenv('INTERNAL_KEY','test-internal-secret')
    sleeps=[]
    monkeypatch.setattr(faults.time,'sleep',lambda duration:sleeps.append(duration))
    h={'X-Internal-Key':'test-internal-secret'}
    cliente.put('/internal/fault',json={'lentitud_seconds':1.25},headers=h)
    try:
        est,_=_alumno(cliente)
        assert cliente.post('/v1/enrollments',headers=_admin(),
                            json={'student_id':est['id'],'curso_id':1}).status_code==201
        assert sleeps==[1.25]
    finally:
        cliente.put('/internal/fault',json={},headers=h)


def test_misma_clave_saga_en_paralelo_no_repite_efectos(setup_saga):
    sg,obj,sessions,events,clients=setup_saga
    key,alumno=str(uuid.uuid4()),str(uuid.uuid4())
    with ThreadPoolExecutor(6) as pool:
        results=list(pool.map(lambda _:run(obj,key,alumno),range(6)))
    assert len({result['id'] for result,replay in results})==1
    assert sum(not replay for result,replay in results)==1
    assert events==list(sg.STEPS)


def test_saga_compensada_permite_nuevo_intento_del_mismo_par(setup_saga):
    sg,obj,sessions,events,clients=setup_saga
    alumno=str(uuid.uuid4())
    clients['INSCRIBIR'].failure=TimeoutError
    first,_=run(obj,alumno=alumno)
    assert first['estado']=='COMPENSADA'
    clients['INSCRIBIR'].failure=None
    second,_=run(obj,alumno=alumno)
    assert second['estado']=='COMPLETADA' and second['id']!=first['id']
