"""Inicialización idempotente de secretos y configuraciones privadas por servicio."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import quote

SECRET_BYTES = {
    'MATRICULAS_DB_PASSWORD': 24, 'MATRICULAS_API_KEYS': 32,
    'MATRICULAS_JWT_SECRET': 48, 'ORCHESTRATOR_API_KEY': 32,
    'INTERNAL_KEY': 32, 'INSCRIPCION_WRITER_KEY': 32,
    'PAGOS_API_KEY': 32, 'PAGOS_CALLBACK_KEY': 32,
}


def atomic_write(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize(state=Path('/state'), config=Path('/config'), provided=None):
    provided = os.environ if provided is None else provided
    state, config = Path(state), Path(config)
    state.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(state / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        master = state / 'secrets.json'
        stored = json.loads(master.read_text(encoding='utf-8')) if master.exists() else {}
        if master.exists() and (not isinstance(stored, dict) or not set(SECRET_BYTES) <= stored.keys()):
            raise ValueError('Almacén de secretos incompleto o inválido; restaurar su copia, no regenerar las claves.')
        values = {name: provided.get(name) or stored.get(name) or secrets.token_urlsafe(size)
                  for name, size in SECRET_BYTES.items()}
        for name, value in values.items():
            if not isinstance(value, str) or not value or '\x00' in value:
                raise ValueError(f'Credencial inválida: {name}')
            if name == 'MATRICULAS_DB_PASSWORD' and ('\n' in value or '\r' in value):
                raise ValueError('MATRICULAS_DB_PASSWORD no admite saltos de línea con POSTGRES_PASSWORD_FILE')
            if name != 'MATRICULAS_DB_PASSWORD':
                parts = value.split(',') if name == 'MATRICULAS_API_KEYS' else [value]
                minimum = 32 if name == 'MATRICULAS_JWT_SECRET' else 24
                if any(len(part.strip()) < minimum for part in parts):
                    raise ValueError(f'Credencial demasiado corta: {name}')
        # Primero se persisten las claves: un arranque interrumpido las reutiliza.
        atomic_write(master, json.dumps(values), 0o600)
        user = provided.get('MATRICULAS_DB_USER') or 'matriculas_user'
        database = provided.get('MATRICULAS_DB_NAME') or 'matriculas_db'
        database_url = (f'postgresql+psycopg2://{quote(user, safe="")}:'
                        f'{quote(values["MATRICULAS_DB_PASSWORD"], safe="")}@matriculas-db:5432/'
                        f'{quote(database, safe="")}')
        def subset(*names):
            return {name: values[name] for name in names}
        bundles = {
            'db': {'POSTGRES_PASSWORD': values['MATRICULAS_DB_PASSWORD']},
            'api': {'DATABASE_URL': database_url, 'API_KEYS': values['MATRICULAS_API_KEYS'],
                    'JWT_SECRET': values['MATRICULAS_JWT_SECRET'],
                    **subset('INTERNAL_KEY', 'INSCRIPCION_WRITER_KEY')},
            'orquestador': {'SERVICE_API_KEY': values['MATRICULAS_API_KEYS'],
                            **subset('ORCHESTRATOR_API_KEY', 'INTERNAL_KEY', 'INSCRIPCION_WRITER_KEY',
                                     'PAGOS_API_KEY', 'PAGOS_CALLBACK_KEY')},
            'pagos': subset('PAGOS_API_KEY', 'PAGOS_CALLBACK_KEY', 'INTERNAL_KEY'),
            'gateway': subset('MATRICULAS_API_KEYS', 'MATRICULAS_JWT_SECRET', 'ORCHESTRATOR_API_KEY'),
            'legacy': {'DB_PASSWORD': values['MATRICULAS_DB_PASSWORD']},
            # Perfiles privilegiados de pruebas: ya administran el daemon por
            # su socket y necesitan también las claves de inyección de fallas.
            'runner': {'API_KEY': values['MATRICULAS_API_KEYS'], **values},
        }
        for service, bundle in bundles.items():
            # Volumen exclusivo, montado read-only en su destinatario. Permite
            # lectura a los servicios que corren como UID 10001.
            atomic_write(config / service / 'env.json', json.dumps(bundle), 0o444)
        atomic_write(config / 'db/password', values['MATRICULAS_DB_PASSWORD'], 0o444)


if __name__ == '__main__':
    initialize()
    print('Configuraciones privadas inicializadas; se conservaron las claves persistidas.')
