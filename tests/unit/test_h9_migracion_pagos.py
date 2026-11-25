"""Actualización desde el esquema anterior a H9, sin reconstruir payments."""
import multiprocessing
import sqlite3
import traceback

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pagos.app.config import Settings
from pagos.app.database import make_engine, prepare_database
from pagos.app.main import create_app
from pagos.app.models import PaymentOperation, PaymentSchemaVersion, RefundKey
from pagos.app.transbank import TransbankClient


# DDL congelado de la versión anterior: no se deriva del modelo vigente.
LEGACY_DDL = """
CREATE TABLE payments (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    alumno_id VARCHAR(36) NOT NULL, curso_id INTEGER NOT NULL, monto INTEGER NOT NULL,
    estado VARCHAR(16) NOT NULL CHECK (estado IN
        ('PENDIENTE','PROCESANDO','COBRADO','RECHAZADO','ABORTADO','EXPIRADO','CANCELADO','REEMBOLSADO')),
    buy_order VARCHAR(26) NOT NULL UNIQUE, session_id VARCHAR(64) NOT NULL,
    token VARCHAR(80) UNIQUE, webpay_url VARCHAR(256), expires_at DATETIME NOT NULL,
    tbk_status VARCHAR(24), response_code INTEGER, authorization_code VARCHAR(16),
    payment_type VARCHAR(4), refund_key VARCHAR(128) UNIQUE, refund_type VARCHAR(16),
    notificado_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
);
CREATE INDEX ix_payments_estado ON payments (estado);
"""
KEY = "h9-schema-local-api-key-32-characters"


def _legacy(path):
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_DDL)
        for name, estado, token, refund_key in (
            ("pendiente", "PENDIENTE", "token-pendiente", None),
            ("cobrado", "COBRADO", "token-cobrado", None),
            ("devuelto", "REEMBOLSADO", "token-devuelto", "legacy-refund"),
            ("cancelado", "CANCELADO", None, "legacy-cancel"),
            ("incierto", "PENDIENTE", None, None),
        ):
            db.execute("""INSERT INTO payments
                (id, idempotency_key, alumno_id, curso_id, monto, estado, buy_order, session_id,
                 token, webpay_url, expires_at, refund_key, refund_type, created_at, updated_at)
                VALUES (?, ?, 'alumno-legacy', 1, 15000, ?, ?, 'sesion-legacy', ?,
                        'http://webpay.test', '2099-01-01 00:00:00.000000', ?, ?,
                        '2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')""",
                       (name, name, estado, f"orden-{name}", token, refund_key,
                        "REVERSED" if estado == "REEMBOLSADO" else None))


def _snapshot(path):
    with sqlite3.connect(path) as db:
        return {"rows": db.execute("SELECT * FROM payments ORDER BY id").fetchall(),
                "schema": db.execute("SELECT type, name, sql FROM sqlite_master "
                                     "WHERE tbl_name = 'payments' ORDER BY type, name").fetchall()}


def test_h9_actualizacion_conserva_datos_importa_claves_y_es_repetible(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    _legacy(path)
    before = _snapshot(path)
    settings = Settings(database_url=f"sqlite:///{path}", api_key=KEY, internal_key="i" * 32,
                        callback_key="c" * 32, reconcile_seconds=0)

    def sin_pasarela(*_):
        pytest.fail("Una migración o un replay no debe llamar a la pasarela")

    for method in ("crear", "confirmar", "estado", "reembolsar"):
        monkeypatch.setattr(TransbankClient, method, sin_pasarela)
    for _ in range(2):
        app = create_app(settings, notifier=lambda *_: True, start_worker=False)
        try:
            with TestClient(app) as client:
                assert _snapshot(path) == before
                with app.state.service.sessions() as db:
                    assert db.scalars(select(PaymentSchemaVersion.version)).all() == [1]
                    assert {k.key: k.original_key for k in db.scalars(select(RefundKey))} == {
                        "legacy-refund": "devuelto", "legacy-cancel": "cancelado"}
                    uncertain = db.get(PaymentOperation, ("incierto", "CREATE"))
                    assert uncertain.phase == "UNCERTAIN"
                headers = {"X-API-Key": KEY, "Idempotency-Key": "pendiente"}
                replay = client.post("/v1/payments", headers=headers,
                                     json={"alumno_id": "alumno-legacy", "curso_id": 1, "monto": 15000})
                assert replay.status_code == 200 and replay.json()["payment_id"] == "pendiente"
                headers["Idempotency-Key"] = "legacy-refund"
                replay = client.post("/v1/payments/refund", headers=headers, json={"original_key": "devuelto"})
                assert replay.status_code == 200 and replay.json()["estado"] == "REEMBOLSADO"
                conflict = client.post("/v1/payments/refund", headers=headers, json={"original_key": "cobrado"})
                assert conflict.status_code == 422 and conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
                headers["Idempotency-Key"] = "incierto"
                retry = client.post("/v1/payments", headers=headers,
                                    json={"alumno_id": "alumno-legacy", "curso_id": 1, "monto": 15000})
                assert retry.status_code == 503 and retry.json()["error"]["code"] == "REINTENTAR"
                assert _snapshot(path) == before
        finally:
            app.state.service.sessions.kw["bind"].dispose()


def _migrar_proceso(url, inicio, salida):
    engine = make_engine(url)
    try:
        inicio.wait(timeout=15)
        prepare_database(engine)
        with engine.connect() as conn:
            salida.put({"versions": conn.scalars(select(PaymentSchemaVersion.version)).all()})
    except Exception:
        salida.put({"error": traceback.format_exc()})
    finally:
        engine.dispose()


@pytest.mark.parametrize("legacy", [False, True], ids=["nueva", "anterior"])
def test_h9_dos_procesos_pueden_actualizar_la_misma_base(tmp_path, legacy):
    path = tmp_path / "compartida.db"
    if legacy:
        _legacy(path)
        before = _snapshot(path)
    mp = multiprocessing.get_context("spawn")
    inicio, salida = mp.Barrier(3), mp.Queue()
    workers = [mp.Process(target=_migrar_proceso, args=(f"sqlite:///{path}", inicio, salida))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        inicio.wait(timeout=15)
        results = [salida.get(timeout=20) for _ in workers]
        assert results == [{"versions": [1]}, {"versions": [1]}], "\n".join(r.get("error", str(r)) for r in results)
        if legacy:
            assert _snapshot(path) == before
    finally:
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        salida.close()
        salida.join_thread()
