"""H9: respuestas perdidas, rechazos locales y claves alias después de reiniciar."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from pagos.app.main import create_app
from pagos.app.models import PaymentOperation
from pagos.app.resilience import TransbankUnavailable
from tests.integration.test_h9_idempotencia_pagos import (
    PAYLOAD, _cobrar, _competir, _filas, _post, _retener_primera_respuesta, pagos,
)


@pytest.mark.parametrize("phase", ["RUNNING", "UNCERTAIN"])
def test_h9_creacion_sin_respuesta_no_se_reenvia_tras_reinicio(pagos, monkeypatch, phase):
    key = "h9-respuesta-perdida:COBRAR"
    client = pagos.service.webpay.client
    real = client.crear

    def perder_respuesta(*args):
        real(*args)
        raise TransbankUnavailable("Respuesta de creación perdida")

    monkeypatch.setattr(client, "crear", perder_respuesta)
    response = _post(pagos.client, "/v1/payments", key, PAYLOAD)
    assert response.status_code == 502
    fila = _filas(pagos)[key]
    assert fila["token"] is None and pagos.fake.calls["create"] == len(pagos.fake.tx) == 1
    # RUNNING representa el registro que dejaría un proceso interrumpido antes de persistir el resultado.
    with pagos.service.sessions() as db:
        db.execute(update(PaymentOperation).where(PaymentOperation.payment_id == fila["id"])
                   .values(phase=phase))
        db.commit()
    app = create_app(pagos.settings, notifier=lambda *_: True, start_worker=False)
    try:
        with TestClient(app) as restarted:
            for _ in range(2):
                response = _post(restarted, "/v1/payments", key, PAYLOAD)
                assert response.status_code == 503 and response.headers["Retry-After"]
                assert response.json()["error"]["code"] == "REINTENTAR"
            assert pagos.fake.calls["create"] == 1
            assert _filas(pagos)[key]["id"] == fila["id"]
    finally:
        app.state.service.sessions.kw["bind"].dispose()


@pytest.mark.parametrize("operacion", ["crear", "reembolsar"])
def test_h9_rechazo_local_libera_operacion_y_permite_reintentar(pagos, operacion):
    key = "h9-rechazo-local:COBRAR"
    if operacion == "reembolsar":
        _cobrar(pagos, key)
        path, idem, payload = "/v1/payments/refund", "h9-local:comp:COBRAR", {"original_key": key}
    else:
        path, idem, payload = "/v1/payments", key, PAYLOAD
    guard = pagos.service.webpay.guard
    for _ in range(pagos.settings.bulkhead):
        assert guard.semaphore.acquire(blocking=False)
    try:
        response = _post(pagos.client, path, idem, payload)
        assert response.status_code == 503 and response.headers["Retry-After"]
        assert pagos.fake.calls["refund"] == 0
        assert pagos.fake.calls["create"] == int(operacion == "reembolsar")
    finally:
        for _ in range(pagos.settings.bulkhead):
            guard.semaphore.release()
    response = _post(pagos.client, path, idem, payload)
    assert response.status_code == 200, response.text
    assert response.json()["estado"] == ("PENDIENTE" if operacion == "crear" else "REEMBOLSADO")
    assert pagos.fake.calls["create"] == 1 and pagos.fake.calls["refund"] == int(operacion == "reembolsar")


@pytest.mark.parametrize("recuperacion", ["reintento", "reconciliacion"])
@pytest.mark.parametrize("phase", ["RUNNING", "UNCERTAIN"])
def test_h9_reembolso_sin_respuesta_se_resuelve_sin_otro_post(pagos, monkeypatch, recuperacion, phase):
    key, refund_key = "h9-refund-perdido:COBRAR", "h9-perdido:comp:COBRAR"
    token = _cobrar(pagos, key)
    client = pagos.service.webpay.client
    real = client.reembolsar

    def perder_respuesta(*args):
        real(*args)
        raise TransbankUnavailable("Respuesta de reembolso perdida")

    monkeypatch.setattr(client, "reembolsar", perder_respuesta)
    response = _post(pagos.client, "/v1/payments/refund", refund_key, {"original_key": key})
    assert response.status_code == 502
    fila = _filas(pagos)[key]
    assert fila["estado"] == "COBRADO" and pagos.fake.tx[token]["status"] == "REVERSED"
    with pagos.service.sessions() as db:
        db.execute(update(PaymentOperation).where(PaymentOperation.payment_id == fila["id"],
                                                  PaymentOperation.operation == "REFUND").values(phase=phase))
        db.commit()
    app = create_app(pagos.settings, notifier=lambda *_: True, start_worker=False)
    try:
        with TestClient(app) as restarted:
            if recuperacion == "reconciliacion":
                assert app.state.service.reconciliar()["reembolsos_resueltos"] == 1
            else:
                response = _post(restarted, "/v1/payments/refund", refund_key, {"original_key": key})
                assert response.status_code == 200, response.text
            assert _filas(pagos)[key]["estado"] == "REEMBOLSADO"
            assert pagos.fake.calls["refund"] == 1 and pagos.fake.calls["status"] == 1
            assert _post(restarted, "/v1/payments/refund", refund_key,
                         {"original_key": key}).json()["estado"] == "REEMBOLSADO"
            assert pagos.fake.calls["refund"] == 1
    finally:
        app.state.service.sessions.kw["bind"].dispose()


def test_h9_clave_alias_del_replay_queda_vinculada_al_pago_original(pagos):
    primero, segundo = "h9-alias-primero:COBRAR", "h9-alias-segundo:COBRAR"
    _cobrar(pagos, primero)
    token_segundo = _cobrar(pagos, segundo)
    for key in ("h9-original:comp:COBRAR", "h9-alias:comp:COBRAR"):
        response = _post(pagos.client, "/v1/payments/refund", key, {"original_key": primero})
        assert response.status_code == 200
    before = _filas(pagos)
    app = create_app(pagos.settings, notifier=lambda *_: True, start_worker=False)
    try:
        with TestClient(app) as restarted:
            response = _post(restarted, "/v1/payments/refund", "h9-alias:comp:COBRAR",
                             {"original_key": segundo})
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
            assert pagos.fake.calls["refund"] == 1 and pagos.fake.tx[token_segundo]["status"] == "AUTHORIZED"
            assert _filas(pagos) == before
    finally:
        app.state.service.sessions.kw["bind"].dispose()


@pytest.mark.parametrize("modo", ["hilos", "procesos"])
def test_h9_cancelacion_espera_a_la_creacion_en_curso_y_conserva_el_token(pagos, modo):
    key, refund_key = "h9-crear-cancelar:COBRAR", "h9-cancelar:comp:COBRAR"
    requests = [("/v1/payments", key, PAYLOAD),
                ("/v1/payments/refund", refund_key, {"original_key": key})]
    with _retener_primera_respuesta(pagos.fake, "crear") as pausa:
        results = _competir(pagos, modo, requests, pausa)
    assert results[0]["status"] == 201 and results[1]["status"] == 503, results
    assert results[1]["body"]["error"]["code"] == "REINTENTAR"
    fila = _filas(pagos)[key]
    assert fila["estado"] == "PENDIENTE" and set(pagos.fake.tx) == {fila["token"]}
    response = _post(pagos.client, *requests[1])
    assert response.status_code == 200 and response.json()["estado"] == "CANCELADO"
    assert pagos.fake.calls["create"] == 1 and pagos.fake.calls["refund"] == 0
    assert _filas(pagos)[key]["token"] == fila["token"]
    assert _post(pagos.client, *requests[0]).status_code == 409
