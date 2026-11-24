"""Saga persistente. La intención se confirma antes de cada efecto externo."""
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .errors import ConflictError, IdempotencyKeyReusedError, TooManyRequestsError
from .time import utc_now

log = logging.getLogger("saga")
ACTIVE = "estado IN ('EN_CURSO','ESPERANDO_PAGO','COMPENSANDO','COMPENSACION_PENDIENTE','COMPLETADA')"
STEPS = ("RESERVAR", "COBRAR", "INSCRIBIR")


class Saga(Base):
    __tablename__ = "sagas"
    __table_args__ = (
        Index("uq_saga_activa", "alumno_id", "curso_id", unique=True,
              sqlite_where=text(ACTIVE), postgresql_where=text(ACTIVE)),
        CheckConstraint("estado IN ('EN_CURSO','ESPERANDO_PAGO','COMPLETADA','COMPENSANDO','COMPENSADA','COMPENSACION_PENDIENTE')"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    alumno_id: Mapped[str] = mapped_column(String(36))
    curso_id: Mapped[int] = mapped_column(Integer)
    monto: Mapped[int] = mapped_column(Integer)
    estado: Mapped[str] = mapped_column(String(32), default="EN_CURSO")
    motivo_fallo: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Cobro con redirección (Webpay Plus): dónde debe pagar el comprador y hasta cuándo se espera.
    pago_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    pago_expira_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
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
    data = {k: getattr(s, k) for k in ("id", "alumno_id", "curso_id", "monto", "estado", "motivo_fallo")}
    data["pago_url"] = s.pago_url
    data["pago_expira_at"] = s.pago_expira_at.isoformat() + "Z" if s.pago_expira_at else None
    return data


def verificar_esquema(engine):
    """Falla con un mensaje claro si la base de sagas es anterior al cobro con Webpay (SQLite no
    permite alterar un CHECK: sin esto el primer pago fallaría con un error confuso)."""
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as conn:
        ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='sagas'")).scalar()
        columns = {row[1] for row in conn.execute(text('PRAGMA table_info(sagas)'))}
    if ddl and ("ESPERANDO_PAGO" not in ddl or not {'pago_url', 'pago_expira_at'} <= columns):
        raise RuntimeError("La base de sagas tiene el esquema anterior al cobro con Webpay (falta ESPERANDO_PAGO). "
                           "Requiere una migración que conserve los datos. El arranque actualizado del "
                           "orquestador realiza la migración transaccional con copia de seguridad.")


def _parse_utc(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")) if value else None
    except ValueError:
        return None


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
    def __init__(self, sessions, clients, retries=3, backoff=0.1, stale_seconds=60,
                 poll_seconds=15, pago_gracia=60, pago_max_seconds=900):
        self.sessions, self.clients = sessions, clients
        self.retries, self.backoff, self.stale_seconds = retries, backoff, stale_seconds
        # Espera de pago: cada cuánto se consulta a Pagos, gracia tras el vencimiento y tope si Pagos no informa vencimiento.
        self.poll_seconds, self.pago_gracia, self.pago_max_seconds = poll_seconds, pago_gracia, pago_max_seconds
        # El mismo límite que los clientes forward, antes de cualquier reserva.
        # El permiso cubre la ejecución, no la espera del comprador en Webpay.
        capacity = int(os.getenv("SAGA_BULKHEAD", "8"))
        if capacity < 1:
            raise ValueError("SAGA_BULKHEAD debe ser mayor que cero")
        self.admission = threading.BoundedSemaphore(capacity)

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
            previous = self._replay(db, key, digest)
            if previous is not None:
                return previous, True
            if not self.admission.acquire(blocking=False):
                # Otra petición pudo registrar la clave mientras se consultaba.
                previous = self._replay(db, key, digest)
                if previous is not None:
                    return previous, True
                raise TooManyRequestsError("Capacidad de sagas agotada; reintenta con la misma clave")
            try:
                saga = Saga(idempotency_key=key, request_hash=digest, **payload)
                db.add(saga)
                try:
                    db.commit()  # claim: índices de clave y alumno/curso deciden la carrera
                except IntegrityError:
                    db.rollback()
                    previous = self._replay(db, key, digest)
                    if previous is None:
                        raise ConflictError("Ya existe una saga activa para alumno y curso")
                    return previous, True
                with saga_lock(db.get_bind(), saga.id) as acquired:
                    if not acquired:
                        return snapshot(saga), False
                    if not self.clients["COBRAR"].available:
                        saga.motivo_fallo = "PAGOS_CIRCUITO_ABIERTO"
                        self.transition(db, saga, "COMPENSANDO")
                        self.transition(db, saga, "COMPENSADA")
                        return snapshot(saga), False
                    return self._avanzar(db, saga, 0), False
            finally:
                self.admission.release()

    def _replay(self, db, key, digest):
        previous = db.execute(select(Saga).where(Saga.idempotency_key == key)).scalar_one_or_none()
        if previous is None:
            return None
        if previous.request_hash != digest:
            raise IdempotencyKeyReusedError("La clave identifica otro payload")
        return snapshot(previous)

    def _avanzar(self, db, saga, desde):
        """Ejecuta los pasos hacia adelante desde STEPS[desde]. Si el cobro requiere que el comprador
        pague en Webpay (Pagos responde PENDIENTE), la saga queda ESPERANDO_PAGO y se reanuda con
        pago_resuelto()."""
        for step in STEPS[desde:]:
            row = self.intent(db, saga, step, "forward")
            start = time.monotonic()
            try:
                result = self.clients[step].forward(snapshot(saga), row.clave)
            except Exception as exc:
                # No se guardan cuerpos ni mensajes de terceros (pueden contener PII).
                known = getattr(exc, "business", False) or not getattr(exc, "enviado", True)
                row.estado = "FALLIDO" if known else "INCIERTO"
                row.error = type(exc).__name__
                saga.motivo_fallo = f"{step}:{row.error}"
                self.transition(db, saga, row.estado, step, time.monotonic()-start)
                self.compensar(db, saga)
                return snapshot(saga)
            if step == "COBRAR" and isinstance(result, dict) and result.get("estado") == "PENDIENTE":
                # El paso queda PENDIENTE: hay un cobro abierto en Pagos que, si hace falta, se cancela.
                saga.pago_url = (result.get("redirect_url") or "")[:256] or None
                saga.pago_expira_at = (_parse_utc(result.get("expira_at"))
                                       or utc_now() + timedelta(seconds=self.pago_max_seconds))
                self.transition(db, saga, "ESPERANDO_PAGO")
                return snapshot(saga)
            row.estado = "OK"
            self.transition(db, saga, "OK", step, time.monotonic()-start)
        self.transition(db, saga, "COMPLETADA")
        return snapshot(saga)

    def pago_resuelto(self, saga_id, estado_pago):
        """Reanuda una saga ESPERANDO_PAGO con el resultado informado por Pagos (idempotente)."""
        with self.sessions() as db:
            saga = db.get(Saga, saga_id)
            if saga is None:
                return None
            with saga_lock(db.get_bind(), saga_id) as acquired:
                if not acquired:
                    return snapshot(saga)
                saga = db.get(Saga, saga_id, populate_existing=True)
                if saga.estado != "ESPERANDO_PAGO":
                    return snapshot(saga)
                row = db.execute(select(SagaStep).where(SagaStep.saga_id == saga.id,
                                 SagaStep.paso == "COBRAR", SagaStep.tipo == "forward")).scalar_one()
                if estado_pago == "COBRADO":
                    if not self.admission.acquire(blocking=False):
                        # Conservar ESPERANDO_PAGO: el próximo sondeo reanudará
                        # la inscripción sin compensar un cobro sano por carga.
                        return snapshot(saga)
                    try:
                        row.estado = "OK"
                        self.transition(db, saga, "OK", "COBRAR")
                        self.transition(db, saga, "EN_CURSO")
                        return self._avanzar(db, saga, STEPS.index("COBRAR") + 1)
                    finally:
                        self.admission.release()
                # RECHAZADO / ABORTADO / EXPIRADO: Pagos garantiza que no habrá cobro, no hay nada que reembolsar.
                row.estado, row.error = "FALLIDO", estado_pago
                saga.motivo_fallo = f"COBRAR:{estado_pago}"
                self.transition(db, saga, "FALLIDO", "COBRAR")
                self.compensar(db, saga)
                return snapshot(saga)

    def sondear_pago(self, saga_id):
        """Pregunta a Pagos por el cobro (fuente de verdad) y reanuda o expira la espera. Lo usa tanto el
        aviso de Pagos (solo una pista) como el reconciliador, de modo que un aviso perdido no bloquea."""
        with self.sessions() as db:
            saga = db.get(Saga, saga_id)
            if saga is None or saga.estado != "ESPERANDO_PAGO":
                return None if saga is None else snapshot(saga)
            snap = snapshot(saga)
            limite = saga.pago_expira_at + timedelta(seconds=self.pago_gracia)
        try:
            estado = (self.clients["COBRAR"].query(snap, f"{saga_id}:COBRAR") or {}).get("estado")
        except Exception:
            estado = None  # Pagos no responde: se reintenta en el próximo ciclo
        if estado in ("COBRADO", "RECHAZADO", "ABORTADO", "EXPIRADO"):
            return self.pago_resuelto(saga_id, estado)
        with self.sessions() as db:
            saga = db.get(Saga, saga_id)
            with saga_lock(db.get_bind(), saga_id) as acquired:
                if not acquired:
                    return snapshot(saga)
                saga = db.get(Saga, saga_id, populate_existing=True)
                if saga.estado != "ESPERANDO_PAGO":
                    return snapshot(saga)
                if utc_now() > limite:
                    # Pagos no cierra el cobro: se trata como incierto para pedir su cancelación/reembolso.
                    row = db.execute(select(SagaStep).where(SagaStep.saga_id == saga.id,
                                     SagaStep.paso == "COBRAR", SagaStep.tipo == "forward")).scalar_one()
                    row.estado, row.error = "INCIERTO", "PAGO_SIN_RESPUESTA"
                    saga.motivo_fallo = "COBRAR:PAGO_SIN_RESPUESTA"
                    self.transition(db, saga, "INCIERTO", "COBRAR")
                    self.compensar(db, saga)
                else:
                    saga.updated_at = utc_now()  # espacia el siguiente sondeo
                    db.commit()
                return snapshot(saga)

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
                    row.estado = "INCIERTO" if getattr(exc, "enviado", True) else "FALLIDO"
                    row.error = type(exc).__name__
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
            # Sagas esperando que el comprador pague: no se compensan por inactividad (puede estar pagando);
            # se consulta a Pagos, que decide el vencimiento.
            esperando = db.scalars(select(Saga.id).where(Saga.estado == "ESPERANDO_PAGO",
                Saga.updated_at < utc_now()-timedelta(seconds=self.poll_seconds)).limit(50)).all()
        for sid in esperando:
            self.sondear_pago(sid)
        return len(ids) + len(esperando)
