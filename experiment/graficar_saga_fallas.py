"""Figuras, tablas y resumen interpretado del experimento saga_fallas (experiment/scripts/saga_fallas.py).

Uso:  python experiment/graficar_saga_fallas.py [<ts>]      (por defecto, el experimento más reciente)
Lee experiment/data/raw/saga_fallas_<ts>_*.csv y escribe en experiment/results/:
  saga_fallas_cascada_<ts>.png       línea de tiempo (1.ª repetición) con y sin patrón: latencias, circuito, bulkhead, sagas
  saga_fallas_fases_<ts>.png         comparación por fase (medianas entre repeticiones; rangos del p95 en CSV)
  saga_fallas_antes_despues_<ts>.png estado de los tres servicios antes/después en E1 y E2
  saga_fallas_fases_<ts>.csv|md      tabla por variante y fase (medianas) y saga_fallas_fases_por_rep_<ts>.csv (cada repetición)
  resumen_saga_fallas_<ts>.md        lectura de la evidencia con los números reales de la corrida
"""
from __future__ import annotations

import sys
import hashlib
import json
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
FASES = ("antes", "pagos_lento", "pagos_recuperacion", "lenta", "alivio", "bulkhead", "pico", "caida", "recuperacion")
ACEPTADAS = (201, 202)  # 202 = matrícula en espera de pago; 201 = cobro creado (fase de carga directa a Pagos)
RAPIDA = 0.2  # una respuesta "rápida" (fail-fast) tarda menos de 200 ms
TBK_OPERACIONES = ("crear", "confirmar", "estado", "reembolsar")
TBK_CONTADORES = {
    "pagos_tbk_protegidos": "intentos_protegidos_tbk",
    "pagos_tbk_fallas_simuladas": "fallas_simuladas_tbk",
    "pagos_tbk_cliente_inicios": "inicios_cliente_tbk",
}


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


def _valor_en(m, t, col):
    if col not in m:
        return np.nan
    # No sustituir una última muestra ausente por una lectura anterior aparentemente válida.
    s = m[m.t <= t]
    return float(s[col].iloc[-1]) if len(s) else np.nan


def _sel(d: dict, v: str, rep: int):
    return (d[k][(d[k]["variante"] == v) & (d[k]["rep"] == rep)] for k in ("requests", "sondas", "metricas", "fases"))


def tabla_por_rep(d):
    filas = []
    for v in VARIANTES:
        for rep in sorted(d['fases'].loc[d['fases'].variante == v, 'rep'].unique()):
            rq0, sd, mt, fs = _sel(d, v, rep)
            mt = mt.sort_values('t', kind='stable')
            counters = [c for c in mt if c.startswith(('orq_intentos_', 'orq_bulkhead_rech_', *TBK_CONTADORES)) or c in
                        ('pagos_tbk_intentos', 'pagos_bulkhead_rech', 'pagos_circuito_rech', 'orq_admision_rech', 'orq_compensaciones')]
            for col in counters:
                values = pd.to_numeric(mt[col].dropna(), errors='raise')
                if not np.isfinite(values).all() or (values < 0).any() or (values.diff() < 0).any():
                    raise ValueError(f'Contador reiniciado: {col}, {v}, rep {rep}; la métrica no permite calcular deltas')
            for _, f in fs.iterrows():
                r = rq0[(rq0.fase == f.fase) & (rq0.codigo != -1)]
                s = sd[sd.fase == f.fase]
                phase_metrics = mt[mt.fase == f.fase]
                def delta(col):
                    if col not in mt:
                        return np.nan
                    if 'punto' in phase_metrics:
                        a = phase_metrics[phase_metrics.punto == 'inicio'][col]
                        b = phase_metrics[phase_metrics.punto == 'fin'][col]
                        return float(b.iloc[-1]-a.iloc[0]) if len(a) and len(b) else np.nan
                    return _valor_en(mt, f.t_fin, col)-_valor_en(mt, f.t_ini, col)
                def peak(col):
                    return phase_metrics[col].max() if col in phase_metrics else np.nan
                n = len(r)
                reason = r.get('motivo_fallo', pd.Series('', index=r.index)).fillna('')
                ff = (r.codigo == 503) & (reason == 'PAGOS_CIRCUITO_ABIERTO') & (r.latencia_s < RAPIDA)
                row = dict(variante=v, rep=int(rep), fase=f.fase, destino=f.get('destino', 'matriculas'), workers=int(f.workers),
                    tbk_latencia_ms=f.tbk_latencia_ms, tbk_caido=f.tbk_caido, pagos_latencia_ms=f.get('pagos_latencia_ms', np.nan),
                    peticiones=n, req_por_s=n/max(f.t_fin-f.t_ini, 1e-9), p50_s=r.latencia_s.median(),
                    p95_s=r.latencia_s.quantile(.95), max_s=r.latencia_s.max(),
                    pct_aceptadas=100*r.codigo.isin(ACEPTADAS).mean(), pct_503=100*(r.codigo == 503).mean(),
                    pct_429=100*(r.codigo == 429).mean(), pct_otros=100*(~r.codigo.isin(ACEPTADAS+(503,429))).mean(),
                    pct_rapidas=100*(r.latencia_s < RAPIDA).mean(), pct_fail_fast_orq=100*ff.mean(),
                    rechazos_bulkhead_orq=sum(delta(c) for c in mt if c.startswith('orq_bulkhead_rech_')),
                    rechazos_admision_orq=delta('orq_admision_rech'), compensaciones_orq=delta('orq_compensaciones'),
                    intentos_a_pagos=delta('orq_intentos_COBRAR_forward'),
                    reembolsos_a_pagos=delta('orq_intentos_COBRAR_compensacion'),
                    consultas_a_pagos=delta('orq_intentos_COBRAR_consulta'),
                    orq_cobros_en_vuelo_max=peak('orq_en_vuelo_COBRAR_forward'),
                    rechazos_bulkhead_pagos=delta('pagos_bulkhead_rech'),
                    rechazos_circuito_tbk=delta('pagos_circuito_rech'),
                    intentos_legacy_tbk=delta('pagos_tbk_intentos'))
                for raw, output in TBK_CONTADORES.items():
                    row[output] = delta(raw)
                    for op in TBK_OPERACIONES:
                        row[f'{output}_{op}'] = delta(f'{raw}_{op}')
                # Alias de tabla corregido: solo evidencia explícita de inicios, nunca CSV legacy.
                row['intentos_a_tbk'] = row['inicios_cliente_tbk']
                for name, short in [('orq_sagas','orq'), ('ins_enrollments','ins'), ('pagos_health','pagos')]:
                    probes = s[s.sonda == name]
                    row[f'sonda_{short}_p95_s'] = probes.latencia_s.quantile(.95)
                    row[f'sonda_{short}_fallos'] = int((probes.codigo != 200).sum())
                filas.append(row)
    return pd.DataFrame(filas)


def tabla_agregada(por_rep: pd.DataFrame) -> pd.DataFrame:
    num = [c for c in por_rep.columns if c not in ("variante", "rep", "fase", "destino")]
    g = por_rep.groupby(["variante", "fase"], sort=False)
    t = g[num].median()
    # Una mediana parcial no demuestra la medición de todas las repeticiones.
    for col in num:
        if col == 'intentos_a_tbk' or col.startswith(tuple(TBK_CONTADORES.values())):
            valid = g[col].count()
            t[col] = t[col].where(valid == g.size())
            if col in TBK_CONTADORES.values():
                t[f'n_reps_{col}'] = valid
    t["p95_s_min"], t["p95_s_max"] = g["p95_s"].min(), g["p95_s"].max()
    t["p50_s_min"], t["p50_s_max"] = g["p50_s"].min(), g["p50_s"].max()
    t["n_reps"], t["destino"] = g["rep"].nunique(), g["destino"].first()
    t = t.reset_index()
    t["_o"] = t["fase"].map({f: i for i, f in enumerate(FASES)})
    return t.sort_values(["variante", "_o"]).drop(columns="_o").reset_index(drop=True)


def _sombrear(ax, fs: pd.DataFrame, etiquetar=False):
    colores = {"pagos_lento": "#fff9c4", "pagos_recuperacion": "#e0f2f1", "antes": "#e8f5e9", "lenta": "#fff3e0", "alivio": "#f1f8e9", "bulkhead": "#ede7f6", "pico": "#ffe0b2",
               "caida": "#ffcdd2", "recuperacion": "#e3f2fd"}
    for _, f in fs.iterrows():
        ax.axvspan(f["t_ini"], f["t_fin"], color=colores.get(f["fase"], "#eee"), alpha=0.9, lw=0)
        if etiquetar:
            ax.text((f["t_ini"] + f["t_fin"]) / 2, 1.02, f["fase"], transform=ax.get_xaxis_transform(), ha="center", fontsize=9)


def fig_cascada(d, ts):
    rep = int(d['fases'].rep.min())
    fig, axes = plt.subplots(6, 2, figsize=(19, 20), sharex='col', sharey='row')
    for j, variant in enumerate(VARIANTES):
        rq, sd, mt, fs = _sel(d, variant, rep)
        for ax in axes[:,j]:
            _sombrear(ax, fs)
        axes[0,j].set_title(ETIQ[variant]+f' · repetición {rep}')
        for code in sorted(rq.codigo.unique()):
            rows = rq[rq.codigo == code]
            axes[0,j].scatter(rows.t, rows.latencia_s, s=5, alpha=.5, label=str(code),
                               color={201:'#1f77b4',202:'#ff7f0e',429:'#777777',503:'#d62728'}.get(code,'black'))
        axes[0,j].set_ylabel('POST inicial (s)'); axes[0,j].legend(fontsize=8)
        for name in sd.sonda.unique():
            rows = sd[sd.sonda == name]
            axes[1,j].plot(rows.t, rows.latencia_s, label=name, lw=.8)
        axes[1,j].set_ylabel('Sondas (s)'); axes[1,j].legend(fontsize=8)
        mapping = {'CLOSED':0, 'HALF_OPEN':1, 'OPEN':2}
        for col, label in [('orq_cb_COBRAR','Orq→Pagos (crear)'), ('pagos_cb','Pagos→Transbank (crear)')]:
            axes[2,j].step(mt.t, mt[col].map(mapping), where='post', label=label)
        for op in ('confirmar', 'estado', 'reembolsar'):
            col = f'pagos_cb_{op}'
            if col in mt:
                axes[2,j].step(mt.t, mt[col].map(mapping), where='post', label=f'TBK ({op})', ls='--', lw=.8)
        axes[2,j].set_yticks([0,1,2], ['CLOSED','HALF_OPEN','OPEN']); axes[2,j].legend(fontsize=8)
        for col, label in [('orq_admision_rech','Admisión Orq'), ('pagos_bulkhead_rech','Bulkhead Pagos')]:
            if col in mt:
                axes[3,j].plot(mt.t, mt[col], label=label)
        axes[3,j].set_ylabel('Rechazos acumulados'); axes[3,j].legend(fontsize=8)
        for col, label in [('orq_intentos_COBRAR_forward','Intentos Orq→Pagos'),
                           ('pagos_tbk_protegidos','Intentos protegidos Pagos→TBK'),
                           ('pagos_tbk_fallas_simuladas','Fallas simuladas Pagos→TBK'),
                           ('pagos_tbk_cliente_inicios','Inicios cliente Pagos→TBK')]:
            if col in mt:
                axes[4,j].plot(mt.t, mt[col], label=label)
        missing = [c for c in TBK_CONTADORES if c not in mt or mt[c].isna().any()]
        if missing:
            if not any(c in mt and mt[c].notna().any() for c in TBK_CONTADORES) and 'pagos_tbk_intentos' in mt:
                axes[4,j].plot(mt.t, mt.pagos_tbk_intentos, label='Histórico Pagos→TBK (semántica ambigua)', ls=':')
            axes[4,j].text(.02, .98, 'Telemetría de cliente desconocida o parcial',
                           transform=axes[4,j].transAxes, va='top', fontsize=8)
        axes[4,j].set_ylabel('Eventos acumulados'); axes[4,j].legend(fontsize=8)
        for state in ('COMPLETADA','COMPENSADA','ESPERANDO_PAGO','COMPENSACION_PENDIENTE'):
            axes[5,j].plot(mt.t, mt['orq_sagas_'+state], label=state)
        axes[5,j].set_ylabel('Sagas'); axes[5,j].legend(fontsize=7)
        axes[5,j].set_xticks((fs.t_ini+fs.t_fin)/2,
            [f'{row.fase} ({row.t_ini:.0f}–{row.t_fin:.0f}s)' for _,row in fs.iterrows()],
            rotation=60, ha='right', fontsize=8)
        axes[5,j].set_xlabel('Tiempo desde el inicio; límites efectivos de cada fase')
    fig.tight_layout()
    path = OUT/f'saga_fallas_cascada_{ts}.png'; fig.savefig(path, dpi=130); plt.close(fig)
    return path


def fig_fases(t, ts):
    phases = list(dict.fromkeys(t.fase))
    fig, axes = plt.subplots(2, 2, figsize=(16,10))
    for ax, (col, label) in zip(axes.flat, [('p95_s','p95 POST (s)'), ('pct_fail_fast_orq','Fail-fast Orq comprobado (%)'),
                                          ('intentos_a_pagos','Inicios Orq→Pagos'), ('sonda_orq_p95_s','p95 lectura Orq (s)')]):
        for j, variant in enumerate(VARIANTES):
            rows = t[t.variante == variant].set_index('fase').reindex(phases)
            ax.bar(np.arange(len(phases))+(j-.5)*.38, rows[col], .38, color=COLOR[variant], label=variant)
        ax.set_xticks(np.arange(len(phases)), phases, rotation=55, ha='right'); ax.set_title(label); ax.legend(fontsize=8)
    fig.suptitle('Medianas entre repeticiones; rangos de p95 en las tablas CSV')
    fig.tight_layout()
    path = OUT/f'saga_fallas_fases_{ts}.png'; fig.savefig(path, dpi=130); plt.close(fig)
    return path


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


def _tiene_tabulate() -> bool:
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False


def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) if _tiene_tabulate() else df.to_string(index=False)


def resumen(d, t, por_rep, esc, ts):
    lines = [f'# Evidencia de cascada H6 ({ts})', '',
        'Todas las cifras se calculan desde los CSV de esta corrida. Las tablas agregadas contienen medianas entre repeticiones; los CSV por repetición conservan dispersión y máximos.', '']
    if esc is not None:
        lines += ['## E1/E2: éxito y compensación después del pago', '']
        for scenario in esc.escenario.unique():
            rows = esc[(esc.escenario == scenario) & esc.saga.notna()]
            lines += [f'- {scenario}: {len(rows)} matrículas; invariantes OK={int(rows.invariantes_ok.min())}; pasos `{rows.pasos_firmes.iloc[-1]}`.', '']
    slow = t[t.fase == 'pagos_lento'].set_index('variante')
    if set(VARIANTES) <= set(slow.index):
        con, sin = slow.loc['con_patron'], slow.loc['sin_patron']
        lines += ['## Resultado principal: lentitud aislada de Pagos', '',
            f"Medianas de las repeticiones: {con.pct_fail_fast_orq:.1f}% de fail-fast comprobado con patrón; "
            f"{con.intentos_a_pagos:.0f} inicios de solicitud de crear pago frente a {sin.intentos_a_pagos:.0f} sin patrón. "
            f"p50 de la solicitud {con.p50_s:.3f} s frente a {sin.p50_s:.3f} s; p95 {con.p95_s:.3f} s frente a {sin.p95_s:.3f} s. "
            f"Ocupación máxima muestreada de cobros en el orquestador: {con.orq_cobros_en_vuelo_max:.0f} frente a {sin.orq_cobros_en_vuelo_max:.0f}.", '',
            'El circuito reduce los intentos después del umbral; las primeras llamadas aún pagan el timeout. La p95 puede seguir cerca del timeout si esas llamadas superan el 5% de solicitudes. La carga de bucle cerrado ofrece más solicitudes cuando el rechazo es rápido, por lo que no se comparan volúmenes como si fueran una tasa fija de llegada.', '']
    fields = [c for c in ('variante','fase','workers','pagos_latencia_ms','tbk_latencia_ms','tbk_caido','saga_timeout_s','tbk_timeout_s','comprador') if c in d['fases']]
    lines += ['', '## Parámetros efectivamente ejecutados', '', _md(d['fases'][fields].drop_duplicates()), '',
        'Las variantes conservan los timeouts. Sin patrón usa umbral y capacidad elevados; conserva idempotencia, callbacks, reconciliación y timeouts.', '',
        '## Comparación por fase', '', _md(t[[c for c in ('variante','fase','n_reps','peticiones','p50_s','p95_s','p95_s_min','p95_s_max','pct_429','pct_fail_fast_orq','intentos_a_pagos','compensaciones_orq','orq_cobros_en_vuelo_max','rechazos_admision_orq','intentos_protegidos_tbk','fallas_simuladas_tbk','inicios_cliente_tbk','n_reps_inicios_cliente_tbk','sonda_orq_p95_s') if c in t]].round(3)), '',
        'Fail-fast del orquestador exige HTTP 503, motivo PAGOS_CIRCUITO_ABIERTO y latencia <200 ms. El porcentaje de respuestas rápidas incluye éxitos y otros rechazos: se conserva separado. Intentos y compensaciones son deltas entre límites de fase; ocupación es el máximo muestreado de cobros simultáneos, no CPU.', '',
        'Los contadores distinguen intentos protegidos, fallas simuladas y llamadas iniciadas al cliente. Una llamada iniciada al cliente no demuestra recepción en la pasarela ni un cobro efectivo. El contador aumenta antes de ejecutar el cliente, aunque este falle o siga en curso. Una demora simulada menor que el timeout puede terminar en un inicio de cliente; solo una interrupción en la simulación aumenta fallas_simuladas_tbk.', '',
        'El balance protegidos = simuladas + inicios de cliente se espera al terminar las operaciones normales; durante la simulación puede haber intentos admitidos aún sin desenlace. Los deltas de fases diferentes tampoco tienen que balancear si una llamada atraviesa un límite de fase.', '',
        'Las columnas n_reps_* indican cobertura de telemetría. Si falta una frontera o una repetición, el conteo agregado permanece desconocido; no se reemplaza por cero ni por el contador histórico.', '',
        '## Inicios de cliente Transbank por operación', '',
        _md(t[[c for c in ('variante','fase','n_reps', *(f'inicios_cliente_tbk_{op}' for op in TBK_OPERACIONES)) if c in t]].round(3)), '',
        '## Apertura por frontera y recuperación', '']
    for phase in d['fases'].fase.unique():
        for variant in VARIANTES:
            metrics = d['metricas'][(d['metricas'].variante == variant) & (d['metricas'].fase == phase)]
            n = d['fases'].loc[(d['fases'].variante == variant) & (d['fases'].fase == phase), 'rep'].nunique()
            opened = metrics.loc[metrics.orq_cb_COBRAR == 'OPEN', 'rep'].nunique()
            inner = metrics.loc[metrics.pagos_cb == 'OPEN', 'rep'].nunique()
            lines += [f'- {variant}, {phase}: Orquestador→Pagos (crear) abrió en {opened} de {n} repeticiones; Pagos→Transbank (crear) abrió en {inner} de {n}.']
            if not opened and variant == 'con_patron':
                lines += [f'  El circuito Orquestador→Pagos no abrió en esta fase; no se atribuye a él contención observada en la otra frontera.']
    cols = [c for c in d['metricas'] if c.startswith('orq_cb_COBRAR_') or c.startswith('pagos_cb_')]
    if cols:
        lines += ['', 'Estados observados por operación:', '', _md(d['metricas'].groupby(['variante','rep'])[cols].agg(lambda s: ','.join(sorted(s.dropna().unique()))).reset_index())]
    rq = d['requests']
    if 'fail_fast_sin_efectos' in rq:
        ff = rq[rq.motivo_fallo == 'PAGOS_CIRCUITO_ABIERTO']
        lines += ['', f'Rechazos por circuito de Pagos verificados por saga: {len(ff)}; sin pasos firmes ni peticiones a Pagos: {int((ff.fail_fast_sin_efectos == 1).sum())}.', '']
    if 'estado_final' in rq:
        for phase in ('pagos_recuperacion', 'recuperacion'):
            for variant in VARIANTES:
                metrics = d['metricas'][(d['metricas'].variante == variant) & (d['metricas'].fase == phase)]
                for rep, group in metrics.groupby('rep'):
                    closed = group.sort_values('t').iloc[-1].orq_cb_COBRAR
                    completed = rq[(rq.variante == variant) & (rq.rep == rep) & (rq.fase == phase) & (rq.estado_final == 'COMPLETADA')]
                    lines += [f'- Recuperación {phase}, {variant}, rep {rep}: circuito de creación termina {closed}; {len(completed)} matrículas COMPLETADA de esta fase.']
        lines += ['', 'Resultados de Saga por fase de origen (estado observado al terminar el drenaje):', '',
                  _md(rq[rq.destino == 'matriculas'].groupby(['variante','rep','fase','estado_final']).size().rename('sagas').reset_index())]
    lines += ['', 'Bulkhead aislado: la fase de carga directa a Pagos cancela cada pago por su clave, incluso después de un timeout. No representa una matrícula y se etiqueta como destino pagos.', '',
              '## Estado final y cierre de negocio', '']
    for _, row in d['estados'][d['estados'].momento == 'final'].iterrows():
        lines += [f"- {row.variante}, repetición {row.rep}: convergencia {row.convergencia_s} s (negativo = sin cierre); COMPLETADA {int(row.sagas_COMPLETADA)}, ESPERANDO_PAGO {int(row.sagas_ESPERANDO_PAGO)}, COMPENSADA {int(row.sagas_COMPENSADA)}, COMPENSACION_PENDIENTE {int(row.sagas_COMPENSACION_PENDIENTE)}; cupos huérfanos {int(row.cupos_huerfanos)}, cobros huérfanos {int(row.cobros_huerfanos)}."]
    missing = [c for c in ('orq_intentos_COBRAR_forward','orq_admision_rech', *TBK_CONTADORES)
               if c not in d['metricas'] or d['metricas'][c].isna().any()]
    lines += ['', '## Límites y calidad de la medición', '',
        f'- Telemetría requerida ausente: {", ".join(missing) if missing else "ninguna"}. Valores ausentes se mantienen desconocidos; un contador que disminuye invalida el cálculo y detiene el informe.',
        '- Los CSV históricos con solo pagos_tbk_intentos tienen semántica ambigua: esa serie puede incluir interrupciones anteriores al cliente. No se reinterpreta como tráfico real. Sin pagos_tbk_cliente_inicios explícito, los inicios quedan desconocidos. Las series y resúmenes históricos originales no se modifican.',
        '- Transbank y el comprador son simulados. En Pagos lento, un proxy experimental retrasa únicamente POST /v1/payments; reembolsos y consultas llegan al servicio real. El proxy registra incluso llamadas que continúan después del timeout. Ambas variantes usan el mismo proxy.',
        '- El comprador intenta aprobar los pagos aceptados. El drenaje exige cero ESPERANDO_PAGO, pagos pendientes y llamadas activas; si no se alcanza, el informe declara falta de cierre.',
        '- Procesos locales en una máquina, bases SQLite independientes, un proceso por servicio; no mide Kubernetes, PostgreSQL bajo carga, red distribuida ni Webpay real.',
        '- Carga de bucle cerrado: cada worker espera respuesta y comprador/cancelación antes de pausar y repetir. El volumen admitido difiere entre variantes; comparar también intentos y ocupación, sin atribuir throughput a igual tasa de llegada.',
        '- HALF_OPEN puede durar menos que el intervalo de muestreo de 0,5 s. La transición controlada se verifica en tests H2; la corrida demuestra recuperación mediante CLOSED y matrículas completas posteriores.',
        '- No se deducen causas de código desde un estado de circuito aislado. CSV históricos conservan sus conclusiones originales y no sustituyen esta evidencia.', '']
    path = OUT/f'resumen_saga_fallas_{ts}.md'; path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def generar(ts: str | None = None) -> None:
    ts = ts or _ultimo_ts()
    OUT.mkdir(exist_ok=True)
    manifest_path = RAW/f'saga_fallas_{ts}_manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        for filename, digest in manifest.get('dataset_sha256', {}).items():
            if hashlib.sha256((RAW/filename).read_bytes()).hexdigest() != digest:
                raise ValueError(f'CSV modificado desde la medición: {filename}')
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
