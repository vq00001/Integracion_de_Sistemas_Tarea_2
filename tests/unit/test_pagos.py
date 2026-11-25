"""Servicio Pagos (Transbank Webpay Plus) contra un simulador local: sin red ni Docker.

Cubre el ciclo del cobro (crear → comprador paga → retorno → COBRADO/RECHAZADO), la idempotencia,
la compensación (reembolso / cancelación anticipada), el commit incierto y la resiliencia hacia
Transbank (timeout, circuit breaker con recuperación y bulkhead).
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from pagos.app.config import Settings
from pagos.app.database import utc_now
from pagos.app.main import create_app
from pagos.app.models import Payment
from tests.support.fake_transbank import FakeTransbank

KEY, INTERNAL, CALLBACK = "k" * 32, "i" * 32, "c" * 32
H = {"X-API-Key": KEY}


def _settings(tmp_path, fake, **cambios):
    base = Settings(database_url=f"sqlite:///{tmp_path / 'p.db'}", api_key=KEY, internal_key=INTERNAL,
                    callback_key=CALLBACK, public_base_url="http://gw.test", tbk_base_url=fake.url,
                    tbk_commerce_code="597055555532", tbk_api_key="SECRET", tbk_timeout=0.3, tbk_commit_timeout=1.0,
                    procesando_seconds=0.0, reconcile_seconds=0, cb_threshold=3, cb_recovery_seconds=0.3, bulkhead=2)
    return replace(base, **cambios)


@pytest.fixture
def ctx(tmp_path):
    fake = FakeTransbank(api_key="SECRET").start()
    avisos = []
    estado_aviso = {"ok": True}

    def notifier(key, estado):
        avisos.append((key, estado))
        return estado_aviso["ok"]

    app = create_app(_settings(tmp_path, fake), notifier=notifier, start_worker=False)
    with TestClient(app) as client:
        yield SimpleNamespace(c=client, fake=fake, avisos=avisos, aviso_ok=estado_aviso, app=app, tmp=tmp_path)
    fake.stop()


def crear(ctx, key=None, monto=15000, alumno="alumno-1", curso=1):
    key = key or f"{uuid.uuid4()}:COBRAR"
    r = ctx.c.post("/v1/payments", json={"alumno_id": alumno, "curso_id": curso, "monto": monto},
                   headers={**H, "Idempotency-Key": key})
    return key, r


def token_de(ctx):
    return list(ctx.fake.tx)[-1]


def pagar_y_volver(ctx, aprobar=True):
    token = token_de(ctx)
    ctx.fake.pagar(token, aprobar)
    return ctx.c.get("/v1/pagos/retorno", params={"token_ws": token})


def estado(ctx, key):
    return ctx.c.get(f"/v1/payments/by-key/{key}", headers=H).json()["estado"]


def refund(ctx, original, key=None):
    return ctx.c.post("/v1/payments/refund", json={"original_key": original},
                      headers={**H, "Idempotency-Key": key or f"{uuid.uuid4()}:comp:COBRAR"})


# ----------------------------------------------------------------------------- seguridad y contrato
def test_las_rutas_de_servicio_exigen_clave_y_validan_el_cuerpo(ctx):
    assert ctx.c.post("/v1/payments", json={}, headers={"Idempotency-Key": "k"}).status_code == 401
    assert ctx.c.get("/v1/payments/by-key/x").status_code == 401
    assert ctx.c.post("/v1/payments", json={"alumno_id": "a", "curso_id": 1, "monto": 0},
                      headers={**H, "Idempotency-Key": "k"}).status_code == 422
    assert ctx.c.put("/internal/fault", json={}).status_code == 401


def test_no_arranca_sin_claves_propias(tmp_path):
    fake = FakeTransbank().start()
    try:
        with pytest.raises(RuntimeError, match="PAGOS_API_KEY"):
            _settings(tmp_path, fake, api_key="").validar()
        with pytest.raises(RuntimeError, match="producción"):
            _settings(tmp_path, fake, tbk_base_url="https://webpay3g.transbank.cl").validar()
    finally:
        fake.stop()


# ----------------------------------------------------------------------------- crear + idempotencia
def test_crear_devuelve_pendiente_y_es_idempotente(ctx):
    key, r = crear(ctx)
    assert r.status_code == 201
    cuerpo = r.json()
    assert cuerpo["estado"] == "PENDIENTE" and cuerpo["redirect_url"] == f"http://gw.test/v1/pagos/ir/{cuerpo['payment_id']}"
    assert ctx.fake.calls["create"] == 1
    _, again = crear(ctx, key)
    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["payment_id"] == cuerpo["payment_id"] and ctx.fake.calls["create"] == 1
    _, otro = crear(ctx, key, monto=1)
    assert otro.status_code == 422 and otro.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_la_redireccion_envia_el_token_a_webpay_por_post(ctx):
    _, r = crear(ctx)
    html = ctx.c.get(f"/v1/pagos/ir/{r.json()['payment_id']}")
    assert html.status_code == 200 and "token_ws" in html.text and ctx.fake.url in html.text
    assert token_de(ctx) in html.text
    assert ctx.c.get("/v1/pagos/ir/no-existe").status_code == 404


def test_el_adaptador_envia_las_credenciales_y_rechaza_las_invalidas(tmp_path):
    fake = FakeTransbank(api_key="OTRA").start()  # el simulador espera otra clave
    try:
        with TestClient(create_app(_settings(tmp_path, fake), start_worker=False)) as c:
            r = c.post("/v1/payments", json={"alumno_id": "a", "curso_id": 1, "monto": 100},
                       headers={**H, "Idempotency-Key": "x:COBRAR"})
            assert r.status_code == 502 and "credenciales" in r.json()["error"]["message"]
    finally:
        fake.stop()


# ----------------------------------------------------------------------------- retorno de Webpay
def test_pago_aprobado_se_confirma_una_sola_vez_y_avisa(ctx):
    key, _ = crear(ctx)
    page = pagar_y_volver(ctx)
    assert page.status_code == 200 and "Pago aprobado" in page.text
    assert estado(ctx, key) == "COBRADO"
    assert ctx.fake.calls["commit"] == 1 and (key, "COBRADO") in ctx.avisos
    pagar_y_volver(ctx)  # el comprador recarga la página de resultado
    assert ctx.fake.calls["commit"] == 1


def test_retorno_por_post_como_hace_webpay(ctx):
    key, _ = crear(ctx)
    token = token_de(ctx)
    ctx.fake.pagar(token, True)
    assert ctx.c.post("/v1/pagos/retorno", data={"token_ws": token}).status_code == 200
    assert estado(ctx, key) == "COBRADO"


def test_pago_rechazado_no_deja_cobro_ni_exige_reembolso(ctx):
    key, _ = crear(ctx)
    assert "Pago rechazado" in pagar_y_volver(ctx, aprobar=False).text
    assert estado(ctx, key) == "RECHAZADO" and (key, "RECHAZADO") in ctx.avisos
    assert refund(ctx, key).json()["estado"] == "INEXISTENTE"
    assert ctx.fake.calls["refund"] == 0


def test_comprador_que_anula_en_webpay_queda_abortado(ctx):
    key, _ = crear(ctx)
    r = ctx.c.get("/v1/pagos/retorno", params={"TBK_TOKEN": token_de(ctx), "TBK_ORDEN_COMPRA": "x", "TBK_ID_SESION": "y"})
    assert "Pago cancelado" in r.text and estado(ctx, key) == "ABORTADO"
    assert ctx.fake.calls["commit"] == 0 and (key, "ABORTADO") in ctx.avisos


def test_retorno_desconocido_es_404_y_no_confirma_nada(ctx):
    assert ctx.c.get("/v1/pagos/retorno", params={"token_ws": "inventado"}).status_code == 404
    assert ctx.fake.calls["commit"] == 0


def test_pago_vencido_no_se_confirma_aunque_el_comprador_haya_pagado(ctx):
    key, _ = crear(ctx)
    with ctx.app.state.service.sessions() as db:
        db.execute(update(Payment).values(expires_at=utc_now()))
        db.commit()
    assert estado(ctx, key) == "EXPIRADO"
    ctx.fake.pagar(token_de(ctx), True)
    r = ctx.c.get("/v1/pagos/retorno", params={"token_ws": token_de(ctx)})
    assert "Pago vencido" in r.text and ctx.fake.calls["commit"] == 0


# ----------------------------------------------------------------------------- compensación
def test_reembolso_de_un_cobro_es_repetible_y_no_reembolsa_dos_veces(ctx):
    key, _ = crear(ctx)
    pagar_y_volver(ctx)
    r1 = refund(ctx, key, "S1:comp:COBRAR")
    assert r1.json() == {"estado": "REEMBOLSADO", "tipo": "REVERSED", "pago_estado": None}
    assert refund(ctx, key, "S1:comp:COBRAR").json()["estado"] == "REEMBOLSADO"
    assert refund(ctx, key, "OTRA:comp:COBRAR").json()["estado"] == "REEMBOLSADO"
    assert ctx.fake.calls["refund"] == 1 and estado(ctx, key) == "REEMBOLSADO"


def test_cancelar_un_pago_pendiente_impide_que_un_retorno_tardio_cobre(ctx):
    key, _ = crear(ctx)
    assert refund(ctx, key).json()["estado"] == "CANCELADO"
    ctx.fake.pagar(token_de(ctx), True)  # el comprador igual pagó en Webpay…
    r = ctx.c.get("/v1/pagos/retorno", params={"token_ws": token_de(ctx)})
    assert "Pago cancelado" in r.text and ctx.fake.calls["commit"] == 0  # …pero nunca se confirma
    assert estado(ctx, key) == "CANCELADO"


def test_reembolso_sin_cobro_deja_cancelacion_anticipada(ctx):
    key = f"{uuid.uuid4()}:COBRAR"
    assert refund(ctx, key).json()["estado"] == "INEXISTENTE"
    _, tardio = crear(ctx, key)
    assert tardio.status_code == 409 and tardio.json()["error"]["code"] == "PAGO_CANCELADO"
    assert ctx.fake.calls["create"] == 0


# ----------------------------------------------------------------------------- commit incierto
def test_commit_perdido_se_resuelve_consultando_el_estado(ctx):
    key, _ = crear(ctx)
    ctx.fake.config(commit_perdido=True)  # Transbank autoriza pero la respuesta no llega
    page = pagar_y_volver(ctx)
    assert "Confirmando pago" in page.text and estado(ctx, key) == "PROCESANDO"
    assert refund(ctx, key).status_code == 503  # no se compensa mientras la confirmación sigue incierta
    ctx.fake.config(commit_perdido=False)
    assert ctx.app.state.service.reconciliar()["resueltos"] == 1
    assert estado(ctx, key) == "COBRADO" and (key, "COBRADO") in ctx.avisos
    assert ctx.fake.calls["commit"] == 1  # no se repitió el commit: se consultó el estado


def test_aviso_fallido_se_reintenta_en_la_reconciliacion(ctx):
    ctx.aviso_ok["ok"] = False
    key, _ = crear(ctx)
    pagar_y_volver(ctx)
    with ctx.app.state.service.sessions() as db:
        assert db.query(Payment).one().notificado_at is None
    ctx.aviso_ok["ok"] = True
    assert ctx.app.state.service.reconciliar()["avisados"] == 1
    with ctx.app.state.service.sessions() as db:
        assert db.query(Payment).one().notificado_at is not None


# ----------------------------------------------------------------------------- resiliencia hacia Transbank
def test_transbank_lento_o_caido_abre_el_circuito_y_responde_rapido(ctx):
    put = lambda **b: ctx.c.put("/internal/fault", json=b, headers={"X-Internal-Key": INTERNAL})
    assert put(latencia_ms=2000).json() == {"latencia_ms": 2000, "falla": False}  # > timeout (0,3 s)
    codigos = [crear(ctx)[1].status_code for _ in range(3)]
    assert codigos == [503, 503, 503]                       # simulación anterior al cliente: no enviado
    inicio = time.monotonic()
    r = crear(ctx)[1]
    assert r.status_code == 503 and r.headers["Retry-After"] and time.monotonic() - inicio < 0.2  # fail-fast
    assert 'circuito_estado{dependencia="TRANSBANK",estado="OPEN"} 1' in ctx.c.get("/metrics").text
    metricas = ctx.c.get("/metrics").text
    assert "circuito_rechazos_total{dependencia=\"TRANSBANK\"} 1" in metricas
    assert "transbank_intentos_total 0" in metricas and ctx.fake.calls["create"] == 0
    assert 'transbank_intentos_protegidos_total{operacion="crear"} 3' in metricas
    assert 'transbank_fallas_simuladas_total{operacion="crear"} 3' in metricas
    assert 'transbank_cliente_llamadas_iniciadas_total{operacion="crear"} 0' in metricas
    put(latencia_ms=0, falla=False)
    time.sleep(0.4)                                          # recuperación: HALF_OPEN deja pasar una sonda
    assert crear(ctx)[1].status_code == 201
    assert ctx.fake.calls["create"] == 1
    assert 'estado="CLOSED"} 1' in ctx.c.get("/metrics").text


def test_bulkhead_rechaza_el_exceso_sin_esperar(ctx, monkeypatch):
    saturado, liberar = threading.Event(), threading.Event()
    lock = threading.Lock()
    admitidas = 0
    client = ctx.app.state.service.webpay.client
    crear_real = client.crear

    def crear_bloqueado(*args):
        nonlocal admitidas
        # Guard ya adquirió el permiso: retenemos las dos llamadas dentro del bulkhead.
        with lock:
            admitidas += 1
            if admitidas == 2:
                saturado.set()
        assert liberar.wait(timeout=30), "La prueba no liberó las llamadas admitidas"
        return crear_real(*args)

    monkeypatch.setattr(client, "crear", crear_bloqueado)
    with ThreadPoolExecutor(8) as ex:
        aceptadas = [ex.submit(crear, ctx) for _ in range(2)]
        try:
            assert saturado.wait(timeout=10), "No se ocuparon los dos permisos del bulkhead"
            exceso = [ex.submit(crear, ctx) for _ in range(6)]
            rechazadas = [f.result(timeout=10)[1] for f in exceso]
            # El exceso debe responder antes de liberar la dependencia; no depende de un sleep.
            assert [r.status_code for r in rechazadas] == [503] * 6
            assert all(r.headers.get("Retry-After") for r in rechazadas)
            assert all(not f.done() for f in aceptadas)
            assert admitidas == 2 and ctx.app.state.guard.rejected == 6
            assert ctx.fake.calls["create"] == 0
        finally:
            liberar.set()
        assert [f.result(timeout=10)[1].status_code for f in aceptadas] == [201, 201]

    assert ctx.fake.calls["create"] == 2  # los seis rechazos no llegaron a Transbank
    assert crear(ctx)[1].status_code == 201  # se recupera la capacidad, sin permisos retenidos
    assert ctx.fake.calls["create"] == 3 and ctx.app.state.guard.rejected == 6


def test_confirmar_y_consultar_funcionan_con_el_circuito_cerrado_tras_una_falla_aislada(ctx):
    key, _ = crear(ctx)
    ctx.fake.config(falla=True)
    assert crear(ctx)[1].status_code == 502                  # una falla no abre el circuito (umbral 3)
    ctx.fake.config(falla=False)
    assert "Pago aprobado" in pagar_y_volver(ctx).text and estado(ctx, key) == "COBRADO"


# ----------------------------------------------------------------------------- formulario del simulador (lo que hace el navegador)
def _comprador_en_simulador(ctx, resultado):
    import re
    import requests
    _, r = crear(ctx)
    html = ctx.c.get(f"/v1/pagos/ir/{r.json()['payment_id']}").text
    action = re.search(r"action='([^']+)'", html).group(1)
    token = re.search(r"name='token_ws' value='(\w+)'", html).group(1)
    form = requests.post(action, data={"token_ws": token}, timeout=3)
    assert "Aprobar" in form.text and "Rechazar" in form.text and "Anular" in form.text
    volver = requests.post(ctx.fake.url + "/fake/resolver", data={"token": token, "resultado": resultado},
                           allow_redirects=False, timeout=3)
    assert volver.status_code == 303
    return r.json()["payment_id"], volver.headers["Location"]


def test_el_formulario_del_simulador_devuelve_al_comprador_como_webpay(ctx):
    from urllib.parse import parse_qs, urlparse
    pid, ubicacion = _comprador_en_simulador(ctx, "aprobar")
    assert ubicacion.startswith("http://gw.test/v1/pagos/retorno?token_ws=")
    token = parse_qs(urlparse(ubicacion).query)["token_ws"][0]
    assert "Pago aprobado" in ctx.c.get("/v1/pagos/retorno", params={"token_ws": token}).text

    pid2, ubicacion = _comprador_en_simulador(ctx, "abortar")
    query = parse_qs(urlparse(ubicacion).query)
    assert set(query) == {"TBK_TOKEN", "TBK_ORDEN_COMPRA", "TBK_ID_SESION"}  # Webpay no envía token_ws al anular
    assert "Pago cancelado" in ctx.c.get("/v1/pagos/retorno", params={k: v[0] for k, v in query.items()}).text

    _, ubicacion = _comprador_en_simulador(ctx, "rechazar")
    token = parse_qs(urlparse(ubicacion).query)["token_ws"][0]
    assert "Pago rechazado" in ctx.c.get("/v1/pagos/retorno", params={"token_ws": token}).text
