"""H15: las API keys personales deben conservar la autoridad de Inscripción."""

import importlib.util
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
API_KEY_ADMIN = "h15-administrative-integration-key"


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("MATRICULAS_JWT_SECRET", "h15-test-secret-at-least-32-bytes-long")
    monkeypatch.setenv("JWT_ISSUER", "h15-test-issuer")
    monkeypatch.setenv("JWT_AUDIENCE", "h15-test-audience")
    monkeypatch.setenv("MATRICULAS_API_KEYS", API_KEY_ADMIN)
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "h15-orchestrator-secret")
    monkeypatch.setenv("INSCRIPCION_SERVICE_URL", "http://inscripcion.h15")
    monkeypatch.setenv("BFF_SERVICE_URL", "http://bff.h15")
    monkeypatch.setenv("GATEWAY_RATE_LIMIT", "1000")
    spec = importlib.util.spec_from_file_location("gateway_h15_test", ROOT / "api-gateway" / "app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    yield module
    module._requests.clear()


def _key(prefix: str) -> str:
    return f"mat_{prefix}.{'A' * 43}"


def _response(status: int, body: bytes):
    response = requests.Response()
    response.status_code = status
    response._content = body
    response.headers["Content-Type"] = "application/json"
    return response


@pytest.fixture
def inscripcion(monkeypatch, gateway):
    now = time.time()
    key_owner_a = _key("1111aaaa")
    key_owner_b = _key("2222bbbb")
    key_expired = _key("3333cccc")
    key_revoked = _key("4444dddd")
    keys = {
        key_owner_a: {"owner": "student-1", "expires": now + 300, "revoked": False},
        key_owner_b: {"owner": "student-2", "expires": now + 300, "revoked": False},
        key_expired: {"owner": "student-1", "expires": now - 1, "revoked": False},
        key_revoked: {"owner": "student-1", "expires": now + 300, "revoked": True},
    }
    calls = []

    def request(method, url, *, headers, data, timeout, allow_redirects):
        parsed = urlsplit(url)
        calls.append((method, url, dict(headers)))
        api_key = requests.structures.CaseInsensitiveDict(headers).get("X-API-Key", "")
        if api_key == API_KEY_ADMIN:
            return _response(200, b'{"id":"admin"}')
        credential = keys.get(api_key)
        if credential is None or credential["revoked"] or credential["expires"] <= time.time():
            return _response(401, b'{"error":{"code":"UNAUTHENTICATED"}}')
        if parsed.path.startswith("/v1/students/"):
            requested_owner = parsed.path.rsplit("/", 1)[-1]
            if requested_owner != credential["owner"]:
                return _response(403, b'{"error":{"code":"FORBIDDEN"}}')
            return _response(200, f'{{"id":"{requested_owner}"}}'.encode())
        if parsed.path == "/v1/enrollments":
            requested_owner = parse_qs(parsed.query).get("student_id", [credential["owner"]])[0]
            if requested_owner != credential["owner"]:
                return _response(403, b'{"error":{"code":"FORBIDDEN"}}')
            return _response(200, f'{{"items":[{{"student_id":"{requested_owner}"}}]}}'.encode())
        return _response(404, b'{"error":{"code":"NOT_FOUND"}}')

    monkeypatch.setattr(gateway.requests, "request", request)
    return type("InscripcionStub", (), {
        "key_owner_a": key_owner_a, "key_owner_b": key_owner_b,
        "key_expired": key_expired, "key_revoked": key_revoked, "calls": calls,
    })


def test_h15_api_key_personal_vigente_funciona_a_traves_del_gateway(gateway, inscripcion):
    client = gateway.app.test_client()
    response = client.get(
        "/v1/students/student-1", headers={"X-API-Key": inscripcion.key_owner_a},
    )

    assert response.status_code == 200, response.data
    assert response.json["id"] == "student-1"
    assert requests.structures.CaseInsensitiveDict(inscripcion.calls[-1][2])["X-API-Key"] == inscripcion.key_owner_a


def test_h15_api_key_personal_conserva_el_alcance_por_propietario(gateway, inscripcion):
    client = gateway.app.test_client()
    own_profile = client.get(
        "/v1/students/student-1", headers={"X-API-Key": inscripcion.key_owner_a},
    )
    foreign_profile = client.get(
        "/v1/students/student-2", headers={"X-API-Key": inscripcion.key_owner_a},
    )
    own_enrollments = client.get(
        "/v1/enrollments?student_id=student-1", headers={"X-API-Key": inscripcion.key_owner_a},
    )
    foreign_enrollments = client.get(
        "/v1/enrollments?student_id=student-2", headers={"X-API-Key": inscripcion.key_owner_a},
    )

    assert own_profile.status_code == 200
    assert foreign_profile.status_code == 403
    assert own_enrollments.status_code == 200
    assert foreign_enrollments.status_code == 403


@pytest.mark.parametrize("attribute", ["key_expired", "key_revoked"])
def test_h15_clave_personal_vencida_o_revocada_sigue_rechazada(gateway, inscripcion, attribute):
    response = gateway.app.test_client().get(
        "/v1/students/student-1", headers={"X-API-Key": getattr(inscripcion, attribute)},
    )

    assert response.status_code == 401
    assert len(inscripcion.calls) == 1
    assert requests.structures.CaseInsensitiveDict(inscripcion.calls[0][2])["X-API-Key"] == getattr(inscripcion, attribute)


def test_h15_clave_personal_invalida_no_se_convierte_en_clave_admin(gateway, inscripcion):
    invalid_key = _key("9999ffff")
    response = gateway.app.test_client().get(
        "/v1/students/student-1", headers={"X-API-Key": invalid_key},
    )

    assert response.status_code == 401
    assert len(inscripcion.calls) == 1
    assert requests.structures.CaseInsensitiveDict(inscripcion.calls[0][2])["X-API-Key"] == invalid_key


def test_h15_clave_administrativa_configurada_sigue_funcionando(gateway, inscripcion):
    assert gateway.API_KEYS == (API_KEY_ADMIN,)
    response = gateway.app.test_client().get(
        "/v1/students/student-1", headers={"X-API-Key": API_KEY_ADMIN},
    )

    assert response.status_code == 200, response.data
    assert requests.structures.CaseInsensitiveDict(inscripcion.calls[-1][2])["X-API-Key"] == API_KEY_ADMIN


def test_h15_clave_personal_no_habilita_delegaciones_internas(gateway, inscripcion):
    client = gateway.app.test_client()
    saga = client.get("/v1/sagas/saga-1", headers={"X-API-Key": inscripcion.key_owner_a})
    enrollment = client.post(
        "/v1/matriculas", json={"alumno_id": "student-1"},
        headers={"X-API-Key": inscripcion.key_owner_a},
    )

    assert saga.status_code == 403
    assert enrollment.status_code == 403
    assert inscripcion.calls == []
