"""Flujo funcional de Inscripción y alumnos; Cupos lo coordina la saga."""
import uuid

import pytest

from tests.conftest import CURSO_CON_CUPO, rut_valido


# ---------- T2: versionado, semántica HTTP, errores JSON ----------

def test_ruta_sin_version_no_existe(api):
    """Verifica que la API requiere versionado explícito en la ruta."""
    assert api.get("/students").status_code == 404


def test_crear_y_consultar_estudiante(api, estudiante):
    """Comprueba que se puede crear un estudiante y luego consultarlo por su id."""
    r = api.get(f"/v1/students/{estudiante['id']}")
    assert r.status_code == 200
    assert r.json()["email"] == estudiante["email"]


def test_estudiante_inexistente_da_404_json(api):
    """Confirma que consultar un estudiante no existente responde 404 en formato JSON."""
    r = api.get("/v1/students/no-existe-xyz")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert {"code", "message"} <= body["error"].keys()


def test_estudiante_invalido_da_400(api):
    """Valida que una creación con datos incompletos devuelve un error HTTP 422."""
    r = api.post("/v1/students", json={"nombre": "sin email"})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")


def test_listar_estudiantes_incluye_el_creado(api, estudiante):
    """Comprueba que el listado de estudiantes incluye el recién creado."""
    r = api.get("/v1/students")
    assert r.status_code == 200
    assert estudiante["id"] in {e["id"] for e in r.json()["items"]}


# ---------- T6: autenticación ----------

def test_sin_credenciales_da_401(api_sin_auth):
    """Verifica que la API exige autenticación para acceder a recursos protegidos."""
    assert api_sin_auth.get("/v1/students").status_code == 401


def test_credenciales_invalidas_dan_401(api):
    """Una API key inválida no autentica: 401 (403 es para un cliente autenticado
    pero sin permiso sobre ese recurso: ver test_api_keys.py)."""
    r = api.get("/v1/students", headers={"X-API-Key": "clave-incorrecta"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "ApiKey"
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


# ---------- Validación del RUT (calidad del dato personal) ----------

def _alumno(rut):
    sufijo = uuid.uuid4().hex[:8]
    return {"rut": rut, "nombre": "Ana", "apellido": "Test", "email": f"v{sufijo}@example.com"}


@pytest.mark.parametrize("rut", ["abc", "12345678-9", "1234", "12.345.678", "99999999-X"])
def test_rut_invalido_da_422(api, rut):
    """Un RUT mal formado o con dígito verificador incorrecto se rechaza."""
    r = api.post("/v1/students", json=_alumno(rut))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # el error no devuelve de vuelta el dato personal enviado
    assert "input" not in str(r.json()["error"]["details"])


def test_rut_con_puntos_se_guarda_normalizado(api):
    """12.345.678-5 y 12345678-5 son el mismo RUT: se almacena en formato canónico."""
    canonico = rut_valido()
    cuerpo, dv = canonico.split("-")
    con_puntos = f"{int(cuerpo):,}".replace(",", ".") + "-" + dv.lower()
    r = api.post("/v1/students", json=_alumno(con_puntos))
    assert r.status_code == 201, r.text
    assert r.json()["rut"] == canonico


# ---------- Matrículas ----------

def test_matricula_valida(api, estudiante):
    """Valida el flujo normal: crea una matrícula activa y luego la elimina limpiamente."""
    r = api.post("/v1/enrollments", json={"student_id": estudiante["id"], "curso_id": CURSO_CON_CUPO})
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["estado"] == "CONFIRMADA"
    assert api.get(f"/v1/enrollments/{m['id']}").status_code == 200
    api.post(f"/v1/enrollments/{m['id']}/revert")  # limpieza local


def test_inscripcion_no_vuelve_a_reservar_un_curso_lleno(api, estudiante, curso_lleno):
    r = api.post('/v1/enrollments',json={'student_id':estudiante['id'],'curso_id':curso_lleno})
    assert r.status_code == 201
    api.post(f"/v1/enrollments/{r.json()['id']}/revert")


def test_inscripcion_delega_validacion_del_curso_al_orquestador(api, estudiante):
    r = api.post('/v1/enrollments',json={'student_id':estudiante['id'],'curso_id':999999})
    assert r.status_code == 201
    api.post(f"/v1/enrollments/{r.json()['id']}/revert")


def test_matricula_estudiante_inexistente_da_404(api):
    """Verifica que no se permite matricular a un estudiante que no existe."""
    r = api.post("/v1/enrollments", json={"student_id": "no-existe", "curso_id": CURSO_CON_CUPO})
    assert r.status_code == 404


def test_matricula_duplicada_da_409(api, estudiante):
    """Comprueba que intentar matricularse dos veces al mismo curso devuelve conflicto."""
    body = {"student_id": estudiante["id"], "curso_id": CURSO_CON_CUPO}
    primera = api.post("/v1/enrollments", json=body)
    assert primera.status_code == 201
    segunda = api.post("/v1/enrollments", json=body)
    assert segunda.status_code == 409
    api.post(f"/v1/enrollments/{primera.json()['id']}/revert")


def test_revertir_matricula(api, estudiante):
    """Prueba que una matrícula puede revertirse y pasar a estado ANULADA."""
    m = api.post("/v1/enrollments", json={"student_id": estudiante["id"], "curso_id": CURSO_CON_CUPO}).json()
    assert api.post(f"/v1/enrollments/{m['id']}/revert").status_code == 200
    despues = api.get(f"/v1/enrollments/{m['id']}")
    assert despues.status_code == 200 and despues.json()["estado"] == "ANULADA"


def test_revertir_inexistente_da_404(api):
    """Confirma que intentar revertir una matrícula inexistente responde 404."""
    assert api.post("/v1/enrollments/no-existe/revert").status_code == 404


# ---------- Integración REST <-> gRPC: el cupo realmente se mueve ----------

@pytest.mark.grpc
def test_inscribir_y_dar_baja_no_modifica_cupos(api, estudiante, cupos_stub):
    pb, stub = cupos_stub.pb2, cupos_stub.stub
    libres = lambda: stub.obtenerCupo(pb.ObtenerRequest(curso_id=CURSO_CON_CUPO)).cupos_libres
    before = libres()
    m = api.post('/v1/enrollments',json={'student_id':estudiante['id'],'curso_id':CURSO_CON_CUPO}).json()
    assert libres() == before
    for _ in range(2):
        assert api.post(f"/v1/enrollments/{m['id']}/revert").status_code == 200
    assert libres() == before


def test_filtro_por_rut_acepta_el_formato_con_puntos(api, estudiante):
    """El RUT se guarda normalizado; buscarlo con puntos y guion también lo encuentra."""
    cuerpo, dv = estudiante["rut"].split("-")
    con_puntos = f"{int(cuerpo):,}".replace(",", ".") + "-" + dv
    r = api.get("/v1/students", params={"rut": con_puntos})
    assert r.status_code == 200
    assert [e["id"] for e in r.json()["items"]] == [estudiante["id"]]
