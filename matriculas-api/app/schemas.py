from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .models import EstadoMatricula
from .rut import normalizar_rut

# ---------------------------------------------------------------------------
# Estudiantes
# ---------------------------------------------------------------------------


# Nombres: letras y dígitos Unicode (con tildes/ñ), espacios, apóstrofo, guion y punto. Se rechazan
# los símbolos < > ; " ` { } \ / & = que no forman parte de ningún nombre y habilitan XSS
# almacenado o inyección en un futuro portal. (Se aceptan dígitos: hay apellidos/apodos con ellos.)
_NOMBRE_RE = re.compile(r"^[^\W_](?:[^\W_]|[ '’.\-])*$")
# Teléfono: opcional +, dígitos, espacios, guiones y paréntesis; entre 7 y 20 caracteres.
_TELEFONO_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{6,19}$")


class StudentCreate(BaseModel):
    rut: str = Field(..., min_length=3, max_length=12, examples=["19011022-3"])
    nombre: str = Field(..., min_length=1, max_length=150, examples=["Ana"])
    apellido: str = Field(..., min_length=1, max_length=150, examples=["Pérez"])
    email: EmailStr = Field(..., examples=["ana.perez@example.com"])
    telefono: str | None = Field(None, max_length=20, examples=["+56912345678"])

    @field_validator("nombre", "apellido")
    @classmethod
    def _validar_nombre(cls, valor: str) -> str:
        valor = " ".join(valor.split())  # recorta y colapsa espacios: "   " queda vacío
        if not valor or not _NOMBRE_RE.match(valor):
            raise ValueError("solo letras, espacios, apóstrofo, guion y punto")
        return valor

    @field_validator("email")
    @classmethod
    def _canonicalizar_email(cls, valor: str) -> str:
        # Forma canónica: minúsculas. Así la restricción única también atrapa Ana@x.com / ana@x.com.
        return str(valor).strip().lower()

    @field_validator("telefono")
    @classmethod
    def _validar_telefono(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        valor = valor.strip()
        if not _TELEFONO_RE.match(valor):
            raise ValueError("formato de teléfono inválido (dígitos, +, espacios, guiones, paréntesis)")
        return valor

    @field_validator("rut")
    @classmethod
    def _validar_rut(cls, valor: str) -> str:
        # Se guarda en formato canónico (sin puntos, con guion y DV en mayúscula)
        return normalizar_rut(valor)


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rut: str
    nombre: str
    apellido: str
    email: EmailStr
    telefono: str | None
    created_at: datetime
    updated_at: datetime


class StudentPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[StudentOut]


class ApiKeyInfo(BaseModel):
    """Metadatos de la API key activa de un estudiante (nunca incluye el secreto)."""

    model_config = ConfigDict(from_attributes=True)

    student_id: str
    key_prefix: str = Field(..., description="Identificador público de la clave; no revela el secreto.",
                            examples=["mat_1a2b3c4d"])
    created_at: datetime
    expires_at: datetime | None = Field(None, description="Vencimiento (null = no caduca)")


class ApiKeyIssued(ApiKeyInfo):
    api_key: str = Field(
        ...,
        description="Valor completo de la clave. Se muestra UNA sola vez: no se puede recuperar.",
        examples=["mat_1a2b3c4d.Zx9..."],
    )


class StudentPasswordSet(BaseModel):
    """Contraseña inicial o reiniciada por un administrador; nunca se devuelve."""

    password: str = Field(..., min_length=12, max_length=128)


class AccessToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ---------------------------------------------------------------------------
# Matrículas
# ---------------------------------------------------------------------------


class EnrollmentCreate(BaseModel):
    student_id: str = Field(..., description="Id del estudiante (UUID)")
    curso_id: int = Field(
        ..., ge=1, description="Id del curso en el sistema Cupos", examples=[5]
    )


class EnrollmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    student_id: str
    curso_id: int
    estado: EstadoMatricula
    motivo: str | None
    created_at: datetime
    updated_at: datetime


class EnrollmentPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[EnrollmentOut]


# ---------------------------------------------------------------------------
# Cursos (disponibilidad, leída de Cupos)
# ---------------------------------------------------------------------------


class CourseOut(BaseModel):
    curso_id: int
    nombre: str
    cupos_totales: int
    cupos_libres: int = Field(
        ..., description="Referencial: puede estar desactualizado hasta `CURSOS_CACHE_TTL_SECONDS` "
                         "segundos. La reserva la decide Cupos en la saga."
    )


class CourseList(BaseModel):
    items: list[CourseOut]


class CourseAvailability(BaseModel):
    curso_id: int
    cupos_libres: int


# ---------------------------------------------------------------------------
# Errores (envoltorio JSON uniforme para toda la API)
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str = Field(..., examples=["SIN_CUPOS"])
    message: str = Field(..., examples=["El curso 5 no tiene cupos disponibles"])
    details: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
