"""H9: exclusión de efectos externos con una BD compartida y Webpay HTTP local.

La primera respuesta de la pasarela se retiene DESPUÉS del efecto remoto. Así la
segunda petición encuentra la operación en curso, sin depender de sleeps ni de
que el planificador haga coincidir dos requests. Se repite con hilos y procesos
spawn: los procesos tienen aplicaciones, conexiones y memoria independientes.
"""
from __future__ import annotations

import copy
import multiprocessing
import os
import queue
import threading
import traceback
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pagos.app.config import Settings
from pagos.app.main import create_app
from pagos.app.models import Payment
from tests.support.fake_transbank import API, FakeTransbank

KEY = "h9-pagos-api-key-only-local-tests-32"
INTERNAL = "h9-internal-key-only-local-tests-32"
CALLBACK = "h9-callback-key-only-local-tests-32"
PAYLOAD = {"alumno_id": "alumno-h9", "curso_id": 1, "monto": 15000}


def _post(client, path, key, payload):
    return client.post(path, json=payload, headers={"X-API-Key": KEY, "Idempotency-Key": key})


@pytest.fixture
def pagos(tmp_path):
    fake = FakeTransbank(api_key="SECRET-H9").start()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'pagos.db'}",
        api_key=KEY, internal_key=INTERNAL, callback_key=CALLBACK,
        public_base_url="http://gateway.test", tbk_base_url=fake.url,
        tbk_api_key="SECRET-H9", tbk_timeout=40, tbk_commit_timeout=5,
        reconcile_seconds=0, bulkhead=8,
    )
    app = create_app(settings, notifier=lambda *_: True, start_worker=False)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield SimpleNamespace(client=client, fake=fake, settings=settings,
                                  service=app.state.service)
    finally:
        fake.stop()
        app.state.service.sessions.kw["bind"].dispose()


def _filas(pagos):
    with pagos.service.sessions() as db:
        return {row["idempotency_key"]: dict(row)
                for row in db.execute(select(Payment.__table__)).mappings()}


def _crear(pagos, key):
    response = _post(pagos.client, "/v1/payments", key, PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


def _cobrar(pagos, key):
    _crear(pagos, key)
    token = _filas(pagos)[key]["token"]
    pagos.fake.pagar(token)
    response = pagos.client.get("/v1/pagos/retorno", params={"token_ws": token})
    assert response.status_code == 200, response.text
    assert _filas(pagos)[key]["estado"] == "COBRADO"
    assert pagos.fake.tx[token]["status"] == "AUTHORIZED"
    return token


@contextmanager
def _retener_primera_respuesta(fake, operacion):
    """No cambia el adaptador ni la BD; el simulador hace su efecto normalmente."""
    efecto, liberar = threading.Event(), threading.Event()
    lock = threading.Lock()
    original = fake.server.RequestHandlerClass
    retenida = False

    class Handler(original):
        def _json(self, code, body):
            nonlocal retenida
            path = urlparse(self.path).path
            coincide = path == API if operacion == "crear" else path.endswith("/refunds")
            with lock:
                primera = self.command == "POST" and code == 200 and coincide and not retenida
                if primera:
                    retenida = True
            if primera:
                efecto.set()
                if not liberar.wait(timeout=35):
                    raise TimeoutError("La prueba no liberó la respuesta de Webpay")
            return super()._json(code, body)

    fake.server.RequestHandlerClass = Handler
    try:
        yield SimpleNamespace(efecto=efecto, liberar=liberar)
    finally:
        liberar.set()
        fake.server.RequestHandlerClass = original


def _solicitud(settings, request, listo, iniciar, terminado, salida, shared_client=None):
    """Entrada importable por spawn; nunca hereda sesiones ni conexiones del padre."""
    try:
        context = (nullcontext(shared_client) if shared_client is not None else
                   TestClient(create_app(settings, notifier=lambda *_: True, start_worker=False),
                              raise_server_exceptions=False))
        with context as client:
            listo.set()
            if not iniciar.wait(timeout=25):
                raise TimeoutError("La prueba no inició la petición")
            response = _post(client, *request)
            try:
                body = response.json()
            except ValueError:
                body = response.text
            salida.put({"status": response.status_code, "body": body, "pid": os.getpid(),
                        "retry_after": response.headers.get("Retry-After")})
    except Exception:
        salida.put({"error": traceback.format_exc(), "pid": os.getpid()})
    finally:
        terminado.set()


@dataclass
class _Worker:
    runner: object
    listo: object
    iniciar: object
    terminado: object
    salida: object


def _competir(pagos, modo, requests, pausa):
    """Primero un efecto sin respuesta; después el contendiente sobre la misma BD.

    También admite una solución que serialice en BD: si el contendiente espera,
    se libera la primera respuesta y se comprueba el resultado final. Los límites
    de espera solo acotan bloqueos; no se usan como umbral de rendimiento.
    """
    workers = []
    mp = multiprocessing.get_context("spawn") if modo == "procesos" else None
    try:
        for request in requests:
            factory = mp or threading
            listo, iniciar, terminado = factory.Event(), factory.Event(), factory.Event()
            salida = mp.Queue() if mp else queue.Queue()
            args = (pagos.settings, request, listo, iniciar, terminado, salida,
                    None if mp else pagos.client)
            runner = (mp.Process(target=_solicitud, args=args) if mp else
                      threading.Thread(target=_solicitud, args=args, daemon=True))
            worker = _Worker(runner, listo, iniciar, terminado, salida)
            workers.append(worker)
            runner.start()
        for worker in workers:
            assert worker.listo.wait(timeout=15), "Un worker no pudo preparar su API de Pagos"
        workers[0].iniciar.set()
        assert pausa.efecto.wait(timeout=10), "La primera petición no llegó a la pasarela"
        assert not workers[0].terminado.is_set(), "La primera respuesta debe seguir retenida"
        workers[1].iniciar.set()
        # Sin exclusión, el segundo efecto y su respuesta ocurren mientras el primero espera.
        # Con exclusión se admite replay, rechazo recuperable o espera por el primer resultado.
        workers[1].terminado.wait(timeout=5)
        pausa.liberar.set()
        results = []
        for worker in workers:
            assert worker.terminado.wait(timeout=15), "Una petición quedó bloqueada"
            result = worker.salida.get(timeout=5)
            assert "error" not in result, result.get("error")
            results.append(result)
        if mp:
            assert len({r["pid"] for r in results}) == 2
            assert all(r["pid"] != os.getpid() for r in results)
        return results
    finally:
        pausa.liberar.set()
        for worker in workers:
            worker.iniciar.set()
        for worker in workers:
            worker.runner.join(timeout=15)
            if mp and worker.runner.is_alive():
                worker.runner.terminate()
                worker.runner.join(timeout=5)
            if mp:
                worker.salida.close()
                worker.salida.join_thread()


def _replay_o_reintento(result, exitos):
    assert result["status"] in (*exitos, 503), result
    if result["status"] == 503:
        assert result["body"]["error"]["code"] == "REINTENTAR", result
        assert result["retry_after"] and int(result["retry_after"]) > 0


@pytest.mark.parametrize("modo", ["hilos", "procesos"])
def test_h9_creaciones_concurrentes_generan_una_sola_transaccion(pagos, modo):
    key = "h9-creacion:COBRAR"
    request = ("/v1/payments", key, PAYLOAD)
    with _retener_primera_respuesta(pagos.fake, "crear") as pausa:
        results = _competir(pagos, modo, [request, request], pausa)

    filas = _filas(pagos)
    assert len(filas) == 1  # una fila local no basta para demostrar idempotencia externa
    assert pagos.fake.calls["create"] == 1, "La misma clave inició dos creaciones en Webpay"
    assert len(pagos.fake.tx) == 1, "Quedó una transacción externa sin correspondencia local"
    assert set(pagos.fake.tx) == {filas[key]["token"]}
    assert filas[key]["estado"] == "PENDIENTE"
    for result in results:
        _replay_o_reintento(result, (200, 201))
    assert any(r["status"] == 201 for r in results), results
    replay = _post(pagos.client, *request)
    assert replay.status_code == 200 and replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["payment_id"] == filas[key]["id"]
    assert pagos.fake.calls["create"] == 1


@pytest.mark.parametrize("modo", ["hilos", "procesos"])
@pytest.mark.parametrize("misma_clave", [True, False], ids=["misma-clave", "claves-distintas"])
def test_h9_reembolsos_concurrentes_envian_una_sola_solicitud(pagos, modo, misma_clave):
    key = "h9-reembolso:COBRAR"
    token = _cobrar(pagos, key)
    refund_keys = ["h9-a:comp:COBRAR", "h9-a:comp:COBRAR" if misma_clave else "h9-b:comp:COBRAR"]
    requests = [("/v1/payments/refund", rk, {"original_key": key}) for rk in refund_keys]
    with _retener_primera_respuesta(pagos.fake, "reembolsar") as pausa:
        results = _competir(pagos, modo, requests, pausa)

    assert pagos.fake.calls["refund"] == 1, "El mismo pago envió dos reembolsos a Webpay"
    assert pagos.fake.tx[token]["status"] == "REVERSED"
    fila = _filas(pagos)[key]
    assert fila["estado"] == "REEMBOLSADO" and fila["refund_type"] == "REVERSED"
    for result in results:
        _replay_o_reintento(result, (200,))
    assert any(r["status"] == 200 for r in results), results
    for request in requests:
        replay = _post(pagos.client, *request)
        assert replay.status_code == 200 and replay.json()["estado"] == "REEMBOLSADO"
    assert pagos.fake.calls["refund"] == 1


@pytest.mark.parametrize("destino", ["cobrado", "pendiente", "inexistente", "reembolsado"])
def test_h9_clave_de_reembolso_no_se_puede_reutilizar_para_otro_pago(pagos, destino):
    original, otro, refund_key = "h9-original:COBRAR", "h9-otro:COBRAR", "h9-unica:comp:COBRAR"
    _cobrar(pagos, original)
    if destino in ("cobrado", "reembolsado"):
        _cobrar(pagos, otro)
    elif destino == "pendiente":
        _crear(pagos, otro)
    if destino == "reembolsado":
        assert _post(pagos.client, "/v1/payments/refund", "h9-otra:comp:COBRAR",
                     {"original_key": otro}).status_code == 200
    assert _post(pagos.client, "/v1/payments/refund", refund_key,
                 {"original_key": original}).status_code == 200
    filas_antes, tx_antes = _filas(pagos), copy.deepcopy(pagos.fake.tx)
    reembolsos_antes = pagos.fake.calls["refund"]

    response = _post(pagos.client, "/v1/payments/refund", refund_key, {"original_key": otro})

    assert pagos.fake.calls["refund"] == reembolsos_antes, (
        f"La clave se validó después del efecto externo: HTTP {response.status_code}; "
        f"pago local={_filas(pagos)[otro]['estado']}; "
        f"pasarela={pagos.fake.tx[filas_antes[otro]['token']]['status']}"
    )
    assert pagos.fake.tx == tx_antes, "La pasarela cambió aunque la clave pertenecía a otro pago"
    assert _filas(pagos) == filas_antes, "Una clave conflictiva no debe cambiar ni insertar pagos"
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize("modo", ["hilos", "procesos"])
def test_h9_dos_pagos_no_pueden_reservar_la_misma_clave_de_reembolso(pagos, modo):
    primero, segundo, refund_key = "h9-primero:COBRAR", "h9-segundo:COBRAR", "h9-compartida:comp:COBRAR"
    token_primero, token_segundo = _cobrar(pagos, primero), _cobrar(pagos, segundo)
    requests = [("/v1/payments/refund", refund_key, {"original_key": key})
                for key in (primero, segundo)]
    with _retener_primera_respuesta(pagos.fake, "reembolsar") as pausa:
        results = _competir(pagos, modo, requests, pausa)

    assert pagos.fake.calls["refund"] == 1, "La clave no se reservó antes de reembolsar"
    assert pagos.fake.tx[token_primero]["status"] == "REVERSED"
    assert pagos.fake.tx[token_segundo]["status"] == "AUTHORIZED"
    filas = _filas(pagos)
    assert filas[primero]["estado"] == "REEMBOLSADO" and filas[primero]["refund_key"] == refund_key
    assert filas[segundo]["estado"] == "COBRADO" and filas[segundo]["refund_key"] is None
    assert results[0]["status"] == 200, results
    assert results[1]["status"] == 422, results
    assert results[1]["body"]["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize("cambio", [{"monto": 1}, {"alumno_id": "otro-alumno"}, {"curso_id": 2}],
                         ids=["monto", "alumno", "curso"])
def test_h9_payload_distinto_se_rechaza_antes_de_un_segundo_efecto(pagos, cambio):
    key = "h9-payload:COBRAR"
    requests = [("/v1/payments", key, PAYLOAD), ("/v1/payments", key, {**PAYLOAD, **cambio})]
    with _retener_primera_respuesta(pagos.fake, "crear") as pausa:
        results = _competir(pagos, "procesos", requests, pausa)

    assert results[0]["status"] == 201, results
    assert results[1]["status"] == 422, results
    assert results[1]["body"]["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert pagos.fake.calls["create"] == len(pagos.fake.tx) == 1
    fila = _filas(pagos)[key]
    assert (fila["alumno_id"], fila["curso_id"], fila["monto"]) == ("alumno-h9", 1, 15000)
