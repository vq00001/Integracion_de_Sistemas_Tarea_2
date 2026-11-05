"""Controles internos, excluidos del gateway y del contrato público."""
import os
import secrets
import threading
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .database import get_db
from .models import Enrollment, EstadoMatricula
from .schemas import EnrollmentCreate, EnrollmentOut

_lock = threading.Lock()
_mode, _delay = False, 0.0


def internal_auth(x_internal_key: str = Header(default="")):
    expected = os.getenv("INTERNAL_KEY", "")
    if not expected or not secrets.compare_digest(x_internal_key, expected):
        raise HTTPException(403, "Acceso interno requerido")


router = APIRouter(prefix="/internal", dependencies=[Depends(internal_auth)], include_in_schema=False)


class Fault(BaseModel):
    falla: bool = False
    lentitud_seconds: float = Field(0, ge=0, le=30)


@router.put("/fault")
def configurar(payload: Fault):
    global _mode, _delay
    with _lock:
        _mode, _delay = payload.falla, payload.lentitud_seconds
    return payload


def aplicar_falla():
    with _lock:
        mode, delay = _mode, _delay
    if delay:
        time.sleep(delay)
    if mode:
        raise HTTPException(503, "Falla simulada de Inscripción")


@router.post("/enrollments/cancel", response_model=EnrollmentOut)
def cancelar(payload: EnrollmentCreate, idempotency_key: str = Header(min_length=1, max_length=128),
             db=Depends(get_db)):
    # Tombstone: una inscripción tardía con la misma clave no puede resucitar.
    def buscar():
        return db.execute(select(Enrollment).where(Enrollment.student_id == payload.student_id,
                          Enrollment.client_key == idempotency_key)).scalar_one_or_none()
    row = buscar()
    if row is None:
        row = Enrollment(student_id=payload.student_id, curso_id=payload.curso_id,
                         client_key=idempotency_key, estado=EstadoMatricula.ANULADA)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = buscar()
            if row is None:
                raise HTTPException(409, "Otra inscripción ocupa la clave o el curso")
    if row.curso_id != payload.curso_id:
        raise HTTPException(422, "Clave reutilizada")
    row.estado = EstadoMatricula.ANULADA
    db.commit()
    return row
