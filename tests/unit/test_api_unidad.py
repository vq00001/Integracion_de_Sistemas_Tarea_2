"""Pruebas unitarias de la API de Matrículas, EN PROCESO (TestClient + SQLite temporal).

No necesitan Docker ni Cupos: cubren seguridad y política que no se pueden fijar de forma
determinista por red (arranque sin clave, límite de intentos, vencimiento de claves, cabeceras de
deprecación). Las rutas que llaman a Cupos no se ejercitan aquí (las cubre la suite de integración).
"""
import importlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import ROOT, rut_valido

from fastapi.testclient import TestClient

ADMIN = secrets.token_urlsafe(32)


@pytest.fixture(scope="module")
def m():
    api_dir = ROOT / "matriculas-api"
    tmp = Path(tempfile.mkdtemp(prefix="mat_unit_"))
    # No importar archivos generados en otro intérprete desde el repo montado.
    shutil.copytree(api_dir / "app", tmp / "app",
                    ignore=shutil.ignore_patterns("grpc_stubs", "__pycache__"))
    subprocess.run([sys.executable, str(api_dir / "generar_stubs.py"),
                    "--destino", str(tmp / "app" / "grpc_stubs")],
                   check=True, capture_output=True)
    previo = {k: os.environ.get(k) for k in ("API_KEYS", "JWT_SECRET", "DATABASE_URL", "RECONCILE_INTERVAL_SECONDS")}
    os.environ.update(API_KEYS=ADMIN, JWT_SECRET=secrets.token_urlsafe(32),
                      DATABASE_URL=f"sqlite:///{tmp / 'm.db'}", RECONCILE_INTERVAL_SECONDS="0")
    sys.path.insert(0, str(tmp))
    for nombre in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        sys.modules.pop(nombre)
    try:
        ns = type("Mods", (), {})()
        ns.config = importlib.import_module("app.config")
        ns.security = importlib.import_module("app.security")
        ns.ratelimit = importlib.import_module("app.ratelimit")
        ns.database = importlib.import_module("app.database")
        ns.models = importlib.import_module("app.models")
        ns.main = importlib.import_module("app.main")
        yield ns
    finally:
        sys.path.remove(str(tmp))
        for k, v in previo.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)



@pytest.fixture
def cliente(m):
    m.security._limitador_fallos.reiniciar()
    m.security._limitador_escrituras.reiniciar()
    with TestClient(m.main.app) as c:
        yield c


def _admin():
    return {"X-API-Key": ADMIN}


def _alumno(cliente):
    sufijo = uuid.uuid4().hex[:8]
    est = cliente.post("/v1/students", headers=_admin(), json={
        "rut": rut_valido(), "nombre": "Test", "apellido": "Unit", "email": f"u{sufijo}@example.com"}).json()
    clave = cliente.post(f"/v1/students/{est['id']}/api-key", headers=_admin()).json()
    return est, clave


# ---------- Arranque fail-closed (hallazgo 1) ----------

@pytest.mark.parametrize("claves", [set(), {"dev-matriculas-key-123"}, {"corta"}, {"x" * 10, ADMIN}])
def test_la_api_se_niega_a_arrancar_sin_clave_admin_fuerte(m, claves):
    with pytest.raises(RuntimeError):
        m.config.validar_claves_admin(claves)


def test_clave_admin_fuerte_es_aceptada(m):
    m.config.validar_claves_admin({ADMIN})


def test_el_arranque_completo_falla_con_la_clave_publica_del_repositorio(m, monkeypatch):
    monkeypatch.setattr(m.config.settings, "api_keys", "dev-matriculas-key-123")
    with pytest.raises(RuntimeError, match="API_KEYS"):
        with TestClient(m.main.app):
            pass


def test_ningun_archivo_de_configuracion_o_codigo_publica_la_clave_por_defecto():
    """compose, plantillas .env y código no pueden volver a traer la clave pública. (La
    documentación .md sí puede NOMBRARLA, para explicar que está prohibida.)"""
    prohibida = "dev-matriculas-key-123"
    permitidos = {"config.py", "test_api_unidad.py"}   # la lista de prohibidos y su prueba
    for ruta in ROOT.rglob("*"):
        if (not ruta.is_file() or ruta.name in permitidos or ruta.suffix == ".md" or ruta.name == ".env"
                or {".git", "node_modules", "__pycache__", ".generated", "archivo_rev2"} & set(ruta.parts)):
            continue
        try:
            texto = ruta.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        assert prohibida not in texto, f"{ruta.relative_to(ROOT)} aún publica la clave por defecto"


# ---------- Claves de estudiante ----------

def test_formato_de_clave_prefijo_publico_independiente_del_secreto(m):
    clave, prefijo = m.security.generar_api_key()
    assert clave.startswith(prefijo + ".") and len(prefijo) == 12
    secreto = clave.split(".", 1)[1]
    assert secreto[:4] not in prefijo or len(set(secreto[:4])) == 0 or prefijo != "mat_" + secreto[:8]


def test_una_clave_vencida_da_401_y_se_revoca_sola_al_emitir_otra(cliente, m):
    est, clave = _alumno(cliente)
    h = {"X-API-Key": clave["api_key"]}
    assert cliente.get(f"/v1/students/{est['id']}", headers=h).status_code == 200
    with m.database.SessionLocal() as db:   # vence la clave
        fila = db.query(m.models.ApiKey).filter_by(student_id=est["id"]).one()
        fila.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        db.commit()
    r = cliente.get(f"/v1/students/{est['id']}", headers=h)
    assert r.status_code == 401 and "vencida" in r.json()["error"]["message"]
    nueva = cliente.post(f"/v1/students/{est['id']}/api-key", headers=_admin())
    assert nueva.status_code == 201, nueva.text
    assert cliente.get(f"/v1/students/{est['id']}", headers={"X-API-Key": nueva.json()["api_key"]}).status_code == 200


# ---------- Límite de intentos (hallazgo 5) ----------

def test_los_intentos_fallidos_se_limitan_con_429_y_retry_after(cliente, m, monkeypatch):
    monkeypatch.setattr(m.security._limitador_fallos, "limite", 5)
    codigos = [cliente.get("/v1/students", headers={"X-API-Key": f"mala-{i}"}).status_code for i in range(8)]
    assert codigos[:5] == [401] * 5 and codigos[5:] == [429] * 3, codigos
    r = cliente.get("/v1/students", headers={"X-API-Key": "otra"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert r.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    # bloqueada la IP, ni siquiera una clave válida pasa mientras dura el bloqueo
    assert cliente.get("/v1/students", headers=_admin()).status_code == 429


def test_los_aciertos_no_consumen_el_limite_de_intentos_fallidos(cliente, m, monkeypatch):
    monkeypatch.setattr(m.security._limitador_fallos, "limite", 3)
    for _ in range(10):
        assert cliente.get("/v1/students", headers=_admin()).status_code == 200


def test_un_estudiante_no_puede_escribir_en_bucle_pero_el_admin_si(cliente, m, monkeypatch):
    est, clave = _alumno(cliente)
    monkeypatch.setattr(m.security._limitador_escrituras, "limite", 3)
    h = {"X-API-Key": clave["api_key"]}
    codigos = [cliente.post(f"/v1/enrollments/{uuid.uuid4()}/revert", headers=h).status_code for _ in range(5)]
    assert codigos == [404, 404, 404, 429, 429], codigos
    admin = [cliente.post(f"/v1/enrollments/{uuid.uuid4()}/revert", headers=_admin()).status_code for _ in range(6)]
    assert admin == [404] * 6


def test_el_limitador_de_ventana_expira():
    from app.ratelimit import LimitadorVentana
    lim = LimitadorVentana(2, 0.2)
    assert lim.consumir("a") is None and lim.consumir("a") is None
    assert lim.consumir("a") is not None and lim.consumir("b") is None
    import time; time.sleep(0.25)
    assert lim.consumir("a") is None


# ---------- Política de deprecación hecha código (D3, limitación 1) ----------

def test_las_rutas_deprecadas_responden_con_deprecation_sunset_y_link(cliente, m, monkeypatch):
    monkeypatch.setattr(m.config.settings, "deprecated_routes", json.dumps({
        "/v1/health": {"deprecation": "@1793145600", "sunset": "Wed, 28 Oct 2027 00:00:00 GMT",
                       "link": "https://ejemplo.cl/docs/migracion"}}))
    r = cliente.get("/v1/health")
    assert r.headers["Deprecation"] == "@1793145600"
    assert r.headers["Sunset"].startswith("Wed, 28 Oct 2027")
    assert 'rel="deprecation"' in r.headers["Link"]


def test_las_rutas_no_deprecadas_no_llevan_las_cabeceras(cliente):
    r = cliente.get("/v1/health")
    assert "Deprecation" not in r.headers and "Sunset" not in r.headers
