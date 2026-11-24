"""Carga la configuración privada y ejecuta el servicio sin pasar secretos por argv."""
import json
import os
import re
import sys
from pathlib import Path


def load():
    path = os.getenv('SERVICE_ENV_FILE')
    if not path:
        return
    values = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(values, dict) or any(
            not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or not isinstance(value, str)
            for key, value in values.items()):
        raise RuntimeError('Configuración privada inválida')
    for name, value in values.items():
        if not os.environ.get(name):
            os.environ[name] = value


if __name__ == '__main__':
    load()
    if len(sys.argv) < 2:
        raise SystemExit('Falta el comando del servicio')
    os.execvp(sys.argv[1], sys.argv[1:])
