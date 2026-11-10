"""Punto de entrada HTTP: autenticación, rate limit y proxy a servicios internos."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque

import requests
from flask import Flask, g, jsonify, request

app = Flask(__name__)

INSCRIPCION_URL = os.getenv(
    "INSCRIPCION_SERVICE_URL", "http://matriculas-api:8000"
).rstrip("/")
ORQUESTADOR_URL = os.getenv(
    "ORCHESTRATOR_SERVICE_URL", "http://orquestador:8000"
).rstrip("/")
BFF_URL = os.getenv("BFF_SERVICE_URL", "http://bff:8090").rstrip("/")
JWT_SECRET = os.getenv("MATRICULAS_JWT_SECRET", "")
JWT_ISSUER = os.getenv("JWT_ISSUER", "matriculas-api-local")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "matriculas-api")
API_KEYS = tuple(
    k.strip() for k in os.getenv("MATRICULAS_API_KEYS", "").split(",") if k.strip()
)
ORCHESTRATOR_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")
RATE_LIMIT = max(1, int(os.getenv("GATEWAY_RATE_LIMIT", "120")))
RATE_WINDOW = max(1, int(os.getenv("GATEWAY_RATE_WINDOW_SECONDS", "60")))
PROXY_TIMEOUT = max(0.1, float(os.getenv("GATEWAY_PROXY_TIMEOUT_SECONDS", "30")))

_requests: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _jwt_subject(token: str) -> str | None:
    """Valida el JWS HS256 emitido por matriculas-api, sin aceptar algoritmos alternativos."""
    if len(JWT_SECRET) < 32:
        return None
    try:
        header_part, payload_part, signature_part = token.split(".")
        expected = hmac.new(
            JWT_SECRET.encode(),
            f"{header_part}.{payload_part}".encode(),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature_part)):
            return None
        header = json.loads(_b64url_decode(header_part))
        claims = json.loads(_b64url_decode(payload_part))
        if (
            header.get("alg") != "HS256"
            or claims.get("iss") != JWT_ISSUER
            or claims.get("aud") != JWT_AUDIENCE
            or int(claims.get("exp", 0)) <= time.time()
        ):
            return None
        subject = claims.get("sub")
        return subject if isinstance(subject, str) and subject else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _principal() -> tuple[str | None, str | None]:
    """Devuelve identidad para rate-limit y error; la credencial original sigue al servicio."""
    authorization = request.headers.get("Authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() == "bearer" and credential:
        subject = _jwt_subject(credential.strip())
        if subject:
            g.jwt_subject = subject
            g.is_m2m = False
            return f"user:{subject}", None
        return None, "Bearer token inválido o vencido"

    api_key = request.headers.get("X-API-Key", "")
    if api_key and any(hmac.compare_digest(api_key, known) for known in API_KEYS):
        g.jwt_subject = None
        g.is_m2m = True
        return f"api-key:{hashlib.sha256(api_key.encode()).hexdigest()[:16]}", None
    return None, "Se requiere un Bearer JWT válido o una API key de integración"


def _permitir_peticion(identity: str) -> tuple[bool, int]:
    """Realizar la validación de forma sincrónica para evitar errores de concurrencia."""
    now = time.monotonic()
    with _rate_lock:
        history = _requests[identity]
        while history and history[0] <= now - RATE_WINDOW:
            history.popleft()
        if len(history) >= RATE_LIMIT:
            return False, max(1, int(RATE_WINDOW - (now - history[0])))
        history.append(now)
    return True, 0


def _proxy(target: str, *, injected_api_key: str | None = None):
    """Convertir petición realizada desde el Frontend a una aceptada por el Orquestador.
    Manda una petición HTTP a los servicios.
    """
    url = f"{target}{request.path}"
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower()
        in {"authorization", "x-api-key", "idempotency-key", "content-type", "accept"}
    }
    if injected_api_key is not None:
        headers.pop("Authorization", None)
        headers["X-API-Key"] = injected_api_key
    try:
        upstream = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=request.get_data(),
            timeout=PROXY_TIMEOUT,
            allow_redirects=False,
        )
    except requests.Timeout:
        return jsonify(
            error={
                "code": "UPSTREAM_TIMEOUT",
                "message": "El servicio interno excedió el tiempo límite",
            }
        ), 504

    except requests.RequestException:
        return jsonify(
            error={
                "code": "UPSTREAM_UNAVAILABLE",
                "message": "El servicio interno no está disponible",
            }
        ), 502

    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    response = app.response_class(upstream.content, status=upstream.status_code)

    for key, value in upstream.headers.items():
        if key.lower() not in excluded:
            response.headers[key] = value
    return response


@app.get("/health")
def health():
    return jsonify(status="ok", service="api-gateway")


@app.before_request
def authenticate_and_limit():
    if request.path == "/health":
        return None

    # La emisión OAuth2 requiere acceso anónimo; el rate limit por IP también la protege.
    if request.path == "/v1/auth/token" and request.method == "POST":
        identity = f"auth-ip:{request.remote_addr or 'unknown'}"
    else:
        identity, error = _principal()
        if error:
            return jsonify(error={"code": "UNAUTHENTICATED", "message": error}), 401

    allowed, retry_after = _permitir_peticion(identity)
    if not allowed:
        response = jsonify(
            error={"code": "RATE_LIMITED", "message": "Límite de solicitudes excedido"}
        )
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response


@app.route("/v1/bff/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def bff_proxy(path: str):
    return _proxy(BFF_URL)


@app.route(
    "/v1/bff/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
def bff_root(path: str):
    return _proxy(BFF_URL)


@app.route("/v1/matriculas", methods=["POST"])
@app.route("/v1/sagas/<path:path>", methods=["GET"])
@app.route("/metrics", methods=["GET"])
def orchestrator_proxy(path: str = ""):
    if not ORCHESTRATOR_API_KEY:
        return jsonify(
            error={
                "code": "GATEWAY_MISCONFIGURED",
                "message": "Falta configurar la clave del orquestador",
            }
        ), 503

    if request.path == "/metrics" and not getattr(g, "is_m2m", False):
        return jsonify(
            error={"code": "FORBIDDEN", "message": "La métrica requiere credencial M2M"}
        ), 403

    if request.method == "POST" and not getattr(g, "is_m2m", False):
        payload = request.get_json(silent=True) or {}
        if payload.get("alumno_id") != getattr(g, "jwt_subject", None):
            return jsonify(
                error={
                    "code": "FORBIDDEN",
                    "message": "Solo puedes iniciar tu propia matrícula",
                }
            ), 403

    upstream = _proxy(ORQUESTADOR_URL, injected_api_key=ORCHESTRATOR_API_KEY)

    if (
        request.method == "GET"
        and request.path.startswith("/v1/sagas/")
        and not getattr(g, "is_m2m", False)
    ):
        saga = upstream.get_json(silent=True) if upstream.status_code < 400 else None
        if saga and saga.get("alumno_id") != getattr(g, "jwt_subject", None):
            return jsonify(
                error={"code": "NOT_FOUND", "message": "Saga inexistente"}
            ), 404

    return upstream


# Maneja el resto de rutas no encontradas en los anteriores endpoints.
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def matriculas_proxy(path: str):
    # /internal es solo para llamadas de la red de servicios y nunca pasa por el gateway.
    if request.path.startswith("/internal"):
        return jsonify(
            error={"code": "NOT_FOUND", "message": "Ruta no encontrada"}
        ), 404
    return _proxy(INSCRIPCION_URL)


if __name__ == "__main__":
    if len(JWT_SECRET) < 32 or not API_KEYS or len(ORCHESTRATOR_API_KEY) < 24:
        raise SystemExit(
            "Configura MATRICULAS_JWT_SECRET, MATRICULAS_API_KEYS y ORCHESTRATOR_API_KEY en .env"
        )
    app.run(host="0.0.0.0", port=8080)
