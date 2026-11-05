"""Consulta de cursos y disponibilidad (lectura).

Cierra un hueco del diseño: el enunciado subraya que Matrículas consulta disponibilidad "con
muchísima frecuencia", pero la API no exponía ninguna ruta de lectura (los RPC obtenerCupo /
listarCursos / listarCursosDisponibles solo se usaban en pruebas) y un portal no podía descubrir
qué `curso_id` existen. Ahora:

  GET /v1/courses                 → listarCursos / listarCursosDisponibles (?disponibles=true)
  GET /v1/courses/{curso_id}      → el curso (404 si no existe)

Ambas se sirven de un caché en memoria con TTL corto (ver cursos_cache.py). El número de cupos
es REFERENCIAL: Cupos decide la reserva dentro de la saga.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from .. import schemas
from ..config import settings
from ..cursos_cache import cache_cursos
from ..errors import CursoNoEncontradoError
from ..grpc_client import cupos_client
from ..security import require_api_key

router = APIRouter(
    prefix="/v1/courses",
    tags=["Cursos"],
    dependencies=[Depends(require_api_key)],
)

_ERRORES = {
    401: {"model": schemas.ErrorResponse},
    503: {"model": schemas.ErrorResponse, "description": "Cupos no disponible (caído o circuito abierto)"},
    504: {"model": schemas.ErrorResponse, "description": "Cupos no respondió a tiempo"},
    502: {"model": schemas.ErrorResponse, "description": "Cupos devolvió un error interno"},
}


def _cargar() -> list[dict]:
    return cupos_client.listar_cursos()


def _cabeceras(response: Response, desde_cache: bool) -> None:
    response.headers["Cache-Control"] = f"private, max-age={int(settings.cursos_cache_ttl_seconds)}"
    response.headers["X-Cache"] = "HIT" if desde_cache else "MISS"


@router.get(
    "",
    response_model=schemas.CourseList,
    summary="Listar cursos con su disponibilidad (referencial)",
    description=(
        "Cursos de Cupos con `cupos_libres`. Con `disponibles=true` solo los que tienen cupos. "
        "El dato puede tener hasta `CURSOS_CACHE_TTL_SECONDS` segundos de antigüedad: para "
        "matricular con reserva, usa `POST /v1/matriculas` en el gateway."
    ),
    responses=_ERRORES,
)
def listar_cursos(response: Response, disponibles: bool = Query(False)):
    cursos, hit = cache_cursos.obtener(_cargar)
    _cabeceras(response, hit)
    items = [c for c in cursos if c["cupos_libres"] > 0] if disponibles else cursos
    return schemas.CourseList(items=items)


@router.get(
    "/{curso_id}",
    response_model=schemas.CourseOut,
    summary="Consultar un curso y su disponibilidad (referencial)",
    responses={**_ERRORES, 404: {"model": schemas.ErrorResponse, "description": "El curso no existe"}},
)
def obtener_curso(curso_id: int, response: Response):
    cursos, hit = cache_cursos.obtener(_cargar)
    _cabeceras(response, hit)
    for c in cursos:
        if c["curso_id"] == curso_id:
            return c
    raise CursoNoEncontradoError(f"El curso {curso_id} no existe en Cupos")
