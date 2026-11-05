"""Pruebas unitarias del servicio Cupos, SIN levantar servidor ni Docker.

Cubren lo que una prueba de caja negra por red no puede fijar de forma determinista: que Cupos
no confirme una operación de la que el cliente ya se fue (deadline vencido / cancelada) y la
purga de operaciones idempotentes.
"""
import importlib
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from tests.conftest import ROOT


class ContextoFalso:
    """Imita el `context` de gRPC: `abort` lanza una excepción como el real."""

    class Abortado(Exception):
        def __init__(self, code, detalle):
            super().__init__(detalle)
            self.code, self.detalle = code, detalle

    def __init__(self, activo=True, restante=None):
        self.activo, self.restante = activo, restante

    def is_active(self):
        return self.activo

    def time_remaining(self):
        return self.restante

    def abort(self, code, detalle):
        raise self.Abortado(code, detalle)


@pytest.fixture(scope="module")
def srv():
    from grpc_tools import protoc as grpc_tools
    out = Path(tempfile.mkdtemp(prefix="cupos_stubs_"))
    proto_dir = ROOT / "proto"
    rc = grpc_tools.main(["protoc", f"-I{proto_dir}", f"--python_out={out}", f"--grpc_python_out={out}",
                          str(proto_dir / "cupos.proto")])
    assert rc == 0
    os.environ["DB_PATH"] = str(out / "cupos.db")
    sys.path[:0] = [str(out), str(ROOT / "cupos" / "src")]
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    mod.init_db()
    return mod


@pytest.fixture
def servicio(srv):
    return srv.CuposService()


def _libres(srv, curso=1):
    with srv.conexion() as con:
        return con.execute("SELECT cupos_libres FROM cursos WHERE curso_id=?", (curso,)).fetchone()[0]


def test_con_deadline_vencido_no_se_ocupa_nada(srv, servicio):
    antes = _libres(srv)
    clave = uuid.uuid4().hex
    with pytest.raises(ContextoFalso.Abortado) as e:
        servicio.ocuparCupo(srv.cupos_pb2.OcuparRequest(curso_id=1, idempotency_key=clave),
                            ContextoFalso(restante=0))
    assert "DEADLINE_EXCEEDED" in str(e.value.code)
    assert _libres(srv) == antes
    r = servicio.consultarOperacion(srv.cupos_pb2.ConsultarOperacionRequest(idempotency_key=clave),
                                    ContextoFalso())
    assert r.estado_ocupar == srv.cupos_pb2.ESTADO_SIN_DEFINIR


def test_con_cliente_cancelado_no_se_ocupa_nada(srv, servicio):
    antes = _libres(srv)
    with pytest.raises(ContextoFalso.Abortado):
        servicio.ocuparCupo(srv.cupos_pb2.OcuparRequest(curso_id=1, idempotency_key=uuid.uuid4().hex),
                            ContextoFalso(activo=False))
    assert _libres(srv) == antes


def test_si_el_deadline_vence_antes_del_commit_se_hace_rollback(srv, servicio):
    """El deadline es válido al entrar y deja de serlo justo antes del COMMIT: se deshace todo."""
    class Se_va_despues(ContextoFalso):
        llamadas = 0

        def time_remaining(self):
            self.llamadas += 1
            return 5.0 if self.llamadas == 1 else 0   # 1.ª comprobación ok, 2.ª vencida

    antes = _libres(srv)
    clave = uuid.uuid4().hex
    with pytest.raises(ContextoFalso.Abortado):
        servicio.ocuparCupo(srv.cupos_pb2.OcuparRequest(curso_id=1, idempotency_key=clave), Se_va_despues())
    assert _libres(srv) == antes, "se confirmó una ocupación cuyo cliente ya se había ido"
    r = servicio.consultarOperacion(srv.cupos_pb2.ConsultarOperacionRequest(idempotency_key=clave),
                                    ContextoFalso())
    assert r.estado_ocupar == srv.cupos_pb2.ESTADO_SIN_DEFINIR


def test_purga_borra_lo_cerrado_y_conserva_la_ocupacion_vigente(srv, servicio):
    pb2 = srv.cupos_pb2
    vigente, cerrada = uuid.uuid4().hex, uuid.uuid4().hex
    servicio.ocuparCupo(pb2.OcuparRequest(curso_id=2, idempotency_key=vigente), ContextoFalso())
    servicio.ocuparCupo(pb2.OcuparRequest(curso_id=2, idempotency_key=cerrada), ContextoFalso())
    servicio.liberarCupo(pb2.LiberarRequest(curso_id=2, idempotency_key=cerrada), ContextoFalso())
    with srv.transaccion_escritura() as con:       # envejece todo 60 días
        con.execute("UPDATE operaciones_idempotentes SET creado_en = datetime('now', '-60 days')")
    assert srv.purgar_operaciones(30) >= 2
    estado = lambda k: servicio.consultarOperacion(pb2.ConsultarOperacionRequest(idempotency_key=k), ContextoFalso())
    assert estado(vigente).estado_ocupar == pb2.CUPO_OCUPADO, "se purgó una ocupación vigente"
    assert estado(cerrada).estado_ocupar == pb2.ESTADO_SIN_DEFINIR
    # y la ocupación vigente sigue pudiendo liberarse (el invariante no depende de la antigüedad)
    assert servicio.liberarCupo(pb2.LiberarRequest(curso_id=2, idempotency_key=vigente),
                                ContextoFalso()).estado == pb2.CUPO_LIBERADO


def test_compensar_sin_reserva_no_libera_de_mas_y_bloquea_reserva_tardia(srv, servicio):
    pb = srv.cupos_pb2
    owner, unknown = uuid.uuid4().hex, uuid.uuid4().hex
    servicio.ocuparCupo(pb.OcuparRequest(curso_id=1,idempotency_key=owner),ContextoFalso())
    before = _libres(srv)
    for _ in range(2):
        assert servicio.liberarCupo(pb.LiberarRequest(curso_id=1,idempotency_key=unknown),ContextoFalso()).estado == pb.CUPO_LIBERADO
        assert _libres(srv) == before
    with pytest.raises(ContextoFalso.Abortado) as e:
        servicio.ocuparCupo(pb.OcuparRequest(curso_id=1,idempotency_key=unknown),ContextoFalso())
    assert 'FAILED_PRECONDITION' in str(e.value.code)
    assert _libres(srv) == before
    servicio.liberarCupo(pb.LiberarRequest(curso_id=1,idempotency_key=owner),ContextoFalso())
