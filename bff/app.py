"""BFF web: respuestas de portal compuestas desde cursos e inscripciones."""
import asyncio
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, TimeoutError as FuturesTimeout, wait
from contextlib import contextmanager
import os
import threading
import time

import httpx
import grpc
from flask import Flask, jsonify, request
from grpc_stubs import cupos_pb2, cupos_pb2_grpc

app = Flask(__name__)
MATRICULAS_API_URL = os.getenv("INSCRIPCION_SERVICE_URL", "http://matriculas-api:8000").rstrip("/")
BFF_TIMEOUT = max(0.1, float(os.getenv("BFF_UPSTREAM_TIMEOUT_SECONDS", "4")))
CUPOS_CHANNEL = grpc.insecure_channel(os.getenv("CUPOS_GRPC_TARGET", "cupos:50051"))
CUPOS_STUB = cupos_pb2_grpc.CuposStub(CUPOS_CHANNEL)
MAX_WORKERS = max(2, int(os.getenv("BFF_MAX_WORKERS", "16")))
_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
# Cada agregación necesita dos workers. El permiso se conserva hasta que ambos
# trabajos terminan, incluso si el dashboard ya respondió con un error.
_admission = threading.BoundedSemaphore(MAX_WORKERS // 2)


class Budget:
    """Un deadline y una señal de cancelación compartidos por ambos transportes."""

    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        self._cancelled = False
        self._callbacks = set()
        self._lock = threading.Lock()

    def remaining(self):
        with self._lock:
            remaining = self.deadline - time.monotonic()
            if self._cancelled or remaining <= 0:
                raise FuturesTimeout()
            return remaining

    def transport_timeout(self):
        # Dejar un margen para cancelar, recoger resultados y componer la respuesta.
        return self.remaining() * 0.8

    @contextmanager
    def on_cancel(self, callback):
        with self._lock:
            if self._cancelled or time.monotonic() >= self.deadline:
                raise FuturesTimeout()
            self._callbacks.add(callback)
        try:
            yield
        finally:
            with self._lock:
                self._callbacks.discard(callback)

    def cancel(self):
        with self._lock:
            self._cancelled = True
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            callback()


def _get(path: str, headers: dict[str, str], budget: Budget):
    async def fetch():
        loop, task = asyncio.get_running_loop(), asyncio.current_task()

        def cancel():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # El trabajo ya terminó y cerró su event loop.

        try:
            with budget.on_cancel(cancel):
                async with asyncio.timeout(budget.remaining()):
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        return await client.get(f"{MATRICULAS_API_URL}{path}", headers=headers,
                                                timeout=budget.transport_timeout())
        except (asyncio.CancelledError, TimeoutError) as exc:
            raise FuturesTimeout() from exc

    return asyncio.run(fetch())


def _cursos_disponibles(budget: Budget):
    call = CUPOS_STUB.listarCursosDisponibles.future(
        cupos_pb2.ListarRequest(), timeout=budget.transport_timeout())
    try:
        with budget.on_cancel(call.cancel):
            response = call.result(timeout=budget.remaining())
    except (grpc.FutureTimeoutError, grpc.FutureCancelledError) as exc:
        raise FuturesTimeout() from exc
    finally:
        call.cancel()
    return {"items": [
        {"curso_id": course.curso_id, "nombre": course.nombre,
         "cupos_totales": course.cupos_totales, "cupos_libres": course.cupos_libres}
        for course in response.cursos
    ]}


def _release_when_done(futures):
    if not futures:
        _admission.release()
        return
    remaining = len(futures)
    lock = threading.Lock()

    def finished(_):
        nonlocal remaining
        with lock:
            remaining -= 1
            if remaining == 0:
                _admission.release()

    for future in futures:
        future.add_done_callback(finished)


@app.get("/health")
def health():
    return jsonify(status="ok", service="bff-web")


@app.get("/v1/bff/dashboard")
def dashboard():
    """Una llamada del cliente reúne el catálogo de cursos y sus matrículas."""
    budget = Budget(BFF_TIMEOUT)
    if not _admission.acquire(blocking=False):
        return (jsonify(error={"code": "BFF_OVERLOADED",
                               "message": "Panel ocupado; reintenta en unos segundos"}),
                503, {"Retry-After": "1"})
    headers = {name: request.headers[name] for name in ("Authorization", "X-API-Key")
               if name in request.headers}
    futures = []
    try:
        courses = _pool.submit(_cursos_disponibles, budget)
        futures.append(courses)
        enrollments = _pool.submit(_get, "/v1/enrollments?limit=100", headers, budget)
        futures.append(enrollments)
        done, pending = wait(futures, timeout=budget.remaining(), return_when=FIRST_EXCEPTION)
        # Propagar una falla ya conocida sin esperar al otro servicio lento.
        for future in done:
            if future.exception() is not None:
                future.result()
        if pending:
            raise FuturesTimeout()
        available_courses = courses.result(timeout=budget.remaining())
        enrollment_response = enrollments.result(timeout=budget.remaining())
    except FuturesTimeout:
        return jsonify(error={
            "code": "BFF_DEPENDENCY_TIMEOUT",
            "message": "No fue posible completar el panel; reintenta en unos segundos",
        }), 504
    except grpc.RpcError as exc:
        status = 504 if exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED else 503
        code = "BFF_DEPENDENCY_TIMEOUT" if status == 504 else "CUPOS_UNAVAILABLE"
        return jsonify(error={"code": code, "message": "No fue posible consultar Cupos"}), status
    except httpx.TimeoutException:
        return jsonify(error={"code": "BFF_DEPENDENCY_TIMEOUT",
                              "message": "Inscripción excedió el tiempo límite"}), 504
    except httpx.RequestError:
        return jsonify(error={"code": "INSCRIPCION_UNAVAILABLE",
                              "message": "No fue posible consultar Inscripción"}), 502
    finally:
        budget.cancel()  # Cancela los transportes que ya están en curso.
        for future in futures:
            future.cancel()  # Evita iniciar los trabajos que sigan en cola.
        _release_when_done(futures)

    if enrollment_response.is_error:
        failed = enrollment_response
        try:
            detail = failed.json()
        except ValueError:
            detail = {"message": "El servicio de matrículas devolvió una respuesta no válida"}
        return jsonify(error={"code": "BFF_UPSTREAM_ERROR", "upstream_status": failed.status_code,
                              "details": detail}), failed.status_code

    return jsonify({
        "cursos": available_courses,
        "matriculas": enrollment_response.json(),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
