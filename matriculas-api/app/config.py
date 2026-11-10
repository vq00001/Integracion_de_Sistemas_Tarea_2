"""
Configuración centralizada de la API de Matrículas.

Todos los valores se leen de variables de entorno (con defaults sensatos
para desarrollo local) siguiendo la práctica de 12-factor app. Ver
`.env.example` para la lista completa de variables soportadas.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Valores publicados en versiones anteriores del repositorio. La API se NIEGA a arrancar si
# la clave admin es uno de ellos (o es demasiado corta): una clave conocida por cualquiera que
# lea el repositorio equivale a no tener clave.
CLAVES_ADMIN_PROHIBIDAS = frozenset({"dev-matriculas-key-123", "changeme", "admin", "password"})
LARGO_MINIMO_CLAVE_ADMIN = 24


def validar_claves_admin(claves: set[str]) -> None:
    """Falla el arranque si no hay clave admin o si es débil/pública (fail-closed)."""
    if not claves:
        raise RuntimeError(
            "API_KEYS no está configurada: la API no arranca sin clave de administrador. "
            'Genera una con: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    for clave in claves:
        if clave in CLAVES_ADMIN_PROHIBIDAS or len(clave) < LARGO_MINIMO_CLAVE_ADMIN:
            raise RuntimeError(
                "API_KEYS contiene una clave pública o demasiado corta (mínimo "
                f"{LARGO_MINIMO_CLAVE_ADMIN} caracteres, no puede ser un valor de ejemplo). "
                'Genera una con: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )


def validar_secreto_jwt(secreto: str) -> None:
    """Evita iniciar con un secreto JWT ausente o fácil de adivinar."""
    if len(secreto) < 32:
        raise RuntimeError("JWT_SECRET debe configurarse con al menos 32 caracteres aleatorios.")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Base de datos propia de Matrículas (principio: cada servicio
    # accede solo a su propia base de datos) ---
    database_url: str = "sqlite:///./matriculas.db"

    # --- gRPC: cliente hacia el servicio Cupos ---
    cupos_grpc_target: str = "localhost:50051"
    cupos_grpc_timeout_seconds: float = 2.0

    # Circuit breaker hacia Cupos
    cupos_cb_failure_threshold: int = 5       # fallas consecutivas para abrir el circuito
    cupos_cb_recovery_seconds: float = 15.0   # tiempo antes de probar de nuevo (half-open)

    # --- Autenticación local: API Key para integración, JWT para usuarios ---
    # Credenciales y permisos están descritos en security.py y en el README.
    #  - API key ADMIN: la(s) de esta variable (separadas por coma). Acceso total
    #    (secretaría / integraciones internas). NO tiene valor por defecto: si falta o es una
    #    clave pública/corta, la API no arranca (ver `validar_claves_admin`).
    #  - API key de ESTUDIANTE: se emiten a demanda con
    #    POST /v1/students/{id}/api-key (solo admin), se guardan hasheadas en la
    #    base y solo permiten operar sobre los datos de ese estudiante.
    api_keys: str = ""

    # Secreto local para firmar JWT (HS256). Se genera junto a API_KEYS en .env.
    jwt_secret: str = ""
    jwt_issuer: str = "matriculas-api-local"
    jwt_audience: str = "matriculas-api"
    jwt_ttl_minutes: int = Field(default=60, ge=1, le=1440)

    # Vigencia de las claves de estudiante, en días (0 = no caducan). Rotación: al vencer, el
    # administrador emite otra (la vencida se revoca sola al emitir).
    student_key_ttl_days: int = 90

    # --- Límite de intentos (en memoria, por proceso y por IP del cliente) ---
    # Intentos de autenticación FALLIDOS tolerados por ventana; al superarlos, 429 con Retry-After.
    auth_fail_limit: int = 20
    auth_fail_window_seconds: float = 60.0
    # Escrituras (matricular / revertir) por minuto y por estudiante con clave propia
    # (0 = sin límite). La clave admin no se limita: es de confianza (integraciones internas).
    write_rate_limit_per_minute: int = 60

    # --- Lectura de disponibilidad de cursos (caché en memoria, TTL corto) ---
    cursos_cache_ttl_seconds: float = 5.0

    # --- Documentación interactiva (/docs, /redoc) y /openapi.json ---
    # Poner en false en producción para no publicar el contrato sin autenticación.
    docs_enabled: bool = True

    # --- Política de deprecación: {"/v1/ruta": {"deprecation": "...", "sunset": "...", "link": "..."}} ---
    # JSON. Las rutas listadas responden con las cabeceras Deprecation / Sunset / Link (RFC 9745/8594).
    deprecated_routes: str = "{}"
    # --- Metadata de la app ---
    app_name: str = "Matrículas API"
    app_version: str = "2.0.0"

    @property
    def admin_api_keys(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


settings = Settings()
