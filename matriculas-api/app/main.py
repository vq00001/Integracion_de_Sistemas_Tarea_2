from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi

from .config import settings, validar_claves_admin, validar_secreto_jwt
from .database import init_db
from .errors import register_exception_handlers
from .routers import courses, enrollments, health, students

DESCRIPTION = """Servicio de Inscripción. Gestiona alumnos e inscripciones locales.
La reserva y el cobro pertenecen al orquestador. CONFIRMADA equivale a inscripción ACTIVA.
La misma Idempotency-Key devuelve la inscripción existente; repetir la baja devuelve 200.
"""

@asynccontextmanager
async def lifespan(_: FastAPI):
    validar_claves_admin(settings.admin_api_keys)
    validar_secreto_jwt(settings.jwt_secret)
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=DESCRIPTION,
    contact={"name": "AprendeMás - Equipo de Integración de Sistemas"},
    lifespan=lifespan,
    # En producción DOCS_ENABLED=false: no se publica el contrato ni Swagger sin autenticación.
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

register_exception_handlers(app)


@app.middleware("http")
async def cabeceras_de_deprecacion(request: Request, call_next):
    """Política de deprecación hecha código (D3): las rutas listadas en DEPRECATED_ROUTES
    responden con `Deprecation`, `Sunset` y `Link` para que los consumidores se enteren."""
    response = await call_next(request)
    try:
        rutas = json.loads(settings.deprecated_routes or "{}")
    except ValueError:
        logging.getLogger("matriculas").error("DEPRECATED_ROUTES no es JSON válido")
        return response
    for prefijo, meta in rutas.items():
        if request.url.path.startswith(prefijo):
            response.headers["Deprecation"] = meta.get("deprecation", "true")
            if meta.get("sunset"):
                response.headers["Sunset"] = meta["sunset"]
            if meta.get("link"):
                response.headers["Link"] = f'<{meta["link"]}>; rel="deprecation"'
            break
    return response


from .routers import auth  # noqa: E402

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(students.router)
app.include_router(courses.router)
app.include_router(enrollments.router)
from .faults import router as internal_router
app.include_router(internal_router)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http", "scheme": "bearer", "bearerFormat": "JWT"
    }
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            if operation.get("tags") in (["Salud"],):
                continue
            if operation.get("tags") == ["Autenticación"]:
                continue
            operation["security"] = [{"ApiKeyAuth": []}, {"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
