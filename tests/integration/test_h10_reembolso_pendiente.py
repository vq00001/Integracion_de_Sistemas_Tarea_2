"""H10: una autorización inconsistente sigue siendo una deuda hasta devolverla.

Webpay es HTTP local y las aplicaciones usan SQLite temporal. Se altera la
transacción realmente autorizada, no solo el JSON que ve Pagos: un reembolso
debe devolver ese monto al mismo token y conservar el payload original local.
No se presupone una tabla nueva ni se importa un estado aún no implementado.
"""
import json
import multiprocessing
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from pagos.app.main import create_app
from pagos.app.models import Payment, PaymentOperation, RefundDebt
from tests.integration.test_h9_idempotencia_pagos import (
    KEY, PAYLOAD, _competir, _filas, _post, _replay_o_reintento,
    _retener_primera_respuesta, pagos,
)
from tests.support.fake_transbank import API


@pytest.fixture
def caso(pagos):
    """Fallas solo de refund: commit y consultas siguen siendo reales y sanos."""
    control = SimpleNamespace(modo="rechazo", pausar=False, entrada=threading.Event(),
                              liberar=threading.Event(), refunds=[], lock=threading.Lock())
    avisos = []

    def notificar(key, estado):
        avisos.append((key, estado))
        return True

    pagos.service.notifier = notificar
    original = pagos.fake.server.RequestHandlerClass

    class Handler(original):
        def _es_refund(self):
            path = urlparse(self.path).path
            return self.command == "POST" and path.startswith(API + "/") and path.endswith("/refunds")

        def _body(self):
            body = super()._body()
            if self._es_refund():
                with control.lock:
                    control.refunds.append({"token": urlparse(self.path).path.split("/")[-2],
                                            "amount": json.loads(body)["amount"]})
            return body

        def _preludio(self):
            if self._es_refund():
                control.entrada.set()
                if control.pausar and not control.liberar.wait(timeout=35):
                    raise TimeoutError("La prueba no liberó el reembolso")
                if control.modo in ("rechazo", "no_disponible"):
                    pagos.fake.calls_inc("refund")
                    self._body()
                    code = 422 if control.modo == "rechazo" else 503
                    self._json(code, {"error_message": "Reembolso no procesado por el simulador"})
                    return False
            return super()._preludio()

        def _json(self, code, body):
            if self._es_refund() and code == 200 and control.modo == "respuesta_perdida":
                # El simulador ya dejó REVERSED, pero Pagos recibe un resultado incierto.
                code, body = 500, {"error_message": "Respuesta perdida después del reembolso"}
            return super()._json(code, body)

    pagos.fake.server.RequestHandlerClass = Handler
    try:
        yield SimpleNamespace(**vars(pagos), control=control, avisos=avisos, notificar=notificar)
    finally:
        control.liberar.set()
        pagos.fake.server.RequestHandlerClass = original


def _preparar(caso, diferencia):
    key = "h10-inconsistente:COBRAR"
    response = _post(caso.client, "/v1/payments", key, PAYLOAD)
    assert response.status_code == 201, response.text
    fila = _filas(caso)[key]
    token = fila["token"]
    with caso.fake.lock:
        if diferencia == "monto":
            caso.fake.tx[token]["amount"] = PAYLOAD["monto"] + 3500
        else:
            assert diferencia == "orden"
            caso.fake.tx[token]["buy_order"] = "ORDEN-INCONSISTENTE-H10"
    caso.fake.pagar(token)
    return SimpleNamespace(key=key, token=token, monto=caso.fake.tx[token]["amount"],
                           orden_original=fila["buy_order"], payment_id=fila["id"])


def _retornar(caso, datos):
    response = caso.client.get("/v1/pagos/retorno", params={"token_ws": datos.token})
    assert response.status_code == 200, response.text
    assert caso.fake.calls["commit"] == 1 and caso.fake.calls["refund"] == 1
    assert caso.control.refunds == [{"token": datos.token, "amount": datos.monto}]
    return response


def _estado(client, key):
    response = client.get(f"/v1/payments/by-key/{key}", headers={"X-API-Key": KEY})
    assert response.status_code == 200, response.text
    return response.json()["estado"]


def _exigir_deuda(caso, datos, client=None):
    estado = _estado(client or caso.client, datos.key)
    # Permite conservar PROCESANDO o introducir un estado explícito de devolución pendiente.
    assert estado in ("PROCESANDO", "REEMBOLSO_PENDIENTE"), (
        f"Autorización con deuda de reembolso: local={estado}, "
        f"Webpay={caso.fake.tx[datos.token]['status']}, monto autorizado={datos.monto}"
    )


@contextmanager
def _reiniciar(caso):
    """Nueva app, engine y servicio sobre el mismo archivo; sin worker de fondo."""
    app = create_app(caso.settings, notifier=caso.notificar, start_worker=False)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield app, client
    finally:
        app.state.service.sessions.kw["bind"].dispose()


def _exigir_devolucion(caso, datos, client):
    assert _estado(client, datos.key) == "REEMBOLSADO", (
        f"No se recuperó la devolución: local={_estado(client, datos.key)}, "
        f"Webpay={caso.fake.tx[datos.token]['status']}"
    )
    fila = _filas(caso)[datos.key]
    assert fila["monto"] == PAYLOAD["monto"] and fila["buy_order"] == datos.orden_original
    assert fila["id"] == datos.payment_id and fila["token"] == datos.token
    assert caso.fake.tx[datos.token]["status"] == "REVERSED"
    assert all(r == {"token": datos.token, "amount": datos.monto} for r in caso.control.refunds)
    assert caso.fake.calls["create"] == caso.fake.calls["commit"] == 1
    assert not any(estado == "COBRADO" for _, estado in caso.avisos)


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
@pytest.mark.parametrize("falla", ["rechazo", "no_disponible"], ids=["422-sin-efecto", "503-incierto"])
def test_h10_autorizacion_inconsistente_con_devolucion_fallida_conserva_deuda(caso, diferencia, falla):
    datos = _preparar(caso, diferencia)
    caso.control.modo = falla
    _retornar(caso, datos)
    assert caso.fake.tx[datos.token]["status"] == "AUTHORIZED"
    _exigir_deuda(caso, datos)


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_retorno_y_notificacion_no_afirman_que_no_hubo_cobro(caso, diferencia):
    datos = _preparar(caso, diferencia)
    response = _retornar(caso, datos)
    assert caso.fake.tx[datos.token]["status"] == "AUTHORIZED"
    assert "No se realizó ningún cobro" not in response.text, response.text
    assert "Pago aprobado" not in response.text
    assert not any(estado in ("COBRADO", "RECHAZADO", "ABORTADO", "EXPIRADO", "CANCELADO")
                   for _, estado in caso.avisos), caso.avisos


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_compensacion_no_responde_inexistente_mientras_se_debe_el_reembolso(caso, diferencia):
    datos = _preparar(caso, diferencia)
    caso.control.modo = "no_disponible"
    _retornar(caso, datos)
    response = _post(caso.client, "/v1/payments/refund", "h10-pendiente:comp:COBRAR",
                     {"original_key": datos.key})
    assert caso.fake.tx[datos.token]["status"] == "AUTHORIZED"
    assert response.status_code == 503, response.text
    assert response.headers.get("Retry-After")
    assert response.json()["error"]["code"] == "REINTENTAR"
    _exigir_deuda(caso, datos)


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_reinicio_y_reconciliacion_recuperan_el_monto_real_del_reembolso(caso, diferencia):
    datos = _preparar(caso, diferencia)
    _retornar(caso, datos)  # 422 definitivo sin devolución; sí hubo autorización
    assert caso.fake.tx[datos.token]["status"] == "AUTHORIZED"
    caso.control.modo = "normal"
    with _reiniciar(caso) as (app, client):
        for _ in range(2):
            app.state.service.reconciliar()
        _exigir_devolucion(caso, datos, client)
        assert caso.fake.calls["refund"] == 2  # primer rechazo + una devolución efectiva
        response = _post(client, "/v1/payments/refund", "h10-recuperado:comp:COBRAR",
                         {"original_key": datos.key})
        assert response.status_code == 200 and response.json()["estado"] == "REEMBOLSADO"
        assert caso.fake.calls["refund"] == 2


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_respuesta_perdida_se_recupera_por_consulta_sin_repetir_reembolso(caso, diferencia):
    datos = _preparar(caso, diferencia)
    caso.control.modo = "respuesta_perdida"
    _retornar(caso, datos)
    assert caso.fake.tx[datos.token]["status"] == "REVERSED"
    caso.control.modo = "normal"
    with _reiniciar(caso) as (app, client):
        for _ in range(2):
            app.state.service.reconciliar()
        _exigir_devolucion(caso, datos, client)
        assert caso.fake.calls["refund"] == 1 and caso.fake.calls["status"] >= 1
        response = _post(client, "/v1/payments/refund", "h10-respuesta-perdida:comp:COBRAR",
                         {"original_key": datos.key})
        assert response.status_code == 200 and response.json()["estado"] == "REEMBOLSADO"
        assert caso.fake.calls["refund"] == 1


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_devolucion_inmediata_exitosa_queda_registrada_como_reembolsada(caso, diferencia):
    datos = _preparar(caso, diferencia)
    caso.control.modo = "normal"
    response = _retornar(caso, datos)
    assert caso.fake.tx[datos.token]["status"] == "REVERSED"
    _exigir_devolucion(caso, datos, caso.client)
    assert "Pago reembolsado" in response.text
    assert "No se realizó ningún cobro" not in response.text


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_compensacion_espera_al_reembolso_del_retorno_en_curso(caso, diferencia):
    datos = _preparar(caso, diferencia)
    caso.control.pausar = True
    with ThreadPoolExecutor(1) as executor:
        futuro = executor.submit(_retornar, caso, datos)
        try:
            assert caso.control.entrada.wait(timeout=10), "El retorno no intentó devolver la autorización"
            assert caso.fake.tx[datos.token]["status"] == "AUTHORIZED"
            assert not futuro.done()
            response = _post(caso.client, "/v1/payments/refund", "h10-en-curso:comp:COBRAR",
                             {"original_key": datos.key})
            assert response.status_code == 503, response.text
            assert response.headers.get("Retry-After")
            _exigir_deuda(caso, datos)
        finally:
            caso.control.liberar.set()
        futuro.result(timeout=10)


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
@pytest.mark.parametrize("modo", ["hilos", "procesos"])
def test_h10_reintentos_concurrentes_recuperan_la_deuda_sin_duplicar_devoluciones(caso, diferencia, modo):
    datos = _preparar(caso, diferencia)
    _retornar(caso, datos)
    _exigir_deuda(caso, datos)  # precondición funcional, no una dependencia del esquema nuevo
    caso.control.modo = "normal"
    request = ("/v1/payments/refund", "h10-concurrente:comp:COBRAR", {"original_key": datos.key})
    with _retener_primera_respuesta(caso.fake, "reembolsar") as pausa:
        results = _competir(caso, modo, [request, request], pausa)
    for result in results:
        _replay_o_reintento(result, (200,))
    assert any(result["status"] == 200 for result in results), results
    _exigir_devolucion(caso, datos, caso.client)
    assert caso.fake.calls["refund"] == 2  # primer rechazo + una devolución, también entre procesos


@pytest.mark.parametrize("aprobar", [True, False], ids=["autorizacion-consistente", "tarjeta-rechazada"])
def test_h10_controles_no_crean_deudas_para_el_camino_feliz_ni_para_un_rechazo_real(caso, aprobar):
    key = "h10-control:COBRAR"
    response = _post(caso.client, "/v1/payments", key, PAYLOAD)
    assert response.status_code == 201, response.text
    token = _filas(caso)[key]["token"]
    caso.fake.pagar(token, aprobar)
    response = caso.client.get("/v1/pagos/retorno", params={"token_ws": token})
    assert response.status_code == 200
    assert _estado(caso.client, key) == ("COBRADO" if aprobar else "RECHAZADO")
    assert caso.fake.tx[token]["status"] == ("AUTHORIZED" if aprobar else "FAILED")
    caso.service.reconciliar()
    assert caso.fake.calls["refund"] == 0 and not caso.control.refunds
    if not aprobar:
        response = _post(caso.client, "/v1/payments/refund", "h10-rechazo-real:comp:COBRAR",
                         {"original_key": key})
        assert response.status_code == 200 and response.json()["estado"] == "INEXISTENTE"
        assert caso.fake.calls["refund"] == 0


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_deuda_y_reserva_estan_confirmadas_antes_del_efecto_externo(caso, diferencia):
    datos = _preparar(caso, diferencia)
    caso.control.pausar = True
    with ThreadPoolExecutor(1) as executor:
        futuro = executor.submit(_retornar, caso, datos)
        try:
            assert caso.control.entrada.wait(timeout=10)
            assert caso.fake.calls["refund"] == 0  # servidor detenido antes de intentar la devolución
            with caso.service.sessions() as db:
                debt = db.get(RefundDebt, datos.payment_id)
                assert debt.authorized_amount == datos.monto
                assert debt.authorized_buy_order == caso.fake.tx[datos.token]["buy_order"]
                assert db.get(PaymentOperation, (datos.payment_id, "REFUND")).phase == "RUNNING"
                payment = db.get(Payment, datos.payment_id)
                assert payment.estado == "PROCESANDO" and payment.notificado_at is None
                assert payment.monto == PAYLOAD["monto"] and payment.buy_order == datos.orden_original
        finally:
            caso.control.liberar.set()
        futuro.result(timeout=10)


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_replay_y_metricas_conservan_la_devolucion_pendiente(caso, diferencia):
    datos = _preparar(caso, diferencia)
    _retornar(caso, datos)
    with _reiniciar(caso) as (_, client):
        page = client.get("/v1/pagos/retorno", params={"token_ws": datos.token})
        assert page.status_code == 200 and "Devolución pendiente" in page.text
        assert "No se realizó ningún cobro" not in page.text
        replay = _post(client, "/v1/payments", datos.key, PAYLOAD)
        assert replay.status_code == 200 and replay.headers["Idempotent-Replayed"] == "true"
        assert replay.json()["estado"] == "REEMBOLSO_PENDIENTE"
        assert replay.json()["redirect_url"] is None
        metrics = client.get("/metrics").text
        assert 'pagos_total{estado="REEMBOLSO_PENDIENTE"} 1' in metrics
        assert 'pagos_total{estado="RECHAZADO"} 1' not in metrics
        assert caso.fake.calls["create"] == caso.fake.calls["commit"] == caso.fake.calls["refund"] == 1
        with caso.service.sessions() as db:
            assert db.scalar(select(RefundDebt.authorized_amount).where(
                RefundDebt.payment_id == datos.payment_id)) == datos.monto


def _reconciliar_en_proceso(settings, listo, iniciar, salida):
    app = create_app(settings, notifier=lambda *_: True, start_worker=False)
    try:
        with TestClient(app) as _:
            listo.set()
            if not iniciar.wait(timeout=25):
                raise TimeoutError("No se inició el reconciliador de prueba")
            salida.put({"pid": os.getpid(), "resultado": app.state.service.reconciliar()})
    except Exception:
        salida.put({"pid": os.getpid(), "error": traceback.format_exc()})
    finally:
        app.state.service.sessions.kw["bind"].dispose()


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_dos_reconciliadores_en_procesos_distintos_no_duplican_la_devolucion(caso, diferencia):
    datos = _preparar(caso, diferencia)
    _retornar(caso, datos)
    caso.control.modo = "normal"
    mp = multiprocessing.get_context("spawn")
    salida = mp.Queue()
    listos, inicios = [mp.Event() for _ in range(2)], [mp.Event() for _ in range(2)]
    workers = [mp.Process(target=_reconciliar_en_proceso,
                          args=(caso.settings, listos[i], inicios[i], salida)) for i in range(2)]
    with _retener_primera_respuesta(caso.fake, "reembolsar") as pausa:
        try:
            for worker in workers:
                worker.start()
            assert all(event.wait(timeout=15) for event in listos)
            inicios[0].set()
            assert pausa.efecto.wait(timeout=10), "El primer reconciliador no intentó la devolución pendiente"
            inicios[1].set()
            result = salida.get(timeout=10)  # el segundo termina mientras se retiene la primera respuesta
            assert "error" not in result, result
            pausa.liberar.set()
            other = salida.get(timeout=15)
            assert "error" not in other, other
            assert len({result["pid"], other["pid"], os.getpid()}) == 3
            _exigir_devolucion(caso, datos, caso.client)
            assert caso.fake.calls["refund"] == 2
        finally:
            pausa.liberar.set()
            for event in inicios:
                event.set()
            for worker in workers:
                worker.join(timeout=10)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            salida.close()
            salida.join_thread()


@pytest.mark.parametrize("diferencia", ["monto", "orden"])
def test_h10_autorizacion_tardia_prevalece_sobre_un_rechazo_concurrente(caso, diferencia):
    datos = _preparar(caso, diferencia)
    # Respuesta real de la pasarela, todavía no aplicada al registro local.
    authorized = caso.service.webpay.confirmar(datos.token)
    assert authorized["status"] == "AUTHORIZED" and authorized["response_code"] == 0
    with caso.service.sessions() as db:
        # Otro observador clasificó el commit como rechazado antes de recibir esa respuesta.
        db.execute(update(Payment).where(Payment.id == datos.payment_id)
                   .values(estado="RECHAZADO", tbk_status="UNKNOWN_TOKEN"))
        db.commit()
        payment = db.get(Payment, datos.payment_id)
        caso.service._aplicar(db, payment, authorized)
    assert caso.fake.tx[datos.token]["status"] == "AUTHORIZED"
    _exigir_deuda(caso, datos)
    assert caso.fake.calls["refund"] == 1
    caso.control.modo = "normal"
    with _reiniciar(caso) as (app, client):
        app.state.service.reconciliar()
        _exigir_devolucion(caso, datos, client)
        assert caso.fake.calls["refund"] == 2
        with caso.service.sessions() as db:
            caso.service._aplicar(db, db.get(Payment, datos.payment_id), authorized)
        _exigir_devolucion(caso, datos, client)
        assert caso.fake.calls["refund"] == 2  # repetir la autorización no reabre la deuda saldada
