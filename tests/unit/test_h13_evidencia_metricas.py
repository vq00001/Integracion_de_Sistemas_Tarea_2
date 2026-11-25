"""H13: tablas, figuras e interpretación usan la semántica de cada contador.

Todos los datos son sintéticos y toda salida vive en tmp_path. Los contadores
legacy son deliberadamente distintos para detectar un fallback engañoso.
"""
import re

import numpy as np
import pandas as pd
import pytest

from tests.unit.test_h6_evidencia_cascada import _data, graf
from tests.unit.test_h13_metricas_transbank import OPS, RAW


TABLE = {
    "protegidos": "intentos_protegidos_tbk",
    "simuladas": "fallas_simuladas_tbk",
    "cliente": "inicios_cliente_tbk",
}


def _datos(puntos=True, cero=False):
    data = _data()
    mt = data["metricas"]
    # Por fase de 2 segundos: 10 protegidos, 6 simuladas, 4 inicios del cliente.
    rates = {"protegidos": 5, "simuladas": 3, "cliente": 2}
    por_operacion = {
        "protegidos": {"crear": 4, "estado": 1},
        "simuladas": {"crear": 3},
        "cliente": {"crear": 1, "estado": 1},
    }
    for kind, column in RAW.items():
        mt[column] = mt.t * (0 if cero else rates[kind])
        for op in OPS:
            mt[f"{column}_{op}"] = mt.t * (0 if cero else por_operacion[kind].get(op, 0))
    mt["pagos_tbk_intentos"] = 1000 + mt.t * 100  # no equivale a los inicios explícitos
    if puntos:
        # _data tiene tres muestras por fase: inicio, muestreo, fin.
        mt["punto"] = [p for _ in range(len(mt) // 3) for p in ("inicio", "muestreo", "fin")]
    return data


def _informe(graf, data, ts):
    per = graf.tabla_por_rep(data)
    agregado = graf.tabla_agregada(per)
    return graf.resumen(data, agregado, per, None, ts).read_text(encoding="utf-8")


@pytest.mark.parametrize("puntos", [True, False], ids=["limites-explicitos", "muestras-temporales"])
@pytest.mark.parametrize("cero", [False, True], ids=["contadores-distintos", "cero-medido"])
def test_h13_tabla_y_agregacion_separan_las_tres_etapas_por_fase(graf, puntos, cero):
    data = _datos(puntos, cero)
    per = graf.tabla_por_rep(data)
    assert set(TABLE.values()) <= set(per), "La tabla todavía mezcla admisiones y llamadas al cliente"
    for kind, expected in (("protegidos", 10), ("simuladas", 6), ("cliente", 4)):
        assert (per[TABLE[kind]] == (0 if cero else expected)).all()
    assert (per["intentos_a_tbk"] == per[TABLE["cliente"]]).all(), "El alias de intentos aún usa la columna legacy"
    for op in OPS:
        expected = 2 if op in ("crear", "estado") and not cero else 0
        column = f'{TABLE["cliente"]}_{op}'
        assert column in per and (per[column] == expected).all()
    agregado = graf.tabla_agregada(per)
    assert set(TABLE.values()) <= set(agregado)
    assert (agregado[TABLE["cliente"]] == (0 if cero else 4)).all()


@pytest.mark.parametrize("column", [*RAW.values(), RAW["cliente"] + "_crear"])
def test_h13_reinicio_de_cualquiera_de_los_contadores_nuevos_invalida_los_deltas(graf, column):
    data = _datos()
    mt = data["metricas"]
    mt.loc[(mt.variante == "con_patron") & (mt.rep == 1) & (mt.fase == "caida"), column] = 0
    with pytest.raises(ValueError, match="(?i)(contador|reset|reinici|métrica)"):
        graf.tabla_por_rep(data)


def test_h13_csv_legacy_no_se_reinterpreta_como_llamadas_iniciadas_al_cliente(graf):
    per = graf.tabla_por_rep(_data())
    assert per["intentos_a_tbk"].isna().all(), "Un contador histórico ambiguo se presentó como inicio de cliente"
    for column in TABLE.values():
        assert column in per and per[column].isna().all()


def test_h13_frontera_sin_muestra_de_cliente_permanece_desconocida(graf):
    data = _datos()
    mt = data["metricas"]
    mask = (mt.variante == "con_patron") & (mt.rep == 1) & (mt.fase == "caida") & (mt.punto == "fin")
    for column in (RAW["cliente"], *(RAW["cliente"] + "_" + op for op in OPS)):
        mt.loc[mask, column] = np.nan
    per = graf.tabla_por_rep(data)
    assert set(TABLE.values()) <= set(per)
    row = per[(per.variante == "con_patron") & (per.rep == 1) & (per.fase == "caida")].iloc[0]
    assert pd.isna(row[TABLE["cliente"]]) and pd.isna(row["intentos_a_tbk"])
    assert row[TABLE["protegidos"]] == 10 and row[TABLE["simuladas"]] == 6
    agregado = graf.tabla_agregada(per)
    combined = agregado[(agregado.variante == "con_patron") & (agregado.fase == "caida")].iloc[0]
    assert combined["n_reps"] == 3 and combined["n_reps_inicios_cliente_tbk"] == 2
    assert pd.isna(combined[TABLE["cliente"]]), "Una mediana parcial ocultó la repetición sin telemetría"


def test_h13_ultima_muestra_ausente_no_se_sustituye_por_una_lectura_anterior(graf):
    data = _datos(puntos=False)
    mt = data["metricas"]
    indices = mt[(mt.variante == "con_patron") & (mt.rep == 1) & (mt.fase == "caida")].index
    # El fixture duplica el instante de frontera en el inicio de la fase siguiente.
    # Ambas lecturas de ese instante deben faltar; una segunda lectura válida sí sirve.
    frontera = mt.loc[indices[-1], "t"]
    mt.loc[(mt.variante == "con_patron") & (mt.rep == 1) & (mt.t == frontera), RAW["cliente"]] = np.nan
    per = graf.tabla_por_rep(data)
    row = per[(per.variante == "con_patron") & (per.rep == 1) & (per.fase == "caida")].iloc[0]
    assert pd.isna(row[TABLE["cliente"]])


def test_h13_informe_explica_simulaciones_e_inicios_sin_afirmar_recepcion_en_la_pasarela(graf):
    text = _informe(graf, _datos(), "h13_sintetico_nuevo")
    assert all(column in text for column in TABLE.values()), "El informe omite la separación de los contadores"
    lower = text.lower()
    assert "llamadas iniciadas" in lower and "cliente" in lower
    assert re.search(r"no (?:garantiza|demuestra|prueba).{0,100}(?:recepción|recib|llegada)", lower), (
        "Entrar al cliente no demuestra que la pasarela recibió la petición")


def test_h13_informe_legacy_declara_que_falta_telemetria_del_cliente(graf):
    text = _informe(graf, _data(), "h13_sintetico_legacy")
    assert RAW["cliente"] in text, "El informe presenta el CSV histórico como telemetría suficiente del cliente"
    assert "históric" in text.lower() and "desconocid" in text.lower()


def _capturar_figura(graf, data, monkeypatch, ts):
    capturadas = []
    with monkeypatch.context() as context:
        context.setattr(graf.plt, "close", lambda fig=None: capturadas.append(fig))
        path = graf.fig_cascada(data, ts)
    assert path.parent == graf.OUT and path.is_file()
    return [fig for fig in capturadas if hasattr(fig, "axes")][-1]


def test_h13_figura_traza_los_tres_contadores_correctos_y_no_el_alias_legacy(graf, monkeypatch):
    data = _datos()
    fig = _capturar_figura(graf, data, monkeypatch, "h13_sintetico_curvas")
    try:
        for axis, variante in zip((fig.axes[8], fig.axes[9]), graf.VARIANTES):
            rows = data["metricas"][(data["metricas"].variante == variante) & (data["metricas"].rep == 1)]
            for kind, fragmento in (("protegidos", "proteg"), ("simuladas", "simulad"), ("cliente", "cliente")):
                lines = [line for line in axis.lines if fragmento in line.get_label().lower()]
                assert len(lines) == 1, f"Falta o se duplica la curva {kind} de Pagos→TBK"
                np.testing.assert_array_equal(lines[0].get_ydata(), rows[RAW[kind]].to_numpy())
    finally:
        graf.plt.close(fig)


def test_h13_figura_legacy_no_disfraza_el_contador_ambiguo_de_trafico_real(graf, monkeypatch):
    fig = _capturar_figura(graf, _data(), monkeypatch, "h13_sintetico_curvas_legacy")
    try:
        for axis in (fig.axes[8], fig.axes[9]):
            for line in axis.lines:
                label = line.get_label().lower()
                if "tbk" in label or "transbank" in label:
                    assert "históric" in label or "legacy" in label, "La curva legacy no declara su semántica ambigua"
            notes = " ".join(text.get_text().lower() for text in (*axis.texts, *fig.texts))
            assert any(word in notes for word in ("desconocid", "no disponible", "sin telemetría"))
    finally:
        graf.plt.close(fig)


def test_h13_control_generacion_sintetica_conserva_todos_los_csv_de_entrada(graf, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(graf, "RAW", raw)
    ts = "h13_control_sintetico"
    for name, frame in _datos().items():
        frame.to_csv(raw / f"saga_fallas_{ts}_{name}.csv", index=False)
    before = {path.name: path.read_bytes() for path in raw.iterdir()}
    graf.generar(ts)
    assert {path.name: path.read_bytes() for path in raw.iterdir()} == before
    assert (graf.OUT / f"resumen_saga_fallas_{ts}.md").is_file()
