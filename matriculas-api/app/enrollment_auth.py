"""Credencial de escritura exclusiva de Orquestador -> Inscripción."""
import os
import secrets

from fastapi import Header, HTTPException


def require_enrollment_writer(x_enrollment_writer_key: str = Header(default="")):
    expected = os.getenv("INSCRIPCION_WRITER_KEY", "")
    if not expected or not secrets.compare_digest(x_enrollment_writer_key, expected):
        raise HTTPException(403, "El alta de inscripciones requiere al orquestador")
