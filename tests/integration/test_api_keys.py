"""T6 (ampliado): API keys por estudiante con permisos acotados y clave admin.

- La clave admin administra los recursos; la baja pública permanece deshabilitada.
- La clave de un estudiante se emite a demanda (solo admin), es una por estudiante y solo
  permite operar sobre los datos de ese estudiante.
"""
import uuid

import pytest

from tests.conftest import CURSO_CON_CUPO, Api, BASE_URL, rut_valido
from tests.support.inscripciones import compensar_inscripcion, inscribir


def _crear_estudiante(api) -> dict:
    sufijo = uuid.uuid4().hex[:8]
    r = api.post("/v1/students", json={
        "rut": rut_valido(), "nombre": "Key", "apellido": "Test",
        "email": f"k{sufijo}@example.com",
    })
    assert r.status_code == 201, r.text
    return r.json()


def _con_clave(api) -> tuple[dict, Api]:
    """Estudiante nuevo + cliente HTTP autenticado con SU clave."""
    est = _crear_estudiante(api)
    r = api.post(f"/v1/students/{est['id']}/api-key")
    assert r.status_code == 201, r.text
    return est, Api(BASE_URL, r.json()["api_key"])


@pytest.fixture
def alumno_a(api):
    return _con_clave(api)


@pytest.fixture
def alumno_b(api):
    return _con_clave(api)


# ---------- Emisión ----------

def test_emitir_clave_devuelve_el_secreto_una_sola_vez(api):
    est = _crear_estudiante(api)
    r = api.post(f"/v1/students/{est['id']}/api-key")
    assert r.status_code == 201
    cuerpo = r.json()
    assert cuerpo["api_key"].startswith("mat_") and cuerpo["api_key"].startswith(cuerpo["key_prefix"])
    # la consulta posterior solo trae metadatos, nunca el secreto
    meta = api.get(f"/v1/students/{est['id']}/api-key")
    assert meta.status_code == 200
    assert "api_key" not in meta.json() and meta.json()["key_prefix"] == cuerpo["key_prefix"]


def test_solo_una_clave_activa_por_estudiante(api):
    est = _crear_estudiante(api)
    assert api.post(f"/v1/students/{est['id']}/api-key").status_code == 201
    r = api.post(f"/v1/students/{est['id']}/api-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "API_KEY_ALREADY_EXISTS"


def test_emitir_clave_a_estudiante_inexistente_da_404(api):
    assert api.post("/v1/students/no-existe/api-key").status_code == 404


def test_un_estudiante_no_puede_emitir_claves(alumno_a, alumno_b):
    est_a, cli_a = alumno_a
    est_b, _ = alumno_b
    assert cli_a.post(f"/v1/students/{est_a['id']}/api-key").status_code == 403
    assert cli_a.post(f"/v1/students/{est_b['id']}/api-key").status_code == 403


def test_revocar_y_reemitir(api):
    est, cli = _con_clave(api)
    assert cli.get(f"/v1/students/{est['id']}").status_code == 200
    assert api.delete(f"/v1/students/{est['id']}/api-key").status_code == 204
    # la clave revocada deja de autenticar
    assert cli.get(f"/v1/students/{est['id']}").status_code == 401
    assert api.get(f"/v1/students/{est['id']}/api-key").status_code == 404
    # y se puede emitir una nueva, que sí funciona
    nueva = api.post(f"/v1/students/{est['id']}/api-key")
    assert nueva.status_code == 201
    assert Api(BASE_URL, nueva.json()["api_key"]).get(f"/v1/students/{est['id']}").status_code == 200


def test_un_estudiante_no_puede_revocar_claves(alumno_a):
    est, cli = alumno_a
    assert cli.delete(f"/v1/students/{est['id']}/api-key").status_code == 403


# ---------- Alcance de la clave de estudiante: estudiantes ----------

def test_estudiante_ve_su_propia_ficha_y_su_clave(alumno_a):
    est, cli = alumno_a
    assert cli.get(f"/v1/students/{est['id']}").status_code == 200
    assert cli.get(f"/v1/students/{est['id']}/api-key").status_code == 200


def test_estudiante_no_ve_a_otro(alumno_a, alumno_b):
    _, cli_a = alumno_a
    est_b, _ = alumno_b
    assert cli_a.get(f"/v1/students/{est_b['id']}").status_code == 403
    assert cli_a.get(f"/v1/students/{est_b['id']}/api-key").status_code == 403


def test_estudiante_no_lista_ni_crea_estudiantes(alumno_a):
    _, cli = alumno_a
    assert cli.get("/v1/students").status_code == 403
    r = cli.post("/v1/students", json={
        "rut": rut_valido(), "nombre": "X", "apellido": "Y", "email": f"{uuid.uuid4().hex[:8]}@example.com",
    })
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


# ---------- Alcance de la clave de estudiante: matrículas ----------

def test_estudiante_consulta_su_matricula_pero_no_puede_anularla(alumno_a):
    est, cli = alumno_a
    r = inscribir(cli, json={"student_id": est["id"], "curso_id": CURSO_CON_CUPO})
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert cli.get(f"/v1/enrollments/{eid}").status_code == 200
    assert cli.post(f"/v1/enrollments/{eid}/revert").status_code == 403
    assert cli.get(f"/v1/enrollments/{eid}").json()["estado"] == "CONFIRMADA"
    compensar_inscripcion(r)


def test_estudiante_no_matricula_a_otro(alumno_a, alumno_b):
    _, cli_a = alumno_a
    est_b, _ = alumno_b
    r = inscribir(cli_a, json={"student_id": est_b["id"], "curso_id": CURSO_CON_CUPO})
    assert r.status_code == 403
    # y no quedó nada creado a nombre de B
    assert not [e for e in _listar(est_b)]


def _listar(est):
    from tests.conftest import API_KEY
    return Api(BASE_URL, API_KEY).get("/v1/enrollments", params={"student_id": est["id"]}).json()["items"]


def test_matricula_ajena_se_trata_como_inexistente(alumno_a, alumno_b):
    est_a, cli_a = alumno_a
    _, cli_b = alumno_b
    alta = inscribir(cli_a, json={"student_id": est_a["id"], "curso_id": CURSO_CON_CUPO})
    assert alta.status_code == 201
    eid = alta.json()["id"]
    assert cli_b.get(f"/v1/enrollments/{eid}").status_code == 404
    assert cli_b.post(f"/v1/enrollments/{eid}/revert").status_code == 403
    # la matrícula sigue confirmada (B no pudo revertirla)
    assert cli_a.get(f"/v1/enrollments/{eid}").json()["estado"] == "CONFIRMADA"
    compensar_inscripcion(alta)


def test_listado_de_matriculas_se_acota_al_propio_estudiante(api, alumno_a, alumno_b):
    est_a, cli_a = alumno_a
    est_b, cli_b = alumno_b
    altas = []
    for est, cli in ((est_a, cli_a), (est_b, cli_b)):
        r = inscribir(cli, json={"student_id": est["id"], "curso_id": CURSO_CON_CUPO})
        assert r.status_code == 201
        altas.append(r)
    try:
        propios = cli_a.get("/v1/enrollments").json()
        assert {e["student_id"] for e in propios["items"]} == {est_a["id"]}
        # filtrar por otro estudiante está prohibido
        assert cli_a.get("/v1/enrollments", params={"student_id": est_b["id"]}).status_code == 403
        # el admin sigue viendo todo
        assert api.get("/v1/enrollments", params={"student_id": est_b["id"]}).json()["total"] >= 1
    finally:
        for alta in altas:
            compensar_inscripcion(alta)


# ---------- La clave admin sigue funcionando ----------

def test_admin_puede_operar_sobre_cualquier_estudiante(api, alumno_a):
    est, _ = alumno_a
    assert api.get(f"/v1/students/{est['id']}").status_code == 200
    assert api.get("/v1/students").status_code == 200


def test_clave_con_formato_de_estudiante_pero_falsa_da_401(api_sin_auth):
    r = api_sin_auth.get("/v1/students/x", headers={"X-API-Key": "mat_" + "a" * 43})
    assert r.status_code == 401
