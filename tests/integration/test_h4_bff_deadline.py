"""Regresiones H4: deadline total y recuperación de los workers del BFF.

Flask, ThreadPoolExecutor y el cliente HTTP productivos; servidor HTTP local que
entrega el cuerpo poco a poco, sin superar el timeout de inactividad de HTTP.
Cupos es un servidor gRPC local controlado, con stubs generados temporalmente.
No requiere Docker. Las esperas externas de 3 s son watchdogs; los límites del BFF se
comprueban aparte, con margen de scheduling. Sin xfail ni skips.
"""
import importlib
import runpy
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import grpc
import pytest
import httpx

from tests.integration.test_gateway_bff import ROOT, bff  # noqa: F401


BUDGET = 0.6
SCHEDULING_MARGIN = 0.2
RECOVERY_MARGIN = 0.35
ENROLLMENTS = b'{"items":[],"total":0}'


@pytest.fixture(autouse=True)
def _configuracion_h4(monkeypatch):
    # Configurar antes de importar el módulo y construir su pool.
    monkeypatch.setenv('BFF_UPSTREAM_TIMEOUT_SECONDS', str(BUDGET))
    monkeypatch.setenv('BFF_MAX_WORKERS', '2')


@pytest.fixture(scope='module')
def proto(tmp_path_factory):
    folder = tmp_path_factory.mktemp('h4-grpc')
    runpy.run_path(str(ROOT / 'bff/generar_stubs.py'))['generar'](folder / 'h4_grpc_stubs')
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(folder))
        try:
            yield (importlib.import_module('h4_grpc_stubs.cupos_pb2'),
                   importlib.import_module('h4_grpc_stubs.cupos_pb2_grpc'))
        finally:
            for name in list(sys.modules):
                if name == 'h4_grpc_stubs' or name.startswith('h4_grpc_stubs.'):
                    sys.modules.pop(name)


@pytest.fixture
def servicios(bff, monkeypatch, proto):
    pb, rpc = proto
    state = SimpleNamespace(
        bff=bff, slow_rpc=False, slow_http=False, slow_headers=False, drop_http=False,
        rpc_started=threading.Event(), rpc_release=threading.Event(),
        http_started=threading.Event(), http_release=threading.Event(),
        rpc_finished=threading.Event(), rpc_cancelled=threading.Event(),
        http_disconnected=threading.Event(),
        lock=threading.Lock(), calls=[],
    )

    class Cupos(rpc.CuposServicer):
        def listarCursosDisponibles(self, request, context):
            state.rpc_started.set()
            cancelled = threading.Event()
            def stopped():
                cancelled.set()
                state.rpc_cancelled.set()
            context.add_callback(stopped)
            try:
                if state.slow_rpc:
                    while not state.rpc_release.wait(0.01):
                        if cancelled.is_set():
                            return pb.ListarCursosResponse()
                return pb.ListarCursosResponse(cursos=[pb.Curso(
                    curso_id=7, nombre='Redes', cupos_totales=20, cupos_libres=4)])
            finally:
                state.rpc_finished.set()

    rpc_pool = ThreadPoolExecutor(max_workers=2)
    grpc_server = grpc.server(rpc_pool)
    rpc.add_CuposServicer_to_server(Cupos(), grpc_server)
    port = grpc_server.add_insecure_port('127.0.0.1:0')
    grpc_server.start()
    channel = grpc.insecure_channel(f'127.0.0.1:{port}')
    stub = rpc.CuposStub(channel)

    class RecordingRpc:
        def future(self, request, *, timeout):
            with state.lock:
                state.calls.append(('grpc', timeout))
            return stub.listarCursosDisponibles.future(request, timeout=timeout)

    class Enrollments(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            with state.lock:
                state.calls.append(('http', self.path, dict(self.headers)))
            if state.drop_http:
                assert state.rpc_started.wait(3)
                state.http_started.set()
                self.close_connection = True
                return
            if state.slow_headers:
                try:
                    self.wfile.write(b'HTTP/1.0 200 OK\r\nX-Slow: ')
                    self.wfile.flush()
                    state.http_started.set()
                    while not state.http_release.wait(0.02):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                    self.wfile.write(b'\r\nContent-Length: ' + str(len(ENROLLMENTS)).encode()
                                     + b'\r\nContent-Type: application/json\r\n\r\n' + ENROLLMENTS)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    state.http_disconnected.set()
                return
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
                state.http_disconnected.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Enrollments)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    worker.start()
    monkeypatch.setattr(bff, 'MATRICULAS_API_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setattr(bff, 'cupos_pb2', pb)
    monkeypatch.setattr(bff, 'CUPOS_STUB', SimpleNamespace(listarCursosDisponibles=RecordingRpc()))
    try:
        yield state
    finally:
        state.rpc_release.set()
        state.http_release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        channel.close()
        grpc_server.stop(0).wait(timeout=3)
        rpc_pool.shutdown(wait=True, cancel_futures=True)


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
    assert s.http_disconnected.wait(RECOVERY_MARGIN), 'El cliente no cerró el transporte HTTP'
    s.slow_rpc = s.slow_http = False
    recovered = _dashboard(s.bff)
    assert recovered.status_code == 200, recovered.json
    assert recovered.json['matriculas'] == {'items': [], 'total': 0}


@pytest.mark.parametrize('dependency', ['grpc', 'http'])
def test_h4_timeout_de_transporte_deja_margen_al_deadline_de_agregacion(servicios, monkeypatch, dependency):
    s = servicios
    http_timeouts = []

    async def get(_client, _url, **kwargs):
        http_timeouts.append(kwargs.get('timeout'))
        return httpx.Response(200, content=ENROLLMENTS)

    monkeypatch.setattr(s.bff.httpx.AsyncClient, 'get', get)
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


def test_h4_deadline_cancela_http_tambien_durante_cabeceras_lentas(servicios):
    s = servicios
    s.slow_headers = True
    start = time.monotonic()
    response = _dashboard(s.bff)
    elapsed = time.monotonic() - start
    assert response.status_code == 504
    assert elapsed <= BUDGET + SCHEDULING_MARGIN
    assert s.http_started.is_set() and not s.http_release.is_set()
    assert _capacidad_completa_disponible(s.bff._pool)


def test_h4_falla_http_rapida_cancela_rpc_en_curso_y_recupera_capacidad(servicios):
    s = servicios
    s.slow_rpc, s.drop_http = True, True
    response = _dashboard(s.bff)
    assert response.status_code == 502
    assert response.json['error']['code'] == 'INSCRIPCION_UNAVAILABLE'
    assert not s.rpc_release.is_set()
    assert s.rpc_cancelled.wait(SCHEDULING_MARGIN), 'No se canceló la llamada gRPC en curso'
    assert s.rpc_finished.wait(SCHEDULING_MARGIN)
    assert _capacidad_completa_disponible(s.bff._pool)
    s.slow_rpc = s.drop_http = False
    assert _dashboard(s.bff).status_code == 200


def test_h4_sobrecarga_no_encola_mas_llamadas_y_se_recupera_tras_timeout(servicios):
    s = servicios
    s.slow_rpc = s.slow_http = True
    with ThreadPoolExecutor(max_workers=1) as caller:
        original = caller.submit(_dashboard, s.bff)
        assert s.rpc_started.wait(3)
        assert s.http_started.wait(3)
        with s.lock:
            before = list(s.calls)
        overloaded = _dashboard(s.bff)
        assert overloaded.status_code == 503
        assert overloaded.json['error']['code'] == 'BFF_OVERLOADED'
        assert int(overloaded.headers['Retry-After']) >= 1
        assert s.bff.app.test_client().get('/health').status_code == 200
        with s.lock:
            assert s.calls == before, 'La sobrecarga inició nuevas llamadas a dependencias'
        assert original.result(timeout=3).status_code == 504
    assert _capacidad_completa_disponible(s.bff._pool)
    s.slow_rpc = s.slow_http = False
    assert _dashboard(s.bff).status_code == 200


def test_h4_admision_se_libera_cuando_termina_el_trabajo_no_solo_al_responder(servicios, monkeypatch):
    s = servicios
    started, release = threading.Event(), threading.Event()
    calls = []

    def non_cooperative(*args):
        calls.append(args)
        started.set()
        assert release.wait(3)
        return httpx.Response(200, content=ENROLLMENTS)

    with monkeypatch.context() as patch:
        patch.setattr(s.bff, 'BFF_TIMEOUT', 0.05)
        patch.setattr(s.bff, '_get', non_cooperative)
        try:
            assert _dashboard(s.bff).status_code == 504
            assert started.is_set()
            # Cancelar un Future no detiene este doble deliberadamente hostil.
            # El BFF debe contenerlo sin admitir más trabajo sobre su capacidad.
            assert _dashboard(s.bff).status_code == 503
            assert len(calls) == 1
        finally:
            release.set()
        assert _capacidad_completa_disponible(s.bff._pool)
    assert _dashboard(s.bff).status_code == 200
