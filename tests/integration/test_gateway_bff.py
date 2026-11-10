"""Integration coverage for the HTTP gateway and the BFF composition boundary.

These tests exercise the real Flask applications while replacing only their downstream
services. That keeps routing, authentication, request forwarding, concurrent limits and
composition behavior deterministic and independent of the developer's Docker state.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("MATRICULAS_JWT_SECRET", "test-secret-that-is-at-least-32-bytes-long")
    monkeypatch.setenv("JWT_ISSUER", "gateway-test")
    monkeypatch.setenv("JWT_AUDIENCE", "gateway-test-api")
    monkeypatch.setenv("MATRICULAS_API_KEYS", "integration-key,second-key")
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "orchestrator-secret")
    monkeypatch.setenv("BFF_SERVICE_URL", "http://bff.test")
    monkeypatch.setenv("ORCHESTRATOR_SERVICE_URL", "http://orchestrator.test")
    monkeypatch.setenv("GATEWAY_RATE_LIMIT", "1000")
    module = _load_module("gateway_app_test", ROOT / "api-gateway" / "app.py")
    module._requests.clear()
    module.app.config.update(TESTING=True)
    yield module
    module._requests.clear()


def _jwt(gateway, *, subject="student-1", exp=None, issuer=None, audience=None, alg="HS256", secret=None):
    header = {"alg": alg, "typ": "JWT"}
    payload = {
        "sub": subject,
        "iss": issuer if issuer is not None else gateway.JWT_ISSUER,
        "aud": audience if audience is not None else gateway.JWT_AUDIENCE,
        "exp": int(time.time()) + 300 if exp is None else exp,
    }
    encoded = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = hmac.new((secret or gateway.JWT_SECRET).encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64(signature)}"


def _upstream(status=200, body=b'{"ok":true}', headers=None):
    response = Mock()
    response.status_code = status
    response.content = body
    response.headers = headers or {"Content-Type": "application/json"}
    return response


def test_gateway_health_is_public_and_does_not_consume_quota(gateway):
    gateway.RATE_LIMIT = 1
    client = gateway.app.test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/v1/bff/dashboard").status_code == 401


def test_gateway_bff_proxy_preserves_method_path_query_body_and_allowed_headers(gateway, monkeypatch):
    call = Mock(return_value=_upstream(201, b'{"created":true}', {"Content-Type": "application/json", "X-Upstream": "yes"}))
    monkeypatch.setattr(gateway.requests, "request", call)
    token = _jwt(gateway)
    response = gateway.app.test_client().post(
        "/v1/bff/private/report?term=math&limit=2",
        data=b'{"filter":"active"}',
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": "req-123",
            "X-Ignore-Me": "drop",
        },
    )
    assert response.status_code == 201
    assert response.data == b'{"created":true}'
    assert response.headers["X-Upstream"] == "yes"
    args = call.call_args.kwargs
    assert args["method"] == "POST"
    assert args["url"] == "http://bff.test/v1/bff/private/report?term=math&limit=2"
    assert args["data"] == b'{"filter":"active"}'
    assert args["headers"]["Authorization"] == f"Bearer {token}"
    assert args["headers"]["Idempotency-Key"] == "req-123"
    assert "X-Ignore-Me" not in args["headers"]
    assert args["allow_redirects"] is False


def test_gateway_forwards_domain_routes_and_blocks_internal_paths(gateway, monkeypatch):
    call = Mock(return_value=_upstream())
    monkeypatch.setattr(gateway.requests, "request", call)
    client = gateway.app.test_client()
    token = _jwt(gateway)
    response = client.get("/v1/students/student-1?include=profile", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert call.call_args.kwargs["url"] == "http://matriculas-api:8000/v1/students/student-1?include=profile"
    call.reset_mock()
    denied = client.get("/internal/admin", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 404
    call.assert_not_called()


def test_gateway_accepts_configured_api_key_and_forwards_it(gateway, monkeypatch):
    call = Mock(return_value=_upstream())
    monkeypatch.setattr(gateway.requests, "request", call)
    response = gateway.app.test_client().get("/v1/bff/dashboard", headers={"X-API-Key": "second-key"})
    assert response.status_code == 200
    forwarded = call.call_args.kwargs["headers"]
    assert next(value for name, value in forwarded.items() if name.lower() == "x-api-key") == "second-key"


@pytest.mark.parametrize("authorization", [
    "", "Basic abc", "Bearer", "Bearer malformed.token", "Bearer a.b.c",
])
def test_gateway_rejects_missing_or_malformed_credentials(gateway, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    response = gateway.app.test_client().get("/v1/bff/dashboard", headers=headers)
    assert response.status_code == 401
    assert response.json["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("claims", [
    {"exp": 1},
    {"iss": "wrong-issuer"},
    {"aud": "wrong-audience"},
    {"sub": ""},
])
def test_gateway_rejects_expired_or_invalid_jwt_claims(gateway, claims):
    normalized = {"iss": "issuer", "aud": "audience", "sub": "subject"}
    token = _jwt(gateway, **{normalized.get(name, name): value for name, value in claims.items()})
    response = gateway.app.test_client().get("/v1/bff/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_gateway_rejects_wrong_signature_algorithm_and_unknown_api_key(gateway):
    client = gateway.app.test_client()
    bad_alg = _jwt(gateway, alg="none")
    assert client.get("/v1/bff/dashboard", headers={"Authorization": f"Bearer {bad_alg}"}).status_code == 401
    wrong_sig = _jwt(gateway, secret="not-the-configured-secret")
    assert client.get("/v1/bff/dashboard", headers={"Authorization": f"Bearer {wrong_sig}"}).status_code == 401
    assert client.get("/v1/bff/dashboard", headers={"X-API-Key": "unknown"}).status_code == 401


def test_gateway_prefers_invalid_bearer_over_valid_api_key(gateway):
    response = gateway.app.test_client().get(
        "/v1/bff/dashboard",
        headers={"Authorization": "Bearer invalid", "X-API-Key": "integration-key"},
    )
    assert response.status_code == 401


def test_gateway_limit_is_per_identity_and_shared_across_routes(gateway, monkeypatch):
    gateway.RATE_LIMIT = 1
    monkeypatch.setattr(gateway.requests, "request", Mock(return_value=_upstream()))
    client = gateway.app.test_client()
    jwt_1 = _jwt(gateway, subject="user-1")
    jwt_2 = _jwt(gateway, subject="user-2")
    assert client.get("/v1/bff/dashboard", headers={"Authorization": f"Bearer {jwt_1}"}).status_code == 200
    limited = client.get("/v1/bff/another", headers={"Authorization": f"Bearer {jwt_1}"})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert client.get("/v1/bff/dashboard", headers={"Authorization": f"Bearer {jwt_2}"}).status_code == 200


def test_gateway_limit_is_atomic_under_concurrent_requests(gateway, monkeypatch):
    gateway.RATE_LIMIT = 5
    barrier = threading.Barrier(20)
    monkeypatch.setattr(gateway.requests, "request", Mock(return_value=_upstream()))
    token = _jwt(gateway, subject="same-user")

    def request_once(_):
        with gateway.app.test_client() as client:
            barrier.wait(timeout=5)
            return client.get("/v1/bff/dashboard", headers={"Authorization": f"Bearer {token}"}).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(pool.map(request_once, range(20)))
    assert statuses.count(200) == 5
    assert statuses.count(429) == 15


def test_gateway_orchestrator_proxy_injects_internal_key_and_checks_owner(gateway, monkeypatch):
    call = Mock(return_value=_upstream(201))
    monkeypatch.setattr(gateway.requests, "request", call)
    client = gateway.app.test_client()
    token = _jwt(gateway, subject="student-1")
    denied = client.post(
        "/v1/matriculas", json={"alumno_id": "student-2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 403
    call.assert_not_called()

    accepted = client.post(
        "/v1/matriculas?dry_run=false", json={"alumno_id": "student-1"},
        headers={"Authorization": f"Bearer {token}", "X-API-Key": "integration-key"},
    )
    assert accepted.status_code == 201
    args = call.call_args.kwargs
    assert args["url"] == "http://orchestrator.test/v1/matriculas?dry_run=false"
    assert args["headers"]["X-API-Key"] == "orchestrator-secret"
    assert "Authorization" not in args["headers"]


def test_gateway_hides_another_students_saga_and_allows_m2m_metrics(gateway, monkeypatch):
    call = Mock(return_value=_upstream(200, b'{"alumno_id":"student-2","estado":"CONFIRMADA"}'))
    monkeypatch.setattr(gateway.requests, "request", call)
    client = gateway.app.test_client()
    jwt = _jwt(gateway, subject="student-1")
    hidden = client.get("/v1/sagas/saga-2", headers={"Authorization": f"Bearer {jwt}"})
    assert hidden.status_code == 404
    assert hidden.json["error"]["code"] == "NOT_FOUND"

    metrics = client.get("/metrics", headers={"X-API-Key": "integration-key"})
    assert metrics.status_code == 200
    assert call.call_args.kwargs["url"] == "http://orchestrator.test/metrics"
    assert call.call_args.kwargs["headers"]["X-API-Key"] == "orchestrator-secret"


def test_gateway_returns_misconfigured_for_orchestrator_routes(gateway, monkeypatch):
    gateway.ORCHESTRATOR_API_KEY = ""
    response = gateway.app.test_client().get(
        "/v1/sagas/saga-1", headers={"X-API-Key": "integration-key"},
    )
    assert response.status_code == 503
    assert response.json["error"]["code"] == "GATEWAY_MISCONFIGURED"


def test_gateway_maps_upstream_timeout_and_unavailability(gateway, monkeypatch):
    client = gateway.app.test_client()
    monkeypatch.setattr(gateway.requests, "request", Mock(side_effect=gateway.requests.Timeout))
    timeout = client.get("/v1/bff/dashboard", headers={"X-API-Key": "integration-key"})
    assert timeout.status_code == 504 and timeout.json["error"]["code"] == "UPSTREAM_TIMEOUT"
    monkeypatch.setattr(gateway.requests, "request", Mock(side_effect=gateway.requests.ConnectionError))
    unavailable = client.get("/v1/bff/dashboard", headers={"X-API-Key": "second-key"})
    assert unavailable.status_code == 502 and unavailable.json["error"]["code"] == "UPSTREAM_UNAVAILABLE"


@pytest.fixture
def bff(monkeypatch):
    # The test runner has grpcio, while the generated production stubs are created in the image.
    # Tiny import stubs let us exercise the real BFF Flask app without generating source files.
    grpc_stubs = types.ModuleType("grpc_stubs")
    cupos_pb2 = types.ModuleType("grpc_stubs.cupos_pb2")
    cupos_pb2.ListarRequest = lambda: object()
    cupos_pb2_grpc = types.ModuleType("grpc_stubs.cupos_pb2_grpc")
    cupos_pb2_grpc.CuposStub = lambda channel: object()
    grpc_stubs.cupos_pb2 = cupos_pb2
    grpc_stubs.cupos_pb2_grpc = cupos_pb2_grpc
    monkeypatch.setitem(sys.modules, "grpc_stubs", grpc_stubs)
    monkeypatch.setitem(sys.modules, "grpc_stubs.cupos_pb2", cupos_pb2)
    monkeypatch.setitem(sys.modules, "grpc_stubs.cupos_pb2_grpc", cupos_pb2_grpc)
    monkeypatch.setenv("INSCRIPCION_SERVICE_URL", "http://inscripcion.test")
    module = _load_module("bff_app_test", ROOT / "bff" / "app.py")
    module.app.config.update(TESTING=True)
    yield module
    module._pool.shutdown(wait=True, cancel_futures=True)


def test_bff_dashboard_composes_parallel_course_and_enrollment_results(bff, monkeypatch):
    events = []
    lock = threading.Lock()

    def courses(*args, **kwargs):
        with lock:
            events.append(("courses", time.monotonic()))
        return {"items": [{"curso_id": 4, "nombre": "Redes", "cupos_totales": 20, "cupos_libres": 7}]}

    def enrollments(path, headers):
        with lock:
            events.append(("enrollments", time.monotonic()))
        assert path == "/v1/enrollments?limit=100"
        assert headers == {"Authorization": "Bearer user-token", "X-API-Key": "secondary"}
        return types.SimpleNamespace(ok=True, json=lambda: {"items": [{"id": "e1"}], "total": 1})

    monkeypatch.setattr(bff, "_cursos_disponibles", courses)
    monkeypatch.setattr(bff, "_get", enrollments)
    response = bff.app.test_client().get("/v1/bff/dashboard", headers={
        "Authorization": "Bearer user-token", "X-API-Key": "secondary", "X-Other": "not forwarded",
    })
    assert response.status_code == 200
    assert response.json == {
        "cursos": {"items": [{"curso_id": 4, "nombre": "Redes", "cupos_totales": 20, "cupos_libres": 7}]},
        "matriculas": {"items": [{"id": "e1"}], "total": 1},
    }
    assert len(events) == 2


def test_bff_starts_course_and_enrollment_dependencies_concurrently(bff, monkeypatch):
    rendezvous = threading.Barrier(2)
    monkeypatch.setattr(bff, "_cursos_disponibles", lambda: (rendezvous.wait(timeout=2), {"items": []})[1])
    monkeypatch.setattr(bff, "_get", lambda *args: (rendezvous.wait(timeout=2), types.SimpleNamespace(
        ok=True, json=lambda: {"items": [], "total": 0},
    ))[1])
    response = bff.app.test_client().get("/v1/bff/dashboard")
    assert response.status_code == 200


def test_bff_reports_enrollment_http_error_with_upstream_status(bff, monkeypatch):
    monkeypatch.setattr(bff, "_cursos_disponibles", lambda: {"items": []})
    monkeypatch.setattr(bff, "_get", lambda *args: types.SimpleNamespace(
        ok=False, status_code=403, json=lambda: {"detail": "forbidden"},
    ))
    response = bff.app.test_client().get("/v1/bff/dashboard")
    assert response.status_code == 403
    assert response.json["error"]["code"] == "BFF_UPSTREAM_ERROR"
    assert response.json["error"]["upstream_status"] == 403


@pytest.mark.parametrize("failure,status,code", [
    ("timeout", 504, "BFF_DEPENDENCY_TIMEOUT"),
    ("unavailable", 502, "INSCRIPCION_UNAVAILABLE"),
])
def test_bff_maps_enrollment_dependency_failures(bff, monkeypatch, failure, status, code):
    import requests

    monkeypatch.setattr(bff, "_cursos_disponibles", lambda: {"items": []})
    error = requests.Timeout if failure == "timeout" else requests.ConnectionError
    monkeypatch.setattr(bff, "_get", Mock(side_effect=error))
    response = bff.app.test_client().get("/v1/bff/dashboard")
    assert response.status_code == status
    assert response.json["error"]["code"] == code


def test_bff_returns_gateway_timeout_when_dependency_exceeds_budget(bff, monkeypatch):
    release = threading.Event()
    bff.BFF_TIMEOUT = 0.01
    monkeypatch.setattr(bff, "_cursos_disponibles", lambda: {"items": []})

    def slow_enrollments(*args):
        release.wait(timeout=2)
        return types.SimpleNamespace(ok=True, json=lambda: {"items": [], "total": 0})

    monkeypatch.setattr(bff, "_get", slow_enrollments)
    try:
        response = bff.app.test_client().get("/v1/bff/dashboard")
    finally:
        release.set()
    assert response.status_code == 504
    assert response.json["error"]["code"] == "BFF_DEPENDENCY_TIMEOUT"


def test_bff_maps_grpc_deadline_and_unavailable(bff, monkeypatch):
    import grpc

    class RpcFailure(grpc.RpcError):
        def __init__(self, status):
            self.status = status

        def code(self):
            return self.status

    monkeypatch.setattr(bff, "_cursos_disponibles", Mock(side_effect=RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)))
    monkeypatch.setattr(bff, "_get", lambda *args: None)
    deadline = bff.app.test_client().get("/v1/bff/dashboard")
    assert deadline.status_code == 504
    assert deadline.json["error"]["code"] == "BFF_DEPENDENCY_TIMEOUT"

    monkeypatch.setattr(bff, "_cursos_disponibles", Mock(side_effect=RpcFailure(grpc.StatusCode.UNAVAILABLE)))
    unavailable = bff.app.test_client().get("/v1/bff/dashboard")
    assert unavailable.status_code == 503
    assert unavailable.json["error"]["code"] == "CUPOS_UNAVAILABLE"


def test_bff_health_is_public_and_dashboard_rejects_wrong_method(bff):
    client = bff.app.test_client()
    assert client.get("/health").status_code == 200
    assert client.post("/v1/bff/dashboard").status_code == 405

