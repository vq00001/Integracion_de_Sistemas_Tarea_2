import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from .database import Base
from .errors import register_exception_handlers
from .saga import Orchestrator, Saga, SagaStep, snapshot
from .saga_clients import clients_from_env

logging.basicConfig(level=logging.INFO, format="%(message)s")

engine = create_engine(os.getenv("SAGA_DATABASE_URL", "sqlite:///./sagas.db"),
                       connect_args={"check_same_thread": False} if os.getenv("SAGA_DATABASE_URL", "sqlite:").startswith("sqlite") else {})
sessions = sessionmaker(engine)
orchestrator = Orchestrator(sessions, clients_from_env(),
    retries=int(os.getenv("SAGA_RETRIES", "3")), backoff=float(os.getenv("SAGA_BACKOFF_SECONDS", "0.1")),
    stale_seconds=float(os.getenv("SAGA_STALE_SECONDS", "60")))


def auth(x_api_key: str = Header(default="")):
    expected = os.getenv("ORCHESTRATOR_API_KEY", "")
    if not expected or not secrets.compare_digest(expected, x_api_key):
        raise HTTPException(401, "Clave de integración requerida")


@asynccontextmanager
async def lifespan(app):
    if len(os.getenv("ORCHESTRATOR_API_KEY", "")) < 24:
        raise RuntimeError("ORCHESTRATOR_API_KEY debe tener al menos 24 caracteres")
    # Solo tablas del orquestador en su base propia.
    Base.metadata.create_all(engine, tables=[Saga.__table__, SagaStep.__table__])
    stop = threading.Event()
    def recover():
        while not stop.is_set():
            try:
                orchestrator.reconcile()
            except Exception:
                import logging
                logging.getLogger("saga").error('Reconciliación pospuesta')
            stop.wait(float(os.getenv("SAGA_RECONCILE_SECONDS", "10")))
    worker = threading.Thread(target=recover, daemon=True)
    worker.start()
    yield
    stop.set()
    worker.join(timeout=30)


app = FastAPI(title="Orquestador de Matrículas", version="2.0.0", lifespan=lifespan)
register_exception_handlers(app)


class Matricular(BaseModel):
    alumno_id: str = Field(min_length=1, max_length=36)
    curso_id: int = Field(ge=1)
    monto: int = Field(gt=0, description="Unidades monetarias enteras; sin datos de tarjeta")


class SagaOut(Matricular):
    id: str
    estado: str = Field(pattern="^(EN_CURSO|COMPLETADA|COMPENSANDO|COMPENSADA|COMPENSACION_PENDIENTE)$")
    motivo_fallo: str | None


from .schemas import ErrorResponse
errors = {code: {"model": ErrorResponse} for code in (401, 404, 409, 422)}
# Mantener el contrato idéntico entre Python 3.12 y versiones posteriores.
errors[422]["description"] = "Unprocessable Entity"


@app.post("/v1/matriculas", response_model=SagaOut, dependencies=[Depends(auth)],
          responses={200: {"model": SagaOut}, 201: {"model": SagaOut}, 202: {"model": SagaOut},
                     503: {"model": SagaOut}, **errors})
def matricular(payload: Matricular, response: Response,
               idempotency_key: str = Header(min_length=1, max_length=128)):
    result, replay = orchestrator.matricular(idempotency_key, **payload.model_dump())
    state = result["estado"]
    response.status_code = (200 if replay else 201) if state == "COMPLETADA" else 202 if state in (
        "EN_CURSO", "COMPENSANDO", "COMPENSACION_PENDIENTE") else 503
    if replay:
        response.headers["Idempotent-Replayed"] = "true"
    return result


@app.get("/v1/sagas/{saga_id}", response_model=SagaOut, dependencies=[Depends(auth)], responses=errors)
def consultar(saga_id: str):
    with sessions() as db:
        saga = db.get(Saga, saga_id)
        if saga is None:
            raise HTTPException(404, "Saga inexistente")
        return snapshot(saga)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    lines = []
    with sessions() as db:
        for state, count in db.execute(select(Saga.estado, func.count()).group_by(Saga.estado)):
            lines.append(f'sagas_total{{estado="{state}"}} {count}')
        count = db.scalar(select(func.count()).select_from(SagaStep).where(SagaStep.tipo == "compensacion", SagaStep.estado == "OK"))
        lines.append(f'compensaciones_total {count}')
    for name, client in orchestrator.clients.items():
        for state in ("CLOSED", "OPEN", "HALF_OPEN"):
            lines.append(f'circuito_estado{{dependencia="{name}",estado="{state}"}} {int(client.breaker.state.value == state)}')
        lines.extend([f'bulkhead_rechazos_total{{dependencia="{name}"}} {client.rejected}',
                      f'paso_duracion_seconds_sum{{paso="{name}"}} {client.seconds}',
                      f'paso_duracion_seconds_count{{paso="{name}"}} {client.calls}'])
    return "\n".join(lines)+"\n"
