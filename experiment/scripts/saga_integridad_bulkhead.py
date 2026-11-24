#!/usr/bin/env python3
"""Evidencia reproducible de dos hallazgos abiertos de la saga (sin Docker ni red):

  A) Integridad: POST /v1/enrollments inscribe SIN pasar por la saga (sin cupo ni pago), incluso en un curso que no existe.
  B) Bulkhead del orquestador con dependencias SANAS: al superar la capacidad (8) los rechazos se tratan como fallas
     inciertas y las matrículas se compensan aunque nada esté caído.

Uso (desde la raíz):  python experiment/scripts/saga_integridad_bulkhead.py
Salida: experiment/data/raw/saga_integridad_bulkhead_<ts>.csv y experiment/results/resumen_saga_integridad_bulkhead_<ts>.md
"""
import csv
import importlib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW, OUT = ROOT / "experiment" / "data" / "raw", ROOT / "experiment" / "results"
CONCURRENCIAS = (4, 8, 12, 24, 48)
CAPACIDAD, LATENCIA_S = 8, 0.3


def rut(cuerpo: int) -> str:
    suma = sum(int(d) * ((i % 6) + 2) for i, d in enumerate(reversed(str(cuerpo))))
    dv = 11 - suma % 11
    return f"{cuerpo}-{'0' if dv == 11 else 'K' if dv == 10 else dv}"


def preparar():
    tmp = Path(tempfile.mkdtemp(prefix="exp_integridad_"))
    shutil.copytree(ROOT / "matriculas-api/app", tmp / "app", ignore=shutil.ignore_patterns("grpc_stubs", "__pycache__"))
    subprocess.run([sys.executable, str(ROOT / "matriculas-api/generar_stubs.py"), "--destino", str(tmp / "app/grpc_stubs")],
                   check=True, capture_output=True)
    admin = secrets.token_urlsafe(32)
    os.environ.update(API_KEYS=admin, JWT_SECRET=secrets.token_urlsafe(32), DATABASE_URL=f"sqlite:///{tmp / 'm.db'}",
                      RECONCILE_INTERVAL_SECONDS="0", INTERNAL_KEY=secrets.token_urlsafe(32))
    sys.path.insert(0, str(tmp))
    return tmp, admin


def experimento_a(admin: str) -> list[dict]:
    from fastapi.testclient import TestClient
    main = importlib.import_module("app.main")
    filas = []
    with TestClient(main.app) as c:
        h = {"X-API-Key": admin}
        sid = c.post("/v1/students", headers=h, json={"rut": rut(21000001), "nombre": "Ana", "apellido": "Prueba", "email": "a@example.com"}).json()["id"]
        for curso, nota in ((5, "curso con 2 cupos"), (999, "curso INEXISTENTE")):
            r = c.post("/v1/enrollments", headers={**h, "Idempotency-Key": f"k{curso}"}, json={"student_id": sid, "curso_id": curso})
            filas.append({"experimento": "A_bypass_saga", "caso": f"POST /v1/enrollments curso {curso} ({nota})", "codigo": r.status_code,
                          "estado": r.json().get("estado"), "esperado": "rechazo (la regla exige cupo y pago)"})
    return filas


def experimento_b(tmp: Path) -> list[dict]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    sg, sc = importlib.import_module("app.saga"), importlib.import_module("app.saga_clients")
    filas = []
    for n in CONCURRENCIAS:
        eng = create_engine(f"sqlite:///{tmp / f's{n}.db'}", connect_args={"check_same_thread": False, "timeout": 30})
        sg.Base.metadata.create_all(eng, tables=[sg.Saga.__table__, sg.SagaStep.__table__])
        sess = sessionmaker(eng)
        op = lambda s, key, timeout: time.sleep(LATENCIA_S)  # noqa: E731  dependencia SANA con 0,3 s de latencia
        comp = lambda s, key, orig, timeout: time.sleep(0.05)  # noqa: E731
        clientes = {p: sc.Client(op, comp, timeout=2, capacity=CAPACIDAD, threshold=10_000, recovery=15) for p in sg.STEPS}
        orq = sg.Orchestrator(sess, clientes, retries=3, backoff=0.01, stale_seconds=3600)
        t0 = time.monotonic()
        with ThreadPoolExecutor(n) as ex:
            estados = list(ex.map(lambda _: orq.matricular(str(uuid.uuid4()), str(uuid.uuid4()), 1, 100)[0]["estado"], range(n)))
        c = Counter(estados)
        filas.append({"experimento": "B_bulkhead_dependencias_sanas", "simultaneas": n, "capacidad_bulkhead": CAPACIDAD,
                      "COMPLETADA": c["COMPLETADA"], "COMPENSADA": c["COMPENSADA"], "COMPENSACION_PENDIENTE": c["COMPENSACION_PENDIENTE"],
                      "pct_completadas": round(100 * c["COMPLETADA"] / n, 1), "rechazos_bulkhead": sum(x.rejected for x in clientes.values()),
                      "segundos": round(time.monotonic() - t0, 2)})
        print(f"  {n:>3} simultáneas → {dict(c)}  (rechazos del bulkhead: {filas[-1]['rechazos_bulkhead']})", flush=True)
    return filas


def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    RAW.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(exist_ok=True)
    tmp, admin = preparar()
    print("A) Integridad: inscribir sin pasar por la saga")
    a = experimento_a(admin)
    for f in a:
        print(f"  {f['caso']} → {f['codigo']} {f['estado']}")
    print("B) Bulkhead del orquestador con dependencias sanas (0,3 s por paso, capacidad 8)")
    b = experimento_b(tmp)
    campos = list(dict.fromkeys(k for f in a + b for k in f))
    ruta = RAW / f"saga_integridad_bulkhead_{ts}.csv"
    with ruta.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        w.writerows(a + b)
    md = [f"# Integridad y bulkhead con dependencias sanas ({ts})", "",
          "## A) POST /v1/enrollments evita la saga", "",
          "| Caso | Código | Estado | Esperado |", "|---|---|---|---|"]
    md += [f"| {f['caso']} | {f['codigo']} | {f['estado']} | {f['esperado']} |" for f in a]
    md += ["", "Una inscripción `CONFIRMADA` sin reservar cupo ni cobrar rompe las dos reglas de negocio del enunciado.", "",
           "## B) Bulkhead del orquestador (capacidad 8) con dependencias SANAS", "",
           "| Simultáneas | COMPLETADA | COMPENSADA | COMPENSACION_PENDIENTE | % completadas | Rechazos del bulkhead |", "|---:|---:|---:|---:|---:|---:|"]
    md += [f"| {f['simultaneas']} | {f['COMPLETADA']} | {f['COMPENSADA']} | {f['COMPENSACION_PENDIENTE']} | {f['pct_completadas']} | {f['rechazos_bulkhead']} |" for f in b]
    md += ["", "Hasta la capacidad (8) todas se completan; por encima, el rechazo del bulkhead se trata como falla incierta y la matrícula se compensa "
           "aunque ninguna dependencia esté caída."]
    (OUT / f"resumen_saga_integridad_bulkhead_{ts}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"→ {ruta.relative_to(ROOT)}\n→ experiment/results/resumen_saga_integridad_bulkhead_{ts}.md")


if __name__ == "__main__":
    main()
