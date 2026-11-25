"""H11 sobre la API de Pagos y Webpay HTTP local, con SQLite temporal."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from pagos.app import resilience
from tests.integration.test_h9_idempotencia_pagos import (
    PAYLOAD, _cobrar, _filas, _post, pagos,
)
from tests.support.fake_transbank import API
from tests.unit.test_h11_breaker_transbank import Reloj


@pytest.fixture
def http_h11(pagos, monkeypatch):
    reloj = Reloj()

    def sin_sleep(_):
        pytest.fail("H11 no usa esperas para adelantar la recuperación")

    monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=reloj, sleep=sin_sleep))
    control = SimpleNamespace(fallar_crear=False, retener_siguiente=False, lock=threading.Lock(),
                              entrada=threading.Event(), liberar=threading.Event())
    original = pagos.fake.server.RequestHandlerClass

    class Handler(original):
        def _preludio(self):
            if self.command == "POST" and urlparse(self.path).path == API:
                with control.lock:
                    retener = control.retener_siguiente
                    control.retener_siguiente = False
                if retener:
                    control.entrada.set()
                    if not control.liberar.wait(timeout=35):
                        raise TimeoutError("La prueba no liberó la creación de Webpay")
                if control.fallar_crear:
                    pagos.fake.calls_inc("create")
                    self._body()
                    self._json(503, {"error_message": "CREATE no disponible en el simulador H11"})
                    return False
            return super()._preludio()

    pagos.fake.server.RequestHandlerClass = Handler
    try:
        yield SimpleNamespace(**vars(pagos), control=control, reloj=reloj)
    finally:
        control.liberar.set()
        pagos.fake.server.RequestHandlerClass = original


def _fallar_cinco_creaciones(ctx, token=None):
    ctx.control.fallar_crear = True
    for i in range(5):
        response = _post(ctx.client, "/v1/payments", f"h11-fallo-{i}:COBRAR", PAYLOAD)
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == "TRANSBANK_ERROR"
        if token is not None:
            # Consulta protegida REAL hacia Webpay; consultar la BD local no ejercita el breaker.
            assert ctx.service.webpay.estado(token)["status"] == "AUTHORIZED"


def _exigir_rechazo_local(ctx, response, intentos_antes):
    assert ctx.fake.calls["create"] == intentos_antes, "La creación rechazada llegó a Webpay"
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "TRANSBANK_NO_DISPONIBLE"
    assert response.headers.get("Retry-After")


def test_h11_api_abre_crear_tras_cinco_503_intercalados_con_consultas_sanas(http_h11):
    ctx = http_h11
    token = _cobrar(ctx, "h11-control:COBRAR")
    baseline = ctx.fake.calls["create"]
    _fallar_cinco_creaciones(ctx, token)
    assert ctx.fake.calls["create"] == baseline + 5 and ctx.fake.calls["status"] == 5
    response = _post(ctx.client, "/v1/payments", "h11-no-enviar:COBRAR", PAYLOAD)
    _exigir_rechazo_local(ctx, response, baseline + 5)
    assert len(ctx.fake.tx) == 1  # solo la transacción de control


def test_h11_api_circuito_de_crear_abierto_permite_compensar_un_cobro_sano(http_h11):
    ctx = http_h11
    key = "h11-compensacion:COBRAR"
    token = _cobrar(ctx, key)
    baseline = ctx.fake.calls["create"]
    _fallar_cinco_creaciones(ctx)
    response = _post(ctx.client, "/v1/payments/refund", "h11-compensar:comp:COBRAR", {"original_key": key})
    assert response.status_code == 200, response.text
    assert response.json()["estado"] == "REEMBOLSADO"
    assert ctx.fake.calls["refund"] == 1 and ctx.fake.tx[token]["status"] == "REVERSED"
    assert _filas(ctx)[key]["estado"] == "REEMBOLSADO"
    assert ctx.service.webpay.estado(token)["status"] == "REVERSED"
    response = _post(ctx.client, "/v1/payments", "h11-sigue-abierto:COBRAR", PAYLOAD)
    _exigir_rechazo_local(ctx, response, baseline + 5)


def test_h11_api_consulta_no_cierra_half_open_y_solo_una_creacion_prueba_recuperacion(http_h11):
    ctx = http_h11
    token = _cobrar(ctx, "h11-control-sonda:COBRAR")
    baseline = ctx.fake.calls["create"]
    _fallar_cinco_creaciones(ctx)
    ctx.reloj.avanzar(ctx.settings.cb_recovery_seconds + 1)
    assert ctx.service.webpay.estado(token)["status"] == "AUTHORIZED"
    ctx.control.fallar_crear = False
    ctx.control.retener_siguiente = True
    with ThreadPoolExecutor(1) as executor:
        probe = executor.submit(_post, ctx.client, "/v1/payments", "h11-sonda:COBRAR", PAYLOAD)
        try:
            assert ctx.control.entrada.wait(timeout=10), "No entró la sonda HTTP de creación"
            assert not probe.done()
            metrics = ctx.client.get("/metrics").text
            assert 'circuito_estado{dependencia="TRANSBANK",estado="HALF_OPEN"} 1' in metrics
            assert 'transbank_circuito_estado{operacion="crear",estado="HALF_OPEN"} 1' in metrics
            assert 'transbank_circuito_estado{operacion="estado",estado="CLOSED"} 1' in metrics
            response = _post(ctx.client, "/v1/payments", "h11-esperar-sonda:COBRAR", PAYLOAD)
            _exigir_rechazo_local(ctx, response, baseline + 5)
            assert 'transbank_circuito_rechazos_total{operacion="crear"} 1' in ctx.client.get("/metrics").text
        finally:
            ctx.control.liberar.set()
        assert probe.result(timeout=10).status_code == 201
    assert 'transbank_circuito_estado{operacion="crear",estado="CLOSED"} 1' in ctx.client.get("/metrics").text
    # La misma clave rechazada localmente puede continuar tras una recuperación real de crear.
    replay = _post(ctx.client, "/v1/payments", "h11-esperar-sonda:COBRAR", PAYLOAD)
    assert replay.status_code == 200 and replay.headers["Idempotent-Replayed"] == "true"
    assert ctx.fake.calls["create"] == baseline + 7
