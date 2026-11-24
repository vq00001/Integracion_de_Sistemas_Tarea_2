"""H4, fase RED: deadline total y recuperación de los workers del BFF.

Flask, ThreadPoolExecutor y el cliente HTTP productivos; servidor HTTP local que
entrega el cuerpo poco a poco, sin superar el timeout de inactividad de requests.
Cupos es un doble que respeta el deadline recibido. No requiere Docker ni stubs
generados. Las esperas externas de 3 s son watchdogs; los límites del BFF se
comprueban aparte, con margen de scheduling. Sin xfail ni skips.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import grpc
import pytest
import requests

from tests.integration.test_gateway_bff import bff  # noqa: F401


BUDGET = 0.6
SCHEDULING_MARGIN = 0.2
RECOVERY_MARGIN = 0.35
ENROLLMENTS = b'{"items":[],"total":0}'


@pytest.fixture(autouse=True)
def _configuracion_h4(monkeypatch):
    # Configurar antes de importar el módulo y construir su pool.
    monkeypatch.setenv('BFF_UPSTREAM_TIMEOUT_SECONDS', str(BUDGET))
    monkeypatch.setenv('BFF_MAX_WORKERS', '2')


class DeadlineExceeded(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.DEADLINE_EXCEEDED


@pytest.fixture
def servicios(bff, monkeypatch):
    state = SimpleNamespace(
        bff=bff, slow_rpc=False, slow_http=False,
        rpc_started=threading.Event(), rpc_release=threading.Event(),
        http_started=threading.Event(), http_release=threading.Event(),
        lock=threading.Lock(), calls=[],
    )

    def courses(_request, *, timeout):
        with state.lock:
            state.calls.append(('grpc', timeout))
        state.rpc_started.set()
        if state.slow_rpc and not state.rpc_release.wait(timeout):
            raise DeadlineExceeded()
        return SimpleNamespace(cursos=[SimpleNamespace(
            curso_id=7, nombre='Redes', cupos_totales=20, cupos_libres=4)])

    class Enrollments(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            with state.lock:
                state.calls.append(('http', self.path, dict(self.headers)))
            slow = state.slow_http
            # JSON válido seguido de espacios: el receptor debe leer todo el
            # Content-Length. Cada byte llega antes de su timeout de lectura.
            payload = ENROLLMENTS + (b' ' * 65536 if slow else b'')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            state.http_started.set()
            try:
                sent = 0
                if slow:
                    self.wfile.write(ENROLLMENTS)
                    self.wfile.flush()
                    sent = len(ENROLLMENTS)
                    while sent < len(payload) and not state.http_release.wait(0.02):
                        self.wfile.write(payload[sent:sent+1])
                        self.wfile.flush()
                        sent += 1
                # El teardown termina también las llamadas que Future.cancel()
                # no pudo interrumpir, para no dejar hilos colgados en fase RED.
                self.wfile.write(payload[sent:])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # El cliente cerró el transporte al vencer su presupuesto.

    server = ThreadingHTTPServer(('127.0.0.1', 0), Enrollments)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    worker.start()
    monkeypatch.setattr(bff, 'MATRICULAS_API_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setattr(bff, 'CUPOS_STUB', SimpleNamespace(listarCursosDisponibles=courses))
    try:
        yield state
    finally:
        state.rpc_release.set()
        state.http_release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def _dashboard(bff, headers=None):
    with bff.app.test_client() as client:
        return client.get('/v1/bff/dashboard', headers=headers or {})


def _capacidad_completa_disponible(pool):
    # Dos sondas deben ejecutarse simultáneamente: un único worker libre no
    # oculta que la llamada HTTP vencida sigue reteniendo el otro.
    rendezvous = threading.Barrier(2)
    probes = [pool.submit(rendezvous.wait, 3) for _ in range(2)]
    try:
        done, pending = wait(probes, timeout=RECOVERY_MARGIN)
        return not pending and all(f.exception() is None for f in done)
    finally:
        rendezvous.abort()


def test_h4_dos_dependencias_lentas_respetan_un_solo_deadline_total(servicios):
    s = servicios
    s.slow_rpc = s.slow_http = True
    with ThreadPoolExecutor(max_workers=1) as caller:
        start = time.monotonic()
        future = caller.submit(_dashboard, s.bff)
        try:
            assert s.rpc_started.wait(3), 'No comenzó la consulta de Cupos'
            assert s.http_started.wait(3), 'No comenzó la consulta de Inscripción'
            # Cupos consume parte del presupuesto y responde. La espera del
            # resultado REST debe usar el restante, no un nuevo plazo completo.
            s.rpc_release.wait(BUDGET * 0.6)
            s.rpc_release.set()
            remaining = max(0, start + BUDGET + SCHEDULING_MARGIN - time.monotonic())
            done, _ = wait([future], timeout=remaining)
            within_budget = bool(done)
            elapsed = time.monotonic() - start
        finally:
            s.rpc_release.set()
            s.http_release.set()
        response = future.result(timeout=3)
    assert within_budget, (
        f'El dashboard seguía esperando a los {elapsed:.3f}s; '
        f'presupuesto total {BUDGET}s + margen {SCHEDULING_MARGIN}s')
    assert response.status_code == 504
    assert response.json['error']['code'] == 'BFF_DEPENDENCY_TIMEOUT'


def test_h4_timeout_libera_workers_aunque_http_siga_entregando_bytes(servicios):
    s = servicios
    s.slow_rpc = s.slow_http = True
    response = _dashboard(s.bff)
    assert response.status_code == 504
    assert response.json['error']['code'] == 'BFF_DEPENDENCY_TIMEOUT'
    assert s.rpc_started.is_set() and s.http_started.is_set()
    # El servidor sigue dispuesto a transmitir. La liberación debe proceder
    # del cliente BFF y su presupuesto, no de que el test desbloquee el servidor.
    assert not s.http_release.is_set()
    assert _capacidad_completa_disponible(s.bff._pool), (
        'Tras el 504 sigue ocupado un worker: cancelar el futuro no terminó la llamada HTTP')
    s.slow_rpc = s.slow_http = False
    recovered = _dashboard(s.bff)
    assert recovered.status_code == 200, recovered.json
    assert recovered.json['matriculas'] == {'items': [], 'total': 0}


@pytest.mark.parametrize('dependency', ['grpc', 'http'])
def test_h4_timeout_de_transporte_deja_margen_al_deadline_de_agregacion(servicios, monkeypatch, dependency):
    s = servicios
    http_timeouts = []

    def get(_url, **kwargs):
        http_timeouts.append(kwargs.get('timeout'))
        response = requests.Response()
        response.status_code = 200
        response._content = ENROLLMENTS
        response._content_consumed = True
        return response

    monkeypatch.setattr(s.bff.requests, 'get', get)
    response = _dashboard(s.bff)
    assert response.status_code == 200
    with s.lock:
        rpc_timeouts = [call[1] for call in s.calls if call[0] == 'grpc']
    received = rpc_timeouts if dependency == 'grpc' else http_timeouts
    assert received, f'No se llamó a {dependency}'
    for timeout in received:
        limits = timeout if isinstance(timeout, tuple) else (timeout,)
        assert all(value is not None and 0 < value < BUDGET for value in limits), (
            f'{dependency} usa {timeout}; debe dejar margen antes del deadline total {BUDGET}')


def test_h4_control_pool_ocupado_no_inicia_trabajo_despues_de_responder(servicios):
    s = servicios
    release = threading.Event()
    started = threading.Barrier(3)

    def occupy():
        started.wait(timeout=3)
        assert release.wait(3), 'El test no liberó el pool ocupado'

    holders = [s.bff._pool.submit(occupy) for _ in range(2)]
    try:
        started.wait(timeout=3)
        response = _dashboard(s.bff)
        # Se puede agotar el deadline de cola o rechazar la sobrecarga antes.
        assert response.status_code in (429, 503, 504)
        if response.status_code != 504:
            assert int(response.headers['Retry-After']) >= 1
        with s.lock:
            assert s.calls == []
    finally:
        release.set()
        for future in holders:
            future.result(timeout=3)
    assert _capacidad_completa_disponible(s.bff._pool)
    with s.lock:
        assert s.calls == [], 'Se inició trabajo de un dashboard que ya había respondido con error'


def test_h4_control_agregacion_sana_conserva_datos_y_credenciales(servicios):
    s = servicios
    response = _dashboard(s.bff, {
        'Authorization': 'Bearer alumno-test', 'X-API-Key': 'm2m-test', 'X-Ignore': 'no-enviar'})
    assert response.status_code == 200
    assert response.json == {
        'cursos': {'items': [{'curso_id': 7, 'nombre': 'Redes', 'cupos_totales': 20, 'cupos_libres': 4}]},
        'matriculas': {'items': [], 'total': 0},
    }
    with s.lock:
        http_calls = [call for call in s.calls if call[0] == 'http']
    assert len(http_calls) == 1
    _, path, headers = http_calls[0]
    assert path == '/v1/enrollments?limit=100'
    assert headers['Authorization'] == 'Bearer alumno-test'
    assert headers['X-API-Key'] == 'm2m-test'
    assert 'X-Ignore' not in headers
