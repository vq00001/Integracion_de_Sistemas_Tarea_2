"""Genera los stubs gRPC de la API a partir de proto/cupos.proto.

Es la única fuente de verdad: los stubs NO se versionan en git, se generan en
el build de Docker (ver Dockerfile) o a mano con:

    python matriculas-api/generar_stubs.py

Los stubs quedan en matriculas-api/app/grpc_stubs/. El generador de gRPC
produce `import cupos_pb2 as cupos__pb2`, que no funciona dentro de un
paquete; este script lo reemplaza por un import relativo.
"""
import argparse
import re
import sys
from pathlib import Path

from grpc_tools import protoc

AQUI = Path(__file__).resolve().parent
CANDIDATOS = [AQUI / "proto", AQUI.parent / "proto"]  # Docker (/srv/proto) o repo
DESTINO = AQUI / "app" / "grpc_stubs"


def generar(destino: Path = DESTINO) -> None:
    proto_dir = next((p for p in CANDIDATOS if (p / "cupos.proto").exists()), None)
    if proto_dir is None:
        sys.exit("No se encontró cupos.proto en: " + ", ".join(map(str, CANDIDATOS)))
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "__init__.py").touch()

    rc = protoc.main([
        "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"--python_out={destino}",
        f"--grpc_python_out={destino}",
        str(proto_dir / "cupos.proto"),
    ])
    if rc != 0:
        sys.exit("Falló la generación de stubs")

    grpc_file = destino / "cupos_pb2_grpc.py"
    texto = grpc_file.read_text(encoding="utf-8")
    texto = re.sub(r"^import cupos_pb2 as cupos__pb2$",
                   "from . import cupos_pb2 as cupos__pb2", texto, flags=re.M)
    grpc_file.write_text(texto, encoding="utf-8", newline="\n")
    print(f"Stubs generados en {destino}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destino', type=Path, default=DESTINO,
                        help='Paquete de salida; permite aislar los stubs de cada prueba')
    args = parser.parse_args()
    generar(args.destino)


if __name__ == "__main__":
    main()
