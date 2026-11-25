"""Un pago es un cobro con Webpay Plus. No se guardan datos de tarjeta: solo el token de la
transacción, los códigos de resultado y el estado."""
from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, utc_now

ESTADOS = ("PENDIENTE", "PROCESANDO", "COBRADO", "RECHAZADO", "ABORTADO", "EXPIRADO", "CANCELADO", "REEMBOLSADO")
# Estados en los que ya no puede ocurrir un cobro nuevo.
SIN_COBRO = ("RECHAZADO", "ABORTADO", "EXPIRADO", "CANCELADO")
TERMINALES = SIN_COBRO + ("COBRADO", "REEMBOLSADO")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("estado IN (" + ",".join(f"'{e}'" for e in ESTADOS) + ")"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    alumno_id: Mapped[str] = mapped_column(String(36))
    curso_id: Mapped[int] = mapped_column(Integer)
    monto: Mapped[int] = mapped_column(Integer)
    estado: Mapped[str] = mapped_column(String(16), default="PENDIENTE", index=True)
    # Webpay Plus
    buy_order: Mapped[str] = mapped_column(String(26), unique=True)
    session_id: Mapped[str] = mapped_column(String(64))
    token: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    webpay_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    expires_at: Mapped[object] = mapped_column(DateTime)
    tbk_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authorization_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    payment_type: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # Reembolso (compensación)
    refund_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    refund_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Aviso al orquestador (best effort; el orquestador además consulta por su cuenta).
    notificado_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class RefundKey(Base):
    """Cada clave, incluidos los aliases de un replay, pertenece a un solo cobro."""
    __tablename__ = "payment_refund_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    original_key: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utc_now)


class PaymentOperation(Base):
    """Reserva durable antes de enviar CREATE o REFUND; no es un lock en memoria."""
    __tablename__ = "payment_operations"
    __table_args__ = (
        CheckConstraint("operation IN ('CREATE', 'REFUND')"),
        CheckConstraint("phase IN ('RUNNING', 'UNCERTAIN', 'DONE')"),
    )

    payment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation: Mapped[str] = mapped_column(String(8), primary_key=True)
    phase: Mapped[str] = mapped_column(String(12), default="RUNNING")
    created_at: Mapped[object] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class PaymentSchemaVersion(Base):
    __tablename__ = "payment_schema_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[object] = mapped_column(DateTime, default=utc_now)
