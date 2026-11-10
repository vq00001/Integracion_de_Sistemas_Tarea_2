"""Autenticación local por API key y OAuth2/JWT, con autorización por propietario.

Las integraciones pueden seguir usando X-API-Key. Los usuarios inician sesión localmente
en /v1/auth/token y usan un JWT Bearer firmado con HS256. Ambos mecanismos producen el
mismo Principal, así las rutas conservan una única implementación de autorización. Las
contraseñas se almacenan con PBKDF2-SHA256; no se requieren servicios externos.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette import status
from starlette.exceptions import HTTPException

from . import models
from .config import settings
from .database import get_db
from .errors import TooManyRequestsError
from .ratelimit import LimitadorVentana
from .time import utc_now

log = logging.getLogger("matriculas.security")

# Prefijo legible: permite reconocer una clave de estudiante (y detectar fugas con
# escáneres de secretos). Formato: mat_<id público de 8 hex>.<secreto>. El id público es
# ALEATORIO e independiente del secreto: mostrarlo en listados/logs no revela ni un carácter
# de él (antes se mostraban los primeros 4 caracteres del propio secreto).
STUDENT_KEY_PREFIX = "mat_"

_limitador_fallos = LimitadorVentana(settings.auth_fail_limit, settings.auth_fail_window_seconds)
_limitador_escrituras = LimitadorVentana(settings.write_rate_limit_per_minute, 60.0)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token", auto_error=False)
JWT_ISSUER = settings.jwt_issuer
JWT_AUDIENCE = settings.jwt_audience
_PASSWORD_ITERATIONS = 310_000


@dataclass(frozen=True)
class Principal:
    """Quién hace la solicitud, ya autenticado."""

    es_admin: bool
    student_id: str | None = None  # identidad de propietario para clave personal o JWT


# ---------------------------------------------------------------------------
# Claves de estudiante
# ---------------------------------------------------------------------------

def generar_api_key() -> tuple[str, str]:
    """Devuelve (clave_completa, prefijo_publico). Secreto aleatorio de 256 bits."""
    prefijo = f"{STUDENT_KEY_PREFIX}{secrets.token_hex(4)}"
    return f"{prefijo}.{secrets.token_urlsafe(32)}", prefijo


def hash_api_key(clave: str) -> str:
    return hashlib.sha256(clave.encode()).hexdigest()


def hash_password(password: str) -> str:
    """Hash de contraseña con PBKDF2-SHA256 y sal aleatoria; formato versionado."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verificar_password(password: str, password_hash: str) -> bool:
    """Compara una contraseña con el hash almacenado en tiempo constante."""
    try:
        algoritmo, iteraciones, salt_b64, digest_b64 = password_hash.split("$", 3)
        if algoritmo != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        esperado = base64.urlsafe_b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iteraciones))
        return hmac.compare_digest(actual, esperado)
    except (ValueError, TypeError):
        return False


def crear_access_token(student_id: str) -> str:
    """Crea un JWT firmado localmente (HS256), sin depender de un proveedor externo."""
    expira = utc_now() + timedelta(minutes=settings.jwt_ttl_minutes)
    cabecera = {"alg": "HS256", "typ": "JWT"}
    claims = {"sub": student_id, "iss": JWT_ISSUER, "aud": JWT_AUDIENCE,
              "iat": int(utc_now().timestamp()), "exp": int(expira.timestamp())}
    partes = [_b64url(json.dumps(parte, separators=(",", ":")).encode())
              for parte in (cabecera, claims)]
    firma = hmac.new(settings.jwt_secret.encode(), ".".join(partes).encode(), hashlib.sha256).digest()
    return f"{partes[0]}.{partes[1]}.{_b64url(firma)}"


def _b64url(valor: bytes) -> str:
    return base64.urlsafe_b64encode(valor).rstrip(b"=").decode()


def verificar_access_token(token: str) -> str | None:
    """Valida firma, algoritmo, emisor, audiencia y expiración; retorna el subject."""
    try:
        encabezado, carga, firma = token.split(".")
        firma_esperada = hmac.new(settings.jwt_secret.encode(), f"{encabezado}.{carga}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(firma_esperada, _b64url_decode(firma)):
            return None
        header = json.loads(_b64url_decode(encabezado))
        claims = json.loads(_b64url_decode(carga))
        if (header.get("alg") != "HS256" or claims.get("iss") != JWT_ISSUER
                or claims.get("aud") != JWT_AUDIENCE
                or int(claims.get("exp", 0)) <= int(utc_now().timestamp())):
            return None
        sub = claims.get("sub")
        return sub if isinstance(sub, str) and sub else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _b64url_decode(valor: str) -> bytes:
    return base64.urlsafe_b64decode(valor + "=" * (-len(valor) % 4))


# ---------------------------------------------------------------------------
# Verificación
# ---------------------------------------------------------------------------

def _es_admin(candidata: str) -> str | None:
    """Devuelve la clave admin que coincide (o None). Compara contra todas en
    tiempo constante para no filtrar información por diferencias de tiempo."""
    coincidencia = None
    for valida in settings.admin_api_keys:
        if hmac.compare_digest(candidata.encode(), valida.encode()):
            coincidencia = valida
    return coincidencia


def _no_autenticado(detalle: str, esquema: str = "ApiKey") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detalle,
        headers={"WWW-Authenticate": esquema},
    )


def _ip(request: Request) -> str:
    return request.client.host if request.client else "desconocido"


def _fallo_de_autenticacion(request: Request, detalle: str, esquema: str = "ApiKey") -> HTTPException:
    _limitador_fallos.registrar(_ip(request))
    return _no_autenticado(detalle, esquema)


def fallo_de_autenticacion(request: Request, detalle: str) -> HTTPException:
    """Registra también los fallos del endpoint de inicio de sesión local."""
    return _fallo_de_autenticacion(request, detalle, "Bearer")


def comprobar_limite_fallos(request: Request) -> None:
    """Aplica el límite común por IP antes de procesar credenciales."""
    espera = _limitador_fallos.excedido(_ip(request))
    if espera is not None:
        raise TooManyRequestsError(
            "Demasiados intentos de autenticación fallidos. Espera antes de reintentar.",
            retry_after=espera,
        )


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    bearer_token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Principal:
    """Autentica con API key heredada o JWT. Mantiene la interfaz usada por las rutas."""
    comprobar_limite_fallos(request)
    if x_api_key:
        if _es_admin(x_api_key) is not None:
            return Principal(es_admin=True)
        fila = db.execute(
            select(models.ApiKey.student_id, models.ApiKey.expires_at).where(
                models.ApiKey.key_hash == hash_api_key(x_api_key),
                models.ApiKey.revoked_at.is_(None),
            )
        ).first()
        if fila is None:
            raise _fallo_de_autenticacion(request, "API key inválida")
        student_id, expires_at = fila
        if expires_at is not None and expires_at <= utc_now():
            raise _fallo_de_autenticacion(request, "API key vencida: solicita una nueva al administrador")
        return Principal(es_admin=False, student_id=student_id)

    if bearer_token:
        student_id = verificar_access_token(bearer_token)
        vigente = student_id and db.execute(
            select(models.StudentCredential.student_id)
            .join(models.Student, models.Student.id == models.StudentCredential.student_id)
            .where(models.StudentCredential.student_id == student_id,
                   models.Student.anonimizado_en.is_(None))
        ).scalar_one_or_none()
        if vigente:
            return Principal(es_admin=False, student_id=student_id)
        raise _fallo_de_autenticacion(request, "Bearer token inválido, vencido o revocado", "Bearer")
    raise _fallo_de_autenticacion(request, "Se requiere X-API-Key o Authorization: Bearer")


def limitar_escrituras(principal: Principal) -> None:
    """429 si una identidad de estudiante supera sus escrituras por minuto."""
    if principal.es_admin:
        return
    espera = _limitador_escrituras.consumir(principal.student_id or "")
    if espera is not None:
        raise TooManyRequestsError(
            "Demasiadas operaciones en poco tiempo. Espera antes de reintentar.", retry_after=espera
        )


# ---------------------------------------------------------------------------
# Autorización
# ---------------------------------------------------------------------------

def _prohibido(detalle: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detalle)


def require_admin(principal: Principal = Depends(require_api_key)) -> Principal:
    if not principal.es_admin:
        raise _prohibido("Esta operación requiere credenciales de administrador")
    return principal


def exigir_acceso_a_estudiante(principal: Principal, student_id: str) -> None:
    """Un admin accede a cualquiera; un estudiante solo a sí mismo (403 si no)."""
    if not principal.es_admin and principal.student_id != student_id:
        raise _prohibido("Solo puedes acceder a tus propios datos")
