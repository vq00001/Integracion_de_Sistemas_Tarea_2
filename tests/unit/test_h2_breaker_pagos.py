"""H2: cobro y operaciones auxiliares conservan historiales independientes.

Se usan el Client y los adaptadores de Pagos reales y, en el caso HTTP, la API y
la saga persistente reales (SQLite temporal). El transporte de Pagos es un doble
con timeouts y reloj virtual: sin sleeps, Docker, Transbank ni pagos reales.
Los tests conservan las regresiones de la fase RED y los controles de recuperación.
"""
import importlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.test_api_unidad import m  # noqa: F401
from tests.unit.test_saga import setup_saga  # noqa: F401


@pytest.fixture
def pagos(m, monkeypatch):
    sc = importlib.import_module('app.saga_clients')
    cb = importlib.import_module('app.circuit_breaker')
    env = SimpleNamespace(sc=sc, now=100., mode='timeout', calls=[], aux_timeout=set(),
                          started=threading.Event(), release=threading.Event())
    # Cambiar solo el reloj del módulo del breaker, no time.monotonic global:
    # TestClient, locks y futuros conservan sus relojes reales.
    monkeypatch.setattr(cb, 'time', SimpleNamespace(monotonic=lambda: env.now))
    monkeypatch.setenv('PAGOS_URL', 'http://pagos.h2')
    monkeypatch.setenv('SAGA_CB_THRESHOLD', '5')
    monkeypatch.setenv('SAGA_CB_RECOVERY_SECONDS', '15')
    monkeypatch.setenv('SAGA_TIMEOUT_SECONDS', '2')

    def http(method, url, payload, key, timeout, **kwargs):
        path = urlsplit(url).path
        if method == 'POST' and path == '/v1/payments':
            env.calls.append('COBRAR')
            if env.mode == 'timeout':
                env.now += timeout  # simula que venció el plazo de esta llamada
                raise TimeoutError('Pagos lento')
            if env.mode == 'sonda':
                env.started.set()
                assert env.release.wait(2), 'La prueba no liberó la sonda'
            return {'estado': 'COBRADO'}
        if method == 'POST' and path == '/v1/payments/refund':
            env.calls.append('REEMBOLSAR')
            if 'reembolso' in env.aux_timeout:
                raise TimeoutError('Reembolso lento')
            return {'estado': 'REEMBOLSADO'}
        if method == 'GET' and path.startswith('/v1/payments/by-key/'):
            env.calls.append('CONSULTAR')
            if 'consulta' in env.aux_timeout:
                raise TimeoutError('Consulta lenta')
            return {'estado': 'COBRADO'}
        raise AssertionError(f'Operación inesperada: {method} {path}')

    monkeypatch.setattr(sc, 'http', http)
    # Probar la construcción productiva, no una configuración especial de test.
    env.client = sc.clients_from_env()['COBRAR']
    env.saga = {'id': 'saga-h2', 'alumno_id': 'alumno-h2', 'curso_id': 1, 'monto': 100}
    return env


def _fallar_cobro(pagos):
    with pytest.raises(pagos.sc.DependencyFailure):
        pagos.client.forward(pagos.saga, 'saga-h2:COBRAR')


def _auxiliar(pagos, operacion):
    if operacion == 'reembolso':
        return pagos.client.compensate(pagos.saga, 'saga-h2:comp:COBRAR', 'saga-h2:COBRAR')
    return pagos.client.query(pagos.saga, 'saga-h2:COBRAR')


def _abrir_solo_con_cobros(pagos):
    for _ in range(5):
        _fallar_cobro(pagos)
    assert pagos.client.breaker.state.value == 'OPEN'
    assert not pagos.client.available


@pytest.mark.parametrize('auxiliares', [('consulta',), ('reembolso', 'consulta')],
                         ids=['consulta-exitosa', 'reembolso-y-consulta-exitosos'])
def test_h2_consultas_y_reembolsos_no_borran_fallas_de_cobro(pagos, auxiliares):
    for _ in range(5):
        _fallar_cobro(pagos)
        for operacion in auxiliares:
            _auxiliar(pagos, operacion)
    assert pagos.calls.count('COBRAR') == 5
    assert pagos.client.breaker.state.value == 'OPEN', pagos.calls
    assert not pagos.client.available


@pytest.mark.parametrize('operacion', ['reembolso', 'consulta'])
def test_h2_operacion_auxiliar_sana_no_reabre_admision_de_cobros(pagos, operacion):
    _abrir_solo_con_cobros(pagos)
    before = pagos.calls.count('COBRAR')
    # Cobrar caído no debe impedir cancelar cobros ni consultar pagos existentes
    # cuando esas operaciones sí están sanas, ni convertirlas en pruebas de cobro.
    _auxiliar(pagos, operacion)
    assert pagos.client.breaker.state.value == 'OPEN'
    assert not pagos.client.available
    _fallar_cobro(pagos)  # rechazo local, sin nueva llamada de cobro
    assert pagos.calls.count('COBRAR') == before


@pytest.mark.parametrize('operacion', ['reembolso', 'consulta'])
def test_h2_auxiliar_exitoso_no_cierra_cobro_en_half_open(pagos, operacion):
    _abrir_solo_con_cobros(pagos)
    pagos.now += 15
    assert pagos.client.breaker.state.value == 'HALF_OPEN'
    _auxiliar(pagos, operacion)
    assert pagos.client.breaker.state.value == 'HALF_OPEN'
    # La sonda real aún falla: debe reabrir, sin tomar el auxiliar como recuperación.
    _fallar_cobro(pagos)
    assert pagos.client.breaker.state.value == 'OPEN'


def test_h2_api_tras_cinco_sagas_fallidas_rechaza_sin_reservar_ni_llamar_pagos(
        setup_saga, pagos, monkeypatch):
    sg, obj, sessions, events, clients = setup_saga
    clients['COBRAR'] = pagos.client
    obj.stale_seconds = 3600
    api = importlib.import_module('app.orchestrator_main')
    key = 'h2-api-test-key-with-at-least-32-characters'
    monkeypatch.setenv('ORCHESTRATOR_API_KEY', key)
    monkeypatch.setattr(api, 'orchestrator', obj)
    monkeypatch.setattr(api, 'sessions', sessions)
    monkeypatch.setattr(api, 'engine', sessions.kw['bind'])
    headers = {'X-API-Key': key}

    def matricular(client):
        return client.post('/v1/matriculas', headers={
            **headers, 'Idempotency-Key': str(uuid.uuid4())},
            json={'alumno_id': str(uuid.uuid4()), 'curso_id': 1, 'monto': 100})

    with TestClient(api.app) as client:
        for _ in range(5):
            result = matricular(client)
            assert result.status_code == 503, result.text
            assert result.json()['estado'] == 'COMPENSADA'
        before_events, before_calls, before_time = list(events), list(pagos.calls), pagos.now
        result = matricular(client)
        sid = result.json()['id']
        with sessions() as db:
            steps = db.scalars(select(sg.SagaStep).where(sg.SagaStep.saga_id == sid)).all()
        evidence = {'circuito': pagos.client.breaker.state.value,
                    'nuevas_reservas': events.count('RESERVAR') - before_events.count('RESERVAR'),
                    'nuevos_cobros': pagos.calls.count('COBRAR') - before_calls.count('COBRAR'),
                    'demora_virtual': pagos.now - before_time}
        assert result.status_code == 503
        assert result.json()['motivo_fallo'] == 'PAGOS_CIRCUITO_ABIERTO', evidence
        assert result.json()['estado'] == 'COMPENSADA'
        assert events == before_events and pagos.calls == before_calls, evidence
        assert pagos.now == before_time and steps == [], evidence
        # El estado es consultable y el circuito no está permanentemente cerrado
        # al trabajo: tras el cooldown, una NUEVA saga puede completar la operación.
        view = client.get('/v1/sagas/'+sid, headers=headers)
        assert view.status_code == 200 and view.json()['estado'] == 'COMPENSADA'
        pagos.now += 15
        pagos.mode = 'sano'
        recovered = matricular(client)
        assert recovered.status_code == 201, recovered.text
        assert recovered.json()['estado'] == 'COMPLETADA'
        assert pagos.client.breaker.state.value == 'CLOSED'
        assert events.count('RESERVAR') == before_events.count('RESERVAR') + 1
        assert events.count('INSCRIBIR') == 1


# Controles de las transiciones y de la sonda única del circuito forward.
def test_h2_control_sonda_de_cobro_exitosa_cierra_y_reanuda(pagos):
    _abrir_solo_con_cobros(pagos)
    pagos.now += 15
    assert pagos.client.breaker.state.value == 'HALF_OPEN'
    pagos.mode = 'sano'
    assert pagos.client.forward(pagos.saga, 'sonda') == {'estado': 'COBRADO'}
    assert pagos.client.breaker.state.value == 'CLOSED' and pagos.client.available
    assert pagos.client.forward(pagos.saga, 'otro') == {'estado': 'COBRADO'}
    assert pagos.calls.count('COBRAR') == 7


def test_h2_control_sonda_fallida_reabre_y_reinicia_cooldown(pagos):
    _abrir_solo_con_cobros(pagos)
    pagos.now += 15
    _fallar_cobro(pagos)
    assert pagos.client.breaker.state.value == 'OPEN'
    attempts = pagos.calls.count('COBRAR')
    pagos.now += 14
    _fallar_cobro(pagos)
    assert pagos.calls.count('COBRAR') == attempts
    assert not pagos.client.available
    pagos.now += 1
    assert pagos.client.breaker.state.value == 'HALF_OPEN'


@pytest.mark.parametrize('auxiliar', [None, 'reembolso', 'consulta'],
                         ids=['solo-sonda', 'con-reembolso', 'con-consulta'])
def test_h2_control_half_open_permite_una_sola_sonda(pagos, auxiliar):
    _abrir_solo_con_cobros(pagos)
    pagos.now += 15
    pagos.mode = 'sonda'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pagos.client.forward, pagos.saga, 'sonda')
        try:
            assert pagos.started.wait(2)
            if auxiliar is not None:
                _auxiliar(pagos, auxiliar)
                assert pagos.client.breaker.state.value == 'HALF_OPEN'
            _fallar_cobro(pagos)  # no se admite una segunda sonda simultánea
            assert pagos.client.breaker.state.value == 'HALF_OPEN'
            assert pagos.calls.count('COBRAR') == 6
        finally:
            pagos.release.set()
        assert future.result(timeout=2) == {'estado': 'COBRADO'}
    assert pagos.client.breaker.state.value == 'CLOSED'


@pytest.mark.parametrize('operacion,kind,other', [
    ('reembolso', 'compensacion', 'consulta'), ('consulta', 'consulta', 'compensacion')])
def test_h2_falla_auxiliar_tiene_umbral_y_recuperacion_propios(pagos, operacion, kind, other):
    pagos.aux_timeout.add(operacion)
    for _ in range(5):
        with pytest.raises(pagos.sc.DependencyFailure):
            _auxiliar(pagos, operacion)
    assert pagos.client.breakers[kind].state.value == 'OPEN'
    assert pagos.client.breaker.state.value == 'CLOSED' and pagos.client.available
    assert pagos.client.breakers[other].state.value == 'CLOSED'
    before = list(pagos.calls)
    with pytest.raises(pagos.sc.DependencyFailure):
        _auxiliar(pagos, operacion)
    assert pagos.calls == before  # rechazo del auxiliar sin tocar la dependencia
    pagos.mode = 'sano'
    assert pagos.client.forward(pagos.saga, 'nuevo-cobro') == {'estado': 'COBRADO'}
    _auxiliar(pagos, 'consulta' if operacion == 'reembolso' else 'reembolso')
    assert pagos.client.breakers[kind].state.value == 'OPEN'  # otros éxitos no lo cierran
    pagos.aux_timeout.clear()
    pagos.now += 15
    _auxiliar(pagos, operacion)
    assert pagos.client.breakers[kind].state.value == 'CLOSED'


def test_h2_metricas_distinguen_forward_de_compensacion_y_consulta(setup_saga, pagos, monkeypatch):
    _, obj, sessions, _, _ = setup_saga
    _abrir_solo_con_cobros(pagos)
    _auxiliar(pagos, 'reembolso')
    _auxiliar(pagos, 'consulta')
    api = importlib.import_module('app.orchestrator_main')
    monkeypatch.setenv('ORCHESTRATOR_API_KEY', 'h2-metrics-api-key-with-at-least-32-characters')
    monkeypatch.setattr(obj, 'clients', {'COBRAR': pagos.client})
    monkeypatch.setattr(api, 'orchestrator', obj)
    monkeypatch.setattr(api, 'sessions', sessions)
    monkeypatch.setattr(api, 'engine', sessions.kw['bind'])
    with TestClient(api.app) as client:
        response = client.get('/metrics')
    assert response.status_code == 200
    # La métrica original sigue representando la admisión del cobro.
    assert 'circuito_estado{dependencia="COBRAR",estado="OPEN"} 1' in response.text
    for operation, expected in [('forward', 'OPEN'), ('compensacion', 'CLOSED'), ('consulta', 'CLOSED')]:
        for state in ('CLOSED', 'OPEN', 'HALF_OPEN'):
            series = (f'saga_circuito_operacion_estado{{dependencia="COBRAR",'
                      f'operacion="{operation}",estado="{state}"}} {int(state == expected)}')
            assert series in response.text
