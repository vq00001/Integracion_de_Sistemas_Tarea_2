import logging
import os
import sqlite3
import threading
import time
from concurrent import futures
from contextlib import contextmanager

import grpc

import cupos_pb2
import cupos_pb2_grpc

# VARIABLES DE CONFIGURACION
DB_PATH = os.getenv("DB_PATH", "/data/cupos.db")
PORT = os.getenv("GRPC_PORT", "50051")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))

# para experimento
LATENCIA_MS = int(os.getenv("SIMULAR_LATENCIA_MS", "0"))
# "antes": la latencia ocurre ANTES de procesar (Cupos lento: el deadline vence antes de que
#         se confirme nada, y como Cupos verifica el deadline, no retiene el cupo).
# "despues": la latencia ocurre DESPUES de confirmar (la operacion ya se hizo y la respuesta
#         llega tarde: es el caso de resultado realmente incierto para el cliente).
LATENCIA_MODO = os.getenv("SIMULAR_LATENCIA_MODO", "antes")
# Dias que se conservan las operaciones idempotentes YA CERRADAS (0 = no purgar nunca).
# Las ocupaciones vigentes (cupo retenido y no liberado) no se purgan nunca: son el
# registro que permite a Cupos proteger su invariante de capacidad.
RETENCION_DIAS = int(os.getenv("IDEMPOTENCIA_RETENCION_DIAS", "0"))


# datos para base de datos
CURSOS_INICIALES = [
    (1, "Programación en Python", 30),
    (2, "Bases de Datos", 25),
    (3, "Redes y Comunicaciones", 20),
    (4, "Diseño Web", 15),
    (5, "Ciberseguridad Básica", 2),
]

# documentar los accesos y operaciones en la base de datos
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cupos")


# BASE DE DATOS


@contextmanager
def conexion():
    """Una conexión por llamada: seguro con el ThreadPool de gRPC."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@contextmanager
def transaccion_escritura():
    """Transacción de escritura serializada (BEGIN IMMEDIATE).

    Adquiere el candado de escritura de SQLite ANTES de leer la tabla de
    operaciones idempotentes. Así, dos llamadas simultáneas con la misma
    clave no pueden ambas ver "clave no registrada": la segunda espera a que
    la primera haga COMMIT y entonces encuentra el resultado guardado y lo
    devuelve. Sin esto, la segunda llamada descontaba el cupo, chocaba con la
    clave primaria al registrar la operación y llegaba al cliente como un
    error UNKNOWN (que la API trataba como falla de Cupos).
    """
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _deadline_vigente(context) -> bool:
    """False si el cliente ya cancelo o su deadline vencio.

    Cupos no debe CONFIRMAR una operacion de la que el cliente ya se fue: eso dejaba cupos
    huerfanos (ocupados sin matricula). Se comprueba antes de modificar y otra vez justo antes
    del COMMIT; si no esta vigente se hace ROLLBACK. Queda una ventana minima (entre la
    comprobacion y que la respuesta llegue) que Matriculas cubre con el conciliador.
    """
    if not context.is_active():
        return False
    restante = context.time_remaining()
    return restante is None or restante > 0


def _exigir_vigente(context) -> None:
    if not _deadline_vigente(context):
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "El deadline del cliente ya vencio")


def _exigir_clave(clave: str, context) -> None:
    if not clave:
        context.abort(
            grpc.StatusCode.INVALID_ARGUMENT,
            "idempotency_key es obligatoria: sin ella la operacion no seria idempotente",
        )


def purgar_operaciones(retencion_dias: int) -> int:
    """Borra operaciones idempotentes CERRADAS con mas de `retencion_dias` dias.

    Se purgan: (a) pares ocupar+liberar cuya liberacion es antigua, y (b) rechazos
    (SIN_CUPOS / CURSO_NO_ENCONTRADO) antiguos. NO se purga una ocupacion vigente.
    Devuelve cuantas filas se borraron.
    """
    if retencion_dias <= 0:
        return 0
    corte = f"-{int(retencion_dias)} days"
    with transaccion_escritura() as con:
        borradas = con.execute(
            "DELETE FROM operaciones_idempotentes WHERE clave IN ("
            "  SELECT clave FROM operaciones_idempotentes "
            "  WHERE operacion = 'liberar' AND creado_en < datetime('now', ?))",
            (corte,),
        ).rowcount
        borradas += con.execute(
            "DELETE FROM operaciones_idempotentes "
            "WHERE operacion = 'ocupar' AND estado != ? AND creado_en < datetime('now', ?)",
            (cupos_pb2.CUPO_OCUPADO, corte),
        ).rowcount
    if borradas:
        log.info("purga de operaciones idempotentes cerradas: %d filas", borradas)
    return borradas


def _purga_periodica() -> None:
    while True:
        time.sleep(3600)
        try:
            purgar_operaciones(RETENCION_DIAS)
        except Exception:  # la purga nunca debe tumbar el servicio
            log.exception("fallo la purga de operaciones idempotentes")


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with conexion() as con:
        con.execute("PRAGMA journal_mode=WAL")

        # crear base de datos minima para el microservicio
        con.execute(
            """CREATE TABLE IF NOT EXISTS cursos (
                curso_id      INTEGER PRIMARY KEY,
                nombre        TEXT    NOT NULL,
                cupos_totales INTEGER NOT NULL CHECK (cupos_totales >= 0),
                cupos_libres  INTEGER NOT NULL,
                CHECK (cupos_libres >= 0 AND cupos_libres <= cupos_totales)
            )"""
        )

        # La operación forma parte de la identidad: una misma clave puede
        # usarse una vez para ocupar y otra vez para liberar.
        tabla_operaciones = con.execute(
            "PRAGMA table_info(operaciones_idempotentes)"
        ).fetchall()
        if tabla_operaciones:
            claves_primarias = [columna["name"] for columna in tabla_operaciones if columna["pk"]]
            if claves_primarias == ["clave"]:
                con.execute("ALTER TABLE operaciones_idempotentes RENAME TO operaciones_idempotentes_legacy")
                tabla_operaciones = []

        if not tabla_operaciones:
            con.execute(
                """CREATE TABLE IF NOT EXISTS operaciones_idempotentes (
                    clave      TEXT NOT NULL,
                    operacion  TEXT NOT NULL,
                    curso_id   INTEGER NOT NULL,
                    estado     INTEGER NOT NULL,
                    creado_en  TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (clave, operacion)
                )"""
            )
            if con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='operaciones_idempotentes_legacy'"
            ).fetchone():
                con.execute(
                    """INSERT INTO operaciones_idempotentes
                    (clave, operacion, curso_id, estado, creado_en)
                    SELECT clave, operacion, curso_id, estado, creado_en
                    FROM operaciones_idempotentes_legacy"""
                )
                con.execute("DROP TABLE operaciones_idempotentes_legacy")

        ## si no hay datos en la tabla, insertarlos
        if con.execute("SELECT COUNT(*) FROM cursos").fetchone()[0] == 0:
            con.executemany(
                "INSERT INTO cursos VALUES (?, ?, ?, ?)",
                [(i, n, t, t) for i, n, t in CURSOS_INICIALES],
            )
            log.info("Base inicializada con %d cursos", len(CURSOS_INICIALES))


def a_curso(row: sqlite3.Row) -> cupos_pb2.Curso:
    return cupos_pb2.Curso(
        curso_id=row["curso_id"],
        nombre=row["nombre"],
        cupos_totales=row["cupos_totales"],
        cupos_libres=row["cupos_libres"],
    )


# MICROSERVICIO CUPOS


class CuposService(cupos_pb2_grpc.CuposServicer):
    def _simular_latencia(self):
        if LATENCIA_MS > 0:
            time.sleep(LATENCIA_MS / 1000)

    # seleccionar los cupos libres de un curso
    def obtenerCupo(self, request, context):
        self._simular_latencia()
        with conexion() as con:
            row = con.execute(
                "SELECT cupos_libres FROM cursos WHERE curso_id = ?",
                (request.curso_id,),
            ).fetchone()
        if row is None:
            context.abort(
                grpc.StatusCode.NOT_FOUND, f"Curso {request.curso_id} no existe"
            )
        return cupos_pb2.ObtenerResponse(cupos_libres=row["cupos_libres"])

    # devolver todos los cursos en la base de datos
    def listarCursos(self, request, context):
        self._simular_latencia()
        with conexion() as con:
            rows = con.execute("SELECT * FROM cursos ORDER BY curso_id").fetchall()
        return cupos_pb2.ListarCursosResponse(cursos=[a_curso(r) for r in rows])

    # devolver todos los cursos en la base de datos
    def listarCursosDisponibles(self, request, context):
        self._simular_latencia()
        with conexion() as con:
            rows = con.execute(
                "SELECT * FROM cursos WHERE cupos_libres > 0 ORDER BY curso_id"
            ).fetchall()
        return cupos_pb2.ListarCursosResponse(cursos=[a_curso(r) for r in rows])

    def _latencia_despues(self):
        if LATENCIA_MODO == "despues":
            self._simular_latencia()

    def ocuparCupo(self, request, context):
        if LATENCIA_MODO != "despues":
            self._simular_latencia()
        clave = request.idempotency_key
        _exigir_clave(clave, context)
        _exigir_vigente(context)

        with transaccion_escritura() as con:
            # revisar que no sea un request repetido (la clave identifica UNA operacion)
            previa = con.execute(
                "SELECT estado, curso_id FROM operaciones_idempotentes "
                "WHERE clave = ? AND operacion = 'ocupar'",
                (clave,),
            ).fetchone()
            liberada = con.execute(
                "SELECT curso_id FROM operaciones_idempotentes WHERE clave=? AND operacion='liberar'",
                (clave,),
            ).fetchone()
            if liberada is not None:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION,
                              "Reserva cancelada; no se puede ocupar con esa clave")
            if previa is not None:
                if previa["curso_id"] != request.curso_id:
                    context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT,
                        "La idempotency_key ya se uso para ocupar otro curso",
                    )
                log.info("ocuparCupo repetido, clave=%s -> resultado cacheado", clave)
                estado = previa["estado"]
            else:
                # resta uno a la cantidad de cupos libres, si es que quedan
                cur = con.execute(
                    "UPDATE cursos SET cupos_libres = cupos_libres - 1 "
                    "WHERE curso_id = ? AND cupos_libres > 0",
                    (request.curso_id,),
                )
                if cur.rowcount == 1:
                    estado = cupos_pb2.CUPO_OCUPADO
                else:
                    existe = con.execute(
                        "SELECT 1 FROM cursos WHERE curso_id = ?", (request.curso_id,)
                    ).fetchone()
                    estado = cupos_pb2.SIN_CUPOS if existe else cupos_pb2.CURSO_NO_ENCONTRADO

                con.execute(
                    "INSERT INTO operaciones_idempotentes (clave, operacion, curso_id, estado) "
                    "VALUES (?, 'ocupar', ?, ?)",
                    (clave, request.curso_id, estado),
                )
                # Ultima comprobacion ANTES del COMMIT: si el cliente ya se fue, se deshace todo.
                _exigir_vigente(context)

        self._latencia_despues()
        log.info("ocupar cupo curso=%s -> %s", request.curso_id, cupos_pb2.Estado.Name(estado))
        return cupos_pb2.OcuparResponse(estado=estado)

    def liberarCupo(self, request, context):
        if LATENCIA_MODO != "despues":
            self._simular_latencia()
        clave = request.idempotency_key
        _exigir_clave(clave, context)
        _exigir_vigente(context)

        with transaccion_escritura() as con:
            previa = con.execute(
                "SELECT estado, curso_id FROM operaciones_idempotentes WHERE clave=? AND operacion='liberar'",
                (clave,),
            ).fetchone()
            ocupacion = con.execute(
                "SELECT estado, curso_id FROM operaciones_idempotentes WHERE clave=? AND operacion='ocupar'",
                (clave,),
            ).fetchone()
            if any(row is not None and row["curso_id"] != request.curso_id for row in (previa, ocupacion)):
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Clave usada en otro curso")
            if previa is not None:
                estado = previa["estado"]
            else:
                # Solo devuelve capacidad si esta clave ocupó. Si no existe, guarda
                # una cancelación anticipada, incluso si el curso tampoco existe.
                if ocupacion is not None and ocupacion["estado"] == cupos_pb2.CUPO_OCUPADO:
                    cur = con.execute(
                        "UPDATE cursos SET cupos_libres=cupos_libres+1 "
                        "WHERE curso_id=? AND cupos_libres<cupos_totales", (request.curso_id,))
                    if cur.rowcount != 1:
                        context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Capacidad inconsistente")
                estado = cupos_pb2.CUPO_LIBERADO
                con.execute(
                    "INSERT INTO operaciones_idempotentes (clave, operacion, curso_id, estado) "
                    "VALUES (?, 'liberar', ?, ?)", (clave, request.curso_id, estado),
                )
                _exigir_vigente(context)

        self._latencia_despues()
        log.info("liberar cupo curso=%s -> %s", request.curso_id, cupos_pb2.Estado.Name(estado))
        return cupos_pb2.LiberarResponse(estado=estado)

    def consultarOperacion(self, request, context):
        """Solo lectura: que paso con una clave (no modifica ningun cupo)."""
        self._simular_latencia()
        _exigir_clave(request.idempotency_key, context)
        with conexion() as con:
            filas = con.execute(
                "SELECT operacion, estado, curso_id FROM operaciones_idempotentes WHERE clave = ?",
                (request.idempotency_key,),
            ).fetchall()
        por_op = {f["operacion"]: f for f in filas}
        ocupar, liberar = por_op.get("ocupar"), por_op.get("liberar")
        return cupos_pb2.ConsultarOperacionResponse(
            estado_ocupar=ocupar["estado"] if ocupar else cupos_pb2.ESTADO_SIN_DEFINIR,
            estado_liberar=liberar["estado"] if liberar else cupos_pb2.ESTADO_SIN_DEFINIR,
            curso_id=(ocupar or liberar)["curso_id"] if (ocupar or liberar) else 0,
        )


def serve() -> None:
    init_db()
    try:
        purgar_operaciones(RETENCION_DIAS)
    except Exception:
        log.exception("fallo la purga inicial")
    if RETENCION_DIAS > 0:
        threading.Thread(target=_purga_periodica, name="purga", daemon=True).start()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=MAX_WORKERS))
    cupos_pb2_grpc.add_CuposServicer_to_server(CuposService(), server)
    server.add_insecure_port(f"[::]:{PORT}")
    server.start()
    log.info(
        "Servicio Cupos escuchando en :%s (latencia simulada: %d ms, modo=%s)",
        PORT, LATENCIA_MS, LATENCIA_MODO,
    )
    server.wait_for_termination()


if __name__ == "__main__":
    serve()