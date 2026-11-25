"""Regresión de H7 con API/Cupos y bases temporales; no administra Docker.

Uso desde la raíz: .venv/bin/python tests/support/validar_h7.py
Argumentos opcionales reemplazan la selección predeterminada de pytest.
"""
from pathlib import Path
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ['tests']
FILTERS = ['-m', 'not falla', '--ignore=tests/integration/test_h5_arranque_limpio.py',
           '--deselect=tests/integration/test_h5_arranque_limpio.py']


def port():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        return server.getsockname()[1]


def main():
    processes = []
    with tempfile.TemporaryDirectory(prefix='h7-regresion-') as folder:
        tmp = Path(folder)
        shutil.copytree(ROOT / 'matriculas-api/app', tmp / 'app',
                        ignore=shutil.ignore_patterns('grpc_stubs', '__pycache__'))
        py = sys.executable
        subprocess.run([py, str(ROOT / 'matriculas-api/generar_stubs.py'),
                        '--destino', str(tmp / 'app/grpc_stubs')], check=True)
        subprocess.run([py, '-m', 'grpc_tools.protoc', f'-I{ROOT}/proto',
                        f'--python_out={tmp}', f'--grpc_python_out={tmp}',
                        str(ROOT / 'proto/cupos.proto')], check=True)
        api_port, grpc_port = port(), port()
        key = secrets.token_urlsafe(32)
        base = f'http://127.0.0.1:{api_port}'
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(tmp),
               'API_KEYS': key, 'API_KEY': key, 'JWT_SECRET': secrets.token_urlsafe(48),
               'DATABASE_URL': f'sqlite:///{tmp}/inscripcion.db',
               'SAGA_DATABASE_URL': f'sqlite:///{tmp}/sagas.db',
               'DB_PATH': str(tmp / 'cupos.db'), 'GRPC_PORT': str(grpc_port),
               'CUPOS_GRPC_TARGET': f'127.0.0.1:{grpc_port}',
               'CUPOS_GRPC_ADDR': f'127.0.0.1:{grpc_port}',
               'BASE_URL': base, 'INSCRIPCION_TEST_URL': base,
               'INSCRIPCION_WRITER_KEY': secrets.token_urlsafe(32),
               'INTERNAL_KEY': secrets.token_urlsafe(32),
               'SIMULAR_LATENCIA_MS': '0', 'SIMULAR_LATENCIA_MODO': 'antes'}
        try:
            for name, command in [
                ('cupos', [py, str(ROOT / 'cupos/src/server.py')]),
                ('api', [py, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                         '--port', str(api_port)]),
            ]:
                with (tmp / f'{name}.log').open('w') as log:
                    processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log))
            deadline = time.monotonic() + 30
            while True:
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError('Un servicio temporal terminó antes de estar listo')
                try:
                    with urllib.request.urlopen(base + '/v1/health', timeout=1) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError('La API temporal no respondió en 30 segundos')
                time.sleep(.1)
            selection = sys.argv[1:] or DEFAULT
            return subprocess.run([py, '-m', 'pytest', *selection, *FILTERS, '-q', '-p', 'no:cacheprovider',
                                   '--tb=short'], cwd=ROOT, env=env).returncode
        except Exception:
            for log in sorted(tmp.glob('*.log')):
                print(f'{log.name}:\n{log.read_text()[-3000:]}', file=sys.stderr)
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    sys.exit(main())
