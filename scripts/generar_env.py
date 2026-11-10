#!/usr/bin/env python3
"""Genera el archivo .env con secretos ALEATORIOS (clave admin y contrasena de PostgreSQL).

El repositorio ya no trae ningun secreto por defecto: la API se niega a arrancar sin una clave admin
propia. Este script copia .env.example a .env y completa lo que falta.

  python scripts/generar_env.py            # crea .env (no pisa uno existente)
  python scripts/generar_env.py --forzar   # lo regenera (las claves nuevas invalidan las anteriores)

OJO: si ya existe un volumen de PostgreSQL creado con otra contrasena, cambiarla aqui no la cambia en
el volumen: `docker compose down -v` (borra los datos) o `ALTER USER` dentro de la base.
"""
import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--forzar", action="store_true", help="sobrescribe un .env existente")
    args = ap.parse_args()

    destino, plantilla = ROOT / ".env", ROOT / ".env.example"
    if destino.exists() and not args.forzar:
        lineas = destino.read_text(encoding="utf-8").splitlines()
        secretos = {
            "MATRICULAS_DB_PASSWORD": secrets.token_urlsafe(24),
            "MATRICULAS_API_KEYS": secrets.token_urlsafe(32),
            "ORCHESTRATOR_API_KEY": secrets.token_urlsafe(32),
            "INTERNAL_KEY": secrets.token_urlsafe(32),
            "MATRICULAS_JWT_SECRET": secrets.token_urlsafe(48),
        }
        modificada = False
        for nombre, valor in secretos.items():
            indices = [i for i, linea in enumerate(lineas) if linea.startswith(nombre + "=")]
            if indices:
                # Completa solo valores vacíos: conserva toda credencial ya configurada.
                indice = indices[-1]
                if not lineas[indice].split("=", 1)[1].strip():
                    lineas[indice] = f"{nombre}={valor}"
                    modificada = True
            else:
                lineas.append(f"{nombre}={valor}")
                modificada = True
        if modificada:
            destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")
            try:
                destino.chmod(0o600)
            except OSError:
                pass
            print(".env existente: se completaron credenciales vacías o faltantes sin cambiar las ya configuradas.")
        else:
            print(".env ya existe: no se modifica (usa --forzar para regenerarlo).")
        return 0
    valores = {
        "ORCHESTRATOR_API_KEY": secrets.token_urlsafe(32),
        "INTERNAL_KEY": secrets.token_urlsafe(32),
        "MATRICULAS_API_KEYS": secrets.token_urlsafe(32),
        "MATRICULAS_JWT_SECRET": secrets.token_urlsafe(48),
        "MATRICULAS_DB_PASSWORD": secrets.token_urlsafe(24),
    }
    lineas = []
    for linea in plantilla.read_text(encoding="utf-8").splitlines():
        nombre = linea.split("=", 1)[0]
        lineas.append(f"{nombre}={valores[nombre]}" if nombre in valores else linea)
    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    try:
        destino.chmod(0o600)  # solo el dueno lee los secretos (no aplica en Windows)
    except OSError:
        pass
    print(f"Creado {destino.relative_to(ROOT)} con claves aleatorias. No lo subas al repositorio ni lo envies en el ZIP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
