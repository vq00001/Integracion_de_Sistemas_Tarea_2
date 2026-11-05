#!/usr/bin/env python3
"""Ejecuta la suite de pruebas completa, limpia los cachés y deja el sistema en blanco.

Pasos:
  1. Espera a que la API responda.
  2. Ejecuta pytest (sin escribir cachés propios).
  3. Borra los cachés de la corrida: __pycache__, .pytest_cache y los stubs gRPC que
     generan las pruebas (tests/.generated).
  4. (Opcional, activo en el contenedor) Restablece el estado: elimina los contenedores
     y los volúmenes de datos de Cupos y Matrículas, para que la próxima corrida parta de
     cero y no herede cupos retenidos ni matrículas de pruebas anteriores.
  5. Termina con el mismo código de salida de pytest (0 = todo bien).

Uso:
  python scripts/ejecutar_tests.py                  # suite completa (122 pruebas)
  python scripts/ejecutar_tests.py --rapido         # sin las pruebas que apagan contenedores
  python scripts/ejecutar_tests.py --reset-estado   # además borra contenedores y volúmenes
  python scripts/ejecutar_tests.py -k idempotency   # cualquier otro argumento va a pytest

En contenedor:
  docker compose run --rm --build tests [opciones]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SERVICIOS = ["gateway", "orquestador", "matriculas-api", "cupos", "matriculas-db"]
CACHES = ("__pycache__", ".pytest_cache", ".generated")


def esperar_api(segundos: float = 120) -> None:
    print(f"Esperando a la API en {BASE_URL} ...", flush=True)
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        try:
            if requests.get(f"{BASE_URL}/v1/health", timeout=3).status_code == 200:
                print("API lista.", flush=True)
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    sys.exit("La API no respondió a tiempo: ¿está levantado el sistema (docker compose up -d)?")


def limpiar_caches() -> int:
    """Borra cachés de la corrida. Devuelve cuántas carpetas eliminó."""
    n = 0
    for nombre in CACHES:
        for ruta in list(ROOT.rglob(nombre)):
            if {".git", "node_modules", ".venv"} & set(ruta.parts) or not ruta.is_dir():
                continue
            shutil.rmtree(ruta, ignore_errors=True)
            n += 1
    return n


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], cwd=ROOT, capture_output=True, text=True)


def nombre_proyecto() -> str:
    r = _docker("compose", "config", "--format", "json")
    try:
        return json.loads(r.stdout)["name"]
    except (ValueError, KeyError):
        return os.getenv("COMPOSE_PROJECT_NAME", "integracion-sistemas-t1")


def restablecer_estado() -> None:
    """Elimina contenedores y volúmenes de datos de los servicios (no el que ejecuta esto)."""
    print("Restableciendo el estado (contenedores y volúmenes de datos) ...", flush=True)
    r = _docker("compose", "rm", "-sf", *SERVICIOS)
    if r.returncode != 0:
        print("  aviso: no se pudieron eliminar los contenedores:", r.stderr.strip())
    proyecto = nombre_proyecto()
    vols = _docker("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={proyecto}")
    nombres = vols.stdout.split()
    if nombres:
        r = _docker("volume", "rm", *nombres)
        print("  volúmenes eliminados:" if r.returncode == 0 else "  aviso al eliminar volúmenes:",
              " ".join(nombres) if r.returncode == 0 else r.stderr.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rapido", action="store_true",
                    help='omite las pruebas "falla" (las que apagan/reinician contenedores)')
    ap.add_argument("--reset-estado", action=argparse.BooleanOptionalAction,
                    default=os.getenv("RESET_ESTADO", "0") == "1",
                    help="al terminar, borra contenedores y volúmenes (por defecto: variable RESET_ESTADO)")
    args, pytest_extra = ap.parse_known_args()

    esperar_api()
    from tests.conftest import API_KEY
    from tests.support.servicios import esperar_catalogo, esperar_cupos
    try:
        esperar_cupos(os.getenv("CUPOS_GRPC_ADDR", "localhost:50051"))
        esperar_catalogo(BASE_URL, API_KEY)
    except RuntimeError as exc:
        print(f"Preparación fallida: {exc}", file=sys.stderr)
        return 1
    cmd = [sys.executable, "-m", "pytest", "tests", "-q", "-ra", "-p", "no:cacheprovider"]
    if args.rapido:
        cmd += ["-m", "not falla"]
    cmd += pytest_extra
    print("$", " ".join(cmd), flush=True)
    codigo = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).returncode

    print(f"\nLimpiando cachés de la corrida: {limpiar_caches()} carpeta(s) eliminada(s).", flush=True)
    if args.reset_estado:
        restablecer_estado()
    print("\nRESULTADO:", "TODAS LAS PRUEBAS PASARON" if codigo == 0 else f"HAY FALLOS (código {codigo})")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
