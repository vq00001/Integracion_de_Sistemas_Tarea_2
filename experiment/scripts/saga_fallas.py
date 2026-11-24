#!/usr/bin/env python3
"""Experimento saga_fallas: evidencia ANTES / DESPUÉS de los tres escenarios del encargo U3.

Levanta el sistema con procesos reales (Cupos gRPC, Inscripción, Orquestador y Pagos) sin Docker; solo
Transbank (tests/support/fake_transbank.py) y el comprador están simulados. Mide:

  E1  Éxito:                       la matrícula completa y los tres pasos quedan firmes.
  E2  Fallo con compensación:      falla el ÚLTIMO paso tras pagar; se reembolsa y se libera el cupo.
  E3  Falla en cascada contenida:  Transbank lento → alivio → bulkhead → pico de carga → caído → recuperación, con la
                                   misma línea de tiempo en dos variantes:
        con_patron  Circuit Breaker y Bulkhead activos (configuración normal).
        sin_patron  Circuit Breaker y Bulkhead desactivados (umbral y capacidad altísimos): la
                    respuesta a "qué habría pasado SIN el patrón".

En E3 una carga constante de POST /v1/matriculas atraviesa las fases y, en paralelo, dos sondas miden "el
resto del sistema": GET /v1/sagas/{id} (mismo proceso que la saga: ahí se ve la cascada), GET
/v1/health de Pagos (mismo proceso que la llamada lenta a Transbank: ahí se ve si el bulkhead protege sus hilos) y
GET /v1/enrollments en Inscripción (otro servicio: ahí se ve el aislamiento). Un muestreador lee /metrics
cada 0,5 s (estado del circuito, rechazos del bulkhead, intentos reales hacia Transbank, sagas por estado).

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
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.support.fake_transbank import FakeTransbank  # noqa: E402

PY = sys.executable
RAW = ROOT / "experiment" / "data" / "raw"
ADMIN = "local-testing-key-32-characters-admin"
INTERNAL = "local-internal-testing-key-32-characters"
ORQ_KEY = "local-orchestrator-testing-key-32-characters"
PAGOS_KEY = "local-pagos-testing-key-32-characters-xx"
CALLBACK = "local-callback-testing-key-32-characters"
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
    destino: str = "matriculas"   # "matriculas" = POST /v1/matriculas (saga completa); "pagos" = POST /v1/payments directo


def plan(rapido: bool) -> list[Fase]:
    """antes → lenta → alivio → bulkhead → pico → caida → recuperacion.
    · alivio:   Transbank sano para que el circuito se cierre antes de las fases siguientes.
    · bulkhead: carga DIRECTA a Pagos con Transbank lento pero sin llegar al timeout (1,2 s < 1,5 s): el circuito no
                se dispara y se aísla el efecto del bulkhead sobre los hilos de Pagos.
    · pico:     carga a la saga completa con Transbank lento, partiendo con el circuito cerrado."""
    if rapido:
        return [Fase("antes", 4, 4, 0.15), Fase("lenta", 6, 4, 0.15, latencia_ms=3000), Fase("alivio", 7, 4, 0.15),
                Fase("bulkhead", 6, 50, 0.1, latencia_ms=1200, destino="pagos"),
                Fase("pico", 6, 40, 0.5, latencia_ms=3000), Fase("caida", 4, 4, 0.15, falla=True), Fase("recuperacion", 8, 4, 0.15)]
    return [Fase("antes", 8, 4, 0.15), Fase("lenta", 10, 4, 0.15, latencia_ms=3000), Fase("alivio", 8, 4, 0.15),
            Fase("bulkhead", 8, 60, 0.1, latencia_ms=1200, destino="pagos"),
            Fase("pico", 10, 60, 0.5, latencia_ms=3000), Fase("caida", 8, 4, 0.15, falla=True), Fase("recuperacion", 12, 4, 0.15)]


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
        r = PATRONES[self.variante]
        self._lanzar("cupos", [PY, str(ROOT / "cupos/src/server.py")], DB_PATH=str(W / "cupos.db"), GRPC_PORT=str(self.p_cupos))
        self._lanzar("inscripcion", [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.p_ins)],
                     API_KEYS=ADMIN, JWT_SECRET="local-test-secret-at-least-32-characters", INTERNAL_KEY=INTERNAL,
                     DATABASE_URL=f"sqlite:///{W}/inscripcion.db", CUPOS_GRPC_TARGET=f"127.0.0.1:{self.p_cupos}")
        self._lanzar("pagos", [PY, "-m", "uvicorn", "pagos.app.main:app", "--host", "127.0.0.1", "--port", str(self.p_pagos)],
                     DATABASE_URL=f"sqlite:///{W}/pagos.db", PAGOS_API_KEY=PAGOS_KEY, PAGOS_CALLBACK_KEY=CALLBACK,
                     INTERNAL_KEY=INTERNAL, ORQUESTADOR_URL=self.orq, PAGOS_PUBLIC_BASE_URL=self.pagos,
                     TBK_BASE_URL=self.fake.url, TBK_COMMERCE_CODE="597055555532", TBK_API_KEY=TBK_SECRET,
                     TBK_TIMEOUT_SECONDS="1.5", PAGOS_RECONCILE_SECONDS="1", PAGOS_TTL_SECONDS="600",
                     PAGOS_CB_RECOVERY_SECONDS="5", PAGOS_CB_THRESHOLD=r["PAGOS_CB_THRESHOLD"], PAGOS_BULKHEAD=r["PAGOS_BULKHEAD"])
        self._lanzar("orquestador", [PY, "-m", "uvicorn", "app.orchestrator_main:app", "--host", "127.0.0.1", "--port", str(self.p_orq)],
                     SAGA_DATABASE_URL=f"sqlite:///{W}/sagas.db", ORCHESTRATOR_API_KEY=ORQ_KEY, INTERNAL_KEY=INTERNAL,
                     SERVICE_API_KEY=ADMIN, INSCRIPCION_URL=self.ins, PAGOS_URL=self.pagos, PAGOS_API_KEY=PAGOS_KEY,
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
        requests.get(f"{self.pagos}/v1/pagos/retorno", params={"token_ws": token}, timeout=15)

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
        except sqlite3.Error:
            return {}
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


def parsear_metricas(texto: str) -> list[tuple[str, dict, float]]:
    out = []
    for linea in texto.splitlines():
        m = _LINEA.match(linea.strip())
        if m:
            etiquetas = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or ""))
            out.append((m.group(1), etiquetas, float(m.group(3))))
    return out


def _valor(ms, nombre, **etq):
    return next((v for n, e, v in ms if n == nombre and all(e.get(k) == x for k, x in etq.items())), 0.0)


def _circuito(ms, dep):
    return next((e["estado"] for n, e, v in ms if n == "circuito_estado" and e.get("dependencia") == dep and v == 1), "?")


def muestra_metricas(stack: Stack) -> dict:
    fila = {}
    try:
        o = parsear_metricas(requests.get(f"{stack.orq}/metrics", timeout=2).text)
        fila["orq_ok"] = 1
        for e in ESTADOS_SAGA:
            fila[f"orq_sagas_{e}"] = _valor(o, "sagas_total", estado=e)
        fila["orq_compensaciones"] = _valor(o, "compensaciones_total")
        for dep in ("RESERVAR", "COBRAR", "INSCRIBIR"):
            fila[f"orq_cb_{dep}"] = _circuito(o, dep)
            fila[f"orq_bulkhead_rech_{dep}"] = _valor(o, "bulkhead_rechazos_total", dependencia=dep)
    except requests.RequestException:
        fila["orq_ok"] = 0
    try:
        p = parsear_metricas(requests.get(f"{stack.pagos}/metrics", timeout=2).text)
        fila["pagos_ok"] = 1
        fila["pagos_cb"] = _circuito(p, "TRANSBANK")
        fila["pagos_bulkhead_rech"] = _valor(p, "bulkhead_rechazos_total", dependencia="TRANSBANK")
        fila["pagos_circuito_rech"] = _valor(p, "circuito_rechazos_total", dependencia="TRANSBANK")
        fila["pagos_tbk_intentos"] = _valor(p, "transbank_intentos_total")
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
    sid_sonda = stack.matricular(pool.tomar()).json()["id"]  # saga fija para la sonda del orquestador
    inicio = time.monotonic()
    fase_actual = [fases[0].nombre]
    reqs, sondas, metricas, estados, marcas = [], [], [], [], []
    parar_sondas, parar_muestreo = threading.Event(), threading.Event()

    def t_rel():
        return round(time.monotonic() - inicio, 3)

    def muestrear():
        while not parar_muestreo.is_set():
            fila = {"t": t_rel(), "fase": fase_actual[0], **muestra_metricas(stack)}
            metricas.append(fila)
            parar_muestreo.wait(0.5)

    def sondear(nombre, url, headers):
        s = requests.Session()
        while not parar_sondas.is_set():
            t0 = time.monotonic()
            try:
                codigo = s.get(url, headers=headers, timeout=10).status_code
            except requests.RequestException:
                codigo = 0
            sondas.append({"t": round(t0 - inicio, 3), "fase": fase_actual[0], "sonda": nombre,
                           "codigo": codigo, "latencia_s": round(time.monotonic() - t0, 4)})
            parar_sondas.wait(0.25)

    def worker(fase: Fase, fin: float):
        s = requests.Session()
        while time.monotonic() < fin and fase.destino == "pagos":
            t0 = time.monotonic()
            try:
                r = s.post(f"{stack.pagos}/v1/payments", timeout=20, json={"alumno_id": str(uuid.uuid4()), "curso_id": 1, "monto": 15000},
                           headers={"X-API-Key": PAGOS_KEY, "Idempotency-Key": f"exp-{uuid.uuid4()}:COBRAR"})
                codigo = r.status_code
                estado = "PENDIENTE" if codigo == 201 else (r.json().get("error", {}).get("code", "?") if r.headers.get("content-type", "").startswith("application/json") else "?")
            except requests.RequestException as exc:
                codigo, estado = 0, type(exc).__name__
            reqs.append({"t": round(t0 - inicio, 3), "fase": fase.nombre, "codigo": codigo, "estado": estado,
                         "latencia_s": round(time.monotonic() - t0, 4)})
            time.sleep(fase.think)
        while time.monotonic() < fin and fase.destino == "matriculas":
            alumno = pool.tomar()
            if alumno is None:
                reqs.append({"t": t_rel(), "fase": fase.nombre, "codigo": -1, "estado": "SIN_ALUMNOS", "latencia_s": 0.0})
                return
            t0 = time.monotonic()
            try:
                r = stack.matricular(alumno, s, timeout=20)
                codigo, estado = r.status_code, (r.json().get("estado") if r.headers.get("content-type", "").startswith("application/json") else "?")
            except requests.RequestException as exc:
                codigo, estado = 0, type(exc).__name__
            reqs.append({"t": round(t0 - inicio, 3), "fase": fase.nombre, "codigo": codigo, "estado": estado,
                         "latencia_s": round(time.monotonic() - t0, 4)})
            time.sleep(fase.think)

    hilos = [threading.Thread(target=muestrear, daemon=True),
             threading.Thread(target=sondear, daemon=True, args=("orq_sagas", f"{stack.orq}/v1/sagas/{sid_sonda}", {"X-API-Key": ORQ_KEY})),
             threading.Thread(target=sondear, daemon=True, args=("ins_enrollments", f"{stack.ins}/v1/enrollments?limit=1", {"X-API-Key": ADMIN})),
             threading.Thread(target=sondear, daemon=True, args=("pagos_health", f"{stack.pagos}/v1/health", {}))]
    for h in hilos:
        h.start()

    try:
        for fase in fases:
            fase_actual[0] = fase.nombre
            stack.falla_pagos(fase.latencia_ms, fase.falla)
            antes = stack.snapshot()
            t_ini = t_rel()
            print(f"  fase {fase.nombre:<12} {fase.segundos:>4.0f} s · {fase.workers:>2} workers · latencia TBK {fase.latencia_ms} ms · caído={fase.falla} · → {fase.destino}", flush=True)
            fin = time.monotonic() + fase.segundos
            ts_w = [threading.Thread(target=worker, args=(fase, fin)) for _ in range(fase.workers)]
            for h in ts_w:
                h.start()
            for h in ts_w:
                h.join()
            marcas.append({"variante": variante, "fase": fase.nombre, "t_ini": t_ini, "t_fin": t_rel(), "workers": fase.workers,
                           "think_s": fase.think, "tbk_latencia_ms": fase.latencia_ms, "tbk_caido": int(fase.falla), "destino": fase.destino})
            estados.append({"variante": variante, "momento": f"antes_{fase.nombre}", "t": t_ini, **antes})
            estados.append({"variante": variante, "momento": f"despues_{fase.nombre}", "t": t_rel(), **stack.snapshot()})
        stack.falla_pagos(0, False)
        parar_sondas.set()
        # Convergencia: tras la falla, ¿cuánto tarda el sistema en dejar de tener sagas a medias?
        fase_actual[0] = "drenaje"
        t_fin = time.monotonic()
        convergio = None
        while time.monotonic() - t_fin < drenaje_s:
            snap = stack.snapshot()
            if sum(snap[f"sagas_{e}"] for e in EN_VUELO) == 0:
                convergio = time.monotonic() - t_fin
                break
            time.sleep(1)
        final = stack.snapshot()
        completadas, esperando = final["sagas_COMPLETADA"], final["sagas_ESPERANDO_PAGO"]
        final.update(dict(convergencia_s=round(convergio, 1) if convergio is not None else -1,
                          sagas_en_vuelo=sum(final[f"sagas_{e}"] for e in EN_VUELO),
                          cupos_huerfanos=final["cupos_ocupados"] - completadas - esperando,
                          cobros_huerfanos=final["pagos_COBRADO"] - completadas))
        estados.append({"variante": variante, "momento": "final", "t": t_rel(), **final})
        print(f"  convergencia: {'%.1f s' % convergio if convergio is not None else 'NO convergió en %.0f s' % drenaje_s}; "
              f"cupos huérfanos={final['cupos_huerfanos']}, cobros huérfanos={final['cobros_huerfanos']}", flush=True)
    finally:
        parar_sondas.set()
        parar_muestreo.set()
        for h in hilos:
            h.join(timeout=5)
    for fila in reqs + sondas + metricas + marcas + estados:
        fila["variante"], fila["rep"] = variante, rep
    return dict(requests=reqs, sondas=sondas, metricas=metricas, fases=marcas, estados=estados)


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

    if not args.solo_cascada:
        filas_base = []
        for escenario, esperado, falla in ESCENARIOS_BASE:
            stack = Stack("base", "con_patron").start()
            try:
                filas_base.extend(escenario_base(stack, args.repeticiones_base, escenario, esperado, falla))
            finally:
                stack.stop()
        escribir_csv(RAW / f"{prefijo}_escenarios.csv", filas_base)

    if not args.solo_base:
        todo = {k: [] for k in ("requests", "sondas", "metricas", "fases", "estados")}
        variantes = [v.strip() for v in args.variantes.split(",") if v.strip()]
        reps = list(range(args.desde_rep, args.desde_rep + args.repeticiones))
        for rep in reps:
            for variante in variantes:
                stack = Stack("cascada", variante).start()
                try:
                    datos = cascada(stack, plan(args.rapido), ts, args.drenaje, rep)
                finally:
                    stack.stop()
                for k in todo:
                    todo[k].extend(datos[k])
        for k, filas in todo.items():
            ruta = RAW / f"{prefijo}_{k}.csv"
            escribir_csv(ruta, fusionar(ruta, filas, variantes, reps))

    if not args.sin_graficar:
        sys.path.insert(0, str(ROOT / "experiment"))
        import graficar_saga_fallas
        graficar_saga_fallas.generar(ts)
    print("Listo.", flush=True)


if __name__ == "__main__":
    main()
