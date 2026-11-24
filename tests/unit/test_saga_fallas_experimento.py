"""El experimento saga_fallas (experiment/scripts/saga_fallas.py) no se ejecuta en cada corrida de pruebas (dura
minutos), pero sus piezas deterministas sí: formato de /metrics que lee, fusión de CSV, plan de fases y generación
de figuras y resumen a partir de datos sintéticos. Así un cambio de nombre en las métricas o en el CSV lo rompe aquí."""
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from pagos.app.config import Settings
from pagos.app.main import create_app
from tests.conftest import ROOT
from tests.support.fake_transbank import FakeTransbank


def _cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, Path(ROOT) / ruta)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def exp():
    return _cargar("saga_fallas_script", "experiment/scripts/saga_fallas.py")


@pytest.fixture(scope="module")
def graf():
    sys.path.insert(0, str(Path(ROOT) / "experiment"))
    return _cargar("graficar_saga_fallas_mod", "experiment/graficar_saga_fallas.py")


def test_el_experimento_lee_las_metricas_reales_de_pagos(exp, tmp_path):
    fake = FakeTransbank(api_key="SECRET").start()
    cfg = Settings(database_url=f"sqlite:///{tmp_path / 'p.db'}", api_key="k" * 32, internal_key="i" * 32, callback_key="c" * 32,
                   tbk_base_url=fake.url, tbk_api_key="SECRET", reconcile_seconds=0)
    try:
        with TestClient(create_app(cfg, start_worker=False)) as c:
            c.post("/v1/payments", json={"alumno_id": "a", "curso_id": 1, "monto": 100}, headers={"X-API-Key": "k" * 32, "Idempotency-Key": "x:COBRAR"})
            ms = exp.parsear_metricas(c.get("/metrics").text)
    finally:
        fake.stop()
    assert exp._circuito(ms, "TRANSBANK") == "CLOSED"
    assert exp._valor(ms, "transbank_intentos_total") == 1
    for nombre in ("bulkhead_rechazos_total", "circuito_rechazos_total"):
        assert any(n == nombre for n, _, _ in ms), nombre
    assert exp._valor(ms, "pagos_total", estado="PENDIENTE") == 1


def test_parseo_de_metricas_del_orquestador(exp):
    texto = ('sagas_total{estado="ESPERANDO_PAGO"} 7\ncompensaciones_total 3\n'
             'circuito_estado{dependencia="COBRAR",estado="OPEN"} 1\ncircuito_estado{dependencia="COBRAR",estado="CLOSED"} 0\n'
             'bulkhead_rechazos_total{dependencia="COBRAR"} 12\nsaga_paso_duracion_seconds_sum{paso="COBRAR"} 1.5\n')
    ms = exp.parsear_metricas(texto)
    assert exp._valor(ms, "sagas_total", estado="ESPERANDO_PAGO") == 7
    assert exp._circuito(ms, "COBRAR") == "OPEN" and exp._circuito(ms, "RESERVAR") == "?"
    assert exp._valor(ms, "bulkhead_rechazos_total", dependencia="COBRAR") == 12


def test_plan_de_fases_y_variantes(exp):
    for rapido in (True, False):
        fases = exp.plan(rapido)
        assert [f.nombre for f in fases] == ["antes", "pagos_lento", "pagos_recuperacion", "lenta", "alivio", "bulkhead", "pico", "caida", "recuperacion"]
        assert next(f for f in fases if f.nombre == "bulkhead").destino == "pagos"
        assert next(f for f in fases if f.nombre == "bulkhead").latencia_ms < 1500   # menor que el timeout: aísla el bulkhead
        assert next(f for f in fases if f.nombre == "pico").latencia_ms > 1500       # mayor: timeouts reales
    assert int(exp.PATRONES["sin_patron"]["PAGOS_BULKHEAD"]) > 1000 and int(exp.PATRONES["con_patron"]["PAGOS_BULKHEAD"]) == 8


def test_fusion_de_corridas_conserva_lo_que_no_se_repite(exp, tmp_path):
    ruta = tmp_path / "r.csv"
    exp.escribir_csv(ruta, [{"variante": "con_patron", "rep": 1, "x": 1}, {"variante": "sin_patron", "rep": 1, "x": 2},
                            {"variante": "con_patron", "rep": 2, "x": 3}])
    fusion = exp.fusionar(ruta, [{"variante": "con_patron", "rep": 1, "x": 99}], ["con_patron"], [1])
    assert sorted((f["variante"], int(f["rep"]), int(f["x"])) for f in fusion) == [("con_patron", 1, 99), ("con_patron", 2, 3), ("sin_patron", 1, 2)]


def test_percentil_e_invariante_de_rut(exp):
    assert exp.percentil([1, 2, 3, 4, 5], 50) == 3 and exp.percentil([], 95) != exp.percentil([], 95)  # NaN
    assert exp.rut_con_dv(12345678) == "12345678-5"


def _csv_sintetico(raw, ts):
    """Dos variantes × una repetición, con la forma exacta que escribe el experimento."""
    fases = [("antes", 0, 2, 4, 0, 0), ("lenta", 2, 4, 4, 3000, 0), ("alivio", 4, 6, 4, 0, 0), ("bulkhead", 6, 8, 60, 1200, 0),
             ("pico", 8, 10, 60, 3000, 0), ("caida", 10, 12, 4, 0, 1), ("recuperacion", 12, 14, 4, 0, 0)]
    reqs, sondas, mets, marcas, estados = [], [], [], [], []
    for v in ("con_patron", "sin_patron"):
        lento = v == "sin_patron"
        for f, a, b, w, lat, caido in fases:
            marcas.append(dict(variante=v, rep=1, fase=f, t_ini=a, t_fin=b, workers=w, think_s=0.1, tbk_latencia_ms=lat, tbk_caido=caido,
                               destino="pagos" if f == "bulkhead" else "matriculas"))
            for i in range(10):
                t = a + i * (b - a) / 10
                falla = lat or caido
                codigo = 503 if falla else (201 if f == "bulkhead" else 202)
                reqs.append(dict(t=t, fase=f, codigo=codigo, estado="X", latencia_s=(1.6 if lento and lat else 0.05), variante=v, rep=1))
                for nombre in ("orq_sagas", "ins_enrollments", "pagos_health"):
                    sondas.append(dict(t=t, fase=f, sonda=nombre, codigo=200, latencia_s=0.01, variante=v, rep=1))
                fila = dict(t=t, fase=f, variante=v, rep=1, orq_ok=1, pagos_ok=1, pagos_cb="OPEN" if (falla and not lento) else "CLOSED",
                            orq_cb_COBRAR="CLOSED", pagos_bulkhead_rech=max(0, min(t-6, 2))*25 if not lento else 0, pagos_circuito_rech=0, pagos_tbk_intentos=int(t*5),
                            orq_bulkhead_rech_RESERVAR=0, orq_bulkhead_rech_COBRAR=0, orq_bulkhead_rech_INSCRIBIR=0)
                for e in ("EN_CURSO", "ESPERANDO_PAGO", "COMPLETADA", "COMPENSANDO", "COMPENSADA", "COMPENSACION_PENDIENTE"):
                    fila[f"orq_sagas_{e}"] = i
                mets.append(fila)
        estados.append(dict(variante=v, rep=1, momento="final", t=14, convergencia_s=1.0, sagas_en_vuelo=0, sagas_COMPLETADA=0, sagas_ESPERANDO_PAGO=5,
                            sagas_COMPENSADA=5, sagas_COMPENSACION_PENDIENTE=0, cupos_huerfanos=0, cobros_huerfanos=0))
    esc = []
    for e, antes, despues in (("E1_exito", 0, 5), ("E2_falla_ultimo_paso", 0, 0)):
        for m, val in (("antes", antes), ("despues", despues)):
            esc.append(dict(escenario=e, momento=m, cupos_ocupados=val, inscripciones_CONFIRMADA=val, pagos_COBRADO=val,
                            pagos_REEMBOLSADO=5 if (e.startswith("E2") and m == "despues") else 0))
        esc.append(dict(escenario=e, saga=1, estado_final="X", latencia_solicitud_s=0.03, pago_a_estado_final_s=0.1, pasos_firmes="A>B", invariantes_ok=1))
    for nombre, filas in (("requests", reqs), ("sondas", sondas), ("metricas", mets), ("fases", marcas), ("estados", estados), ("escenarios", esc)):
        pd.DataFrame(filas).to_csv(raw / f"saga_fallas_{ts}_{nombre}.csv", index=False)


def test_las_figuras_y_el_resumen_se_generan_desde_los_csv(graf, tmp_path, monkeypatch):
    raw, out = tmp_path / "raw", tmp_path / "results"
    raw.mkdir()
    monkeypatch.setattr(graf, "RAW", raw)
    monkeypatch.setattr(graf, "OUT", out)
    _csv_sintetico(raw, "t1")
    graf.generar("t1")
    for f in ("saga_fallas_cascada_t1.png", "saga_fallas_fases_t1.png", "saga_fallas_antes_despues_t1.png", "saga_fallas_fases_t1.csv",
              "saga_fallas_fases_por_rep_t1.csv", "resumen_saga_fallas_t1.md"):
        assert (out / f).stat().st_size > 0, f
    resumen = (out / "resumen_saga_fallas_t1.md").read_text(encoding="utf-8")
    assert "E1_exito" in resumen and "Bulkhead aislado" in resumen and "cupos huérfanos 0" in resumen
    assert "El circuito Orquestador→Pagos no abrió" in resumen  # el detector del hallazgo se activa con estos datos
    tabla = pd.read_csv(out / "saga_fallas_fases_t1.csv")
    assert set(tabla["fase"]) == set(graf.FASES) - {"pagos_lento", "pagos_recuperacion"} and set(tabla["variante"]) == {"con_patron", "sin_patron"}
