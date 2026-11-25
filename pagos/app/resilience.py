"""Resiliencia hacia Transbank: timeout (en el cliente), circuit breaker, bulkhead e inyección de
fallas para los experimentos (lentitud y caída de la dependencia frágil)."""
from __future__ import annotations

import threading
import time
from enum import Enum


class TransbankUnavailable(Exception):
    """Transbank no respondió bien: timeout, 5xx, red, circuito abierto o bulkhead lleno.
    El resultado es incierto salvo que seguro_no_enviado identifique un rechazo local."""

    def __init__(self, motivo: str = "no disponible", seguro_no_enviado: bool = False):
        super().__init__(motivo)
        self.motivo = motivo
        # True si no se inició el cliente (circuito, bulkhead o falla artificial previa).
        self.seguro_no_enviado = seguro_no_enviado


class TransbankRejected(Exception):
    """Transbank respondió con un error de negocio o de solicitud (4xx). No abre el circuito."""

    def __init__(self, http_status: int, detalle: str = ""):
        super().__init__(f"HTTP {http_status}")
        self.http_status = http_status
        self.detalle = detalle


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Mismo comportamiento que el breaker del orquestador: CLOSED → OPEN tras N fallas
    consecutivas; pasado el tiempo de recuperación deja pasar UNA sonda (HALF_OPEN)."""

    def __init__(self, threshold: int, recovery_seconds: float):
        self.threshold, self.recovery = threshold, recovery_seconds
        self._failures, self._state, self._opened_at, self._probe = 0, CircuitState.CLOSED, None, False
        self._lock = threading.Lock()

    def _half(self):
        if self._state is CircuitState.OPEN and time.monotonic() - self._opened_at >= self.recovery:
            self._state, self._probe = CircuitState.HALF_OPEN, False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._half()
            return self._state

    def allow_request(self) -> bool:
        with self._lock:
            self._half()
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.OPEN or self._probe:
                return False
            self._probe = True
            return True

    def record_success(self):
        with self._lock:
            self._failures, self._state, self._opened_at, self._probe = 0, CircuitState.CLOSED, None, False

    def record_failure(self):
        with self._lock:
            self._probe = False
            if self._state is CircuitState.HALF_OPEN:
                self._open()
                return
            self._failures += 1
            if self._failures >= self.threshold:
                self._open()

    def _open(self):
        self._state, self._opened_at, self._failures = CircuitState.OPEN, time.monotonic(), 0


class Faults:
    """Simulación local antes del cliente (PUT /internal/fault).
    Reproduce espera/caída para el circuito, sin acreditar tráfico hacia la pasarela."""

    def __init__(self):
        self.latencia_ms, self.falla = 0, False
        self._lock = threading.Lock()

    def set(self, latencia_ms: int = 0, falla: bool = False):
        with self._lock:
            self.latencia_ms, self.falla = max(0, int(latencia_ms)), bool(falla)

    def snapshot(self):
        with self._lock:
            return {"latencia_ms": self.latencia_ms, "falla": self.falla}

    def apply(self, timeout: float):
        f = self.snapshot()
        if f["falla"]:
            raise ConnectionError("falla inyectada")
        if f["latencia_ms"]:
            delay = f["latencia_ms"] / 1000
            if delay > timeout:
                time.sleep(timeout)
                raise TimeoutError("timeout inyectado")
            time.sleep(delay)


class Guard:
    """Bulkhead compartido → circuito independiente de la operación → llamada."""

    OPERATIONS = ("crear", "confirmar", "estado", "reembolsar")

    def __init__(self, threshold=5, recovery=15.0, capacity=8, faults: Faults | None = None):
        self._threshold, self._recovery = threshold, recovery
        self._breakers = {name: CircuitBreaker(threshold, recovery) for name in self.OPERATIONS}
        self.semaphore = threading.BoundedSemaphore(capacity)
        self.faults = faults or Faults()
        self.rejected = 0      # bulkhead lleno
        self.blocked = 0       # circuito abierto: la llamada ni siquiera se intentó
        self.attempted = 0     # inicios del cliente; no prueba recepción por la pasarela
        self.protected = 0     # admitidos por bulkhead y circuito, antes de la simulación
        self.simulated = 0     # interrumpidos en la simulación, sin iniciar el cliente
        self.calls = {}
        self.seconds = {}
        self.blocked_by_operation = {name: 0 for name in self.OPERATIONS}
        self.protected_by_operation = {name: 0 for name in self.OPERATIONS}
        self.simulated_by_operation = {name: 0 for name in self.OPERATIONS}
        self.started_by_operation = {name: 0 for name in self.OPERATIONS}
        self._lock = threading.Lock()

    def breaker_for(self, name: str) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(self._threshold, self._recovery)
                self.blocked_by_operation[name] = 0
                self.protected_by_operation[name] = 0
                self.simulated_by_operation[name] = 0
                self.started_by_operation[name] = 0
            return self._breakers[name]

    @property
    def breaker(self) -> CircuitBreaker:
        """Compatibilidad: el circuito histórico representa crear, no las cuatro operaciones."""
        return self.breaker_for("crear")

    @property
    def available(self) -> bool:
        return self.breaker.state is not CircuitState.OPEN

    def metrics_snapshot(self) -> dict:
        """Copias de los contadores; el exportador no recorre diccionarios mutables."""
        with self._lock:
            breakers = tuple(self._breakers.items())
            snapshot = dict(rejected=self.rejected, blocked=self.blocked, attempted=self.attempted,
                            protected=self.protected, simulated=self.simulated,
                            blocked_by_operation=dict(self.blocked_by_operation),
                            protected_by_operation=dict(self.protected_by_operation),
                            simulated_by_operation=dict(self.simulated_by_operation),
                            started_by_operation=dict(self.started_by_operation),
                            calls=dict(self.calls), seconds=dict(self.seconds))
        snapshot["circuits"] = {name: breaker.state.value for name, breaker in breakers}
        return snapshot

    def call(self, name: str, timeout: float, fn, *args):
        breaker = self.breaker_for(name)
        if not self.semaphore.acquire(blocking=False):
            with self._lock:
                self.rejected += 1
            raise TransbankUnavailable("bulkhead lleno", seguro_no_enviado=True)
        start = time.monotonic()
        try:
            if not breaker.allow_request():
                with self._lock:
                    self.blocked += 1
                    self.blocked_by_operation[name] += 1
                raise TransbankUnavailable("circuito abierto", seguro_no_enviado=True)
            with self._lock:
                self.protected += 1
                self.protected_by_operation[name] += 1
            try:
                self.faults.apply(timeout)
            except Exception as exc:
                with self._lock:
                    self.simulated += 1
                    self.simulated_by_operation[name] += 1
                breaker.record_failure()
                raise TransbankUnavailable(type(exc).__name__, seguro_no_enviado=True) from exc
            with self._lock:
                self.attempted += 1
                self.started_by_operation[name] += 1
            try:
                result = fn(*args)
            except TransbankRejected:
                breaker.record_success()
                raise
            except TransbankUnavailable:
                breaker.record_failure()
                raise
            except Exception as exc:  # timeout, red caída, etc.
                breaker.record_failure()
                raise TransbankUnavailable(type(exc).__name__) from exc
            breaker.record_success()
            return result
        finally:
            with self._lock:
                self.calls[name] = self.calls.get(name, 0) + 1
                self.seconds[name] = self.seconds.get(name, 0.0) + time.monotonic() - start
            self.semaphore.release()
