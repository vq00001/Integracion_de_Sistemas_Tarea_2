"""Exporta el contrato OpenAPI de la API a docs/openapi.json y docs/openapi.yaml.

El contrato se versiona en el repositorio y una prueba de contrato (tests/contract) falla
si el archivo difiere del que publica la API en ejecución.

Uso (desde la raíz del repo):

  # Recomendado: exporta lo que sirve la API REAL (la del contenedor), con sus versiones
  # exactas de FastAPI/Pydantic. Evita diferencias entre tu entorno local y el contenedor.
  python matriculas-api/exportar_openapi.py --desde-url http://localhost:8000/openapi.json

  # Alternativa sin la API levantada (requiere los stubs gRPC: python matriculas-api/generar_stubs.py)
  python matriculas-api/exportar_openapi.py
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

import yaml

AQUI = Path(__file__).resolve().parent


def cargar_esquema(url: str | None) -> dict:
    if url:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    sys.path.insert(0, str(AQUI))
    from app.main import app  # noqa: E402  (requiere los stubs gRPC generados)
    return app.openapi()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde-url", help="URL de /openapi.json de la API en ejecución")
    ap.add_argument("--orquestador-desde-url", help="URL de /openapi.json del orquestador (opcional)")
    args = ap.parse_args()

    schema = cargar_esquema(args.desde_url)
    docs = AQUI / "docs"
    docs.mkdir(exist_ok=True)
    # UTF-8 explícito: el contrato tiene tildes y en Windows el valor por defecto es cp1252.
    (docs / "openapi.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    (docs / "openapi.yaml").write_text(
        yaml.safe_dump(schema, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
    # Una exportación remota no requiere FastAPI, stubs gRPC ni configuración local.
    # El contrato del orquestador solo cambia si se exporta localmente o se da su URL.
    orchestrator_schema = None
    if args.orquestador_desde_url:
        with urllib.request.urlopen(args.orquestador_desde_url, timeout=15) as response:
            orchestrator_schema = json.load(response)
    elif not args.desde_url:
        from app.orchestrator_main import app as orchestrator_app
        orchestrator_schema = orchestrator_app.openapi()
    if orchestrator_schema is not None:
        (docs / "orchestrator.openapi.json").write_text(
            json.dumps(orchestrator_schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Contrato exportado en {docs}/openapi.json y openapi.yaml")


if __name__ == "__main__":
    main()
