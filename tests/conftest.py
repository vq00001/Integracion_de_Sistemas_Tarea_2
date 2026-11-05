import importlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def _clave_admin() -> str:
    """Clave admin de las pruebas. NO hay valor por defecto (la API se niega a arrancar sin
    clave propia): se toma de API_KEY / MATRICULAS_API_KEYS o del .env que genera
    `python scripts/generar_env.py`."""
    for nombre in ("API_KEY", "MATRICULAS_API_KEYS"):
        valor = os.getenv(nombre, "").split(",")[0].strip()
        if valor:
            return valor
    env = ROOT / ".env"
    if env.exists():
        for linea in env.read_text(encoding="utf-8").splitlines():
            if linea.startswith("MATRICULAS_API_KEYS="):
                return linea.split("=", 1)[1].split(",")[0].strip()
    pytest.exit("Falta la clave admin: ejecuta `python scripts/generar_env.py` o define API_KEY.", returncode=2)


API_KEY = _clave_admin()
CURSO_CON_CUPO = int(os.getenv("CURSO_CON_CUPO", "1"))
CURSO_CAPACIDAD_BAJA = int(os.getenv("CURSO_CAPACIDAD_BAJA", "5"))
CURSO_LLENO = CURSO_CAPACIDAD_BAJA
CUPOS_GRPC_ADDR = os.getenv("CUPOS_GRPC_ADDR", "localhost:50051")
COMPOSE_ARGS = os.getenv("COMPOSE_ARGS", "-f docker-compose.yml -f docker-compose.test.yml").split()


def rut_valido(semilla: int | None = None) -> str:
    """RUT chileno con dígito verificador correcto (la API valida el módulo 11)."""
    cuerpo = str(semilla if semilla is not None else 10_000_000 + uuid.uuid4().int % 80_000_000)
    suma, factor = 0, 2
    for d in reversed(cuerpo):
        suma += int(d) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - suma % 11
    dv = "0" if resto == 11 else "K" if resto == 10 else str(resto)
    return f"{cuerpo}-{dv}"


class Api:
    """Cliente HTTP mínimo con la API key por defecto."""

    def __init__(self, base_url: str, api_key: str | None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key} if api_key else {}

    def request(self, method: str, path: str, **kw):
        headers = {**self.headers, **kw.pop("headers", {})}
        kw.setdefault("timeout", 10)
        return requests.request(method, f"{self.base_url}{path}", headers=headers, **kw)

    def get(self, path, **kw): return self.request("GET", path, **kw)
    def post(self, path, **kw): return self.request("POST", path, **kw)
    def put(self, path, **kw): return self.request("PUT", path, **kw)
    def delete(self, path, **kw): return self.request("DELETE", path, **kw)


@pytest.fixture(scope="session")
def api() -> Api:
    return Api(BASE_URL, API_KEY)


@pytest.fixture(scope="session")
def api_sin_auth() -> Api:
    return Api(BASE_URL, None)


@pytest.fixture
def estudiante(api) -> dict:
    """Estudiante nuevo y único por prueba (evita colisiones entre tests)."""
    sufijo = uuid.uuid4().hex[:8]
    payload = {
        "rut": rut_valido(),
        "nombre": f"Test {sufijo[:4]}",
        "apellido": "Api",
        "email": f"t{sufijo}@example.com",
        "telefono": "+56900000000",
    }
    r = api.post("/v1/students", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="session")
def cupos_stub(tmp_path_factory):
    """Stub gRPC generado al vuelo desde proto/cupos.proto (paquete `cupo`,
    servicio `Cupos`, métodos camelCase, claves de idempotencia en Ocupar/Liberar)."""
    import grpc
    from grpc_tools import protoc as tools
    out = tmp_path_factory.mktemp("cupos_contract_stubs")
    proto_dir = ROOT / "proto"
    rc = tools.main(["protoc", f"-I{proto_dir}", f"--python_out={out}", f"--grpc_python_out={out}",
                     str(proto_dir / "cupos.proto")])
    assert rc == 0, "no compiló cupos.proto"
    sys.path.insert(0, str(out))
    pb2 = importlib.import_module("cupos_pb2")
    pb2_grpc = importlib.import_module("cupos_pb2_grpc")
    from tests.support.servicios import esperar_cupos
    try:
        esperar_cupos(CUPOS_GRPC_ADDR, timeout=10)
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)
    with grpc.insecure_channel(CUPOS_GRPC_ADDR) as channel:
        stub = pb2_grpc.CuposStub(channel)
        yield type("CuposCtx", (), {"pb2": pb2, "stub": stub, "grpc": grpc})
    sys.path.remove(str(out))


@pytest.fixture
def curso_lleno(cupos_stub):
    """Vacía CURSO_CAPACIDAD_BAJA (id 5, 2 cupos en la semilla real) ocupando sus cupos
    con claves de idempotencia propias de esta prueba, y lo restaura al terminar. La
    semilla real de Cupos no trae ningún curso con cupos_libres=0 de fábrica."""
    pb2, stub = cupos_stub.pb2, cupos_stub.stub
    claves = []
    libres = stub.obtenerCupo(pb2.ObtenerRequest(curso_id=CURSO_CAPACIDAD_BAJA)).cupos_libres
    for _ in range(libres):
        clave = f"setup-lleno-{uuid.uuid4().hex[:8]}"
        stub.ocuparCupo(pb2.OcuparRequest(curso_id=CURSO_CAPACIDAD_BAJA, idempotency_key=clave))
        claves.append(clave)
    yield CURSO_CAPACIDAD_BAJA
    for clave in claves:
        # Cupos solo libera lo que ESA clave ocupó: se libera con la misma clave de la ocupación.
        stub.liberarCupo(pb2.LiberarRequest(curso_id=CURSO_CAPACIDAD_BAJA, idempotency_key=clave))


def compose(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Ejecuta `docker compose`. `env` agrega variables que compose sustituye en
    docker-compose.yml (p. ej. SIMULAR_LATENCIA_MS)."""
    return subprocess.run(["docker", "compose", *COMPOSE_ARGS, *args], cwd=ROOT,
                          check=True, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def esperar_api(api, segundos: float = 40) -> None:
    """Espera a que la API responda tras reiniciarla."""
    import time
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        try:
            if requests.get(f"{api.base_url}/v1/health", timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError("la API no volvió a responder")
