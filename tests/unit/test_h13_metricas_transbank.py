"""H13 RED: distinguir admisión protegida, falla simulada e inicio del cliente.

El contador histórico attempted/intentos no debe anticipar la ejecución del cliente.
Las métricas explícitas se exportan por operación; el muestreo conserva tanto el
total como cada operación y mantiene desconocida la telemetría ausente.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests

from pagos.app import resilience
from pagos.app.resilience import Guard, TransbankRejected, TransbankUnavailable
from pagos.app.transbank import Webpay
from tests.integration.test_h9_idempotencia_pagos import PAYLOAD, _post, pagos
from tests.unit.test_h11_breaker_transbank import ClienteControlado, Reloj, _llamar
from tests.unit.test_saga_fallas_experimento import exp


OPS = ("crear", "confirmar", "estado", "reembolsar")
METRICAS = {
    "protegidos": "transbank_intentos_protegidos_total",
    "simuladas": "transbank_fallas_simuladas_total",
    "cliente": "transbank_cliente_llamadas_iniciadas_total",
}
RAW = {
    "protegidos": "pagos_tbk_protegidos",
    "simuladas": "pagos_tbk_fallas_simuladas",
    "cliente": "pagos_tbk_cliente_inicios",
}


@pytest.fixture
def env_factory(monkeypatch):
    envs = []

    def crear(threshold=5):
        reloj = Reloj()
        monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=reloj, sleep=reloj.avanzar))
        client = ClienteControlado(reloj)
        guard = Guard(threshold=threshold, recovery=30, capacity=8)
        env = SimpleNamespace(reloj=reloj, client=client, guard=guard, webpay=Webpay(client, guard))
        envs.append(env)
        return env

    yield crear
    for env in envs:
        env.client.liberar.set()


@pytest.mark.parametrize("operacion", OPS)
@pytest.mark.parametrize("falla", ["caida", "timeout"])
def test_h13_falla_simulada_antes_del_cliente_no_cuenta_como_intento_iniciado(env_factory, operacion, falla):
    env = env_factory()
    env.guard.faults.set(falla=falla == "caida", latencia_ms=3000 if falla == "timeout" else 0)
    with pytest.raises(TransbankUnavailable):
        _llamar(env, operacion)
    assert env.client.calls[operacion] == 0
    assert env.guard.attempted == 0, "Se contó un inicio de cliente aunque Faults.apply impidió ejecutarlo"


def test_h13_circuito_abierto_no_agrega_intentos_a_fallas_que_nunca_llegaron_al_cliente(env_factory):
    env = env_factory()
    env.guard.faults.set(falla=True)
    for _ in range(5):
        with pytest.raises(TransbankUnavailable):
            _llamar(env, "crear")
    with pytest.raises(TransbankUnavailable) as failure:
        _llamar(env, "crear")
    assert failure.value.seguro_no_enviado and env.guard.blocked == 1
    assert env.client.calls["crear"] == 0
    assert env.guard.attempted == 0, "El contador acumuló cinco fallas sintéticas como llamadas al cliente"


@pytest.mark.parametrize("resultado", ["normal", "timeout", "rechazo"])
def test_h13_control_una_llamada_iniciada_cuenta_aunque_su_respuesta_falle(env_factory, resultado):
    env = env_factory()
    env.client.modos["crear"] = resultado
    if resultado == "normal":
        assert _llamar(env, "crear")["token"] == "token-h11"
    else:
        exception = TransbankRejected if resultado == "rechazo" else TransbankUnavailable
        with pytest.raises(exception):
            _llamar(env, "crear")
    assert env.guard.attempted == env.client.calls["crear"] == 1


def test_h13_esperar_en_la_inyeccion_no_anticipa_el_inicio_del_cliente(env_factory, monkeypatch):
    env = env_factory()
    entrada, liberar = threading.Event(), threading.Event()
    apply_real = env.guard.faults.apply

    def aplicar(timeout):
        entrada.set()
        assert liberar.wait(timeout=10), "La prueba no liberó la etapa de simulación"
        apply_real(timeout)

    monkeypatch.setattr(env.guard.faults, "apply", aplicar)
    with ThreadPoolExecutor(1) as executor:
        futuro = executor.submit(_llamar, env, "crear")
        try:
            assert entrada.wait(timeout=5)
            assert env.client.calls["crear"] == 0
            assert env.guard.attempted == 0, "Se publicó un inicio antes de entrar al cliente"
        finally:
            liberar.set()
        assert futuro.result(timeout=5)["token"] == "token-h11"
    assert env.guard.attempted == env.client.calls["crear"] == 1


def test_h13_control_cuenta_al_entrar_al_cliente_sin_esperar_la_respuesta(env_factory):
    env = env_factory()
    env.client.retener_siguiente = True
    with ThreadPoolExecutor(1) as executor:
        futuro = executor.submit(_llamar, env, "crear")
        try:
            assert env.client.entrada.wait(timeout=5)
            assert not futuro.done()
            assert env.guard.attempted == env.client.calls["crear"] == 1
        finally:
            env.client.liberar.set()
        futuro.result(timeout=5)
    assert env.guard.attempted == 1


@pytest.mark.parametrize("simulada", [True, False], ids=["sin-cliente", "con-cliente"])
def test_h13_concurrencia_conserva_la_cantidad_de_inicios_reales_del_cliente(env_factory, simulada):
    env = env_factory(threshold=100)
    env.guard.faults.set(falla=simulada)
    inicio = threading.Barrier(8)

    def solicitud():
        inicio.wait(timeout=10)
        if simulada:
            with pytest.raises(TransbankUnavailable):
                _llamar(env, "crear")
        else:
            _llamar(env, "crear")

    with ThreadPoolExecutor(8) as executor:
        list(executor.map(lambda _: solicitud(), range(8)))
    assert env.client.calls["crear"] == (0 if simulada else 8)
    assert env.guard.attempted == env.client.calls["crear"], "La concurrencia mezcló simulaciones con inicios de cliente"


@pytest.mark.parametrize("perfil", ["normal", "caida_simulada", "timeout_simulado", "latencia_corta", "http_503"])
def test_h13_exportador_distingue_protegidos_simulaciones_e_inicios_por_operacion(pagos, exp, monkeypatch, perfil):
    reloj = Reloj()
    monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=reloj, sleep=reloj.avanzar))
    client = pagos.service.webpay.client
    monkeypatch.setattr(client, "timeout", 1.0)
    crear_real = client.crear
    inicios = []

    def crear(*args):
        inicios.append(args)
        return crear_real(*args)

    monkeypatch.setattr(client, "crear", crear)
    if perfil == "caida_simulada":
        pagos.service.webpay.guard.faults.set(falla=True)
    elif perfil == "timeout_simulado":
        pagos.service.webpay.guard.faults.set(latencia_ms=2000)
    elif perfil == "latencia_corta":
        pagos.service.webpay.guard.faults.set(latencia_ms=10)
    elif perfil == "http_503":
        pagos.fake.config(falla=True)
    response = _post(pagos.client, "/v1/payments", "h13-medicion:COBRAR", PAYLOAD)
    assert response.status_code in (201, 502, 503), response.text
    simulada = perfil in ("caida_simulada", "timeout_simulado")
    assert len(inicios) == int(not simulada)
    metrics = exp.parsear_metricas(pagos.client.get("/metrics").text)
    assert exp._valor(metrics, "transbank_intentos_total") == len(inicios), "El alias histórico sigue contando una falla previa al cliente"
    expected = {"protegidos": 1, "simuladas": int(simulada), "cliente": len(inicios)}
    for kind, family in METRICAS.items():
        series = {labels["operacion"]: value for name, labels, value in metrics if name == family}
        assert set(series) == set(OPS), f"Falta telemetría explícita de {family} para todas las operaciones"
        assert series == {op: expected[kind] if op == "crear" else 0 for op in OPS}


def _respuesta(text, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    return response


def _telemetria(valores):
    lines = ['circuito_estado{dependencia="TRANSBANK",estado="OPEN"} 1',
             'transbank_intentos_total 999']  # no se usa para inferir los contadores explícitos
    for kind, family in METRICAS.items():
        for op, count in zip(OPS, valores[kind]):
            lines.append(f'{family}{{operacion="{op}"}} {count}')
    for op, state in zip(OPS, ("OPEN", "HALF_OPEN", "CLOSED", "CLOSED")):
        lines.append(f'transbank_circuito_estado{{operacion="{op}",estado="{state}"}} 1')
    return "\n".join(lines)


def _muestrear(exp, monkeypatch, texto):
    stack = SimpleNamespace(orq="http://orq.h13", pagos="http://pagos.h13")
    monkeypatch.setattr(exp.requests, "get", lambda url, **kwargs: _respuesta(texto if url.startswith(stack.pagos) else ""))
    return exp.muestra_metricas(stack)


@pytest.mark.parametrize("cero", [False, True], ids=["valores-distintos", "cero-medido"])
def test_h13_muestreo_conserva_totales_y_operaciones_sin_confundir_las_tres_etapas(exp, monkeypatch, cero):
    valores = {"protegidos": (4, 3, 1, 2), "simuladas": (2, 0, 0, 2), "cliente": (2, 3, 1, 0)}
    if cero:
        valores = {kind: (0, 0, 0, 0) for kind in METRICAS}
    row = _muestrear(exp, monkeypatch, _telemetria(valores))
    for kind, column in RAW.items():
        assert row.get(column) == sum(valores[kind]), f"Se perdió o confundió {column}"
        for op, value in zip(OPS, valores[kind]):
            assert row.get(f"{column}_{op}") == value


def test_h13_control_contador_legacy_no_inventa_inicios_de_cliente(exp, monkeypatch):
    row = _muestrear(exp, monkeypatch, "transbank_intentos_total 999")
    assert all(row.get(column) is None for column in RAW.values())


def test_h13_metrica_parcial_no_se_completa_con_ceros_ni_con_el_contador_legacy(exp, monkeypatch):
    valores = {"protegidos": (4, 3, 1, 2), "simuladas": (2, 0, 0, 2), "cliente": (2, 3, 1, 0)}
    text = _telemetria(valores).replace(
        f'{METRICAS["cliente"]}{{operacion="reembolsar"}} 0', "")
    row = _muestrear(exp, monkeypatch, text)
    assert row.get(RAW["cliente"]) is None
    assert row.get(RAW["cliente"] + "_reembolsar") is None
    assert row.get(RAW["cliente"] + "_crear") == 2, "Se descartó telemetría válida o se sustituyó por el alias legacy"


def test_h13_muestreo_conserva_los_cuatro_circuitos_de_h11(exp, monkeypatch):
    valores = {kind: (0, 0, 0, 0) for kind in METRICAS}
    row = _muestrear(exp, monkeypatch, _telemetria(valores))
    assert row["pagos_cb"] == "OPEN"
    for op, state in zip(OPS, ("OPEN", "HALF_OPEN", "CLOSED", "CLOSED")):
        assert row.get(f"pagos_cb_{op}") == state, f"Se perdió el circuito de {op}"
