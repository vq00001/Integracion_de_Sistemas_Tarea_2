"""H14: fallos de Inscripción detrás del Gateway deben aislarse por cliente."""

import base64
import hashlib
import hmac
import importlib.util
import json
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
JWT_SECRET = "h14-regression-secret-at-least-32-bytes-long"
AUTH_LIMIT = 20


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("MATRICULAS_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("JWT_ISSUER", "h14-test-issuer")
    monkeypatch.setenv("JWT_AUDIENCE", "h14-test-audience")
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "h14-orchestrator-secret")
    monkeypatch.setenv("GATEWAY_RATE_LIMIT", "1000")
    monkeypatch.setenv("INSCRIPCION_SERVICE_URL", "http://inscripcion.h14")
    spec = importlib.util.spec_from_file_location("gateway_h14_test", ROOT / "api-gateway" / "app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    yield module
    module._requests.clear()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(gateway, subject="student-1") -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": subject,
        "iss": gateway.JWT_ISSUER,
        "aud": gateway.JWT_AUDIENCE,
        "exp": int(time.time()) + 300,
    }).encode())
    body = f"{header}.{payload}"
    signature = hmac.new(JWT_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def _response(status: int, body: bytes):
    response = requests.Response()
    response.status_code = status
    response._content = body
    response.headers["Content-Type"] = "application/json"
    return response


def _simular_inscripcion(monkeypatch, gateway):
    """Servicio aislado con el mismo límite previo a comprobar cada credencial."""
    fallos_por_ip = defaultdict(int)
    llamadas = []

    def ip_autenticada(headers):
        # El servicio solo acepta una IP remitida por Gateway cuando lleva firma
        # HMAC. Sin firma válida, usa la IP del socket común del Gateway.
        ip = headers.get("X-Gateway-Client-IP", "")
        firma = headers.get("X-Gateway-Client-IP-Signature", "")
        esperado = hmac.new(
            JWT_SECRET.encode(), f"client-ip:{ip}".encode(), hashlib.sha256,
        ).hexdigest()
        if ip and hmac.compare_digest(firma, esperado):
            return ip
        return "10.0.0.8"

    def transporte(method, url, *, headers, data, timeout, allow_redirects):
        parsed = urlsplit(url)
        assert parsed.netloc == "inscripcion.h14"
        ip = ip_autenticada(headers)
        llamadas.append((method, parsed.path, dict(headers), ip))
        if fallos_por_ip[ip] >= AUTH_LIMIT:
            return _response(429, b'{"error":{"code":"TOO_MANY_REQUESTS"}}')
        if parsed.path == "/v1/auth/token":
            fallos_por_ip[ip] += 1
            return _response(401, b'{"error":{"code":"UNAUTHENTICATED"}}')
        if parsed.path == "/v1/students/student-1":
            return _response(200, b'{"id":"student-1"}')
        return _response(404, b'{"error":{"code":"NOT_FOUND"}}')

    monkeypatch.setattr(gateway.requests, "request", transporte)
    return llamadas, fallos_por_ip


def test_h14_fallos_de_login_de_un_cliente_no_bloquean_jwt_valido_de_otro(gateway, monkeypatch):
    llamadas, _ = _simular_inscripcion(monkeypatch, gateway)
    client = gateway.app.test_client()
    token = _jwt(gateway)
    ip_cliente_a = "198.51.100.11"
    ip_cliente_b = "198.51.100.22"

    # Control: el JWT válido de B funciona antes de los fallos de A.
    inicial = client.get(
        "/v1/students/student-1", headers={"Authorization": f"Bearer {token}"},
        environ_overrides={"REMOTE_ADDR": ip_cliente_b},
    )
    assert inicial.status_code == 200

    # A manda 20 credenciales inválidas e intenta atribuirlas a B con una cabecera.
    for _ in range(AUTH_LIMIT):
        fallo = client.post(
            "/v1/auth/token", data={"username": "desconocido@example.com", "password": "incorrecta"},
            headers={"X-Forwarded-For": ip_cliente_b},
            environ_overrides={"REMOTE_ADDR": ip_cliente_a},
        )
        assert fallo.status_code == 401

    bloqueado_a = client.post(
        "/v1/auth/token", data={"username": "desconocido@example.com", "password": "incorrecta"},
        environ_overrides={"REMOTE_ADDR": ip_cliente_a},
    )
    assert bloqueado_a.status_code == 429

    respuesta_b = client.get(
        "/v1/students/student-1", headers={"Authorization": f"Bearer {token}"},
        environ_overrides={"REMOTE_ADDR": ip_cliente_b},
    )
    assert respuesta_b.status_code == 200, respuesta_b.data

    # El Gateway no reenvía la IP falsificada por A.
    login_calls = [call for call in llamadas if call[1] == "/v1/auth/token"]
    assert len(login_calls) == AUTH_LIMIT
    assert all("X-Forwarded-For" not in call[2] for call in login_calls)
