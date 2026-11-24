"""Gateway: solo las dos rutas del comprador llegan a Pagos y sin credenciales; la API de servicio no."""
import importlib.util
from pathlib import Path

import pytest

from tests.conftest import ROOT


class _Respuesta:
    def __init__(self, status=200, content=b"<html>ok</html>"):
        self.status_code, self.content = status, content
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setenv("MATRICULAS_API_KEYS", "gateway-test-api-key-with-32-characters")
    monkeypatch.setenv("MATRICULAS_JWT_SECRET", "gateway-test-jwt-secret-with-32-characters")
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "gateway-test-orchestrator-key-32-chars")
    spec = importlib.util.spec_from_file_location("gateway_app", Path(ROOT) / "api-gateway" / "app.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    llamadas = []

    def falso(method, url, headers, data, timeout, allow_redirects):
        llamadas.append({"method": method, "url": url, "headers": headers, "data": data})
        return _Respuesta()

    monkeypatch.setattr(modulo.requests, "request", falso)
    modulo.llamadas = llamadas
    return modulo


def test_el_retorno_de_webpay_llega_a_pagos_sin_credenciales(gw):
    c = gw.app.test_client()
    r = c.get("/v1/pagos/retorno?token_ws=abc")
    assert r.status_code == 200 and gw.llamadas[-1]["url"] == "http://pagos:8000/v1/pagos/retorno?token_ws=abc"
    r = c.post("/v1/pagos/retorno", data={"token_ws": "abc"})  # Webpay devuelve al comprador con un POST de formulario
    ultima = gw.llamadas[-1]
    assert r.status_code == 200 and ultima["method"] == "POST" and ultima["url"].startswith("http://pagos:8000/")
    assert b"token_ws=abc" in ultima["data"] and "x-api-key" not in {k.lower() for k in ultima["headers"]}


def test_la_redireccion_a_webpay_es_publica_solo_con_get_y_un_id(gw):
    c = gw.app.test_client()
    assert c.get("/v1/pagos/ir/3f2a").status_code == 200
    assert gw.llamadas[-1]["url"] == "http://pagos:8000/v1/pagos/ir/3f2a"
    assert c.post("/v1/pagos/ir/3f2a").status_code == 401            # otro método: exige credencial
    assert c.get("/v1/pagos/ir/3f2a/extra").status_code == 401       # otra ruta: exige credencial


def test_la_api_de_servicio_de_pagos_no_se_expone_por_el_gateway(gw):
    c = gw.app.test_client()
    h = {"X-API-Key": "gateway-test-api-key-with-32-characters", "Idempotency-Key": "k"}
    assert c.post("/v1/payments", json={}).status_code == 401
    c.post("/v1/payments", json={"alumno_id": "a", "curso_id": 1, "monto": 1}, headers=h)
    assert gw.llamadas[-1]["url"].startswith("http://matriculas-api:8000/")  # nunca a pagos:8000
    c.post("/v1/payments/refund", json={}, headers=h)
    assert gw.llamadas[-1]["url"].startswith("http://matriculas-api:8000/")
    assert c.put("/internal/fault", json={}, headers=h).status_code == 404


def test_las_rutas_publicas_tienen_rate_limit_por_ip(gw):
    gw.RATE_LIMIT = 3
    c = gw.app.test_client()
    codigos = [c.get("/v1/pagos/retorno?token_ws=x").status_code for _ in range(5)]
    assert codigos == [200, 200, 200, 429, 429]
