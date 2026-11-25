from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
import time

from sqlalchemy import create_engine, event, insert, select, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def make_engine(url: str):
    if not url.startswith("sqlite"):
        return create_engine(url)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA busy_timeout=30000")
            deadline = time.monotonic() + 30
            while True:
                try:
                    # Cambiar una base antigua a WAL puede devolver BUSY sin esperar
                    # busy_timeout cuando dos procesos abren su primera conexión.
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.fetchall()
                    break
                except sqlite3.OperationalError as exc:
                    if (getattr(exc, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        finally:
            cur.close()

    return engine


def make_sessions(engine):
    return sessionmaker(engine, expire_on_commit=False)


def prepare_database(engine):
    """Migración aditiva H9, transaccional y serializada entre arranques.

    No cambia filas ni columnas de payments. Las reservas antiguas se importan
    una vez; un cobro antiguo sin token tiene resultado desconocido y no se reenvía.
    """
    from .models import Payment, PaymentOperation, PaymentSchemaVersion, RefundKey

    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        elif engine.dialect.name == "postgresql":
            # Solo durante la migración, nunca mientras se llama a la pasarela.
            conn.execute(text("SELECT pg_advisory_xact_lock(344693329747)"))
        Base.metadata.create_all(conn)
        if conn.scalar(select(PaymentSchemaVersion.version).where(PaymentSchemaVersion.version == 1)):
            return
        for key, original in conn.execute(select(Payment.refund_key, Payment.idempotency_key)
                                          .where(Payment.refund_key.is_not(None))):
            conn.execute(insert(RefundKey).values(key=key, original_key=original))
        for pid in conn.scalars(select(Payment.id).where(Payment.estado == "PENDIENTE",
                                                         Payment.token.is_(None))):
            conn.execute(insert(PaymentOperation).values(payment_id=pid, operation="CREATE", phase="UNCERTAIN"))
        conn.execute(insert(PaymentSchemaVersion).values(version=1))
