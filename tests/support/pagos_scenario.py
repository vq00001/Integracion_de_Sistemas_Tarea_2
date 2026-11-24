"""Matrícula con cobro Webpay de punta a punta, con procesos REALES y sin Docker ni internet.

Levanta Cupos (gRPC), Inscripción, el Orquestador y el servicio Pagos reales. Lo único simulado es
Transbank (tests/support/fake_transbank.py, mismo contrato REST) y el comprador, que "paga" en el
formulario de Webpay y vuelve a /v1/pagos/retorno como lo hace el navegador.

Escenarios (cada uno imprime 'OK: ...'):
  1. camino feliz con redirección: la saga espera al comprador, se reanuda y completa
  2. tarjeta rechazada: se libera el cupo y no hay cobro
  3. falla el ÚLTIMO paso tras pagar: reembolso en Transbank y compensación en orden inverso
  4. el comprador abandona: el pago vence, se libera el cupo y un pago tardío no se confirma
  5. aviso perdido (orquestador congelado): la saga igual se reanuda consultando a Pagos
  6. Transbank caído: compensación, circuito abierto con respuesta inmediata y recuperación
"""
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.support.fake_transbank import FakeTransbank  # noqa: E402

PY = sys.executable
W = Path(os.environ["SAGA_E2E_WORKDIR"])
W.mkdir(exist_ok=True)
# Los stubs gRPC se generan con este intérprete (no se reutilizan los del anfitrión).
shutil.copytree(ROOT / "matriculas-api/app", W / "app", ignore=shutil.ignore_patterns("grpc_stubs", "__pycache__"))
subprocess.run([PY, str(ROOT / "matriculas-api/generar_stubs.py"), "--destino", str(W / "app/grpc_stubs")],
               check=True, capture_output=True)
subprocess.run([PY, "-m", "grpc_tools.protoc", f"-I{ROOT}/proto", f"--python_out={W}", f"--grpc_python_out={W}",
                str(ROOT / "proto/cupos.proto")], check=True)
sys.path.insert(0, str(W))
import grpc  # noqa: E402
import cupos_pb2 as pb  # noqa: E402
import cupos_pb2_grpc  # noqa: E402


def libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


P_INS, P_CUPOS, P_ORQ, P_PAGOS = libre(), libre(), libre(), libre()
ADMIN = "local-testing-key-32-characters-admin"
INTERNAL = "local-internal-testing-key-32-characters"
ORQ_KEY = "local-orchestrator-testing-key-32-characters"
PAGOS_KEY = "local-pagos-testing-key-32-characters-xx"
CALLBACK = "local-callback-testing-key-32-characters"
TBK_SECRET = "SECRET-DE-PRUEBA"

fake = FakeTransbank(api_key=TBK_SECRET).start()

BASE = {**os.environ, "PYTHONPATH": str(W) + os.pathsep + os.environ.get("PYTHONPATH", "")}
procesos = {}


def lanzar(nombre, cmd, **env):
    log = (W / f"{nombre}.log").open("a")
    procesos[nombre] = subprocess.Popen(cmd, cwd=ROOT, env={**BASE, **env}, stdout=log, stderr=log)
    return procesos[nombre]


def esperar(url):
    for _ in range(150):
        for nombre, proc in procesos.items():
            if proc.poll() is not None:
                raise RuntimeError(f"{nombre} terminó antes de estar listo (exit={proc.returncode})")
        try:
            if requests.get(url, timeout=0.3).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.1)
    raise RuntimeError("servicio no disponible: " + url)


def esperar_hasta(condicion, segundos=15, mensaje="condición no cumplida"):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return
        time.sleep(0.1)
    raise AssertionError(mensaje)


# ---------------------------------------------------------------------------------- utilidades de negocio
_rut = 20_000_000


def alumno():
    global _rut
    _rut += 1
    suma = sum(int(d) * ((i % 6) + 2) for i, d in enumerate(reversed(str(_rut))))
    dv = 11 - suma % 11
    dv = "0" if dv == 11 else "K" if dv == 10 else str(dv)
    r = requests.post(f"http://127.0.0.1:{P_INS}/v1/students", headers={"X-API-Key": ADMIN},
                      json={"rut": f"{_rut}-{dv}", "nombre": "Test", "apellido": "Pago",
                            "email": f"{uuid.uuid4().hex}@example.com"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def matricular(student=None, key=None):
    return requests.post(f"http://127.0.0.1:{P_ORQ}/v1/matriculas", timeout=15,
                         json={"alumno_id": student or alumno(), "curso_id": 1, "monto": 15000},
                         headers={"X-API-Key": ORQ_KEY, "Idempotency-Key": key or str(uuid.uuid4())})


def saga(sid):
    return requests.get(f"http://127.0.0.1:{P_ORQ}/v1/sagas/{sid}", headers={"X-API-Key": ORQ_KEY}, timeout=3).json()


def esperar_saga(sid, estado, segundos=15):
    esperar_hasta(lambda: saga(sid)["estado"] == estado, segundos, f"la saga no llegó a {estado}: {saga(sid)}")
    return saga(sid)


def pago(sid):
    r = requests.get(f"http://127.0.0.1:{P_PAGOS}/v1/payments/by-key/{sid}%3ACOBRAR", headers={"X-API-Key": PAGOS_KEY}, timeout=3)
    return r.json()["estado"]


def token_del_formulario(url_pago):
    """Lo que hace el navegador: abre redirect_url y recibe el formulario que envía token_ws a Webpay."""
    html = requests.get(url_pago, timeout=3)
    assert html.status_code == 200 and "token_ws" in html.text and fake.url in html.text, html.text
    return re.search(r"name='token_ws' value='(\w+)'", html.text).group(1)


def comprador(url_pago, aprueba=True):
    """Paga en Webpay (simulado) y vuelve al comercio con token_ws."""
    token = token_del_formulario(url_pago)
    fake.pagar(token, aprueba)
    return requests.get(f"http://127.0.0.1:{P_PAGOS}/v1/pagos/retorno", params={"token_ws": token}, timeout=15)


stub = None


def invariantes(sid, terminal):
    """El estado en los TRES servicios es coherente con el estado final de la saga."""
    reserva = stub.consultarOperacion(pb.ConsultarOperacionRequest(idempotency_key=f"{sid}:RESERVAR"), timeout=3)
    cupo_activo = reserva.estado_ocupar == pb.CUPO_OCUPADO and reserva.estado_liberar != pb.CUPO_LIBERADO
    items = requests.get(f"http://127.0.0.1:{P_INS}/v1/enrollments", params={"client_key": f"{sid}:INSCRIBIR"},
                         headers={"X-API-Key": ADMIN}).json()["items"]
    inscrito = any(i["estado"] == "CONFIRMADA" for i in items)
    estado_pago = pago(sid)
    if terminal == "COMPLETADA":
        assert cupo_activo and inscrito and estado_pago == "COBRADO", (cupo_activo, inscrito, estado_pago)
    else:
        assert not cupo_activo and not inscrito and estado_pago not in ("COBRADO", "PROCESANDO", "PENDIENTE"), \
            (cupo_activo, inscrito, estado_pago)


def pasos_ok(sid):
    lineas = [json.loads(x) for x in (W / "orquestador.log").read_text().splitlines() if x.startswith("{")]
    return [x["paso"] for x in lineas if x["saga_id"] == sid and x["estado"] == "OK"]


def falla_inscripcion(**cuerpo):
    r = requests.put(f"http://127.0.0.1:{P_INS}/internal/fault", json=cuerpo, headers={"X-Internal-Key": INTERNAL}, timeout=3)
    assert r.status_code == 200, r.text


try:
    lanzar("cupos", [PY, str(ROOT / "cupos/src/server.py")], DB_PATH=str(W / "cupos.db"), GRPC_PORT=str(P_CUPOS))
    lanzar("inscripcion", [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(P_INS)],
           API_KEYS=ADMIN, JWT_SECRET="local-test-secret-at-least-32-characters", INTERNAL_KEY=INTERNAL, INSCRIPCION_WRITER_KEY="isolated-writer-test-key-32-characters",
           DATABASE_URL=f"sqlite:///{W}/inscripcion.db", CUPOS_GRPC_TARGET=f"127.0.0.1:{P_CUPOS}")
    lanzar("pagos", [PY, "-m", "uvicorn", "pagos.app.main:app", "--host", "127.0.0.1", "--port", str(P_PAGOS)],
           DATABASE_URL=f"sqlite:///{W}/pagos.db", PAGOS_API_KEY=PAGOS_KEY, PAGOS_CALLBACK_KEY=CALLBACK,
           INTERNAL_KEY=INTERNAL, INSCRIPCION_WRITER_KEY="isolated-writer-test-key-32-characters", ORQUESTADOR_URL=f"http://127.0.0.1:{P_ORQ}",
           PAGOS_PUBLIC_BASE_URL=f"http://127.0.0.1:{P_PAGOS}", TBK_BASE_URL=fake.url,
           TBK_COMMERCE_CODE="597055555532", TBK_API_KEY=TBK_SECRET, TBK_TIMEOUT_SECONDS="1.5",
           PAGOS_RECONCILE_SECONDS="0.3", PAGOS_PROCESANDO_SECONDS="0.5", PAGOS_CB_THRESHOLD="3",
           PAGOS_CB_RECOVERY_SECONDS="1.5", PAGOS_TTL_SECONDS="600")
    orq_cmd = [PY, "-m", "uvicorn", "app.orchestrator_main:app", "--host", "127.0.0.1", "--port", str(P_ORQ)]
    lanzar("orquestador", orq_cmd, SAGA_DATABASE_URL=f"sqlite:///{W}/sagas.db", ORCHESTRATOR_API_KEY=ORQ_KEY,
           INTERNAL_KEY=INTERNAL, INSCRIPCION_WRITER_KEY="isolated-writer-test-key-32-characters", SERVICE_API_KEY=ADMIN, INSCRIPCION_URL=f"http://127.0.0.1:{P_INS}",
           PAGOS_URL=f"http://127.0.0.1:{P_PAGOS}", PAGOS_API_KEY=PAGOS_KEY, PAGOS_CALLBACK_KEY=CALLBACK,
           CUPOS_GRPC_TARGET=f"127.0.0.1:{P_CUPOS}", SAGA_TIMEOUT_SECONDS="3", SAGA_STALE_SECONDS="30",
           SAGA_RECONCILE_SECONDS="0.3", SAGA_PAGO_POLL_SECONDS="0.5", SAGA_PAGO_GRACIA_SECONDS="1",
           SAGA_CB_THRESHOLD="50")
    for url in (f"http://127.0.0.1:{P_INS}/v1/health", f"http://127.0.0.1:{P_PAGOS}/v1/health",
                f"http://127.0.0.1:{P_ORQ}/metrics"):
        esperar(url)
    stub = cupos_pb2_grpc.CuposStub(grpc.insecure_channel(f"127.0.0.1:{P_CUPOS}"))

    # ------------------------------------------------------------------ 1. camino feliz con redirección
    estudiante, clave = alumno(), str(uuid.uuid4())
    r = matricular(estudiante, clave)
    assert r.status_code == 202 and r.json()["estado"] == "ESPERANDO_PAGO", r.text
    s1 = r.json()
    assert s1["pago_url"].startswith(f"http://127.0.0.1:{P_PAGOS}/v1/pagos/ir/") and s1["pago_expira_at"]
    assert matricular(estudiante, clave).json()["pago_url"] == s1["pago_url"]       # replay: misma URL de pago
    assert pasos_ok(s1["id"]) == ["RESERVAR"]                                         # aún no se cobra ni se inscribe
    pagina = comprador(s1["pago_url"], aprueba=True)
    assert "Pago aprobado" in pagina.text
    esperar_saga(s1["id"], "COMPLETADA")
    invariantes(s1["id"], "COMPLETADA")
    assert matricular(estudiante, clave).status_code == 200                           # replay de una saga completada
    assert fake.calls["commit"] == 1 and pasos_ok(s1["id"]) == ["RESERVAR", "COBRAR", "INSCRIBIR"]
    print("OK: camino feliz con redirección a Webpay, reanudación y replay", flush=True)

    # ------------------------------------------------------------------ 2. tarjeta rechazada
    r = matricular()
    assert r.status_code == 202
    assert "Pago rechazado" in comprador(r.json()["pago_url"], aprueba=False).text
    s2 = esperar_saga(r.json()["id"], "COMPENSADA")
    assert s2["motivo_fallo"] == "COBRAR:RECHAZADO" and fake.calls["refund"] == 0     # no hubo cobro que devolver
    invariantes(s2["id"], "COMPENSADA")
    assert pasos_ok(s2["id"]) == ["RESERVAR", "RESERVAR"]                             # reserva y su liberación
    print("OK: tarjeta rechazada libera el cupo sin reembolso", flush=True)

    # ------------------------------------------------------------------ 3. falla el último paso tras pagar
    falla_inscripcion(falla=True)
    r = matricular()
    assert r.status_code == 202
    comprador(r.json()["pago_url"], aprueba=True)
    s3 = esperar_saga(r.json()["id"], "COMPENSADA")
    falla_inscripcion()
    assert pago(s3["id"]) == "REEMBOLSADO" and fake.calls["refund"] == 1
    invariantes(s3["id"], "COMPENSADA")
    # Orden inverso: [reservó, cobró] y luego baja de inscripción, reembolso y liberación del cupo.
    assert pasos_ok(s3["id"]) == ["RESERVAR", "COBRAR", "INSCRIBIR", "COBRAR", "RESERVAR"], pasos_ok(s3["id"])
    print("OK: falla el último paso tras pagar → reembolso y compensación en orden inverso", flush=True)

    # ------------------------------------------------------------------ 4. el comprador abandona
    r = matricular()
    s4_id, url4 = r.json()["id"], r.json()["pago_url"]
    token4 = token_del_formulario(url4)
    with sqlite3.connect(W / "pagos.db") as db:                                       # el plazo de pago ya venció
        db.execute("UPDATE payments SET expires_at = datetime('now', '-1 second') WHERE idempotency_key = ?", (f"{s4_id}:COBRAR",))
    esperar_saga(s4_id, "COMPENSADA")
    assert saga(s4_id)["motivo_fallo"] == "COBRAR:EXPIRADO"
    commits_antes = fake.calls["commit"]
    fake.pagar(token4, True)                                                          # …pero pagó tarde en Webpay
    tarde = requests.get(f"http://127.0.0.1:{P_PAGOS}/v1/pagos/retorno", params={"token_ws": token4}, timeout=5)
    assert "Pago vencido" in tarde.text and fake.calls["commit"] == commits_antes     # no se confirmó: nunca se cobra
    invariantes(s4_id, "COMPENSADA")
    print("OK: pago vencido libera el cupo y un pago tardío no se confirma", flush=True)

    # ------------------------------------------------------------------ 5. aviso perdido (orquestador congelado)
    r = matricular()
    s5_id = r.json()["id"]
    os.kill(procesos["orquestador"].pid, signal.SIGSTOP)                              # el aviso de Pagos no llega
    try:
        comprador(r.json()["pago_url"], aprueba=True)
    finally:
        os.kill(procesos["orquestador"].pid, signal.SIGCONT)
    esperar_saga(s5_id, "COMPLETADA")                                                 # consulta de Pagos + reintento de aviso
    invariantes(s5_id, "COMPLETADA")
    print("OK: aviso perdido, la saga se reanuda consultando a Pagos", flush=True)

    # ------------------------------------------------------------------ 6. Transbank caído (dependencia frágil)
    fake.config(falla=True)
    tiempos, estados = [], []
    for _ in range(5):
        t0 = time.monotonic()
        r = matricular()
        tiempos.append(time.monotonic() - t0)
        estados.append((r.status_code, r.json()["estado"]))
        invariantes(r.json()["id"], "COMPENSADA")                                     # sin cobro, cupo liberado
    assert all(e == (503, "COMPENSADA") for e in estados), estados
    metricas = requests.get(f"http://127.0.0.1:{P_PAGOS}/metrics", timeout=3).text
    assert 'circuito_estado{dependencia="TRANSBANK",estado="OPEN"} 1' in metricas, metricas
    assert max(tiempos[3:]) < 1.0, tiempos                                            # con el circuito abierto responde rápido
    fake.config(falla=False)
    time.sleep(1.8)                                                                   # recuperación → HALF_OPEN → CLOSED
    r = matricular()
    assert r.status_code == 202 and r.json()["estado"] == "ESPERANDO_PAGO", r.text
    comprador(r.json()["pago_url"], aprueba=True)
    esperar_saga(r.json()["id"], "COMPLETADA")
    invariantes(r.json()["id"], "COMPLETADA")
    print("OK: Transbank caído compensa, abre el circuito (respuesta rápida) y se recupera", flush=True)
finally:
    for nombre, proc in procesos.items():
        if proc.poll() is None:
            proc.terminate()
    for nombre, proc in procesos.items():
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    fake.stop()
