"""API del servicio Pagos (FastAPI). Rutas de servicio a servicio protegidas con X-API-Key; las dos
rutas del comprador (/v1/pagos/ir y /v1/pagos/retorno) son públicas: Webpay devuelve al navegador
sin credenciales y la autoridad es Transbank (el retorno se confirma con su API, nunca se confía
en el contenido de la petición)."""
from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from . import pages
from .config import Settings
from .database import make_engine, make_sessions, prepare_database
from .resilience import Faults, Guard, TransbankUnavailable
from .service import HttpNotifier, PagosError, PagosService
from .transbank import TransbankClient, Webpay

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("pagos")


class PagoIn(BaseModel):
    alumno_id: str = Field(min_length=1, max_length=36)
    curso_id: int = Field(ge=1)
    monto: int = Field(gt=0, description="Pesos chilenos enteros")


class PagoOut(BaseModel):
    payment_id: str | None = None
    estado: str
    monto: int | None = None
    expira_at: str | None = None
    redirect_url: str | None = Field(default=None, description="Abrir en el navegador del comprador")


class ReembolsoIn(BaseModel):
    original_key: str = Field(min_length=1, max_length=128)


class ReembolsoOut(BaseModel):
    estado: str
    tipo: str | None = None
    pago_estado: str | None = None


class FaultIn(BaseModel):
    latencia_ms: int = Field(default=0, ge=0, le=60000)
    falla: bool = False


def _error(status: int, code: str, message: str, **headers):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers or None)


def create_app(settings: Settings | None = None, webpay: Webpay | None = None, notifier=None,
               start_worker: bool = True) -> FastAPI:
    s = settings or Settings.from_env()
    engine = make_engine(s.database_url)
    sessions = make_sessions(engine)
    webpay = webpay or Webpay(TransbankClient(s.tbk_base_url, s.tbk_commerce_code, s.tbk_api_key,
                                              s.tbk_timeout, s.tbk_commit_timeout),
                              Guard(s.cb_threshold, s.cb_recovery_seconds, s.bulkhead, Faults()))
    guard, faults = webpay.guard, webpay.guard.faults
    service = PagosService(s, sessions, webpay, notifier or HttpNotifier(s.orquestador_url, s.callback_key, s.callback_timeout))

    @asynccontextmanager
    async def lifespan(app):
        s.validar()
        prepare_database(engine)
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    service.reconciliar()
                except Exception:
                    log.error("Reconciliación de pagos pospuesta")
                stop.wait(s.reconcile_seconds)

        worker = threading.Thread(target=loop, daemon=True)
        if start_worker and s.reconcile_seconds > 0:
            worker.start()
        yield
        stop.set()
        if worker.is_alive():
            worker.join(timeout=10)

    app = FastAPI(title="Pagos (Transbank Webpay Plus)", version="1.0.0", lifespan=lifespan)
    app.state.service, app.state.guard, app.state.faults, app.state.settings = service, guard, faults, s

    def auth(x_api_key: str = Header(default="")):
        if not s.api_key or not secrets.compare_digest(s.api_key, x_api_key):
            raise HTTPException(401, "Clave de integración requerida")

    def internal(x_internal_key: str = Header(default="")):
        if not s.internal_key or not secrets.compare_digest(s.internal_key, x_internal_key):
            raise HTTPException(401, "Clave interna requerida")

    @app.exception_handler(PagosError)
    async def _pagos_error(_, exc: PagosError):
        return _error(exc.status, exc.code, exc.message or exc.code, **({"Retry-After": "2"} if exc.status == 503 else {}))

    @app.exception_handler(TransbankUnavailable)
    async def _tbk_unavailable(_, exc: TransbankUnavailable):
        # 503 si la llamada ni siquiera salió (circuito/bulkhead); 502 si el resultado es incierto.
        if exc.seguro_no_enviado:
            return _error(503, "TRANSBANK_NO_DISPONIBLE", exc.motivo, **{"Retry-After": "5"})
        return _error(502, "TRANSBANK_ERROR", exc.motivo)

    @app.exception_handler(HTTPException)
    async def _http(_, exc: HTTPException):
        return _error(exc.status_code, "UNAUTHENTICATED" if exc.status_code == 401 else "ERROR", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(_, exc: RequestValidationError):
        return _error(422, "VALIDATION_ERROR", "Solicitud inválida")

    # ----------------------------------------------------------- servicio a servicio
    @app.post("/v1/payments", response_model=PagoOut, dependencies=[Depends(auth)])
    def crear(payload: PagoIn, response: Response, idempotency_key: str = Header(min_length=1, max_length=128)):
        vista, creado = service.crear(idempotency_key, payload.alumno_id, payload.curso_id, payload.monto)
        response.status_code = 201 if creado else 200
        if not creado:
            response.headers["Idempotent-Replayed"] = "true"
        return vista

    @app.post("/v1/payments/refund", response_model=ReembolsoOut, dependencies=[Depends(auth)])
    def reembolsar(payload: ReembolsoIn, idempotency_key: str = Header(min_length=1, max_length=128)):
        return service.reembolsar(idempotency_key, payload.original_key)

    @app.get("/v1/payments/by-key/{key}", response_model=PagoOut, dependencies=[Depends(auth)])
    def por_clave(key: str):
        return service.consultar(key)

    # ----------------------------------------------------------- comprador (públicas)
    @app.get("/v1/pagos/ir/{payment_id}", response_class=HTMLResponse)
    def ir(payment_id: str):
        datos = service.datos_redireccion(payment_id)
        if datos is None:
            return HTMLResponse(pages.info("Este pago ya no está disponible"), status_code=410)
        return HTMLResponse(pages.redireccion(*datos))

    @app.api_route("/v1/pagos/retorno", methods=["GET", "POST"], response_class=HTMLResponse)
    async def retorno(request: Request):
        params = dict(request.query_params)
        if request.method == "POST":
            params.update({k: str(v) for k, v in (await request.form()).items()})
        vista = await run_in_threadpool(service.retorno, params.get("token_ws"), params.get("TBK_TOKEN"),
                                        params.get("TBK_ORDEN_COMPRA"))
        if s.result_url:
            return RedirectResponse(f"{s.result_url}?estado={vista['estado']}&pago={vista['payment_id']}", status_code=303)
        return HTMLResponse(pages.resultado(vista))

    # ----------------------------------------------------------- operación
    @app.get("/v1/health")
    def health():
        with sessions() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "service": "pagos", "ambiente": s.ambiente}

    @app.put("/internal/fault", dependencies=[Depends(internal)])
    def poner_falla(payload: FaultIn):
        faults.set(payload.latencia_ms, payload.falla)
        return faults.snapshot()

    @app.get("/internal/fault", dependencies=[Depends(internal)])
    def ver_falla():
        return faults.snapshot()

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        lines = [f'pagos_total{{estado="{k}"}} {v}' for k, v in service.conteos().items()]
        for st in ("CLOSED", "OPEN", "HALF_OPEN"):
            lines.append(f'circuito_estado{{dependencia="TRANSBANK",estado="{st}"}} {int(guard.breaker.state.value == st)}')
        lines.append(f'bulkhead_rechazos_total{{dependencia="TRANSBANK"}} {guard.rejected}')
        lines.append(f'circuito_rechazos_total{{dependencia="TRANSBANK"}} {guard.blocked}')
        lines.append(f'transbank_intentos_total {guard.attempted}')
        for op, n in guard.calls.items():
            lines.append(f'transbank_llamadas_total{{operacion="{op}"}} {n}')
            lines.append(f'transbank_duracion_seconds_sum{{operacion="{op}"}} {guard.seconds.get(op, 0.0)}')
        return "\n".join(lines) + "\n"

    return app


app = create_app()
