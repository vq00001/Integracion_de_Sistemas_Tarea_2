#!/usr/bin/env python3
"""Experimento saga_fallas: evidencia ANTES / DESPUÉS de los tres escenarios del encargo U3.

Levanta el sistema con procesos reales (Cupos gRPC, Inscripción, Orquestador y Pagos) sin Docker; solo
Transbank (tests/support/fake_transbank.py) y el comprador están simulados. Mide:

  E1  Éxito:                       la matrícula completa y los tres pasos quedan firmes.
  E2  Fallo con compensación:      falla el ÚLTIMO paso tras pagar; se reembolsa y se libera el cupo.
  E3  Falla en cascada contenida:  Pagos lento → recuperación → Transbank lento → alivio → bulkhead → pico de carga → caído → recuperación, con la
                                   misma línea de tiempo en dos variantes:
        con_patron  Circuit Breaker y Bulkhead activos (configuración normal).
        sin_patron  Circuit Breaker y Bulkhead desactivados (umbral y capacidad altísimos): la
                    respuesta a "qué habría pasado SIN el patrón".

En E3 una carga de bucle cerrado de POST /v1/matriculas atraviesa las fases y, en paralelo, dos sondas miden "el
resto del sistema": GET /v1/sagas/{id} (mismo proceso que la saga: ahí se ve la cascada), GET
/v1/health de Pagos (mismo proceso que la llamada lenta a Transbank: ahí se ve si el bulkhead protege sus hilos) y
GET /v1/enrollments en Inscripción (otro servicio: ahí se ve el aislamiento). Un muestreador lee /metrics
cada 0,5 s (circuitos, rechazos del bulkhead, admisiones protegidas, fallas simuladas,
llamadas iniciadas al cliente de Transbank y sagas por estado; un inicio no acredita recepción remota).

Uso (desde la raíz del repositorio):
    python experiment/scripts/saga_fallas.py                # completo (~5 min)
    python experiment/scripts/saga_fallas.py --rapido       # fases cortas (~2 min), para probar el script
    python experiment/scripts/saga_fallas.py --solo-base    # solo E1 y E2
    python experiment/scripts/saga_fallas.py --solo-cascada # solo E3
    python experiment/scripts/saga_fallas.py --repeticiones 3   # 3 repeticiones de cada variante (medianas en el resumen)
    python experiment/scripts/saga_fallas.py --solo-cascada --variantes con_patron --ts 1 --sin-graficar
    python experiment/scripts/saga_fallas.py --solo-cascada --variantes sin_patron --ts 1   # fusiona con la anterior

Salida: CSV crudos en experiment/data/raw/ (saga_fallas_<ts>_*.csv) y, salvo --sin-graficar, figuras,
tablas y un resumen interpretado en experiment/results/ (ver experiment/graficar_saga_fallas.py).
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import hashlib
import math
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.support.fake_transbank import FakeTransbank  # noqa: E402
from experiment.support.pagos_proxy import PagosProxy

PY = sys.executable
RAW = ROOT / "experiment" / "data" / "raw"
ADMIN = "local-testing-key-32-characters-admin"
INTERNAL = "local-internal-testing-key-32-characters"
ORQ_KEY = "local-orchestrator-testing-key-32-characters"
PAGOS_KEY = "local-pagos-testing-key-32-characters-xx"
CALLBACK = "local-callback-testing-key-32-characters"
WRITER = "local-experiment-writer-key-32-characters"
TBK_SECRET = "SECRET-DE-PRUEBA"
ESTADOS_SAGA = ("EN_CURSO", "ESPERANDO_PAGO", "COMPLETADA", "COMPENSANDO", "COMPENSADA", "COMPENSACION_PENDIENTE")
EN_VUELO = ("EN_CURSO", "COMPENSANDO", "COMPENSACION_PENDIENTE")


# ------------------------------------------------------------------------------------------ configuración
@dataclass(frozen=True)
class Fase:
    nombre: str
    segundos: float
    workers: int
    think: float             # pausa de cada worker entre peticiones
    latencia_ms: int = 0     # latencia inducida en Transbank (> timeout equivale a un timeout real)
    falla: bool = False      # Transbank caído
    falla_en: str = "transbank"
    destino: str = "matriculas"   # "matriculas" = POST /v1/matriculas (saga completa); "pagos" = POST /v1/payments directo


def plan(rapido: bool) -> list[Fase]:
    """Aísla primero la lentitud de Pagos y luego la de Transbank; recupera ambas."""
    return [Fase("antes", 3 if rapido else 4, 4, 0.15),
            Fase("pagos_lento", 7 if rapido else 10, 4, 0.15, latencia_ms=3000, falla_en="pagos"),
            Fase("pagos_recuperacion", 7 if rapido else 8, 4, 0.15),
            Fase("lenta", 6 if rapido else 8, 4, 0.15, latencia_ms=3000),
            Fase("alivio", 7 if rapido else 8, 4, 0.15),
            Fase("bulkhead", 4 if rapido else 6, 24, 0.2, latencia_ms=1200, destino="pagos"),
            Fase("pico", 6 if rapido else 8, 16, 0.3, latencia_ms=3000),
            Fase("caida", 3 if rapido else 4, 4, 0.15, falla=True),
            Fase("recuperacion", 8 if rapido else 10, 4, 0.15)]


# Resiliencia: "con_patron" es la configuración normal; "sin_patron" deja circuito y bulkhead inertes.
PATRONES = {
    "con_patron": dict(SAGA_BULKHEAD="8", SAGA_CB_THRESHOLD="5", PAGOS_BULKHEAD="8", PAGOS_CB_THRESHOLD="5"),
    "sin_patron": dict(SAGA_BULKHEAD="100000", SAGA_CB_THRESHOLD="1000000",
                       PAGOS_BULKHEAD="100000", PAGOS_CB_THRESHOLD="1000000"),
}


def libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rut_con_dv(cuerpo: int) -> str:
    suma = sum(int(d) * ((i % 6) + 2) for i, d in enumerate(reversed(str(cuerpo))))
    dv = 11 - suma % 11
    return f"{cuerpo}-{'0' if dv == 11 else 'K' if dv == 10 else dv}"


def percentil(valores, p):
    v = sorted(valores)
    if not v:
        return float("nan")
    k = (len(v) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def escribir_csv(ruta: Path, filas: list[dict]) -> None:
    if not filas:
        return
    campos = list(dict.fromkeys(k for f in filas for k in f))
    with ruta.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        w.writerows(filas)
    try:
        mostrar = ruta.relative_to(ROOT)
    except ValueError:  # salida fuera del repositorio
        mostrar = ruta
    print(f"  → {mostrar} ({len(filas)} filas)", flush=True)


def fusionar(ruta: Path, nuevas: list[dict], variantes: list[str], reps: list[int]) -> list[dict]:
    """Permite ejecutar variantes y repeticiones en corridas distintas (mismo --ts): conserva las filas de las demás."""
    previas = []
    if ruta.exists():
        with ruta.open(newline="", encoding="utf-8") as fh:
            previas = [f for f in csv.DictReader(fh)
                       if not (f.get("variante") in variantes and int(float(f.get("rep") or 1)) in reps)]
    return previas + nuevas


# ------------------------------------------------------------------------------------------ el sistema
class Stack:
    """Cupos + Inscripción + Orquestador + Pagos reales y un Transbank simulado, en procesos locales."""

    def __init__(self, nombre: str, variante: str):
        self.nombre, self.variante = nombre, variante
        self.W = Path(tempfile.mkdtemp(prefix=f"saga_fallas_{nombre}_"))
        self.p_ins, self.p_cupos, self.p_orq, self.p_pagos = libre(), libre(), libre(), libre()
        self.procesos: dict[str, subprocess.Popen] = {}
        self.fake = FakeTransbank(api_key=TBK_SECRET)
        self.proxy = PagosProxy(self.pagos)
        self._rut = itertools.count(30_000_000)
        self._rut_lock = threading.Lock()

    # --- URLs
    @property
    def orq(self): return f"http://127.0.0.1:{self.p_orq}"
    @property
    def ins(self): return f"http://127.0.0.1:{self.p_ins}"
    @property
    def pagos(self): return f"http://127.0.0.1:{self.p_pagos}"

    def _lanzar(self, nombre, cmd, **env):
        log = (self.W / f"{nombre}.log").open("a")
        base = {**os.environ, "PYTHONPATH": str(self.W) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        self.procesos[nombre] = subprocess.Popen(cmd, cwd=ROOT, env={**base, **env}, stdout=log, stderr=log)
        log.close()

    def _esperar(self, url):
        for _ in range(200):
            for nombre, proc in self.procesos.items():
                if proc.poll() is not None:
                    raise RuntimeError(f"{nombre} terminó antes de estar listo; ver {self.W / (nombre + '.log')}")
            try:
                if requests.get(url, timeout=0.3).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.1)
        raise RuntimeError("servicio no disponible: " + url)

    def start(self) -> "Stack":
        W = self.W
        shutil.copytree(ROOT / "matriculas-api/app", W / "app", ignore=shutil.ignore_patterns("grpc_stubs", "__pycache__"))
        subprocess.run([PY, str(ROOT / "matriculas-api/generar_stubs.py"), "--destino", str(W / "app/grpc_stubs")],
                       check=True, capture_output=True)
        subprocess.run([PY, "-m", "grpc_tools.protoc", f"-I{ROOT}/proto", f"--python_out={W}", f"--grpc_python_out={W}",
                        str(ROOT / "proto/cupos.proto")], check=True)
        self.fake.start()
        self.proxy.start()
        r = PATRONES[self.variante]
        self._lanzar("cupos", [PY, str(ROOT / "cupos/src/server.py")], DB_PATH=str(W / "cupos.db"), GRPC_PORT=str(self.p_cupos))
        self._lanzar("inscripcion", [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.p_ins)],
                     API_KEYS=ADMIN, INSCRIPCION_WRITER_KEY=WRITER, JWT_SECRET="local-test-secret-at-least-32-characters", INTERNAL_KEY=INTERNAL,
                     DATABASE_URL=f"sqlite:///{W}/inscripcion.db", CUPOS_GRPC_TARGET=f"127.0.0.1:{self.p_cupos}")
        self._lanzar("pagos", [PY, "-m", "uvicorn", "pagos.app.main:app", "--host", "127.0.0.1", "--port", str(self.p_pagos)],
                     DATABASE_URL=f"sqlite:///{W}/pagos.db", PAGOS_API_KEY=PAGOS_KEY, PAGOS_CALLBACK_KEY=CALLBACK,
                     INTERNAL_KEY=INTERNAL, ORQUESTADOR_URL=self.orq, PAGOS_PUBLIC_BASE_URL=self.pagos,
                     TBK_BASE_URL=self.fake.url, TBK_COMMERCE_CODE="597055555532", TBK_API_KEY=TBK_SECRET,
                     TBK_TIMEOUT_SECONDS="1.5", PAGOS_RECONCILE_SECONDS="1", PAGOS_TTL_SECONDS="600",
                     PAGOS_CB_RECOVERY_SECONDS="5", PAGOS_CB_THRESHOLD=r["PAGOS_CB_THRESHOLD"], PAGOS_BULKHEAD=r["PAGOS_BULKHEAD"])
        self._lanzar("orquestador", [PY, "-m", "uvicorn", "app.orchestrator_main:app", "--host", "127.0.0.1", "--port", str(self.p_orq)],
                     SAGA_DATABASE_URL=f"sqlite:///{W}/sagas.db", ORCHESTRATOR_API_KEY=ORQ_KEY, INTERNAL_KEY=INTERNAL,
                     SERVICE_API_KEY=ADMIN, INSCRIPCION_WRITER_KEY=WRITER, INSCRIPCION_URL=self.ins, PAGOS_URL=self.proxy.url, PAGOS_API_KEY=PAGOS_KEY,
                     PAGOS_CALLBACK_KEY=CALLBACK, CUPOS_GRPC_TARGET=f"127.0.0.1:{self.p_cupos}", SAGA_TIMEOUT_SECONDS="2",
                     SAGA_STALE_SECONDS="20", SAGA_RECONCILE_SECONDS="1", SAGA_PAGO_POLL_SECONDS="15",
                     SAGA_PAGO_GRACIA_SECONDS="60", SAGA_CB_RECOVERY_SECONDS="5",
                     SAGA_BULKHEAD=r["SAGA_BULKHEAD"], SAGA_CB_THRESHOLD=r["SAGA_CB_THRESHOLD"])
        for url in (f"{self.ins}/v1/health", f"{self.pagos}/v1/health", f"{self.orq}/metrics"):
            self._esperar(url)
        self._ampliar_cupos()
        return self

    def _ampliar_cupos(self):
        """El experimento mide resiliencia, no agotamiento de cupos: se amplía la capacidad de los cursos."""
        for _ in range(100):
            try:
                con = sqlite3.connect(self.W / "cupos.db", timeout=10)
                if con.execute("SELECT count(*) FROM cursos").fetchone()[0]:
                    con.execute("UPDATE cursos SET cupos_totales = 1000000, cupos_libres = 1000000")
                    con.commit()
                    con.close()
                    return
                con.close()
            except sqlite3.Error:
                pass
            time.sleep(0.1)
        raise RuntimeError("Cupos no inicializó su base")

    def stop(self):
        for p in self.procesos.values():
            if p.poll() is None:
                p.terminate()
        for p in self.procesos.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        self.proxy.stop()
        if self.fake.thread.is_alive():
            self.fake.stop()

    # --- datos de negocio
    def alumno(self, session=None) -> str:
        with self._rut_lock:
            rut = rut_con_dv(next(self._rut))
        r = (session or requests).post(f"{self.ins}/v1/students", headers={"X-API-Key": ADMIN}, timeout=30,
                                       json={"rut": rut, "nombre": "Exp", "apellido": "Saga", "email": f"{uuid.uuid4().hex}@example.com"})
        r.raise_for_status()
        return r.json()["id"]

    def matricular(self, alumno, session=None, timeout=30):
        return (session or requests).post(f"{self.orq}/v1/matriculas", timeout=timeout,
                                          json={"alumno_id": alumno, "curso_id": 1, "monto": 15000},
                                          headers={"X-API-Key": ORQ_KEY, "Idempotency-Key": str(uuid.uuid4())})

    def saga(self, sid):
        return requests.get(f"{self.orq}/v1/sagas/{sid}", headers={"X-API-Key": ORQ_KEY}, timeout=5).json()

    def esperar_saga(self, sid, estado, segundos=20):
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            if self.saga(sid)["estado"] == estado:
                return
            time.sleep(0.1)
        raise AssertionError(f"la saga {sid} no llegó a {estado}: {self.saga(sid)}")

    def comprador(self, pago_url, aprueba=True):
        """Abre la redirección, 'paga' en Webpay (simulado) y vuelve al comercio con token_ws."""
        html = requests.get(pago_url, timeout=5).text
        token = re.search(r"name='token_ws' value='(\w+)'", html).group(1)
        self.fake.pagar(token, aprueba)
        requests.get(f"{self.pagos}/v1/pagos/retorno", params={"token_ws": token}, timeout=15).raise_for_status()

    def falla_pagos(self, latencia_ms=0, falla=False):
        requests.put(f"{self.pagos}/internal/fault", json={"latencia_ms": latencia_ms, "falla": falla},
                     headers={"X-Internal-Key": INTERNAL}, timeout=5).raise_for_status()

    def falla_inscripcion(self, falla=False):
        requests.put(f"{self.ins}/internal/fault", json={"falla": falla}, headers={"X-Internal-Key": INTERNAL},
                     timeout=5).raise_for_status()

    # --- estado leído de la base propia de cada servicio
    def _agrupar(self, db, sql):
        con = sqlite3.connect(self.W / db, timeout=10)
        try:
            return dict(con.execute(sql).fetchall())
        finally:
            con.close()

    def snapshot(self) -> dict:
        s = {}
        con = sqlite3.connect(self.W / "cupos.db", timeout=10)
        s["cupos_ocupados"] = con.execute("SELECT COALESCE(SUM(cupos_totales - cupos_libres), 0) FROM cursos").fetchone()[0]
        con.close()
        for estado in ESTADOS_SAGA:
            s[f"sagas_{estado}"] = 0
        for e, n in self._agrupar("sagas.db", "SELECT estado, count(*) FROM sagas GROUP BY estado").items():
            s[f"sagas_{e}"] = n
        for e in ("PENDIENTE", "COBRADO", "REEMBOLSADO", "CANCELADO", "RECHAZADO", "EXPIRADO", "ABORTADO", "PROCESANDO"):
            s[f"pagos_{e}"] = 0
        for e, n in self._agrupar("pagos.db", "SELECT estado, count(*) FROM payments GROUP BY estado").items():
            s[f"pagos_{e}"] = n
        s["inscripciones_CONFIRMADA"] = self._agrupar("inscripcion.db", "SELECT estado, count(*) FROM enrollments GROUP BY estado").get("CONFIRMADA", 0)
        return s

    def pasos(self, sid) -> str:
        con = sqlite3.connect(self.W / "sagas.db", timeout=10)
        filas = con.execute("SELECT paso, tipo, estado FROM saga_steps WHERE saga_id = ? ORDER BY rowid", (sid,)).fetchall()
        con.close()
        # Solo los pasos que quedaron firmes (OK), en el orden en que ocurrieron: el orden inverso de la compensación se lee aquí.
        return ">".join(("comp:" if t == "compensacion" else "") + p for p, t, e in filas if e == "OK")


# ------------------------------------------------------------------------------------------ métricas (/metrics)
_LINEA = re.compile(r"^(\w+)(?:\{([^}]*)\})?\s+(-?[0-9.eE+-]+)$")
TBK_OPERACIONES = ("crear", "confirmar", "estado", "reembolsar")
TBK_METRICAS = {
    "pagos_tbk_protegidos": "transbank_intentos_protegidos_total",
    "pagos_tbk_fallas_simuladas": "transbank_fallas_simuladas_total",
    "pagos_tbk_cliente_inicios": "transbank_cliente_llamadas_iniciadas_total",
}


def parsear_metricas(texto: str) -> list[tuple[str, dict, float]]:
    out = []
    for linea in texto.splitlines():
        m = _LINEA.match(linea.strip())
        if m:
            etiquetas = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or ""))
            out.append((m.group(1), etiquetas, float(m.group(3))))
    return out


def _valor(ms, nombre, default=0.0, **etq):
    return next((v for n, e, v in ms if n == nombre and all(e.get(k) == x for k, x in etq.items())), default)


def _circuito(ms, dep):
    return next((e["estado"] for n, e, v in ms if n == "circuito_estado" and e.get("dependencia") == dep and v == 1), "?")


def _contador_operacion(ms, family, operation):
    values = [v for n, e, v in ms if n == family and e.get("operacion") == operation]
    if len(values) != 1 or not math.isfinite(values[0]) or values[0] < 0:
        return None
    return values[0]


def muestra_metricas(stack: Stack) -> dict:
    # Esquema estable incluso si la primera muestra falla. Ausencia no significa cero.
    fila = {f"pagos_cb_{op}": "?" for op in TBK_OPERACIONES}
    for column in TBK_METRICAS:
        fila[column] = None
        fila.update({f"{column}_{op}": None for op in TBK_OPERACIONES})
    try:
        response = requests.get(f"{stack.orq}/metrics", timeout=2)
        response.raise_for_status()
        o = parsear_metricas(response.text)
        fila["orq_ok"] = 1
        for e in ESTADOS_SAGA:
            fila[f"orq_sagas_{e}"] = _valor(o, "sagas_total", estado=e)
        fila["orq_compensaciones"] = _valor(o, "compensaciones_total", default=None)
        fila["orq_admision_rech"] = _valor(o, "saga_admision_rechazos_total", default=None)
        for dep in ("RESERVAR", "COBRAR", "INSCRIBIR"):
            fila[f"orq_cb_{dep}"] = _circuito(o, dep)
            for op in ("forward", "compensacion", "consulta"):
                fila[f"orq_cb_{dep}_{op}"] = next((e["estado"] for n, e, v in o if
                    n == "saga_circuito_operacion_estado" and e.get("dependencia") == dep and
                    e.get("operacion") == op and v == 1), "?")
                fila[f"orq_intentos_{dep}_{op}"] = _valor(o, "saga_dependencia_intentos_total", default=None, dependencia=dep, operacion=op)
                fila[f"orq_en_vuelo_{dep}_{op}"] = _valor(o, "saga_dependencia_en_vuelo", default=None, dependencia=dep, operacion=op)
            fila[f"orq_bulkhead_rech_{dep}"] = _valor(o, "bulkhead_rechazos_total", default=None, dependencia=dep)
    except requests.RequestException:
        fila["orq_ok"] = 0
    try:
        response = requests.get(f"{stack.pagos}/metrics", timeout=2)
        response.raise_for_status()
        p = parsear_metricas(response.text)
        fila["pagos_ok"] = 1
        fila["pagos_cb"] = _circuito(p, "TRANSBANK")
        fila["pagos_bulkhead_rech"] = _valor(p, "bulkhead_rechazos_total", default=None, dependencia="TRANSBANK")
        fila["pagos_circuito_rech"] = _valor(p, "circuito_rechazos_total", default=None, dependencia="TRANSBANK")
        fila["pagos_tbk_intentos"] = _valor(p, "transbank_intentos_total", default=None)
        for column, family in TBK_METRICAS.items():
            values = [_contador_operacion(p, family, op) for op in TBK_OPERACIONES]
            fila.update({f"{column}_{op}": value for op, value in zip(TBK_OPERACIONES, values)})
            fila[column] = sum(values) if all(value is not None for value in values) else None
        for op in TBK_OPERACIONES:
            states = [e["estado"] for n, e, v in p if n == "transbank_circuito_estado"
                      and e.get("operacion") == op and v == 1]
            fila[f"pagos_cb_{op}"] = states[0] if len(states) == 1 else "?"
    except requests.RequestException:
        fila["pagos_ok"] = 0
    return fila


# ------------------------------------------------------------------------------------------ E1 y E2
ESCENARIOS_BASE = (("E1_exito", "COMPLETADA", False), ("E2_falla_ultimo_paso", "COMPENSADA", True))


def escenario_base(stack: Stack, n: int, escenario: str, esperado: str, falla: bool) -> list[dict]:
    """E1 (éxito) o E2 (falla el último paso tras pagar): n matrículas con estado antes/después.
    Cada escenario corre sobre un sistema recién levantado: el 'antes' parte de cero y no arrastra al otro."""
    filas = []
    if True:
        print(f"[{escenario}] {n} matrículas…", flush=True)
        stack.falla_inscripcion(falla)
        antes, refunds_antes = stack.snapshot(), stack.fake.calls["refund"]
        detalle = []
        for i in range(n):
            alumno = stack.alumno()
            t0 = time.monotonic()
            r = stack.matricular(alumno)
            lat = time.monotonic() - t0
            assert r.status_code == 202 and r.json()["estado"] == "ESPERANDO_PAGO", r.text
            sid, t1 = r.json()["id"], time.monotonic()
            stack.comprador(r.json()["pago_url"], True)
            stack.esperar_saga(sid, esperado)
            detalle.append(dict(escenario=escenario, saga=i + 1, estado_final=esperado,
                                latencia_solicitud_s=round(lat, 4), pago_a_estado_final_s=round(time.monotonic() - t1, 4),
                                pasos_firmes=stack.pasos(sid)))
        stack.falla_inscripcion(False)
        despues = stack.snapshot()
        reembolsos = stack.fake.calls["refund"] - refunds_antes
        if esperado == "COMPLETADA":
            ok = (despues["cupos_ocupados"] - antes["cupos_ocupados"] == n and despues["inscripciones_CONFIRMADA"] - antes["inscripciones_CONFIRMADA"] == n
                  and despues["pagos_COBRADO"] - antes["pagos_COBRADO"] == n)
        else:
            ok = (despues["cupos_ocupados"] == antes["cupos_ocupados"] and despues["inscripciones_CONFIRMADA"] == antes["inscripciones_CONFIRMADA"]
                  and despues["pagos_REEMBOLSADO"] - antes["pagos_REEMBOLSADO"] == n and reembolsos == n)
        for d in detalle:
            d["invariantes_ok"] = int(ok)
        filas.extend(detalle)
        for momento, snap in (("antes", antes), ("despues", despues)):
            filas.append({"escenario": escenario, "momento": momento, **snap})
        filas.append({"escenario": escenario, "momento": "reembolsos_en_transbank", "reembolsos": reembolsos})
        print(f"    invariantes {'OK' if ok else 'FALLAN'}; pasos de la última saga: {detalle[-1]['pasos_firmes']}", flush=True)
    return filas


# ------------------------------------------------------------------------------------------ E3
class PoolAlumnos:
    """Estudiantes creados ANTES de la carga: así la creación no distorsiona lo que se mide."""

    def __init__(self, stack: Stack, cuantos: int):
        self.q: "queue.Queue[str]" = queue.Queue()
        local = threading.local()

        def crear(_):
            if not hasattr(local, "s"):
                local.s = requests.Session()
            for _ in range(3):
                try:
                    self.q.put(stack.alumno(local.s))
                    return
                except requests.RequestException:
                    time.sleep(0.2)

        t0 = time.monotonic()
        with ThreadPoolExecutor(16) as ex:
            list(ex.map(crear, range(cuantos)))
        print(f"    {self.q.qsize()} estudiantes creados en {time.monotonic() - t0:.1f} s", flush=True)

    def tomar(self):
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None


def cascada(stack: Stack, fases: list[Fase], ts: str, drenaje_s: float, rep: int = 1) -> dict:
    variante = stack.variante
    print(f"[E3 {variante} · repetición {rep}] preparando…", flush=True)
    necesarios = int(sum(f.workers * f.segundos / f.think for f in fases if f.destino == "matriculas") * 1.1) + 20
    pool = PoolAlumnos(stack, necesarios)
    probe = stack.matricular(pool.tomar()).json()
    sid_sonda = probe["id"]
    if probe.get('estado') == 'ESPERANDO_PAGO':
        stack.comprador(probe['pago_url'], True)
    inicio = time.monotonic()
    fase_actual = [fases[0].nombre]
    reqs, sondas, metricas, estados, marcas = [], [], [], [], []
    parar_sondas, parar_muestreo = threading.Event(), threading.Event()
    sampling_lock = threading.Lock()

    def t_rel():
        return round(time.monotonic() - inicio, 4)

    def sample(punto='periodico'):
        # Serializar lecturas evita falsos reinicios de contador por orden de llegada.
        with sampling_lock:
            values = muestra_metricas(stack)
            row = dict(t=t_rel(), fase=fase_actual[0], punto=punto, **values)
            metricas.append(row)
            return row['t']

    def muestrear():
        while not parar_muestreo.is_set():
            sample()
            parar_muestreo.wait(0.5)

    def sondear(nombre, url, headers):
        with requests.Session() as session:
            while not parar_sondas.is_set():
                t0, phase = time.monotonic(), fase_actual[0]
                try:
                    codigo = session.get(url, headers=headers, timeout=10).status_code
                except requests.RequestException:
                    codigo = 0
                sondas.append(dict(t=round(t0-inicio, 4), fase=phase, sonda=nombre,
                                   codigo=codigo, latencia_s=round(time.monotonic()-t0, 4)))
                parar_sondas.wait(0.25)

    def worker(fase, fin):
        with requests.Session() as session:
            while time.monotonic() < fin:
                alumno = pool.tomar() if fase.destino == 'matriculas' else str(uuid.uuid4())
                if alumno is None:
                    reqs.append(dict(t=t_rel(), fase=fase.nombre, codigo=-1, estado='SIN_ALUMNOS', latencia_s=0))
                    return
                t0 = time.monotonic()
                key, data = f'exp-{uuid.uuid4()}:COBRAR', {}
                row = dict(t=round(t0-inicio, 4), fase=fase.nombre, destino=fase.destino)
                try:
                    if fase.destino == 'pagos':
                        response = session.post(stack.pagos+'/v1/payments', timeout=20,
                            json=dict(alumno_id=alumno, curso_id=1, monto=15000),
                            headers={'X-API-Key': PAGOS_KEY, 'Idempotency-Key': key})
                    else:
                        response = stack.matricular(alumno, session, timeout=20)
                    data = response.json()
                    row.update(codigo=response.status_code, estado=data.get('estado'),
                               motivo_fallo=data.get('motivo_fallo', ''), saga_id=data.get('id', ''),
                               error_code=data.get('error', {}).get('code', ''))
                except (requests.RequestException, ValueError) as exc:
                    row.update(codigo=0, estado=type(exc).__name__)
                row['latencia_s'] = round(time.monotonic()-t0, 4)
                # Medir la solicitud antes del comprador; conservar aparte su coste/error.
                try:
                    if fase.destino == 'pagos':
                        cleanup = session.post(stack.pagos+'/v1/payments/refund', timeout=20,
                            json={'original_key': key}, headers={'X-API-Key': PAGOS_KEY,
                            'Idempotency-Key': 'cleanup:'+key})
                        cleanup.raise_for_status()
                    elif data.get('estado') == 'ESPERANDO_PAGO':
                        tb = time.monotonic()
                        stack.comprador(data['pago_url'], True)
                        row['comprador_s'] = round(time.monotonic()-tb, 4)
                except (requests.RequestException, AttributeError, ValueError) as exc:
                    row['comprador_error'] = type(exc).__name__
                reqs.append(row)
                time.sleep(fase.think)

    hilos = [threading.Thread(target=muestrear, daemon=True),
             threading.Thread(target=sondear, daemon=True, args=('orq_sagas', stack.orq+'/v1/sagas/'+sid_sonda, {'X-API-Key': ORQ_KEY})),
             threading.Thread(target=sondear, daemon=True, args=('ins_enrollments', stack.ins+'/v1/enrollments?limit=1', {'X-API-Key': ADMIN})),
             threading.Thread(target=sondear, daemon=True, args=('pagos_health', stack.pagos+'/v1/health', {}))]
    for hilo in hilos:
        hilo.start()
    proxy = getattr(stack, 'proxy', None)
    try:
        for fase in fases:
            fase_actual[0] = fase.nombre
            if proxy:
                proxy.configure(fase.latencia_ms if fase.falla_en == 'pagos' else 0, fase.nombre)
            stack.falla_pagos(fase.latencia_ms if fase.falla_en == 'transbank' else 0, fase.falla)
            antes = stack.snapshot()
            t_ini = sample('inicio')
            print(f"  fase {fase.nombre:<20} {fase.segundos:>3.0f}s · {fase.workers:>2} workers · {fase.falla_en} {fase.latencia_ms}ms · → {fase.destino}", flush=True)
            fin = time.monotonic() + fase.segundos
            workers = [threading.Thread(target=worker, args=(fase, fin)) for _ in range(fase.workers)]
            for hilo in workers:
                hilo.start()
            for hilo in workers:
                hilo.join()
            t_fin = sample('fin')
            marcas.append(dict(fase=fase.nombre, t_ini=t_ini, t_fin=t_fin, workers=fase.workers, think_s=fase.think,
                falla_en=fase.falla_en, pagos_latencia_ms=fase.latencia_ms if fase.falla_en == 'pagos' else 0,
                tbk_latencia_ms=fase.latencia_ms if fase.falla_en == 'transbank' else 0,
                tbk_caido=int(fase.falla), destino=fase.destino, saga_timeout_s=2, tbk_timeout_s=1.5, comprador='aprobar'))
            estados.extend([dict(momento='antes_'+fase.nombre, t=t_ini, **antes),
                            dict(momento='despues_'+fase.nombre, t=t_fin, **stack.snapshot())])
        stack.falla_pagos(0, False)
        if proxy:
            proxy.configure()
        fase_actual[0] = 'drenaje'
        t_drain, convergio = time.monotonic(), None
        while time.monotonic()-t_drain < drenaje_s:
            snap = stack.snapshot()
            pending = sum(snap.get('sagas_'+e, 0) for e in (*EN_VUELO, 'ESPERANDO_PAGO'))
            pending += snap.get('pagos_PENDIENTE', 0) + snap.get('pagos_PROCESANDO', 0)
            if not pending and not (proxy and proxy.active):
                convergio = time.monotonic()-t_drain
                break
            time.sleep(0.2)
        final = stack.snapshot()
        final.update(convergencia_s=round(convergio, 3) if convergio is not None else -1,
            negocio_cerrado=convergio is not None,
            sagas_en_vuelo=sum(final.get('sagas_'+e, 0) for e in EN_VUELO),
            cupos_huerfanos=final['cupos_ocupados']-final['sagas_COMPLETADA']-final['sagas_ESPERANDO_PAGO'],
            cobros_huerfanos=final['pagos_COBRADO']-final['sagas_COMPLETADA'])
        estados.append(dict(momento='final', t=t_rel(), **final))
        # Comprobar la integridad por clave además de los totales agregados.
        for row in reqs:
            if row.get('saga_id') and row.get('destino') == 'matriculas':
                current = stack.saga(row['saga_id'])
                row['estado_final'] = current['estado']
                row['pasos_firmes'] = stack.pasos(row['saga_id'])
        sample('final')
        print(f"  cerrado={final['negocio_cerrado']} · COMPLETADA={final['sagas_COMPLETADA']} · COMPENSADA={final['sagas_COMPENSADA']} · huérfanos={final['cupos_huerfanos']}/{final['cobros_huerfanos']}", flush=True)
    finally:
        parar_sondas.set()
        parar_muestreo.set()
        for hilo in hilos:
            hilo.join(timeout=12)
    frontera = []
    if proxy:
        with proxy.lock:
            frontera = [dict(event, t=round(event['recibido']-inicio, 4)) for event in proxy.events]
        for row in reqs:
            if row.get('motivo_fallo') == 'PAGOS_CIRCUITO_ABIERTO':
                sid = row['saga_id']
                row['fail_fast_sin_efectos'] = int(not row['pasos_firmes'] and
                    not any(e['clave'].startswith(sid+':') for e in frontera))
    for fila in reqs + sondas + metricas + marcas + estados + frontera:
        fila['variante'], fila['rep'] = variante, rep
    return dict(requests=reqs, sondas=sondas, metricas=metricas, fases=marcas, estados=estados, frontera=frontera)


# ------------------------------------------------------------------------------------------ principal
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rapido", action="store_true", help="fases cortas (para probar el script)")
    ap.add_argument("--solo-base", action="store_true", help="solo E1 y E2")
    ap.add_argument("--solo-cascada", action="store_true", help="solo E3")
    ap.add_argument("--sin-graficar", action="store_true", help="no generar figuras ni resumen")
    ap.add_argument("--variantes", default="con_patron,sin_patron",
                    help="variantes de E3 a ejecutar, separadas por coma; con el mismo --ts se fusionan los CSV (útil para correr una por vez)")
    ap.add_argument("--repeticiones-base", type=int, default=5, help="matrículas por escenario en E1 y E2")
    ap.add_argument("--repeticiones", type=int, default=1, help="repeticiones de cada variante de E3 (el resumen usa medianas)")
    ap.add_argument("--desde-rep", type=int, default=1, help="número de la primera repetición (para continuar en otra corrida con el mismo --ts)")
    ap.add_argument("--drenaje", type=float, default=90.0, help="segundos máximos para esperar la convergencia tras E3")
    ap.add_argument("--ts", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    ts, prefijo = args.ts, f"saga_fallas_{args.ts}"
    print(f"Experimento saga_fallas · {ts} · {'rápido' if args.rapido else 'completo'}", flush=True)

    files = sorted(path for folder in ('matriculas-api/app', 'cupos/src', 'pagos/app', 'experiment', 'tests/support')
                   for path in (ROOT/folder).rglob('*.py') if '__pycache__' not in path.parts and 'grpc_stubs' not in path.parts)
    files += sorted((ROOT/'proto').glob('*.proto'))
    source_hash = hashlib.sha256()
    for path in files:
        source_hash.update(str(path.relative_to(ROOT)).encode()+b'\0'+path.read_bytes())
    invocation = dict(ts=ts, started_at=datetime.now(timezone.utc).isoformat(), source_sha256=source_hash.hexdigest(),
        git_head=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        git_status=subprocess.check_output(['git','status','--short'], cwd=ROOT, text=True),
        python=sys.version, argv=sys.argv[1:], patrones=PATRONES,
        metricas_transbank=dict(version=2, columnas=TBK_METRICAS,
                               semantica="Inicios de cliente no demuestran recepción en la pasarela"),
        fases=[vars(f) for f in plan(args.rapido)], timeout_saga_s=2, timeout_tbk_s=1.5)
    manifest_path = RAW/f'{prefijo}_manifest.json'
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    history = previous.get('invocations', [previous] if previous else [])
    manifest = dict(ts=ts, invocations=history+[invocation])
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')

    if not args.solo_cascada:
        filas_base = []
        for escenario, esperado, falla in ESCENARIOS_BASE:
            stack = Stack("base", "con_patron")
            try:
                stack.start()
                filas_base.extend(escenario_base(stack, args.repeticiones_base, escenario, esperado, falla))
            finally:
                stack.stop()
        escribir_csv(RAW / f"{prefijo}_escenarios.csv", filas_base)

    if not args.solo_base:
        todo = {k: [] for k in ("requests", "sondas", "metricas", "fases", "estados", "frontera")}
        variantes = [v.strip() for v in args.variantes.split(",") if v.strip()]
        reps = list(range(args.desde_rep, args.desde_rep + args.repeticiones))
        for rep in reps:
            for variante in variantes:
                stack = Stack("cascada", variante)
                try:
                    stack.start()
                    datos = cascada(stack, plan(args.rapido), ts, args.drenaje, rep)
                finally:
                    stack.stop()
                for k in todo:
                    todo[k].extend(datos[k])
                    ruta = RAW / f"{prefijo}_{k}.csv"
                    escribir_csv(ruta, fusionar(ruta, datos[k], [variante], [rep]))
        for k, filas in todo.items():
            ruta = RAW / f"{prefijo}_{k}.csv"
            escribir_csv(ruta, fusionar(ruta, filas, variantes, reps))

    manifest['dataset_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(RAW.glob(f'{prefijo}_*.csv'))}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')

    if not args.sin_graficar:
        sys.path.insert(0, str(ROOT / "experiment"))
        import graficar_saga_fallas
        graficar_saga_fallas.generar(ts)
    print("Listo.", flush=True)


if __name__ == "__main__":
    main()
