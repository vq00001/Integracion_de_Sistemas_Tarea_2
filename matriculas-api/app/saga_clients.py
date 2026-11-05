"""Adaptadores: solo ids, monto y claves opacas cruzan límites de servicios."""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import grpc
from .circuit_breaker import CircuitBreaker, CircuitState
from .grpc_stubs import cupos_pb2 as pb, cupos_pb2_grpc


class BusinessError(Exception):
    business = True


class IdempotencyConflict(BusinessError):
    pass


class DependencyFailure(Exception):
    pass


class Client:
    def __init__(self, operation, compensation, timeout=2, capacity=8, threshold=5, recovery=15):
        self.operation, self.compensation, self.timeout = operation, compensation, timeout
        self.breaker = CircuitBreaker(threshold, recovery)
        self.semaphore = threading.BoundedSemaphore(capacity)
        self.rejected = 0
        self.seconds = 0.0
        self.calls = 0
        self.lock = threading.Lock()

    @property
    def available(self):
        return self.breaker.state != CircuitState.OPEN

    def call(self, fn, *args):
        if not self.semaphore.acquire(blocking=False):
            with self.lock:
                self.rejected += 1
            raise DependencyFailure("Bulkhead lleno")
        start = time.monotonic()
        try:
            if not self.breaker.allow_request():
                raise DependencyFailure("Circuito abierto")
            try:
                result = fn(*args, timeout=self.timeout)
            except BusinessError:
                self.breaker.record_success()
                raise
            except Exception as exc:
                self.breaker.record_failure()
                raise DependencyFailure(type(exc).__name__) from exc
            self.breaker.record_success()
            return result
        finally:
            with self.lock:
                self.seconds += time.monotonic()-start
                self.calls += 1
            self.semaphore.release()

    def forward(self, saga, key):
        return self.call(self.operation, saga, key)

    def compensate(self, saga, key, original):
        return self.call(self.compensation, saga, key, original)


def http(method, url, payload, key, timeout, internal=False):
    headers = {"Content-Type": "application/json", "Idempotency-Key": key,
               "X-API-Key": os.environ.get("SERVICE_API_KEY", "").split(",")[0].strip()}
    if internal:
        headers["X-Internal-Key"] = os.environ.get("INTERNAL_KEY", "")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 422:
            raise IdempotencyConflict() from exc
        if 400 <= exc.code < 500 and exc.code not in (408, 429):
            raise BusinessError() from exc
        raise DependencyFailure() from exc


def clients_from_env():
    ins = os.getenv("INSCRIPCION_URL", "http://matriculas-api:8000").rstrip("/")
    pay = os.getenv("PAGOS_URL", "http://pagos:8000").rstrip("/")
    channel = grpc.insecure_channel(os.getenv("CUPOS_GRPC_TARGET", "cupos:50051"))
    stub = cupos_pb2_grpc.CuposStub(channel)

    def reserve(s, key, timeout):
        try:
            result = stub.ocuparCupo(pb.OcuparRequest(curso_id=s["curso_id"], idempotency_key=key), timeout=timeout)
        except grpc.RpcError as exc:
            if exc.code() in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.FAILED_PRECONDITION):
                raise BusinessError() from exc
            raise
        if result.estado in (pb.SIN_CUPOS, pb.CURSO_NO_ENCONTRADO):
            raise BusinessError()
        if result.estado != pb.CUPO_OCUPADO:
            raise DependencyFailure()

    def release(s, key, original, timeout):
        result = stub.liberarCupo(pb.LiberarRequest(curso_id=s["curso_id"], idempotency_key=original), timeout=timeout)
        if result.estado != pb.CUPO_LIBERADO:
            raise BusinessError()

    def enroll(s, key, timeout):
        result = http("POST", ins+"/v1/enrollments", dict(student_id=s["alumno_id"], curso_id=s["curso_id"]), key, timeout)
        if result.get("estado") == "ANULADA":
            raise BusinessError()
        if result.get("estado") != "CONFIRMADA":
            raise DependencyFailure()

    def cancel(s, key, original, timeout):
        return http("POST", ins+"/internal/enrollments/cancel",
                    dict(student_id=s["alumno_id"], curso_id=s["curso_id"]), original, timeout, internal=True)

    def charge(s, key, timeout):
        result = http("POST", pay+"/v1/payments", dict(alumno_id=s["alumno_id"], curso_id=s["curso_id"], monto=s["monto"]), key, timeout)
        if result.get("estado") == "RECHAZADO":
            raise BusinessError()
        if result.get("estado") != "COBRADO":
            raise DependencyFailure()

    def refund(s, key, original, timeout):
        result = http("POST", pay+"/v1/payments/refund", dict(original_key=original), key, timeout)
        if result.get("estado") not in ("REEMBOLSADO", "INEXISTENTE"):
            raise DependencyFailure()

    options = dict(timeout=float(os.getenv("SAGA_TIMEOUT_SECONDS", "2")),
                   capacity=int(os.getenv("SAGA_BULKHEAD", "8")),
                   threshold=int(os.getenv("SAGA_CB_THRESHOLD", "5")),
                   recovery=float(os.getenv("SAGA_CB_RECOVERY_SECONDS", "15")))
    return {"RESERVAR": Client(reserve, release, **options), "COBRAR": Client(charge, refund, **options),
            "INSCRIBIR": Client(enroll, cancel, **options)}
