"""Inscripción local; la saga es responsable de Cupos y Pagos."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import (ConflictError, EnrollmentInProgressError,
                      IdempotencyKeyReusedError, NotFoundError)
from ..faults import aplicar_falla
from ..time import utc_now
from ..security import Principal, exigir_acceso_a_estudiante, limitar_escrituras, require_api_key

router = APIRouter(
    prefix="/v1/enrollments",
    tags=["Matrículas"],
    dependencies=[Depends(require_api_key)],
)

E = models.EstadoMatricula


def _estudiante_o_404(db: Session, student_id: str) -> models.Student:
    estudiante = db.get(models.Student, student_id)
    if estudiante is None:
        raise NotFoundError(f"No existe un estudiante con id {student_id}")
    return estudiante


def _matricula_o_404(db: Session, enrollment_id: str, principal: Principal) -> models.Enrollment:
    """Matrícula por id. Para un estudiante, una matrícula AJENA se trata como inexistente
    (404, no 403): así no se le confirma qué ids de matrícula existen."""
    matricula = db.get(models.Enrollment, enrollment_id)
    if matricula is None or (not principal.es_admin and matricula.student_id != principal.student_id):
        raise NotFoundError(f"No existe una matrícula con id {enrollment_id}")
    return matricula


# ---------------------------------------------------------------------------
# Auxiliares de idempotencia
# ---------------------------------------------------------------------------

def _retomar(db, previa, payload, response):
    if previa.curso_id != payload.curso_id:
        raise IdempotencyKeyReusedError("Clave reutilizada con otro curso")
    response.headers["Idempotent-Replayed"] = "true"
    return previa


def _resolver_conflicto(db: Session, payload: schemas.EnrollmentCreate, client_key: str | None,
                        response: Response) -> models.Enrollment:
    """La inserción chocó con un índice único: otro request ganó la carrera."""
    if client_key:
        previa = db.execute(
            select(models.Enrollment).where(
                models.Enrollment.student_id == payload.student_id,
                models.Enrollment.client_key == client_key,
            )
        ).scalar_one_or_none()
        if previa is not None:
            return _retomar(db, previa, payload, response)
    activa = db.execute(
        select(models.Enrollment).where(
            models.Enrollment.student_id == payload.student_id,
            models.Enrollment.curso_id == payload.curso_id,
            models.Enrollment.estado.in_([E.PENDIENTE, E.CONFIRMADA]),
        )
    ).scalar_one_or_none()
    if activa is None:
        raise ConflictError("No se pudo registrar la matrícula por un conflicto; reintenta.", code="CONFLICT")
    if activa.estado == E.CONFIRMADA:
        raise ConflictError(
            "El estudiante ya está matriculado (con inscripción activa) en ese curso",
            code="ALREADY_ENROLLED",
        )
    raise EnrollmentInProgressError(
        "Ya hay una matrícula en proceso para este estudiante y curso. Reintenta con la "
        "misma Idempotency-Key o espera a que se resuelva.",
        details={"enrollment_id": activa.id},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=schemas.EnrollmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Matricular a un estudiante en un curso",
    description="Inscripción local idempotente. Cupos y Pagos los coordina la saga.",
    responses={401: {"model": schemas.ErrorResponse}, 403: {"model": schemas.ErrorResponse},
               404: {"model": schemas.ErrorResponse}, 409: {"model": schemas.ErrorResponse},
               422: {"model": schemas.ErrorResponse, "description": "Unprocessable Entity"}, 429: {"model": schemas.ErrorResponse},
               503: {"model": schemas.ErrorResponse}},
)
def matricular(
    payload: schemas.EnrollmentCreate,
    response: Response,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_api_key),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=128,
        description="Identifica este intento para reintentarlo sin duplicar la inscripción.",
    ),
):
    # Autorización ANTES de consultar la base: un estudiante no debe poder usar esta
    # ruta para averiguar si existe otro estudiante.
    exigir_acceso_a_estudiante(principal, payload.student_id)
    limitar_escrituras(principal)
    _estudiante_o_404(db, payload.student_id)

    # 1) ¿Es el reintento de un intento anterior? La clave se busca SOLO entre las matrículas
    #    de este estudiante: no hay forma de averiguar (422) si otro estudiante usó la misma.
    if idempotency_key:
        previa = db.execute(
            select(models.Enrollment).where(
                models.Enrollment.student_id == payload.student_id,
                models.Enrollment.client_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if previa is not None:
            return _retomar(db, previa, payload, response)

    aplicar_falla()

    # 2) Registrar la inscripción. El índice único parcial resuelve las carreras.
    matricula = models.Enrollment(
        student_id=payload.student_id,
        curso_id=payload.curso_id,
        estado=E.CONFIRMADA,
        client_key=idempotency_key,
    )
    db.add(matricula)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _resolver_conflicto(db, payload, idempotency_key, response)

    return matricula


@router.post(
    "/{enrollment_id}/revert",
    response_model=schemas.EnrollmentOut,
    summary="Dar de baja una inscripción (idempotente)",
    responses={401: {"model": schemas.ErrorResponse}, 404: {"model": schemas.ErrorResponse},
               429: {"model": schemas.ErrorResponse}},
)
def revertir_matricula(
    enrollment_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_api_key),
):
    limitar_escrituras(principal)
    matricula = _matricula_o_404(db, enrollment_id, principal)
    if matricula.estado == E.ANULADA:
        return matricula
    db.execute(update(models.Enrollment).where(models.Enrollment.id == enrollment_id)
               .values(estado=E.ANULADA, updated_at=utc_now()))
    db.commit()
    return db.get(models.Enrollment, enrollment_id)


@router.get(
    "/{enrollment_id}",
    response_model=schemas.EnrollmentOut,
    summary="Consultar una matrícula por id",
    responses={401: {"model": schemas.ErrorResponse}, 404: {"model": schemas.ErrorResponse}},
)
def obtener_matricula(
    enrollment_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_api_key),
):
    return _matricula_o_404(db, enrollment_id, principal)


@router.get(
    "",
    response_model=schemas.EnrollmentPage,
    summary="Listar matrículas",
    description=(
        "Un administrador ve todas (con filtros opcionales). Con una API key de estudiante "
        "solo se listan sus propias matrículas."
    ),
    responses={
        401: {"model": schemas.ErrorResponse},
        403: {"model": schemas.ErrorResponse,
              "description": "Un estudiante filtró por el student_id de otro"},
    },
)
def listar_matriculas(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_api_key),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100_000),
    student_id: str | None = Query(None),
    curso_id: int | None = Query(None),
    estado: models.EstadoMatricula | None = Query(None),
    client_key: str | None = Query(None, max_length=128),
):
    if not principal.es_admin:
        if student_id:
            exigir_acceso_a_estudiante(principal, student_id)  # 403 si pide a otro
        student_id = principal.student_id  # siempre acotado a sus propias matrículas
    stmt = select(models.Enrollment)
    if student_id:
        stmt = stmt.where(models.Enrollment.student_id == student_id)
    if curso_id is not None:
        stmt = stmt.where(models.Enrollment.curso_id == curso_id)
    if client_key is not None:
        stmt = stmt.where(models.Enrollment.client_key == client_key)
    if estado is not None:
        stmt = stmt.where(models.Enrollment.estado == estado)

    # COUNT(*) en la base: no se cargan las filas como objetos ORM (antes era O(N) en memoria).
    total_count = db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()
    items = (
        db.execute(stmt.order_by(models.Enrollment.created_at.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return schemas.EnrollmentPage(total=total_count, limit=limit, offset=offset, items=items)
