"""Lógica de Pagos: cobro con Webpay Plus, idempotente y compensable.

Flujo (Webpay Plus es una redirección del comprador, no un cargo servidor a servidor):

  1. crear()      POST /v1/payments → crea la transacción en Webpay y devuelve PENDIENTE + redirect_url.
  2. comprador    abre redirect_url, paga en Webpay y Webpay lo devuelve a /v1/pagos/retorno.
  3. retorno()    confirma (commit) con Transbank y deja el pago en COBRADO o RECHAZADO; avisa al
                  orquestador, que además consulta por su cuenta (GET /v1/payments/by-key).
  4. reembolsar() compensación: COBRADO → reembolso en Transbank; PENDIENTE → cancela (un retorno
                  tardío ya no confirma); sin pago → deja una cancelación anticipada.

Todas las transiciones son atómicas (UPDATE … WHERE estado IN …): un retorno y una compensación
concurrentes no pueden contradecirse.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .database import utc_now
from .models import Payment, PaymentOperation, RefundKey
from .resilience import TransbankRejected, TransbankUnavailable

log = logging.getLogger("pagos")
NOTIFICABLES = ("COBRADO", "RECHAZADO", "ABORTADO", "EXPIRADO")


class PagosError(Exception):
    status, code = 400, "ERROR"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class IdempotencyConflict(PagosError):
    status, code = 422, "IDEMPOTENCY_KEY_REUSED"


class PagoCancelado(PagosError):
    status, code = 409, "PAGO_CANCELADO"


class PagoNoEncontrado(PagosError):
    status, code = 404, "NOT_FOUND"


class ReembolsoRechazado(PagosError):
    status, code = 409, "REEMBOLSO_RECHAZADO"


class Reintentar(PagosError):
    status, code = 503, "REINTENTAR"


class HttpNotifier:
    """Avisa al orquestador (best effort). El aviso es una pista: el orquestador confirma con
    GET /v1/payments/by-key antes de actuar, así que un aviso falso o perdido no cambia nada."""

    def __init__(self, base_url: str, key: str, timeout: float = 2.0):
        self.url, self.key, self.timeout = f"{base_url}/internal/pagos/resultado", key, timeout

    def __call__(self, payment_key: str, estado: str) -> bool:
        req = urllib.request.Request(
            self.url, data=json.dumps({"payment_key": payment_key, "estado": estado}).encode(),
            headers={"Content-Type": "application/json", "X-Callback-Key": self.key}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as exc:
            return exc.code == 404  # la saga no existe: no hay nadie a quien avisar
        except Exception:
            return False


class PagosService:
    def __init__(self, settings, sessions, webpay, notifier=None):
        self.s, self.sessions, self.webpay = settings, sessions, webpay
        self.notifier = notifier or (lambda key, estado: True)

    # ------------------------------------------------------------------ utilidades
    @property
    def return_url(self) -> str:
        return f"{self.s.public_base_url}/v1/pagos/retorno"

    def vista(self, p: Payment) -> dict:
        v = {"payment_id": p.id, "estado": p.estado, "monto": p.monto, "expira_at": p.expires_at.isoformat() + "Z"}
        if p.estado == "PENDIENTE" and p.token:
            v["redirect_url"] = f"{self.s.public_base_url}/v1/pagos/ir/{p.id}"
        return v

    @staticmethod
    def _por_clave(db, key):
        return db.scalar(select(Payment).where(Payment.idempotency_key == key))

    @staticmethod
    def _cas(db, pid: str, desde: tuple, nuevo: str, **extra) -> bool:
        """Transición atómica: solo si el pago sigue en uno de los estados `desde`."""
        r = db.execute(update(Payment).where(Payment.id == pid, Payment.estado.in_(desde))
                       .values(estado=nuevo, **extra))
        db.commit()
        return r.rowcount == 1

    def _expirar(self, db, p: Payment) -> None:
        if p.estado == "PENDIENTE" and p.expires_at <= utc_now():
            self._cas(db, p.id, ("PENDIENTE",), "EXPIRADO", tbk_status="EXPIRED")
            db.refresh(p)

    def _avisar(self, db, p: Payment) -> None:
        if p.estado in NOTIFICABLES and p.notificado_at is None:
            try:
                if self.notifier(p.idempotency_key, p.estado):
                    db.execute(update(Payment).where(Payment.id == p.id).values(notificado_at=utc_now()))
                    db.commit()
            except Exception:
                log.warning(json.dumps({"evento": "aviso_fallido", "payment_id": p.id}))

    @staticmethod
    def _reservar_clave_reembolso(db, key: str, original_key: str) -> None:
        binding = db.get(RefundKey, key)
        if binding is None:
            db.add(RefundKey(key=key, original_key=original_key))
            try:
                db.commit()
                return
            except IntegrityError:
                db.rollback()
                binding = db.get(RefundKey, key)
                if binding is None:
                    raise
        if binding.original_key != original_key:
            raise IdempotencyConflict("La clave de reembolso identifica otro cobro")

    @staticmethod
    def _reservar_operacion(db, p: Payment, operation: str, refund_key=None) -> bool:
        """La PK y el UPDATE condicional excluyen procesos sin retener locks de BD en HTTP."""
        db.add(PaymentOperation(payment_id=p.id, operation=operation))
        try:
            db.flush()
            condition = Payment.estado == ("PENDIENTE" if operation == "CREATE" else "COBRADO")
            values = {"updated_at": utc_now()}
            if operation == "CREATE":
                condition &= Payment.token.is_(None)
            else:
                values["refund_key"] = func.coalesce(Payment.refund_key, refund_key)
            result = db.execute(update(Payment).where(Payment.id == p.id, condition).values(**values))
            if result.rowcount != 1:
                db.rollback()
                return False
            db.commit()
            db.refresh(p)
            return True
        except IntegrityError:
            db.rollback()
            return False

    @staticmethod
    def _resultado_operacion(db, pid: str, operation: str, phase: str | None) -> None:
        condition = (PaymentOperation.payment_id == pid, PaymentOperation.operation == operation,
                     PaymentOperation.phase != "DONE")
        if phase is None:  # rechazo conocido antes del envío, o respuesta negativa definitiva
            db.execute(delete(PaymentOperation).where(*condition))
        else:
            db.execute(update(PaymentOperation).where(*condition).values(phase=phase))
        db.commit()

    # ------------------------------------------------------------------ 1. crear
    def crear(self, key: str, alumno_id: str, curso_id: int, monto: int):
        with self.sessions() as db:
            p = Payment(idempotency_key=key, alumno_id=alumno_id, curso_id=curso_id, monto=monto,
                        buy_order="MAT" + uuid.uuid4().hex[:23], session_id=key[:61],
                        expires_at=utc_now() + timedelta(seconds=self.s.ttl_seconds))
            db.add(p)
            created = True
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                p = self._por_clave(db, key)
                if p is None:
                    raise
                created = False
                # La cancelación (también la anticipada, que no tiene payload) manda sobre todo lo demás.
                if p.estado == "CANCELADO":
                    raise PagoCancelado("El cobro fue cancelado")
                if (p.alumno_id, p.curso_id, p.monto) != (alumno_id, curso_id, monto):
                    raise IdempotencyConflict("La clave identifica otro cobro")
                self._expirar(db, p)
                if p.token is not None or p.estado != "PENDIENTE":
                    return self.vista(p), False
            if not self._reservar_operacion(db, p, "CREATE"):
                db.refresh(p)
                if p.estado == "CANCELADO":
                    raise PagoCancelado("El cobro fue cancelado")
                if p.token is not None or p.estado != "PENDIENTE":
                    return self.vista(p), False
                raise Reintentar("La creación está en curso o su resultado es incierto")
            try:
                r = self.webpay.crear(p.buy_order, p.session_id, p.monto, self.return_url)
            except TransbankRejected as exc:
                if exc.http_status in (401, 403):
                    self._resultado_operacion(db, p.id, "CREATE", None)
                    raise TransbankUnavailable("credenciales de Transbank inválidas")
                self._cas(db, p.id, ("PENDIENTE",), "RECHAZADO", tbk_status="CREATE_REJECTED")
                self._resultado_operacion(db, p.id, "CREATE", "DONE")
                db.refresh(p)
                return self.vista(p), created
            except TransbankUnavailable as exc:
                self._resultado_operacion(db, p.id, "CREATE", None if exc.seguro_no_enviado else "UNCERTAIN")
                raise
            except Exception:
                self._resultado_operacion(db, p.id, "CREATE", "UNCERTAIN")
                raise
            # Conservar la transacción externa incluso si el pago venció durante la llamada.
            db.execute(update(Payment).where(Payment.id == p.id).values(token=r["token"], webpay_url=r["url"]))
            self._resultado_operacion(db, p.id, "CREATE", "DONE")
            db.refresh(p)
            return self.vista(p), created

    def datos_redireccion(self, payment_id: str):
        with self.sessions() as db:
            p = db.get(Payment, payment_id)
            if p is None:
                raise PagoNoEncontrado("Pago inexistente")
            self._expirar(db, p)
            if p.estado != "PENDIENTE" or not p.token:
                return None
            return p.webpay_url, p.token

    # ------------------------------------------------------------------ 2-3. retorno de Webpay
    def retorno(self, token_ws: str | None, tbk_token: str | None, tbk_orden: str | None) -> dict:
        token = token_ws or tbk_token
        with self.sessions() as db:
            p = db.scalar(select(Payment).where(Payment.token == token)) if token else None
            if p is None and tbk_orden:
                p = db.scalar(select(Payment).where(Payment.buy_order == tbk_orden))
            if p is None:
                raise PagoNoEncontrado("Pago desconocido")
            if tbk_token or not token_ws:
                # El comprador anuló, venció el formulario o hubo un error: no hubo autorización.
                self._cas(db, p.id, ("PENDIENTE",), "ABORTADO", tbk_status="ABORTED")
            else:
                self._expirar(db, p)
                if p.estado == "PENDIENTE" and self._cas(db, p.id, ("PENDIENTE",), "PROCESANDO"):
                    db.refresh(p)
                    self._confirmar(db, p)
            db.refresh(p)
            self._avisar(db, p)
            return self.vista(p)

    def _confirmar(self, db, p: Payment) -> None:
        try:
            r = self.webpay.confirmar(p.token)
        except TransbankRejected:
            try:
                self._resolver_con_estado(db, p)
            except TransbankUnavailable:
                pass  # queda PROCESANDO; el reconciliador lo resuelve
            return
        except TransbankUnavailable as exc:
            log.warning(json.dumps({"evento": "commit_incierto", "payment_id": p.id, "motivo": exc.motivo}))
            return
        self._aplicar(db, p, r)

    def _resolver_con_estado(self, db, p: Payment) -> None:
        """Un commit incierto se decide preguntando a Transbank por el estado de la transacción."""
        try:
            r = self.webpay.estado(p.token)
        except TransbankRejected:
            self._cas(db, p.id, ("PROCESANDO",), "RECHAZADO", tbk_status="UNKNOWN_TOKEN")
            return
        estado = r.get("status")
        if estado in ("AUTHORIZED", "CAPTURED"):
            self._aplicar(db, p, r)
        elif estado == "INITIALIZED":  # el commit nunca llegó a Transbank
            try:
                self._aplicar(db, p, self.webpay.confirmar(p.token))
            except TransbankRejected:
                self._cas(db, p.id, ("PROCESANDO",), "RECHAZADO", tbk_status="COMMIT_REJECTED")
        else:
            self._cas(db, p.id, ("PROCESANDO",), "RECHAZADO", tbk_status=estado)

    def _aplicar(self, db, p: Payment, r: dict) -> None:
        autorizado = r.get("status") == "AUTHORIZED" and r.get("response_code") == 0
        coincide = r.get("amount") == p.monto and r.get("buy_order") == p.buy_order
        nuevo = "COBRADO" if autorizado and coincide else "RECHAZADO"
        self._cas(db, p.id, ("PROCESANDO",), nuevo, tbk_status=str(r.get("status"))[:24],
                  response_code=r.get("response_code"),
                  authorization_code=(str(r["authorization_code"])[:16] if r.get("authorization_code") else None),
                  payment_type=(str(r["payment_type_code"])[:4] if r.get("payment_type_code") else None))
        if autorizado and not coincide:
            # Transbank autorizó algo distinto de lo que se pidió: no se cobra; se devuelve el dinero.
            log.critical(json.dumps({"evento": "monto_u_orden_no_coinciden", "payment_id": p.id}))
            try:
                self.webpay.reembolsar(p.token, int(r.get("amount") or 0))
            except Exception:
                log.critical(json.dumps({"evento": "reembolso_pendiente_manual", "payment_id": p.id}))
        db.refresh(p)

    # ------------------------------------------------------------------ consulta
    def consultar(self, key: str) -> dict:
        with self.sessions() as db:
            p = self._por_clave(db, key)
            if p is None:
                return {"estado": "INEXISTENTE"}
            self._expirar(db, p)
            return self.vista(p)

    # ------------------------------------------------------------------ 4. compensación
    def reembolsar(self, refund_key: str, original_key: str) -> dict:
        with self.sessions() as db:
            self._reservar_clave_reembolso(db, refund_key, original_key)
            p = self._por_clave(db, original_key)
            if p is None:
                # Cancelación anticipada: un cobro tardío con esta clave ya no puede ejecutarse.
                t = Payment(idempotency_key=original_key, alumno_id="-", curso_id=0, monto=0, estado="CANCELADO",
                            buy_order="TMB" + uuid.uuid4().hex[:23], session_id=original_key[:61],
                            expires_at=utc_now(), refund_key=refund_key)
                db.add(t)
                try:
                    db.commit()
                    return {"estado": "INEXISTENTE"}
                except IntegrityError:
                    db.rollback()
                    p = self._por_clave(db, original_key)
                    if p is None:
                        raise Reintentar("Conflicto al registrar la cancelación")
            for _ in range(3):
                db.refresh(p)
                if p.estado == "REEMBOLSADO":
                    return {"estado": "REEMBOLSADO", "tipo": p.refund_type}
                if p.estado == "PENDIENTE":
                    # PostgreSQL espera al escritor del pago antes de tomar el snapshot
                    # de las reservas; SQLite serializa el UPDATE condicional siguiente.
                    db.execute(select(Payment.id).where(Payment.id == p.id).with_for_update())
                    active_creation = select(PaymentOperation.payment_id).where(
                        PaymentOperation.payment_id == p.id, PaymentOperation.operation == "CREATE",
                        PaymentOperation.phase == "RUNNING").exists()
                    cancelled = db.execute(update(Payment).where(
                        Payment.id == p.id, Payment.estado == "PENDIENTE", ~active_creation)
                        .values(estado="CANCELADO", refund_key=func.coalesce(Payment.refund_key, refund_key)))
                    db.commit()
                    if cancelled.rowcount == 1:
                        return {"estado": "CANCELADO"}
                    creation = db.get(PaymentOperation, (p.id, "CREATE"))
                    if creation is not None and creation.phase == "RUNNING":
                        raise Reintentar("Hay una creación en curso; reintentar la cancelación")
                    continue  # cambió de estado en paralelo: reevaluar
                if p.estado == "PROCESANDO":
                    raise Reintentar("Hay una confirmación en curso; reintentar")
                if p.estado == "COBRADO":
                    return self._reembolsar_cobro(db, p, refund_key)
                if p.estado == "CANCELADO":
                    return {"estado": "CANCELADO"}
                return {"estado": "INEXISTENTE", "pago_estado": p.estado}  # nunca hubo cobro
            raise Reintentar("El pago cambia de estado; reintentar")

    def _reembolsar_cobro(self, db, p: Payment, refund_key: str) -> dict:
        if not self._reservar_operacion(db, p, "REFUND", refund_key):
            db.refresh(p)
            if p.estado == "REEMBOLSADO":
                return {"estado": "REEMBOLSADO", "tipo": p.refund_type}
            operation = db.get(PaymentOperation, (p.id, "REFUND"))
            if operation is not None and operation.phase in ("RUNNING", "UNCERTAIN"):
                if self._resolver_reembolso(db, p):
                    return {"estado": "REEMBOLSADO", "tipo": p.refund_type}
            raise Reintentar("El reembolso está en curso o su resultado es incierto")
        try:
            r = self.webpay.reembolsar(p.token, p.monto)
            tipo = r.get("type")
            if r.get("response_code") not in (0, None):
                self._resultado_operacion(db, p.id, "REFUND", None)
                raise ReembolsoRechazado("Transbank no aceptó el reembolso")
        except TransbankRejected as exc:
            # ¿Ya estaba reembolsado? Se pregunta antes de declarar el fallo.
            try:
                tipo = self.webpay.estado(p.token).get("status")
            except (TransbankUnavailable, TransbankRejected):
                self._resultado_operacion(db, p.id, "REFUND", None)
                raise Reintentar("No se pudo confirmar el reembolso")
            if tipo not in ("REVERSED", "NULLIFIED"):
                self._resultado_operacion(db, p.id, "REFUND", None)
                raise ReembolsoRechazado(f"Transbank rechazó el reembolso (HTTP {exc.http_status})")
        except TransbankUnavailable as exc:
            self._resultado_operacion(db, p.id, "REFUND", None if exc.seguro_no_enviado else "UNCERTAIN")
            raise
        except ReembolsoRechazado:
            raise
        except Exception:
            self._resultado_operacion(db, p.id, "REFUND", "UNCERTAIN")
            raise
        if tipo not in ("REVERSED", "NULLIFIED"):
            self._resultado_operacion(db, p.id, "REFUND", "UNCERTAIN")
            raise Reintentar("Respuesta de reembolso sin resultado definitivo")
        self._completar_reembolso(db, p, tipo)
        return {"estado": "REEMBOLSADO", "tipo": tipo}

    def _completar_reembolso(self, db, p: Payment, tipo: str) -> None:
        db.execute(update(Payment).where(Payment.id == p.id, Payment.estado == "COBRADO")
                   .values(estado="REEMBOLSADO", refund_type=tipo))
        self._resultado_operacion(db, p.id, "REFUND", "DONE")
        db.refresh(p)

    def _resolver_reembolso(self, db, p: Payment) -> bool:
        """Recuperar una respuesta perdida consultando; nunca repetir un POST incierto."""
        try:
            tipo = self.webpay.estado(p.token).get("status")
        except (TransbankUnavailable, TransbankRejected):
            return False
        if tipo not in ("REVERSED", "NULLIFIED"):
            return False
        self._completar_reembolso(db, p, tipo)
        return True

    # ------------------------------------------------------------------ reconciliación
    def reconciliar(self) -> dict:
        ahora = utc_now()
        hecho = {"expirados": 0, "resueltos": 0, "avisados": 0, "reembolsos_resueltos": 0}
        with self.sessions() as db:
            r = db.execute(update(Payment).where(Payment.estado == "PENDIENTE", Payment.expires_at <= ahora)
                           .values(estado="EXPIRADO", tbk_status="EXPIRED"))
            db.commit()
            hecho["expirados"] = r.rowcount
            colgados = db.scalars(select(Payment).where(
                Payment.estado == "PROCESANDO",
                Payment.updated_at < ahora - timedelta(seconds=self.s.procesando_seconds)).limit(50)).all()
            for p in colgados:
                try:
                    self._resolver_con_estado(db, p)
                    hecho["resueltos"] += 1
                except (TransbankUnavailable, TransbankRejected):
                    pass
            reembolsos = db.scalars(select(Payment).join(
                PaymentOperation, PaymentOperation.payment_id == Payment.id).where(
                    Payment.estado == "COBRADO", PaymentOperation.operation == "REFUND",
                    PaymentOperation.phase.in_(("RUNNING", "UNCERTAIN"))).limit(50)).all()
            for p in reembolsos:
                hecho["reembolsos_resueltos"] += int(self._resolver_reembolso(db, p))
            pendientes = db.scalars(select(Payment).where(
                Payment.estado.in_(NOTIFICABLES), Payment.notificado_at.is_(None),
                Payment.created_at > ahora - timedelta(days=1)).limit(50)).all()
            for p in pendientes:
                self._avisar(db, p)
                hecho["avisados"] += 1 if p.notificado_at else 0
        return hecho

    def conteos(self) -> dict:
        with self.sessions() as db:
            return dict(db.execute(select(Payment.estado, func.count()).group_by(Payment.estado)).all())
