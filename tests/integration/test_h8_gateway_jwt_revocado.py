"""H8: el Gateway debe consultar a Inscripción antes de delegar un JWT."""

import base64
import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
JWT_SECRET = "h8-regression-secret-at-least-32-bytes-long"


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("MATRICULAS_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("JWT_ISSUER", "h8-test-issuer")
    monkeypatch.setenv("JWT_AUDIENCE", "h8-test-audience")
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "h8-orchestrator-secret")
    monkeypatch.setenv("ORCHESTRATOR_SERVICE_URL", "http://orquestador.h8")
    monkeypatch.setenv("INSCRIPCION_SERVICE_URL", "http://inscripcion.h8")
    monkeypatch.setenv("GATEWAY_RATE_LIMIT", "1000")
    spec = importlib.util.spec_from_file_location("gateway_h8_test", ROOT / "api-gateway" / "app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    yield module
    module._requests.clear()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(gateway, *, subject="student-1", exp=None, secret=None):
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": subject,
        "iss": gateway.JWT_ISSUER,
        "aud": gateway.JWT_AUDIENCE,
        "exp": int(time.time()) + 300 if exp is None else exp,
    }).encode())
    body = f"{header}.{payload}"
    signature = hmac.new((secret or JWT_SECRET).encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def _respuesta(status=200, body=b'{"ok":true}'):
    response = Mock()
    response.status_code = status
    response.content = body
    response.headers = {"Content-Type": "application/json"}
    return response


def _transporte(monkeypatch, gateway, *, usuario_status=200):
    llamadas = []

    def request(method, url, *, headers, data, timeout, allow_redirects):
        llamadas.append((method, url, headers))
        if url.startswith("http://inscripcion.h8/"):
            return _respuesta(usuario_status, b'{"id":"student-1"}')
        assert url.startswith("http://orquestador.h8/")
        assert headers.get("X-API-Key") == "h8-orchestrator-secret"
        assert "Authorization" not in headers
        return _respuesta(201, b'{"id":"saga-h8","estado":"COMPLETADA"}')

    monkeypatch.setattr(gateway.requests, "request", request)
    return llamadas


def test_h8_jwt_vigente_se_verifica_en_inscripcion_antes_de_delegar(gateway, monkeypatch):
    llamadas = _transporte(monkeypatch, gateway)
    token = _jwt(gateway)

    response = gateway.app.test_client().post(
        "/v1/matriculas", headers={"Authorization": f"Bearer {token}"},
        json={"alumno_id": "student-1", "curso_id": 1, "monto": 100},
    )

    assert response.status_code == 201, response.data
    assert [item[1] for item in llamadas] == [
        "http://inscripcion.h8/v1/students/student-1",
        "http://orquestador.h8/v1/matriculas",
    ]


def test_h8_jwt_revocado_por_anonimizacion_no_llega_al_orquestador(gateway, monkeypatch):
    # GET /v1/students/{id} responde 401 después de que Inscripción revoca el JWT.
    llamadas = _transporte(monkeypatch, gateway, usuario_status=401)
    token = _jwt(gateway)

    response = gateway.app.test_client().post(
        "/v1/matriculas", headers={"Authorization": f"Bearer {token}"},
        json={"alumno_id": "student-1", "curso_id": 1, "monto": 100},
    )

    assert response.status_code == 401, response.data
    assert [item[1] for item in llamadas] == ["http://inscripcion.h8/v1/students/student-1"]


@pytest.mark.parametrize("token", ["vencido", "firma-invalida"])
def test_h8_jwt_vencido_o_firma_invalida_no_consulta_servicios(gateway, monkeypatch, token):
    llamadas = _transporte(monkeypatch, gateway)
    credencial = (
        _jwt(gateway, exp=1) if token == "vencido"
        else _jwt(gateway, secret="otra-clave-de-firma-no-valida")
    )

    response = gateway.app.test_client().post(
        "/v1/matriculas", headers={"Authorization": f"Bearer {credencial}"},
        json={"alumno_id": "student-1", "curso_id": 1, "monto": 100},
    )

    assert response.status_code == 401
    assert llamadas == []


def test_h8_jwt_vigente_no_puede_matricular_a_otro_estudiante(gateway, monkeypatch):
    llamadas = _transporte(monkeypatch, gateway)
    token = _jwt(gateway)

    response = gateway.app.test_client().post(
        "/v1/matriculas", headers={"Authorization": f"Bearer {token}"},
        json={"alumno_id": "student-2", "curso_id": 1, "monto": 100},
    )

    assert response.status_code == 403
    assert llamadas == []
