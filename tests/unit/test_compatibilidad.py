"""Compatibilidad de contratos (.proto y OpenAPI) contra la línea base `contrato-base/`.

Hace automático lo que D3 solo describía: si alguien elimina un campo, una ruta o un RPC, o agrega un
campo obligatorio a una petición, esta prueba falla. Además se prueba al verificador mismo con cambios
rompientes SIMULADOS, para que no sea un "verde" vacío.
"""
import copy
import importlib.util
import json

import pytest

from tests.conftest import ROOT


_spec = importlib.util.spec_from_file_location("verificar_compatibilidad", ROOT / "scripts" / "verificar_compatibilidad.py")
vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vc)


@pytest.fixture(scope="module")
def base_proto():
    from google.protobuf import descriptor_pb2
    ds = descriptor_pb2.FileDescriptorSet()
    ds.ParseFromString((vc.BASE / "cupo.v1.desc").read_bytes())
    return ds


@pytest.fixture(scope="module")
def base_api():
    return json.loads((vc.BASE / "openapi.base.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def actual_api():
    return json.loads(vc.OPENAPI.read_text(encoding="utf-8"))


def test_el_proto_actual_es_compatible_con_la_base(base_proto):
    assert vc.comparar_proto(base_proto, vc.descriptor_set(vc.PROTO)) == []


def test_el_openapi_actual_es_compatible_con_la_base(base_api, actual_api):
    errores, _ = vc.comparar_openapi(base_api, actual_api)
    assert vc.errores_no_autorizados(errores) == []


# ---- el verificador detecta rupturas simuladas ----

def test_detecta_campo_y_rpc_eliminados_en_proto(base_proto):
    import copy as c
    roto = c.deepcopy(vc.descriptor_set(vc.PROTO))
    f = roto.file[0]
    del f.message_type[0].field[0]                 # ObtenerRequest pierde curso_id
    del f.service[0].method[0]                     # se elimina un RPC existente
    errores = vc.comparar_proto(base_proto, roto)
    assert any("campo eliminado" in e for e in errores) and any("RPC eliminado" in e for e in errores)


def test_detecta_ruta_y_propiedad_eliminadas_en_openapi(base_api, actual_api):
    roto = copy.deepcopy(actual_api)
    del roto["paths"]["/v1/enrollments"]["post"]
    primera = next(iter(roto["components"]["schemas"]["EnrollmentOut"]["properties"]))
    del roto["components"]["schemas"]["EnrollmentOut"]["properties"][primera]
    errores, _ = vc.comparar_openapi(base_api, roto)
    assert any("operación eliminada" in e for e in errores)
    assert any("propiedad eliminada de la respuesta" in e for e in errores)


def test_detecta_campo_obligatorio_nuevo_en_una_peticion(base_api, actual_api):
    roto = copy.deepcopy(actual_api)
    esquema = roto["components"]["schemas"]["EnrollmentCreate"]
    esquema["properties"]["nuevo"] = {"type": "string"}
    esquema["required"] = [*esquema.get("required", []), "nuevo"]
    errores, _ = vc.comparar_openapi(base_api, roto)
    assert any("OBLIGATORIA nueva" in e for e in errores)


def test_agregar_algo_opcional_es_compatible(base_api, actual_api):
    ok = copy.deepcopy(actual_api)
    ok["components"]["schemas"]["EnrollmentCreate"]["properties"]["comentario"] = {"type": "string"}
    errores, _ = vc.comparar_openapi(base_api, ok)
    assert vc.errores_no_autorizados(errores) == []
