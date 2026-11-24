"""Figuras, tablas y resumen interpretado del experimento saga_fallas (experiment/scripts/saga_fallas.py).

Uso:  python experiment/graficar_saga_fallas.py [<ts>]      (por defecto, el experimento más reciente)
Lee experiment/data/raw/saga_fallas_<ts>_*.csv y escribe en experiment/results/:
  saga_fallas_cascada_<ts>.png       línea de tiempo (1.ª repetición) con y sin patrón: latencias, circuito, bulkhead, sagas
  saga_fallas_fases_<ts>.png         comparación por fase (mediana de las repeticiones; barras de error = rango del p95)
  saga_fallas_antes_despues_<ts>.png estado de los tres servicios antes/después en E1 y E2
  saga_fallas_fases_<ts>.csv|md      tabla por variante y fase (medianas) y saga_fallas_fases_por_rep_<ts>.csv (cada repetición)
  resumen_saga_fallas_<ts>.md        lectura de la evidencia con los números reales de la corrida
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BASE = Path(__file__).resolve().parent
RAW, OUT = BASE / "data" / "raw", BASE / "results"
VARIANTES = ("con_patron", "sin_patron")
ETIQ = {"con_patron": "CON patrón (Circuit Breaker + Bulkhead)", "sin_patron": "SIN patrón (circuito y bulkhead inertes)"}
COLOR = {"con_patron": "#1f77b4", "sin_patron": "#d62728"}
FASES = ("antes", "lenta", "alivio", "bulkhead", "pico", "caida", "recuperacion")
ACEPTADAS = (201, 202)  # 202 = matrícula en espera de pago; 201 = cobro creado (fase de carga directa a Pagos)
RAPIDA = 0.2  # una respuesta "rápida" (fail-fast) tarda menos de 200 ms


def _rel(ruta: Path) -> Path:
    try:
        return ruta.relative_to(BASE.parent)
    except ValueError:  # salida fuera del repositorio
        return ruta


def _ultimo_ts() -> str:
    candidatos = sorted(RAW.glob("saga_fallas_*_fases.csv"))
    if not candidatos:
        sys.exit("No hay datos: ejecuta experiment/scripts/saga_fallas.py")
    return candidatos[-1].name.removeprefix("saga_fallas_").removesuffix("_fases.csv")


def _leer(ts: str, nombre: str):
    ruta = RAW / f"saga_fallas_{ts}_{nombre}.csv"
    if not ruta.exists():
        return None
    df = pd.read_csv(ruta)
    df["rep"] = df["rep"].fillna(1).astype(int) if "rep" in df else 1
    return df


def _valor_en(m: pd.DataFrame, t: float, col: str) -> float:
    s = m[(m["t"] <= t) & m[col].notna()]
    return float(s[col].iloc[-1]) if len(s) else 0.0


def _sel(d: dict, v: str, rep: int):
    return (d[k][(d[k]["variante"] == v) & (d[k]["rep"] == rep)] for k in ("requests", "sondas", "metricas", "fases"))


def tabla_por_rep(d: dict) -> pd.DataFrame:
    filas = []
    for v in VARIANTES:
        for rep in sorted(d["requests"][d["requests"]["variante"] == v]["rep"].unique()):
            rq0, sd, mt, fs = _sel(d, v, rep)
            for _, f in fs.iterrows():
                r = rq0[(rq0["fase"] == f["fase"]) & (rq0["codigo"] != -1)]
                s = sd[sd["fase"] == f["fase"]]
                so, si, sp = (s[s["sonda"] == n] for n in ("orq_sagas", "ins_enrollments", "pagos_health"))
                dur = max(f["t_fin"] - f["t_ini"], 1e-9)
                delta = lambda col: _valor_en(mt, f["t_fin"], col) - _valor_en(mt, f["t_ini"], col)  # noqa: E731
                bulk_orq = sum(delta(c) for c in mt.columns if c.startswith("orq_bulkhead_rech_"))
                n = len(r)
                filas.append({
                    "variante": v, "rep": int(rep), "fase": f["fase"], "destino": f.get("destino", "matriculas"), "workers": int(f["workers"]),
                    "tbk_latencia_ms": int(f["tbk_latencia_ms"]), "tbk_caido": int(f["tbk_caido"]), "peticiones": n, "req_por_s": len(r) / dur,
                    "p50_s": r["latencia_s"].median(), "p95_s": r["latencia_s"].quantile(0.95), "max_s": r["latencia_s"].max(),
                    "pct_aceptadas": 100 * r["codigo"].isin(ACEPTADAS).mean() if n else np.nan,
                    "pct_503": 100 * (r["codigo"] == 503).mean() if n else np.nan,
                    "pct_otros": 100 * (~r["codigo"].isin(ACEPTADAS + (503,))).mean() if n else np.nan,
                    "pct_rapidas": 100 * (r["latencia_s"] < RAPIDA).mean() if n else np.nan,
                    "sonda_orq_p95_s": so["latencia_s"].quantile(0.95) if len(so) else np.nan,
                    "sonda_orq_fallos": int((so["codigo"] != 200).sum()),
                    "sonda_ins_p95_s": si["latencia_s"].quantile(0.95) if len(si) else np.nan,
                    "sonda_pagos_p95_s": sp["latencia_s"].quantile(0.95) if len(sp) else np.nan,
                    "rechazos_bulkhead_orq": bulk_orq,
                    "rechazos_bulkhead_pagos": delta("pagos_bulkhead_rech"),
                    "rechazos_circuito_tbk": delta("pagos_circuito_rech"), "intentos_a_tbk": delta("pagos_tbk_intentos"),
                })
    return pd.DataFrame(filas)


def tabla_agregada(por_rep: pd.DataFrame) -> pd.DataFrame:
    num = [c for c in por_rep.columns if c not in ("variante", "rep", "fase", "destino")]
    g = por_rep.groupby(["variante", "fase"], sort=False)
    t = g[num].median()
    t["p95_s_min"], t["p95_s_max"] = g["p95_s"].min(), g["p95_s"].max()
    t["p50_s_min"], t["p50_s_max"] = g["p50_s"].min(), g["p50_s"].max()
    t["n_reps"], t["destino"] = g["rep"].nunique(), g["destino"].first()
    t = t.reset_index()
    t["_o"] = t["fase"].map({f: i for i, f in enumerate(FASES)})
    return t.sort_values(["variante", "_o"]).drop(columns="_o").reset_index(drop=True)


def _sombrear(ax, fs: pd.DataFrame, etiquetar=False):
    colores = {"antes": "#e8f5e9", "lenta": "#fff3e0", "alivio": "#f1f8e9", "bulkhead": "#ede7f6", "pico": "#ffe0b2",
               "caida": "#ffcdd2", "recuperacion": "#e3f2fd"}
    for _, f in fs.iterrows():
        ax.axvspan(f["t_ini"], f["t_fin"], color=colores.get(f["fase"], "#eee"), alpha=0.9, lw=0)
        if etiquetar:
            ax.text((f["t_ini"] + f["t_fin"]) / 2, 1.02, f["fase"], transform=ax.get_xaxis_transform(), ha="center", fontsize=9)


def fig_cascada(d: dict, ts: str) -> Path:
    rep = int(d["requests"]["rep"].min())
    nreps = int(d["requests"]["rep"].nunique())
    fig, axes = plt.subplots(4, 2, figsize=(17, 14), sharex="col")
    lim = {}
    for j, v in enumerate(VARIANTES):
        rq, sd, mt, fs = _sel(d, v, rep)
        rq = rq[rq["codigo"] != -1]
        a1, a2, a3, a4 = axes[:, j]
        for a in axes[:, j]:
            _sombrear(a, fs, etiquetar=a is a1)
        for cods, col, nom in (((201, 202), "#1f77b4", "201/202 aceptada (en espera de pago)"), ((503,), "#d62728", "503 compensada / fail-fast")):
            s = rq[rq["codigo"].isin(cods)]
            a1.scatter(s["t"], s["latencia_s"], s=6, c=col, label=nom, alpha=0.6)
        otros = rq[~rq["codigo"].isin((201, 202, 503))]
        a1.scatter(otros["t"], otros["latencia_s"], s=10, c="k", marker="x", label="otro (502, timeout cliente…)")
        a1.axhline(RAPIDA, color="gray", ls=":", lw=1)
        a1.set_yscale("log"); a1.set_ylabel("cobros / matrículas\nlatencia (s)"); a1.legend(loc="upper left", fontsize=8)
        a1.set_title(ETIQ[v], fontsize=12, pad=24, color=COLOR[v], fontweight="bold")
        for nombre, col, nom in (("orq_sagas", "#ff7f0e", "GET /v1/sagas/{id} (mismo proceso que la saga)"),
                                 ("pagos_health", "#9467bd", "GET /v1/health de Pagos (mismo proceso que Transbank)"),
                                 ("ins_enrollments", "#2ca02c", "GET /v1/enrollments (otro servicio)")):
            s = sd[sd["sonda"] == nombre]
            a2.plot(s["t"], s["latencia_s"], color=col, lw=1.2, label=nom)
            malos = s[s["codigo"] != 200]
            a2.scatter(malos["t"], malos["latencia_s"], c="k", marker="x", s=25, zorder=5)
        a2.set_yscale("log"); a2.set_ylabel("el resto del sistema\nlatencia (s)"); a2.legend(loc="upper left", fontsize=8)
        for col, c, nom in (("pagos_cb", "#9467bd", "Pagos→Transbank"), ("orq_cb_COBRAR", "#8c564b", "Orquestador→Pagos")):
            a3.step(mt["t"], (mt[col] == "OPEN").astype(int), where="post", color=c, lw=1.6, label=f"circuito {nom} OPEN")
        a3.set_ylim(-0.1, 1.3); a3.set_yticks([0, 1]); a3.set_yticklabels(["CLOSED", "OPEN"])
        rech = sum(mt[c].fillna(0) for c in mt.columns if c.startswith("orq_bulkhead_rech_")) + mt["pagos_bulkhead_rech"].fillna(0)
        b = a3.twinx(); b.plot(mt["t"], rech, color="k", lw=1, ls="--", label="rechazos bulkhead (acum.)"); b.set_ylabel("rechazos acumulados")
        a3.legend(loc="upper left", fontsize=8); b.legend(loc="center left", fontsize=8)
        en_vuelo = mt["orq_sagas_EN_CURSO"].fillna(0) + mt["orq_sagas_COMPENSANDO"].fillna(0)
        for serie, c, nom in ((mt["orq_sagas_COMPENSADA"], "#d62728", "COMPENSADA"), (mt["orq_sagas_COMPENSACION_PENDIENTE"], "#000000", "COMPENSACION_PENDIENTE"),
                              (en_vuelo, "#ff7f0e", "en vuelo (EN_CURSO + COMPENSANDO)"), (mt["orq_sagas_ESPERANDO_PAGO"], "#1f77b4", "ESPERANDO_PAGO")):
            a4.plot(mt["t"], serie, color=c, lw=1.4, label=nom)
        a4.set_ylabel("sagas por estado"); a4.set_xlabel("tiempo desde el inicio de la carga (s)"); a4.legend(loc="upper left", fontsize=8)
        for i, a in enumerate((a1, a2, a4)):
            lim.setdefault(i, []).append(a.get_ylim())
    for i, fila in zip((0, 1, 2), (0, 1, 3)):
        lo, hi = min(x[0] for x in lim[i]), max(x[1] for x in lim[i])
        for j in range(2):
            axes[fila, j].set_ylim(lo, hi)
    fig.suptitle(f"Escenario 3 · misma carga y misma línea de tiempo, con y sin patrón (repetición {rep} de {nreps})", fontsize=14, y=0.995)
    fig.tight_layout()
    ruta = OUT / f"saga_fallas_cascada_{ts}.png"
    fig.savefig(ruta, dpi=130); plt.close(fig)
    return ruta


def fig_fases(t: pd.DataFrame, ts: str) -> Path:
    fig, axes = plt.subplots(1, 4, figsize=(23, 5))
    x = np.arange(len(FASES)); w = 0.38
    for ax, col, titulo, log in ((axes[0], "p50_s", "mediana de la latencia de cobro/matrícula (s)", True),
                                 (axes[1], "p95_s", "p95 de la latencia de cobro/matrícula (s)", True),
                                 (axes[2], "sonda_orq_p95_s", "p95 de GET /v1/sagas/{id} durante la falla (s)", True),
                                 (axes[3], "pct_rapidas", "% de respuestas en < 200 ms (fail-fast)", False)):
        for k, v in enumerate(VARIANTES):
            s = t[t["variante"] == v].set_index("fase").reindex(FASES)
            yerr = None
            if col in ("p50_s", "p95_s"):
                lo, hi = f"{col}_min", f"{col}_max"
                yerr = [(s[col] - s[lo]).fillna(0).clip(lower=0), (s[hi] - s[col]).fillna(0).clip(lower=0)]
            ax.bar(x + (k - 0.5) * w, s[col].fillna(0), w, color=COLOR[v], label=ETIQ[v], yerr=yerr, capsize=2, ecolor="#444")
        ax.set_xticks(x); ax.set_xticklabels(FASES, rotation=25); ax.set_title(titulo, fontsize=10)
        if log:
            ax.set_yscale("log")
    nreps = int(t["n_reps"].max())
    axes[0].legend(fontsize=8)
    fig.suptitle(f"Comparación por fase (mediana de {nreps} repetición(es); barras de error = rango entre repeticiones)")
    fig.tight_layout()
    ruta = OUT / f"saga_fallas_fases_{ts}.png"
    fig.savefig(ruta, dpi=130); plt.close(fig)
    return ruta


def fig_antes_despues(esc: pd.DataFrame, ts: str) -> Path:
    cols = [("cupos_ocupados", "cupos ocupados"), ("inscripciones_CONFIRMADA", "inscripciones\nconfirmadas"),
            ("pagos_COBRADO", "pagos\ncobrados"), ("pagos_REEMBOLSADO", "pagos\nreembolsados")]
    escenarios = list(esc["escenario"].unique())
    fig, axes = plt.subplots(1, len(escenarios), figsize=(6.5 * len(escenarios), 4.5))
    axes = np.atleast_1d(axes)
    for ax, e in zip(axes, escenarios):
        s = esc[(esc["escenario"] == e) & esc["momento"].isin(["antes", "despues"])].set_index("momento")
        x = np.arange(len(cols)); w = 0.38
        for k, (m, c) in enumerate((("antes", "#9e9e9e"), ("despues", "#1f77b4"))):
            bars = ax.bar(x + (k - 0.5) * w, [s.loc[m, c_] for c_, _ in cols], w, color=c, label=m)
            ax.bar_label(bars, fmt="%d", fontsize=8)
        ax.set_xticks(x); ax.set_xticklabels([n for _, n in cols], fontsize=9)
        ax.set_title(e.replace("_", " ")); ax.legend()
    fig.suptitle("Estado de los tres servicios antes y después (lectura directa de la base de cada servicio)")
    fig.tight_layout()
    ruta = OUT / f"saga_fallas_antes_despues_{ts}.png"
    fig.savefig(ruta, dpi=130); plt.close(fig)
    return ruta


def _fmt(x, nd=2):
    return "—" if pd.isna(x) else f"{x:.{nd}f}"


def _rango(t, v, f, c, nd=2):
    s = t[(t["variante"] == v) & (t["fase"] == f)]
    if not len(s):
        return "—"
    lo, hi = s[f"{c}_min"].iloc[0], s[f"{c}_max"].iloc[0]
    return _fmt(s[c].iloc[0], nd) if (pd.isna(lo) or abs(hi - lo) < 1e-9 or s["n_reps"].iloc[0] < 2) else f"{_fmt(s[c].iloc[0], nd)} (rango {_fmt(lo, nd)}–{_fmt(hi, nd)})"


def _tiene_tabulate() -> bool:
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False


def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) if _tiene_tabulate() else df.to_string(index=False)


def _abre(mt: pd.DataFrame, col: str, fase: str, t_ini: float):
    ab = mt[(mt["fase"] == fase) & (mt[col] == "OPEN")]
    return float(ab["t"].iloc[0] - t_ini) if len(ab) else None


def resumen(d: dict, t: pd.DataFrame, por_rep: pd.DataFrame, esc: pd.DataFrame | None, ts: str) -> Path:
    nreps = int(t["n_reps"].max())
    L = [f"# Resumen del experimento saga_fallas ({ts})", "",
         f"Todas las cifras salen de los CSV crudos de `experiment/data/raw/` de esta corrida ({nreps} repetición(es) de cada variante; "
         "las cifras de E3 son medianas y, con más de una repetición, se indica el rango). El texto no es una conclusión previa.", ""]
    if esc is not None:
        L += ["## E1 y E2 — antes y después", ""]
        for e in esc["escenario"].unique():
            s = esc[(esc["escenario"] == e) & esc["saga"].notna()]
            ok = int(s["invariantes_ok"].min()) if len(s) else 0
            ant = esc[(esc["escenario"] == e) & (esc["momento"] == "antes")].iloc[0]
            des = esc[(esc["escenario"] == e) & (esc["momento"] == "despues")].iloc[0]
            L += [f"**{e}** ({len(s)} matrículas): invariantes {'OK' if ok else 'FALLAN'}. Cupos ocupados {int(ant['cupos_ocupados'])} → {int(des['cupos_ocupados'])}; "
                  f"inscripciones confirmadas {int(ant['inscripciones_CONFIRMADA'])} → {int(des['inscripciones_CONFIRMADA'])}; pagos cobrados {int(ant['pagos_COBRADO'])} → {int(des['pagos_COBRADO'])}; "
                  f"reembolsados {int(ant['pagos_REEMBOLSADO'])} → {int(des['pagos_REEMBOLSADO'])}. "
                  f"Latencia de la solicitud (mediana) {s['latencia_solicitud_s'].median():.3f} s; del pago al estado final (mediana) {s['pago_a_estado_final_s'].median():.2f} s. "
                  f"Pasos firmes de la última saga, en orden: `{s['pasos_firmes'].iloc[-1] if len(s) else '—'}`.", ""]
    L += ["## E3 — falla en cascada contenida (misma línea de tiempo, con y sin patrón)", "",
          "Fases: `antes` (sano) → `lenta` (Transbank 3 s) → `alivio` (sano; el circuito se cierra) → `bulkhead` (cobros directos a Pagos con Transbank a 1,2 s) → `pico` (saga completa, Transbank 3 s, muchos workers) → `caida` (Transbank caído) → `recuperacion`.", ""]
    cols = ["variante", "fase", "workers", "peticiones", "req_por_s", "p50_s", "p95_s", "max_s", "pct_aceptadas", "pct_503", "pct_otros", "pct_rapidas",
            "sonda_orq_p95_s", "sonda_orq_fallos", "sonda_ins_p95_s", "sonda_pagos_p95_s", "rechazos_bulkhead_orq", "rechazos_bulkhead_pagos",
            "rechazos_circuito_tbk", "intentos_a_tbk"]
    L += [_md(t[cols].round(2)), ""]

    def g(v, f, c):
        s = t[(t["variante"] == v) & (t["fase"] == f)]
        return s[c].iloc[0] if len(s) else np.nan

    L += ["### Lectura por fase (con patrón frente a sin patrón)", ""]
    for f, nom in (("lenta", "Transbank lento (4 workers)"), ("pico", "Transbank lento + pico de carga"), ("caida", "Transbank caído")):
        L += [f"- **{nom}.** Mediana de POST /v1/matriculas: {_rango(t, 'con_patron', f, 'p50_s')} s con patrón frente a {_rango(t, 'sin_patron', f, 'p50_s')} s sin patrón; "
              f"p95 {_rango(t, 'con_patron', f, 'p95_s')} s frente a {_rango(t, 'sin_patron', f, 'p95_s')} s; "
              f"respuestas en < 200 ms: {_fmt(g('con_patron', f, 'pct_rapidas'), 0)} % frente a {_fmt(g('sin_patron', f, 'pct_rapidas'), 0)} %; "
              f"throughput {_fmt(g('con_patron', f, 'req_por_s'), 1)} frente a {_fmt(g('sin_patron', f, 'req_por_s'), 1)} peticiones/s. "
              f"Sonda del orquestador (p95): {_fmt(g('con_patron', f, 'sonda_orq_p95_s'))} s frente a {_fmt(g('sin_patron', f, 'sonda_orq_p95_s'))} s; "
              f"sonda de Inscripción, otro servicio (p95): {_fmt(g('con_patron', f, 'sonda_ins_p95_s'))} s frente a {_fmt(g('sin_patron', f, 'sonda_ins_p95_s'))} s."]
    L += [f"- **Bulkhead aislado (cobros directos a Pagos con Transbank a 1,2 s, sin llegar al timeout).** Con patrón: {int(g('con_patron', 'bulkhead', 'rechazos_bulkhead_pagos'))} cobros rechazados por el bulkhead "
          f"({_fmt(g('con_patron', 'bulkhead', 'pct_rapidas'), 0)} % de las respuestas en < 200 ms), `GET /v1/health` de Pagos en p95 {_fmt(g('con_patron', 'bulkhead', 'sonda_pagos_p95_s'))} s y "
          f"{_fmt(g('con_patron', 'bulkhead', 'req_por_s'), 1)} peticiones/s. Sin patrón: {int(g('sin_patron', 'bulkhead', 'rechazos_bulkhead_pagos'))} rechazos; "
          f"{_fmt(g('sin_patron', 'bulkhead', 'pct_rapidas'), 0)} % de respuestas rápidas, `GET /v1/health` de Pagos en p95 {_fmt(g('sin_patron', 'bulkhead', 'sonda_pagos_p95_s'))} s y p95 de los cobros "
          f"{_fmt(g('sin_patron', 'bulkhead', 'p95_s'))} s (con patrón {_fmt(g('con_patron', 'bulkhead', 'p95_s'))} s).", ""]

    # --- circuitos, por repetición
    for v in VARIANTES:
        partes = []
        for fase in ("lenta", "pico"):
            tiempos = []
            for rep in sorted(d["fases"][d["fases"]["variante"] == v]["rep"].unique()):
                _, _, mt, fs = _sel(d, v, rep)
                t_ini = float(fs[fs["fase"] == fase]["t_ini"].iloc[0])
                tiempos.append(_abre(mt, "pagos_cb", fase, t_ini))
            ab = [x for x in tiempos if x is not None]
            partes.append(f"circuito Pagos→Transbank en `{fase}`: " + (f"abrió en {len(ab)} de {len(tiempos)} repeticiones (mediana {np.median(ab):.1f} s tras iniciar la fase)" if ab else "nunca abrió"))
        for fase in ("lenta", "pico", "caida"):
            r = t[(t["variante"] == v) & (t["fase"] == fase)].iloc[0]
            partes.append(f"`{fase}`: {int(r['intentos_a_tbk'])} intentos a Transbank, {int(r['rechazos_circuito_tbk'])} frenados por el circuito y {int(r['rechazos_bulkhead_pagos'])} por el bulkhead de Pagos")
        L += [f"- **{ETIQ[v]}** — " + "; ".join(partes) + "."]

    # --- hallazgos detectados en los datos
    L += ["", "### Hallazgos que surgen de esta corrida", ""]
    hall = []
    sin_abrir, total, fases_orq = 0, 0, set()
    for rep in sorted(d["fases"][d["fases"]["variante"] == "con_patron"]["rep"].unique()):
        _, _, mt, fs = _sel(d, "con_patron", rep)
        t_len = float(fs[fs["fase"] == "lenta"]["t_ini"].iloc[0])
        if _abre(mt, "pagos_cb", "lenta", t_len) is not None:
            total += 1
            sin_abrir += _abre(mt, "orq_cb_COBRAR", "lenta", t_len) is None
        fases_orq |= set(mt[mt["orq_cb_COBRAR"] == "OPEN"]["fase"].unique())
    if sin_abrir:
        donde = (f"Solo abrió en fases con ráfagas de fallas simultáneas ({', '.join(f for f in FASES if f in fases_orq)})." if fases_orq
                 else "No abrió en ninguna fase.")
        hall.append(f"**El circuito Orquestador→Pagos no abrió con carga secuencial** (fase `lenta`, 4 workers) en {sin_abrir} de {total} repeticiones, aunque el de Pagos→Transbank sí. {donde} "
                    "Causa identificada en el código (`saga_clients.Client.call`): el cobro y su compensación comparten un mismo breaker y el reembolso, que responde bien porque no pasa por Transbank, "
                    "cuenta como éxito y reinicia el contador de fallas consecutivas (solo se supera el umbral cuando llegan ≥ 5 fallas antes de que cualquier compensación termine). "
                    "Lo reproduce de forma aislada `tests/unit/test_saga_pago.py` (xfail estricto). Consecuencia: con carga baja el orquestador sigue reservando y liberando cupo por cada matrícula fallida en vez de fallar rápido sin reservar.")
    bkc = int(g("con_patron", "bulkhead", "rechazos_bulkhead_pagos"))
    if bkc > 0:
        hall.append(f"**El bulkhead de Pagos sí actuó (fase `bulkhead`):** con patrón rechazó {bkc} cobros que habrían ocupado hilos esperando a un Transbank lento; "
                    f"el `GET /v1/health` de Pagos tardó p95 {_fmt(g('con_patron', 'bulkhead', 'sonda_pagos_p95_s'))} s frente a {_fmt(g('sin_patron', 'bulkhead', 'sonda_pagos_p95_s'))} s sin patrón.")
    else:
        hall.append("**El bulkhead de Pagos no llegó a rechazar nada** en esta corrida: la carga no superó su capacidad. Su efecto no está demostrado por estos datos.")
    if float(por_rep[por_rep["variante"] == "con_patron"]["rechazos_bulkhead_orq"].sum()) == 0:
        hall.append("**El bulkhead del orquestador no rechazó nada** en esta corrida: ninguna dependencia superó 8 llamadas simultáneas, así que no se evalúa aquí; "
                    "el hallazgo de que compensa sagas sanas se prueba con `experiment/scripts/saga_integridad_bulkhead.py`.")
    else:
        hall.append(f"**El bulkhead del orquestador sí rechazó** ({int(por_rep[por_rep['variante'] == 'con_patron']['rechazos_bulkhead_orq'].sum())} llamadas en total, con patrón): "
                    "cada rechazo se trata como falla incierta y compensa la matrícula (hallazgo abierto). Aquí ocurrió con Pagos lento; el mismo mecanismo rechaza también sagas sanas "
                    "bajo concurrencia (se prueba con `experiment/scripts/saga_integridad_bulkhead.py`).")
    hall.append(f"**Una caída inmediata es barata de manejar; la lentitud es la peligrosa.** Con Transbank caído la matrícula respondió en p95 {_rango(t, 'con_patron', 'caida', 'p95_s')} s con patrón y "
                f"{_rango(t, 'sin_patron', 'caida', 'p95_s')} s sin patrón: un error rápido no necesita circuito para ser rápido. El circuito aporta ahí otra cosa: dejó de llamar a Transbank "
                f"({int(g('con_patron', 'caida', 'intentos_a_tbk'))} intentos frente a {int(g('sin_patron', 'caida', 'intentos_a_tbk'))}).")
    pp = por_rep[(por_rep["variante"] == "con_patron") & (por_rep["fase"] == "pico")]["p50_s"]
    if len(pp) and pp.max() > 5 * max(pp.min(), 0.005):
        hall.append(f"**La mediana del pico con patrón varía mucho entre repeticiones ({pp.min():.2f}–{pp.max():.2f} s).** Depende de si el circuito del orquestador alcanza a abrir durante la ráfaga: "
                    "cuando abre, las matrículas fallan rápido sin reservar; cuando no, cada una sigue reservando y liberando cupo y compite por las escrituras serializadas de la SQLite del orquestador. "
                    "Una sola corrida no basta para afirmar un valor; use `--repeticiones`.")
    L += [f"{i}. {h}" for i, h in enumerate(hall, 1)]

    L += ["", "### Estado final tras la falla (convergencia e invariantes)", ""]
    for v in VARIANTES:
        for rep in sorted(d["estados"][d["estados"]["variante"] == v]["rep"].unique()):
            fin = d["estados"][(d["estados"]["variante"] == v) & (d["estados"]["rep"] == rep) & (d["estados"]["momento"] == "final")].iloc[0]
            conv = "no convergió en el tiempo de drenaje" if fin["convergencia_s"] < 0 else f"convergió en {fin['convergencia_s']:.1f} s"
            L += [f"- **{ETIQ[v]}, repetición {rep}:** {conv}; sagas en vuelo {int(fin['sagas_en_vuelo'])}; COMPLETADA {int(fin['sagas_COMPLETADA'])}, ESPERANDO_PAGO {int(fin['sagas_ESPERANDO_PAGO'])}, "
                  f"COMPENSADA {int(fin['sagas_COMPENSADA'])}, COMPENSACION_PENDIENTE {int(fin['sagas_COMPENSACION_PENDIENTE'])}; "
                  f"**cupos huérfanos {int(fin['cupos_huerfanos'])}**, **cobros huérfanos {int(fin['cobros_huerfanos'])}**."]
    L += ["", "## Límites de esta evidencia", "",
          f"- {nreps} repetición(es) por variante: las cifras absolutas varían con la máquina y entre corridas; importa la comparación entre variantes y el comportamiento por fase.",
          "- Transbank y el comprador están simulados; la latencia y la caída se inducen en Pagos (`PUT /internal/fault`), no en el Transbank real.",
          "- Todo corre en una máquina con SQLite y un solo proceso por servicio; el hilo de uvicorn (40) es el recurso que se satura sin bulkhead.",
          "- 'Sin patrón' deja circuito y bulkhead inertes pero conserva los timeouts (2 s la saga, 1,5 s hacia Transbank): aísla el efecto de esos dos patrones.",
          "- La fase `bulkhead` carga directamente a Pagos (no pasa por la saga) para aislar el bulkhead del circuito; la fase `pico` carga la saga completa.",
          "- Las matrículas de E3 quedan en `ESPERANDO_PAGO` (el comprador no paga): miden la reserva y el cobro pendiente, no la inscripción."]
    ruta = OUT / f"resumen_saga_fallas_{ts}.md"
    ruta.write_text("\n".join(L) + "\n", encoding="utf-8")
    return ruta


def generar(ts: str | None = None) -> None:
    ts = ts or _ultimo_ts()
    OUT.mkdir(exist_ok=True)
    esc = _leer(ts, "escenarios")
    d = {k: _leer(ts, k) for k in ("requests", "sondas", "metricas", "fases", "estados")}
    print(f"Generando figuras y tablas de saga_fallas {ts}…")
    if esc is not None:
        print("  →", _rel(fig_antes_despues(esc, ts)))
    if all(v is not None for v in d.values()) and {"con_patron", "sin_patron"} <= set(d["fases"]["variante"]):
        por_rep = tabla_por_rep(d)
        t = tabla_agregada(por_rep)
        por_rep.round(3).to_csv(OUT / f"saga_fallas_fases_por_rep_{ts}.csv", index=False)
        t.round(3).to_csv(OUT / f"saga_fallas_fases_{ts}.csv", index=False)
        (OUT / f"saga_fallas_fases_{ts}.md").write_text(_md(t.round(2)), encoding="utf-8")
        print("  →", _rel(fig_cascada(d, ts)))
        print("  →", _rel(fig_fases(t, ts)))
        print("  →", _rel(resumen(d, t, por_rep, esc, ts)))
    else:
        print("  (E3 incompleto: se necesitan ambas variantes, con_patron y sin_patron, para comparar)")


if __name__ == "__main__":
    generar(sys.argv[1] if len(sys.argv) > 1 else None)
