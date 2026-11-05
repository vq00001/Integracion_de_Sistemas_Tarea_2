"""Saga persistente. La intención se confirma antes de cada efecto externo."""
import hashlib
import json
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .errors import ConflictError, IdempotencyKeyReusedError
from .time import utc_now

log = logging.getLogger("saga")
ACTIVE = "estado IN ('EN_CURSO','COMPENSANDO','COMPENSACION_PENDIENTE','COMPLETADA')"
STEPS = ("RESERVAR", "COBRAR", "INSCRIBIR")


class Saga(Base):
    __tablename__ = "sagas"
    __table_args__ = (
        Index("uq_saga_activa", "alumno_id", "curso_id", unique=True,
              sqlite_where=text(ACTIVE), postgresql_where=text(ACTIVE)),
        CheckConstraint("estado IN ('EN_CURSO','COMPLETADA','COMPENSANDO','COMPENSADA','COMPENSACION_PENDIENTE')"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    alumno_id: Mapped[str] = mapped_column(String(36))
    curso_id: Mapped[int] = mapped_column(Integer)
    monto: Mapped[int] = mapped_column(Integer)
    estado: Mapped[str] = mapped_column(String(32), default="EN_CURSO")
    motivo_fallo: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class SagaStep(Base):
    __tablename__ = "saga_steps"
    __table_args__ = (UniqueConstraint("saga_id", "paso", "tipo"),
                      CheckConstraint("tipo IN ('forward','compensacion')"),
                      CheckConstraint("estado IN ('PENDIENTE','OK','FALLIDO','INCIERTO')"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    saga_id: Mapped[str] = mapped_column(ForeignKey("sagas.id"), index=True)
    paso: Mapped[str] = mapped_column(String(16))
    tipo: Mapped[str] = mapped_column(String(16))
    estado: Mapped[str] = mapped_column(String(16), default="PENDIENTE")
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    clave: Mapped[str] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


def snapshot(s):
    return {k: getattr(s, k) for k in ("id", "alumno_id", "curso_id", "monto", "estado", "motivo_fallo")}


@contextmanager
def saga_lock(engine, sid):
    """Un dueño por saga, incluso entre workers y durante commits intermedios."""
    key = int.from_bytes(hashlib.sha256(sid.encode()).digest()[:8], "big", signed=True)
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            acquired = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
            try:
                yield acquired
            finally:
                if acquired:
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    else:
        import fcntl
        folder = Path(engine.url.database or "/tmp/saga").resolve().parent / ".saga-locks"
        folder.mkdir(exist_ok=True)
        with (folder / sid).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Orchestrator:
    def __init__(self, sessions, clients, retries=3, backoff=0.1, stale_seconds=60):
        self.sessions, self.clients = sessions, clients
        self.retries, self.backoff, self.stale_seconds = retries, backoff, stale_seconds

    def transition(self, db, saga, state, step=None, duration=0):
        if step is None:
            saga.estado = state
        saga.updated_at = utc_now()
        db.commit()
        log.info(json.dumps({"saga_id": saga.id, "paso": step, "estado": state,
                             "duracion_ms": round(duration * 1000, 3)}))

    def intent(self, db, saga, step, kind):
        row = db.execute(select(SagaStep).where(SagaStep.saga_id == saga.id,
                        SagaStep.paso == step, SagaStep.tipo == kind)).scalar_one_or_none()
        if row is None:
            row = SagaStep(saga_id=saga.id, paso=step, tipo=kind,
                           clave=f"{saga.id}:{'comp:' if kind == 'compensacion' else ''}{step}")
            db.add(row)
        row.estado, row.error = "PENDIENTE", None
        row.intentos = (row.intentos or 0) + 1
        self.transition(db, saga, "PENDIENTE", step)
        return row

    def matricular(self, key, alumno_id, curso_id, monto):
        payload = dict(alumno_id=alumno_id, curso_id=curso_id, monto=monto)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.sessions() as db:
            saga = Saga(idempotency_key=key, request_hash=digest, **payload)
            db.add(saga)
            try:
                db.commit()  # claim: índices de clave y alumno/curso deciden la carrera
            except IntegrityError:
                db.rollback()
                previous = db.execute(select(Saga).where(Saga.idempotency_key == key)).scalar_one_or_none()
                if previous is None:
                    raise ConflictError("Ya existe una saga activa para alumno y curso")
                if previous.request_hash != digest:
                    raise IdempotencyKeyReusedError("La clave identifica otro payload")
                return snapshot(previous), True
            with saga_lock(db.get_bind(), saga.id) as acquired:
                if not acquired:
                    return snapshot(saga), False
                if not self.clients["COBRAR"].available:
                    saga.motivo_fallo = "PAGOS_CIRCUITO_ABIERTO"
                    self.transition(db, saga, "COMPENSANDO")
                    self.transition(db, saga, "COMPENSADA")
                    return snapshot(saga), False
                for step in STEPS:
                    row = self.intent(db, saga, step, "forward")
                    start = time.monotonic()
                    try:
                        self.clients[step].forward(snapshot(saga), row.clave)
                    except Exception as exc:
                        # No se guardan cuerpos ni mensajes de terceros (pueden contener PII).
                        row.estado = "FALLIDO" if getattr(exc, "business", False) else "INCIERTO"
                        row.error = type(exc).__name__
                        saga.motivo_fallo = f"{step}:{row.error}"
                        self.transition(db, saga, row.estado, step, time.monotonic()-start)
                        self.compensar(db, saga)
                        return snapshot(saga), False
                    row.estado = "OK"
                    self.transition(db, saga, "OK", step, time.monotonic()-start)
                self.transition(db, saga, "COMPLETADA")
                return snapshot(saga), False

    def compensar(self, db, saga):
        self.transition(db, saga, "COMPENSANDO")
        for step in reversed(STEPS):
            forward = db.execute(select(SagaStep).where(SagaStep.saga_id == saga.id,
                         SagaStep.paso == step, SagaStep.tipo == "forward")).scalar_one_or_none()
            if forward is None or forward.estado == "FALLIDO":
                continue
            # PENDIENTE tras caída equivale a INCIERTO: pudo ejecutar el efecto.
            done = db.execute(select(SagaStep).where(SagaStep.saga_id == saga.id,
                        SagaStep.paso == step, SagaStep.tipo == "compensacion")).scalar_one_or_none()
            if done is not None and done.estado == "OK":
                continue
            for attempt in range(self.retries):
                row = self.intent(db, saga, step, "compensacion")
                start = time.monotonic()
                try:
                    self.clients[step].compensate(snapshot(saga), row.clave, forward.clave)
                except Exception as exc:
                    row.estado, row.error = "INCIERTO", type(exc).__name__
                    self.transition(db, saga, row.estado, step, time.monotonic()-start)
                    if attempt+1 < self.retries:
                        time.sleep(self.backoff * 2**attempt)
                else:
                    row.estado = "OK"
                    self.transition(db, saga, "OK", step, time.monotonic()-start)
                    break
            else:
                self.transition(db, saga, "COMPENSACION_PENDIENTE")
                return
        self.transition(db, saga, "COMPENSADA")

    def reconcile(self):
        with self.sessions() as db:
            ids = db.scalars(select(Saga.id).where(Saga.estado.in_(
                ["EN_CURSO", "COMPENSANDO", "COMPENSACION_PENDIENTE"]),
                Saga.updated_at < utc_now()-timedelta(seconds=self.stale_seconds)).limit(50)).all()
            for sid in ids:
                with saga_lock(db.get_bind(), sid) as acquired:
                    if acquired:
                        saga = db.get(Saga, sid, populate_existing=True)
                        if saga.estado not in ("COMPLETADA", "COMPENSADA"):
                            self.compensar(db, saga)
        return len(ids)
