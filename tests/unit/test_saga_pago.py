"""Saga con cobro por redirección (Webpay Plus): la saga espera al comprador y se reanuda por
aviso de Pagos o por consulta del reconciliador. Sin red ni sleeps (clientes simulados)."""
import importlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import create_engine, text

from tests.unit.test_saga import FakeClient, run, setup_saga  # noqa: F401  (fixture)
from tests.unit.test_api_unidad import m  # noqa: F401  (fixture)


class PagoClient(FakeClient):
    """COBRAR con redirección: forward deja el cobro PENDIENTE; query informa el estado en Pagos."""

    def __init__(self, events):
        super().__init__("COBRAR", events)
        self.estado, self.query_error = "PENDIENTE", None

    def forward(self, saga, key):
        self.events.append("COBRAR")
        return {"estado": "PENDIENTE", "payment_id": "p1", "redirect_url": "http://gw/v1/pagos/ir/p1",
                "expira_at": None}

    def query(self, saga, key):
        assert key == f"{saga['id']}:COBRAR"
        if self.query_error:
            raise self.query_error()
        return {"estado": self.estado}


@pytest.fixture
def esperando(setup_saga):
    sg, obj, sessions, events, clients = setup_saga
    clients["COBRAR"] = PagoClient(events)
    obj.clients, obj.poll_seconds, obj.pago_gracia = clients, 0, 0
    return sg, obj, sessions, events, clients


def estado_de(sg, sessions, sid):
    with sessions() as db:
        saga = db.get(sg.Saga, sid)
        return saga.estado, saga.motivo_fallo


def test_el_cobro_pendiente_deja_la_saga_esperando_sin_inscribir(esperando):
    sg, obj, sessions, events, clients = esperando
    key, alumno = str(uuid.uuid4()), str(uuid.uuid4())
    result, replay = run(obj, key, alumno)
    assert result["estado"] == "ESPERANDO_PAGO" and not replay
    assert result["pago_url"] == "http://gw/v1/pagos/ir/p1" and result["pago_expira_at"]
    assert events == ["RESERVAR", "COBRAR"]
    again, replay = run(obj, key, alumno)  # replay: mismo estado y misma URL, sin efectos
    assert again == result and replay and events == ["RESERVAR", "COBRAR"]
    # El reconciliador NO compensa por inactividad a quien está pagando (stale_seconds=0 en el fixture)…
    assert obj.reconcile() == 1 and events == ["RESERVAR", "COBRAR"]
    assert estado_de(sg, sessions, result["id"])[0] == "ESPERANDO_PAGO"  # …solo consulta a Pagos
    from app.errors import ConflictError
    with pytest.raises(ConflictError):  # el par alumno/curso sigue ocupado mientras se espera
        run(obj, alumno=alumno)


def test_pago_aprobado_reanuda_e_inscribe_una_sola_vez(esperando):
    sg, obj, sessions, events, clients = esperando
    result, _ = run(obj)
    clients["COBRAR"].estado = "COBRADO"
    obj.reconcile()
    assert estado_de(sg, sessions, result["id"]) == ("COMPLETADA", None)
    assert events == ["RESERVAR", "COBRAR", "INSCRIBIR"]
    obj.pago_resuelto(result["id"], "COBRADO")  # aviso repetido: idempotente
    obj.sondear_pago(result["id"])
    assert events == ["RESERVAR", "COBRAR", "INSCRIBIR"]


@pytest.mark.parametrize("estado", ["RECHAZADO", "ABORTADO", "EXPIRADO"])
def test_pago_no_aprobado_libera_el_cupo_sin_reembolsar(esperando, estado):
    sg, obj, sessions, events, clients = esperando
    result, _ = run(obj)
    clients["COBRAR"].estado = estado
    obj.sondear_pago(result["id"])
    assert estado_de(sg, sessions, result["id"]) == ("COMPENSADA", f"COBRAR:{estado}")
    assert events == ["RESERVAR", "COBRAR", "comp:RESERVAR"]  # nunca hubo cobro: no hay comp:COBRAR


def test_falla_la_inscripcion_tras_el_pago_reembolsa_y_libera(esperando):
    sg, obj, sessions, events, clients = esperando
    clients["INSCRIBIR"].failure = TimeoutError
    result, _ = run(obj)
    clients["COBRAR"].estado = "COBRADO"
    obj.sondear_pago(result["id"])
    assert estado_de(sg, sessions, result["id"])[0] == "COMPENSADA"
    assert events == ["RESERVAR", "COBRAR", "INSCRIBIR", "comp:INSCRIBIR", "comp:COBRAR", "comp:RESERVAR"]


def test_si_pagos_no_responde_se_espera_y_al_vencer_se_cancela_el_cobro(esperando):
    sg, obj, sessions, events, clients = esperando
    obj.pago_gracia = 3600
    result, _ = run(obj)
    clients["COBRAR"].query_error = TimeoutError
    obj.sondear_pago(result["id"])
    assert estado_de(sg, sessions, result["id"])[0] == "ESPERANDO_PAGO"  # dentro del plazo: se sigue esperando
    with sessions() as db:
        saga = db.get(sg.Saga, result["id"])
        saga.pago_expira_at -= timedelta(hours=2)
        db.commit()
    obj.pago_gracia = 0
    obj.sondear_pago(result["id"])
    assert estado_de(sg, sessions, result["id"]) == ("COMPENSADA", "COBRAR:PAGO_SIN_RESPUESTA")
    # Incierto: se pide a Pagos cancelar/reembolsar el cobro abierto y luego liberar el cupo.
    assert events == ["RESERVAR", "COBRAR", "comp:COBRAR", "comp:RESERVAR"]


def test_aviso_y_sondeo_concurrentes_no_repiten_la_inscripcion(esperando):
    sg, obj, sessions, events, clients = esperando
    result, _ = run(obj)
    clients["COBRAR"].estado = "COBRADO"
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda i: obj.sondear_pago(result["id"]) if i % 2 else obj.pago_resuelto(result["id"], "COBRADO"), range(6)))
    assert events.count("INSCRIBIR") == 1
    assert estado_de(sg, sessions, result["id"])[0] == "COMPLETADA"


def test_un_esquema_anterior_se_rechaza_con_un_mensaje_claro(m, tmp_path):
    sg = importlib.import_module("app.saga")
    engine = create_engine(f"sqlite:///{tmp_path / 'viejo.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE sagas (id TEXT PRIMARY KEY, estado TEXT, "
                       "CHECK (estado IN ('EN_CURSO','COMPLETADA','COMPENSANDO','COMPENSADA','COMPENSACION_PENDIENTE')))"))
    with pytest.raises(RuntimeError, match="migración"):
        sg.verificar_esquema(engine)


def test_aviso_http_de_pagos_es_autenticado_y_solo_una_pista(esperando, monkeypatch):
    sg, obj, sessions, events, clients = esperando
    api = importlib.import_module("app.orchestrator_main")
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "test-orchestrator-key-with-32-characters")
    monkeypatch.setenv("PAGOS_CALLBACK_KEY", "test-callback-key-with-32-characters-ok")
    monkeypatch.setattr(api, "orchestrator", obj)
    monkeypatch.setattr(api, "sessions", sessions)
    monkeypatch.setattr(api, "engine", sessions.kw["bind"])
    from fastapi.testclient import TestClient
    result, _ = run(obj)
    cuerpo = {"payment_key": f"{result['id']}:COBRAR", "estado": "COBRADO"}
    with TestClient(api.app) as cli:
        assert "/internal/pagos/resultado" not in cli.get("/openapi.json").json()["paths"]
        assert cli.post("/internal/pagos/resultado", json=cuerpo).status_code == 401
        assert cli.post("/internal/pagos/resultado", json=cuerpo, headers={"X-Callback-Key": "x" * 40}).status_code == 401
        h = {"X-Callback-Key": "test-callback-key-with-32-characters-ok"}
        assert cli.post("/internal/pagos/resultado", json={**cuerpo, "payment_key": "nada:COBRAR"}, headers=h).status_code == 404
        # Pagos (la fuente de verdad) aún dice PENDIENTE: el aviso "COBRADO" no basta para inscribir.
        assert cli.post("/internal/pagos/resultado", json=cuerpo, headers=h).status_code == 202
        assert estado_de(sg, sessions, result["id"])[0] == "ESPERANDO_PAGO" and "INSCRIBIR" not in events
        clients["COBRAR"].estado = "COBRADO"
        assert cli.post("/internal/pagos/resultado", json=cuerpo, headers=h).status_code == 202
        assert estado_de(sg, sessions, result["id"])[0] == "COMPLETADA"
        vista = cli.get(f"/v1/sagas/{result['id']}", headers={"X-API-Key": "test-orchestrator-key-with-32-characters"})
        assert vista.json()["estado"] == "COMPLETADA"


class _Pagos(BaseHTTPRequestHandler):
    visto = []

    def log_message(self, *a):
        pass

    def _responder(self, cuerpo):
        _Pagos.visto.append((self.command, self.path, self.headers.get("X-API-Key")))
        raw = json.dumps(cuerpo).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.endswith("/refund"):
            return self._responder({"estado": "CANCELADO"})
        self._responder({"estado": "PENDIENTE", "payment_id": "p9", "redirect_url": "http://gw/v1/pagos/ir/p9",
                         "expira_at": "2030-01-01T00:00:00Z", "token": "no-debe-pasar"})

    def do_GET(self):
        self._responder({"estado": "COBRADO"})


def test_adaptador_real_usa_la_credencial_de_pagos_y_entiende_el_cobro_pendiente(m, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Pagos)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PAGOS_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PAGOS_API_KEY", "clave-de-pagos-" + "p" * 20)
    monkeypatch.setenv("SERVICE_API_KEY", "clave-ADMIN-de-inscripcion-" + "a" * 10)
    sc = importlib.import_module("app.saga_clients")
    cobrar = sc.clients_from_env()["COBRAR"]
    saga = {"id": "abc", "alumno_id": "a", "curso_id": 1, "monto": 100}
    try:
        pendiente = cobrar.forward(saga, "abc:COBRAR")
        assert pendiente == {"estado": "PENDIENTE", "payment_id": "p9", "redirect_url": "http://gw/v1/pagos/ir/p9",
                             "expira_at": "2030-01-01T00:00:00Z"}  # sin el token de Webpay
        assert cobrar.query(saga, "abc:COBRAR") == {"estado": "COBRADO"}
        cobrar.compensate(saga, "abc:comp:COBRAR", "abc:COBRAR")  # CANCELADO es éxito de la compensación
    finally:
        server.shutdown()
    assert {k for _, _, k in _Pagos.visto} == {"clave-de-pagos-" + "p" * 20}  # nunca la clave admin de Inscripción
    assert ("GET", "/v1/payments/by-key/abc%3ACOBRAR", "clave-de-pagos-" + "p" * 20) in _Pagos.visto


# --- Hallazgo del experimento saga_fallas (experiment/scripts/saga_fallas.py) -----------------------------------
def _cliente_que_falla(sc):
    def cobrar(s, key, timeout):
        raise TimeoutError

    def reembolsar(s, key, original, timeout):
        return None  # el reembolso responde bien: no pasa por Transbank

    return sc.Client(cobrar, reembolsar, threshold=5)


def test_control_el_circuito_abre_con_cinco_cobros_fallidos_seguidos(m):
    sc = importlib.import_module("app.saga_clients")
    cliente = _cliente_que_falla(sc)
    for _ in range(5):
        with pytest.raises(sc.DependencyFailure):
            cliente.forward({}, "k")
    assert not cliente.available


def test_el_circuito_del_cobro_abre_aunque_cada_falla_vaya_seguida_de_un_reembolso_exitoso(m):
    sc = importlib.import_module("app.saga_clients")
    cliente = _cliente_que_falla(sc)
    for _ in range(5):
        with pytest.raises(sc.DependencyFailure):
            cliente.forward({}, "k")             # falla el cobro…
        cliente.compensate({}, "c", "k")         # …y la saga compensa: es lo que ocurre con cada matrícula fallida
    assert not cliente.available
