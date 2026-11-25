"""H11: éxitos de otras operaciones no cuentan como recuperación de crear.

Se ejercitan Guard y Webpay reales. El doble del cliente controla las respuestas,
un reloj local adelanta la recuperación y eventos retienen una única sonda.
No se exige una estructura interna nueva ni se modifica time.monotonic global.
"""
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from pagos.app import resilience
from pagos.app.resilience import Guard, TransbankRejected, TransbankUnavailable
from pagos.app.transbank import Webpay


AUXILIARES = ("estado", "confirmar", "reembolsar")


class Reloj:
    def __init__(self):
        self.now = 100.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def avanzar(self, seconds):
        with self.lock:
            self.now += seconds


class ClienteControlado:
    timeout, commit_timeout = 1.0, 2.0

    def __init__(self, reloj):
        self.reloj = reloj
        self.calls = Counter()
        self.modos = {}
        self.lock = threading.Lock()
        self.retener_siguiente = False
        self.entrada, self.liberar = threading.Event(), threading.Event()

    def _call(self, operacion):
        with self.lock:
            self.calls[operacion] += 1
            retener = operacion == "crear" and self.retener_siguiente
            if retener:
                self.retener_siguiente = False
        if retener:
            self.entrada.set()
            assert self.liberar.wait(timeout=10), "La prueba no liberó la sonda"
        if self.modos.get(operacion) == "timeout":
            self.reloj.avanzar(self.timeout)
            raise TimeoutError(f"{operacion} agotó su timeout")
        if self.modos.get(operacion) == "rechazo":
            raise TransbankRejected(422, "Rechazo de negocio del simulador")

    def crear(self, *args):
        self._call("crear")
        return {"token": "token-h11", "url": "http://webpay.test/formulario"}

    def estado(self, token):
        self._call("estado")
        return {"status": "AUTHORIZED"}

    def confirmar(self, token):
        self._call("confirmar")
        return {"status": "AUTHORIZED", "response_code": 0}

    def reembolsar(self, token, amount):
        self._call("reembolsar")
        return {"type": "REVERSED", "response_code": 0}


@pytest.fixture
def entorno(monkeypatch):
    reloj = Reloj()

    def sin_sleep(_):
        pytest.fail("La recuperación debe probarse con reloj virtual")

    monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=reloj, sleep=sin_sleep))
    client = ClienteControlado(reloj)
    guard = Guard(threshold=5, recovery=30, capacity=8)
    yield SimpleNamespace(reloj=reloj, client=client, guard=guard, webpay=Webpay(client, guard))
    client.liberar.set()


def _llamar(env, operacion):
    if operacion == "crear":
        return env.webpay.crear("orden-h11", "sesion-h11", 15000, "http://gateway.test/retorno")
    if operacion == "reembolsar":
        return env.webpay.reembolsar("token-h11", 15000)
    return getattr(env.webpay, operacion)("token-h11")


def _fallar_creacion(env):
    env.client.modos["crear"] = "timeout"
    with pytest.raises(TransbankUnavailable) as failure:
        _llamar(env, "crear")
    assert not failure.value.seguro_no_enviado, "La llamada de prueba debe llegar al cliente"


def _rechazar_creacion_localmente(env):
    before = env.client.calls["crear"]
    failure = None
    try:
        _llamar(env, "crear")
    except TransbankUnavailable as exc:
        failure = exc
    assert env.client.calls["crear"] == before, "El circuito dejó pasar otra creación al cliente"
    assert failure is not None and failure.seguro_no_enviado


def _abrir_con_creaciones(env):
    for _ in range(5):
        _fallar_creacion(env)
    env.client.modos["crear"] = "normal"
    _rechazar_creacion_localmente(env)


@pytest.mark.parametrize("auxiliar", AUXILIARES)
def test_h11_cinco_fallas_de_crear_abren_aunque_otras_operaciones_tengan_exito(entorno, auxiliar):
    for _ in range(5):
        _fallar_creacion(entorno)
        _llamar(entorno, auxiliar)
    assert entorno.client.calls["crear"] == entorno.client.calls[auxiliar] == 5
    entorno.client.modos["crear"] = "normal"
    _rechazar_creacion_localmente(entorno)


@pytest.mark.parametrize("auxiliar", AUXILIARES)
def test_h11_respuesta_4xx_de_otra_operacion_tampoco_limpia_fallas_de_crear(entorno, auxiliar):
    entorno.client.modos[auxiliar] = "rechazo"
    for _ in range(5):
        _fallar_creacion(entorno)
        with pytest.raises(TransbankRejected):
            _llamar(entorno, auxiliar)
    entorno.client.modos["crear"] = "normal"
    _rechazar_creacion_localmente(entorno)


@pytest.mark.parametrize("auxiliar", AUXILIARES)
def test_h11_crear_abierto_permite_otras_operaciones_sin_cerrar_el_circuito(entorno, auxiliar):
    _abrir_con_creaciones(entorno)
    _llamar(entorno, auxiliar)
    assert entorno.client.calls[auxiliar] == 1
    _rechazar_creacion_localmente(entorno)


@pytest.mark.parametrize("auxiliar", AUXILIARES)
def test_h11_fallas_ajenas_no_abren_el_circuito_de_crear(entorno, auxiliar):
    entorno.client.modos[auxiliar] = "timeout"
    for _ in range(5):
        with pytest.raises(TransbankUnavailable) as failure:
            _llamar(entorno, auxiliar)
        assert not failure.value.seguro_no_enviado
    assert entorno.client.calls[auxiliar] == 5
    assert _llamar(entorno, "crear")["token"] == "token-h11"
    assert entorno.client.calls["crear"] == 1


@pytest.mark.parametrize("auxiliar", AUXILIARES)
def test_h11_exito_ajeno_no_sustituye_la_sonda_half_open_de_crear(entorno, auxiliar):
    _abrir_con_creaciones(entorno)
    entorno.reloj.avanzar(31)
    _llamar(entorno, auxiliar)
    entorno.client.retener_siguiente = True
    with ThreadPoolExecutor(1) as executor:
        probe = executor.submit(_llamar, entorno, "crear")
        try:
            assert entorno.client.entrada.wait(timeout=5), "No entró la sonda de crear"
            _rechazar_creacion_localmente(entorno)  # solo una sonda hasta resolver esa operación
        finally:
            entorno.client.liberar.set()
        assert probe.result(timeout=5)["token"] == "token-h11"
    assert _llamar(entorno, "crear")["token"] == "token-h11"


@pytest.mark.parametrize("auxiliar", AUXILIARES)
@pytest.mark.parametrize("resultado_sonda", ["normal", "timeout"], ids=["recuperacion", "reapertura"])
def test_h11_operaciones_sanas_no_interfieren_con_la_sonda_en_curso(entorno, auxiliar, resultado_sonda):
    _abrir_con_creaciones(entorno)
    entorno.reloj.avanzar(31)
    entorno.client.modos["crear"] = resultado_sonda
    entorno.client.retener_siguiente = True
    with ThreadPoolExecutor(1) as executor:
        probe = executor.submit(_llamar, entorno, "crear")
        try:
            assert entorno.client.entrada.wait(timeout=5)
            _llamar(entorno, auxiliar)
            assert entorno.client.calls[auxiliar] == 1
            _rechazar_creacion_localmente(entorno)
        finally:
            entorno.client.liberar.set()
        if resultado_sonda == "timeout":
            with pytest.raises(TransbankUnavailable):
                probe.result(timeout=5)
        else:
            assert probe.result(timeout=5)["token"] == "token-h11"
    if resultado_sonda == "timeout":
        entorno.client.modos["crear"] = "normal"
        _rechazar_creacion_localmente(entorno)
        _llamar(entorno, auxiliar)
        _rechazar_creacion_localmente(entorno)
        entorno.reloj.avanzar(31)
    assert _llamar(entorno, "crear")["token"] == "token-h11"


def test_h11_control_exito_de_crear_si_reinicia_sus_propias_fallas(entorno):
    for _ in range(2):
        for _ in range(4):
            _fallar_creacion(entorno)
        entorno.client.modos["crear"] = "normal"
        assert _llamar(entorno, "crear")["token"] == "token-h11"
    assert entorno.client.calls["crear"] == 10


def test_h11_control_rechazo_de_negocio_de_crear_no_cuenta_como_caida(entorno):
    for _ in range(4):
        _fallar_creacion(entorno)
    entorno.client.modos["crear"] = "rechazo"
    with pytest.raises(TransbankRejected):
        _llamar(entorno, "crear")
    for _ in range(4):
        _fallar_creacion(entorno)
    entorno.client.modos["crear"] = "normal"
    assert _llamar(entorno, "crear")["token"] == "token-h11"


def test_h11_control_half_open_admite_una_sonda_y_recupera_crear(entorno):
    _abrir_con_creaciones(entorno)
    entorno.reloj.avanzar(31)
    entorno.client.retener_siguiente = True
    with ThreadPoolExecutor(1) as executor:
        probe = executor.submit(_llamar, entorno, "crear")
        try:
            assert entorno.client.entrada.wait(timeout=5)
            _rechazar_creacion_localmente(entorno)
        finally:
            entorno.client.liberar.set()
        assert probe.result(timeout=5)["token"] == "token-h11"
    assert _llamar(entorno, "crear")["token"] == "token-h11"
