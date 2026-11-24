"""H2, fase RED: cobro y operaciones auxiliares no deben compartir salud.

Se usan el Client y los adaptadores de Pagos reales y, en el caso HTTP, la API y
la saga persistente reales (SQLite temporal). El transporte de Pagos es un doble
con timeouts y reloj virtual: sin sleeps, Docker, Transbank ni pagos reales.
Los tests que reproducen H2 NO llevan xfail; los controles de recuperación ya
funcionan con el breaker actual y deben seguir funcionando tras corregirlo.
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
    env = SimpleNamespace(sc=sc, now=100., mode='timeout', calls=[],
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
            return {'estado': 'REEMBOLSADO'}
        if method == 'GET' and path.startswith('/v1/payments/by-key/'):
            env.calls.append('CONSULTAR')
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


# Controles: estas pruebas de recuperación deben pasar también antes de corregir H2.
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


def test_h2_control_half_open_permite_una_sola_sonda(pagos):
    _abrir_solo_con_cobros(pagos)
    pagos.now += 15
    pagos.mode = 'sonda'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pagos.client.forward, pagos.saga, 'sonda')
        try:
            assert pagos.started.wait(2)
            _fallar_cobro(pagos)  # no se admite una segunda sonda simultánea
            assert pagos.client.breaker.state.value == 'HALF_OPEN'
            assert pagos.calls.count('COBRAR') == 6
        finally:
            pagos.release.set()
        assert future.result(timeout=2) == {'estado': 'COBRADO'}
    assert pagos.client.breaker.state.value == 'CLOSED'
