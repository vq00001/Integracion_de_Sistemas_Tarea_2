"""H10: migración desde H9, conservación de datos y rollback atómico."""
import multiprocessing
import sqlite3

import pytest
from sqlalchemy import event, select

from pagos.app.database import make_engine, prepare_database
from pagos.app.models import Payment, PaymentOperation, PaymentSchemaVersion, RefundDebt, RefundKey
from tests.unit.test_h9_migracion_pagos import _legacy, _migrar_proceso, _snapshot


# Tablas de soporte congeladas de H9: esta base aún no tiene refund_debts.
H9_DDL = """
CREATE TABLE payment_schema_versions (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL);
INSERT INTO payment_schema_versions VALUES (1, '2026-01-01 00:00:00.000000');
CREATE TABLE payment_refund_keys (
    key VARCHAR(128) PRIMARY KEY, original_key VARCHAR(128) NOT NULL, created_at DATETIME NOT NULL
);
INSERT INTO payment_refund_keys VALUES ('legacy-refund', 'devuelto', '2026-01-01 00:00:00.000000');
INSERT INTO payment_refund_keys VALUES ('legacy-cancel', 'cancelado', '2026-01-01 00:00:00.000000');
CREATE TABLE payment_operations (
    payment_id VARCHAR(36) NOT NULL, operation VARCHAR(8) NOT NULL, phase VARCHAR(12) NOT NULL,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    PRIMARY KEY (payment_id, operation), CHECK (operation IN ('CREATE', 'REFUND')),
    CHECK (phase IN ('RUNNING', 'UNCERTAIN', 'DONE'))
);
INSERT INTO payment_operations VALUES
    ('incierto', 'CREATE', 'UNCERTAIN', '2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000');
"""


def _base_h9(path, refund_phase=None):
    _legacy(path)
    with sqlite3.connect(path) as db:
        db.executescript(H9_DDL)
        for pid, status, response_code in (("deuda", "AUTHORIZED", 0), ("rechazo-real", "FAILED", -1)):
            db.execute("""INSERT INTO payments
                (id, idempotency_key, alumno_id, curso_id, monto, estado, buy_order, session_id,
                 token, expires_at, tbk_status, response_code, notificado_at, created_at, updated_at)
                VALUES (?, ?, 'alumno-legacy', 1, 15000, 'RECHAZADO', ?, 'sesion-legacy', ?,
                        '2099-01-01 00:00:00.000000', ?, ?, '2026-01-01 00:00:00.000000',
                        '2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')""",
                       (pid, pid, f"orden-{pid}", f"token-{pid}", status, response_code))
        if refund_phase:
            db.execute("INSERT INTO payment_operations VALUES ('deuda', 'REFUND', ?, ?, ?)",
                       (refund_phase, "2026-01-01 00:00:00.000000", "2026-01-01 00:00:00.000000"))


def _verificar(engine, before):
    with engine.connect() as conn:
        rows = conn.execute(select(Payment.__table__)).mappings().all()
        assert len(rows) == len(before["rows"])
        original = {row[0]: row for row in before["rows"]}
        # Comparar con sqlite3 mantiene idéntica representación de fechas antes/después.
        raw = conn.exec_driver_sql("SELECT * FROM payments ORDER BY id").all()
        for row in raw:
            esperado = list(original[row[0]])
            if row[0] == "deuda":
                esperado[5] = "PROCESANDO"
            assert tuple(row) == tuple(esperado)
        assert conn.scalars(select(PaymentSchemaVersion.version).order_by(PaymentSchemaVersion.version)).all() == [1, 2]
        debt = conn.execute(select(RefundDebt.__table__)).mappings().one()
        assert debt["payment_id"] == "deuda" and debt["reason"] == "LEGACY_AUTHORIZED"
        assert debt["authorized_amount"] is None and debt["authorized_buy_order"] is None
        assert conn.scalar(select(PaymentOperation.phase).where(
            PaymentOperation.payment_id == "deuda", PaymentOperation.operation == "REFUND")) == "UNCERTAIN"
        assert conn.scalar(select(PaymentOperation.phase).where(
            PaymentOperation.payment_id == "incierto", PaymentOperation.operation == "CREATE")) == "UNCERTAIN"
        assert dict(conn.execute(select(RefundKey.key, RefundKey.original_key)).all()) == {
            "legacy-refund": "devuelto", "legacy-cancel": "cancelado"}


@pytest.mark.parametrize("refund_phase", [None, "RUNNING", "DONE"], ids=["sin-operacion", "en-curso", "terminada"])
def test_h10_actualizacion_desde_h9_recupera_deuda_y_conserva_datos(tmp_path, refund_phase):
    path = tmp_path / "h9.db"
    _base_h9(path, refund_phase)
    before = _snapshot(path)
    engine = make_engine(f"sqlite:///{path}")
    try:
        for _ in range(2):
            prepare_database(engine)
            _verificar(engine, before)
            assert _snapshot(path)["schema"] == before["schema"]
    finally:
        engine.dispose()


def test_h10_fallo_de_migracion_revierte_deuda_estados_y_version(tmp_path):
    path = tmp_path / "rollback.db"
    _base_h9(path)
    before = _snapshot(path)
    engine = make_engine(f"sqlite:///{path}")

    def interrumpir(_conn, _cursor, statement, parameters, _context, _many):
        if statement.startswith("INSERT INTO payment_schema_versions") and parameters[0] == 2:
            raise RuntimeError("interrupción de migración H10")

    try:
        event.listen(engine, "before_cursor_execute", interrumpir)
        with pytest.raises(RuntimeError, match="interrupción de migración H10"):
            prepare_database(engine)
        event.remove(engine, "before_cursor_execute", interrumpir)
        assert _snapshot(path) == before
        with engine.connect() as conn:
            assert conn.scalars(select(PaymentSchemaVersion.version)).all() == [1]
            assert conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE name = 'payment_refund_debts'").first() is None
        prepare_database(engine)
        _verificar(engine, before)
    finally:
        engine.dispose()


def test_h10_dos_procesos_importan_una_sola_deuda_desde_h9(tmp_path):
    path = tmp_path / "concurrente.db"
    _base_h9(path)
    before = _snapshot(path)
    mp = multiprocessing.get_context("spawn")
    inicio, salida = mp.Barrier(3), mp.Queue()
    workers = [mp.Process(target=_migrar_proceso, args=(f"sqlite:///{path}", inicio, salida)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        inicio.wait(timeout=15)
        results = [salida.get(timeout=20) for _ in workers]
        assert results == [{"versions": [1, 2]}, {"versions": [1, 2]}], results
        engine = make_engine(f"sqlite:///{path}")
        try:
            _verificar(engine, before)
        finally:
            engine.dispose()
    finally:
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        salida.close()
        salida.join_thread()
