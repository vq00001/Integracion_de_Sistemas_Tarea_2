"""Migración transaccional del esquema SQLite anterior a Webpay, con backup."""
from datetime import datetime, UTC
from contextlib import closing
import fcntl
import os
from pathlib import Path
import sqlite3
import uuid

from sqlalchemy.schema import CreateIndex, CreateTable


def migrar_esquema(engine):
    if engine.dialect.name != 'sqlite' or engine.url.database in (None, '', ':memory:'):
        return None
    path = Path(engine.url.database).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    locks = path.parent / '.saga-locks'
    locks.mkdir(exist_ok=True)
    with (locks / 'schema-migration').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        engine.dispose()
        with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as db:
            ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='sagas' AND type='table'").fetchone()
            if ddl is None:
                return None
            columns = [row[1] for row in db.execute('PRAGMA table_info(sagas)')]
            if 'ESPERANDO_PAGO' in ddl[0] and {'pago_url', 'pago_expira_at'} <= set(columns):
                return None
            from .saga import Saga
            expected = {column.name for column in Saga.__table__.columns}
            required = expected - {'pago_url', 'pago_expira_at'}
            if not required <= set(columns) <= expected:
                raise RuntimeError('Esquema de sagas desconocido: requiere migración manual conservando los datos.')
            if db.execute('PRAGMA user_version').fetchone()[0] > 2:
                raise RuntimeError('La base de sagas es de una versión posterior; no se migra hacia atrás.')
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            try:
                stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
                backup = path.with_name(path.name + '.pre-webpay.' + stamp + '.bak')
                fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                # El lock de escritura ya impide nuevas escrituras; otro lector
                # puede obtener la copia consistente del estado confirmado.
                with closing(sqlite3.connect(path)) as reader, closing(sqlite3.connect(backup)) as target:
                    reader.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('La copia de seguridad de sagas no superó integrity_check.')
                extras = db.execute("SELECT name, sql FROM sqlite_master WHERE tbl_name='sagas' "
                                    "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
                count = db.execute('SELECT count(*) FROM sagas').fetchone()[0]
                new_name = 'sagas_migracion_' + uuid.uuid4().hex
                create = str(CreateTable(Saga.__table__).compile(engine)).replace(
                    'CREATE TABLE sagas', 'CREATE TABLE ' + new_name, 1)
                db.execute(create)
                names = ','.join('"' + name + '"' for name in columns)
                db.execute(f'INSERT INTO {new_name} ({names}) SELECT {names} FROM sagas')
                db.execute('DROP TABLE sagas')
                db.execute(f'ALTER TABLE {new_name} RENAME TO sagas')
                for index in Saga.__table__.indexes:
                    db.execute(str(CreateIndex(index).compile(engine)))
                own_indexes = {index.name for index in Saga.__table__.indexes}
                for name, sql in extras:
                    if name not in own_indexes:
                        db.execute(sql)
                if db.execute('SELECT count(*) FROM sagas').fetchone()[0] != count:
                    raise RuntimeError('La migración no conservó todas las sagas.')
                if db.execute('PRAGMA foreign_key_check').fetchall():
                    raise RuntimeError('La migración encontró referencias inválidas; se revierte.')
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('La migración no superó integrity_check; se revierte.')
                db.execute('PRAGMA user_version=2')
                db.execute('COMMIT')
                return backup
            except Exception:
                db.execute('ROLLBACK')
                raise
