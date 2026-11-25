"""Autenticación OAuth2 local y autorización de matrículas por propietario."""
import uuid

import pytest

from tests.conftest import BASE_URL, CURSO_CON_CUPO, Api, rut_valido
from tests.support.inscripciones import compensar_inscripcion, inscribir


@pytest.fixture
def estudiante_b(api):
    suffix = uuid.uuid4().hex[:8]
    r = api.post("/v1/students", json={
        "rut": rut_valido(), "nombre": "Segundo", "apellido": "Usuario",
        "email": f"segundo-{suffix}@example.com",
    })
    assert r.status_code == 201, r.text
    return r.json()


def _token_para(api, estudiante: dict, password: str = "ClaveSeguraLocal-123") -> str:
    r = api.put(f"/v1/students/{estudiante['id']}/password", json={"password": password})
    assert r.status_code == 204, r.text
    r = api.post("/v1/auth/token", data={"username": estudiante["email"], "password": password})
    assert r.status_code == 200, r.text
    assert r.json()["token_type"] == "bearer"
    return r.json()["access_token"]


def test_jwt_local_consulta_matriculas_propias_sin_baja_publica(api, estudiante):
    jwt = _token_para(api, estudiante)
    usuario = Api(BASE_URL, None)
    usuario.headers = {"Authorization": f"Bearer {jwt}"}
    assert usuario.get(f"/v1/students/{estudiante['id']}").status_code == 200

    alta = inscribir(usuario, json={
        "student_id": estudiante["id"], "curso_id": CURSO_CON_CUPO,
    })
    assert alta.status_code == 201, alta.text
    matricula_id = alta.json()["id"]
    assert usuario.get(f"/v1/enrollments/{matricula_id}").status_code == 200
    assert usuario.get("/v1/enrollments").json()["items"][0]["student_id"] == estudiante["id"]
    assert usuario.post(f"/v1/enrollments/{matricula_id}/revert").status_code == 403
    assert usuario.get(f"/v1/enrollments/{matricula_id}").json()["estado"] == "CONFIRMADA"
    compensar_inscripcion(alta)


def test_jwt_no_puede_leer_ni_modificar_recursos_ajenos(api, estudiante, estudiante_b):
    token_a = _token_para(api, estudiante)
    token_b = _token_para(api, estudiante_b)
    cli_a, cli_b = Api(BASE_URL, None), Api(BASE_URL, None)
    cli_a.headers = {"Authorization": f"Bearer {token_a}"}
    cli_b.headers = {"Authorization": f"Bearer {token_b}"}

    assert cli_a.get(f"/v1/students/{estudiante_b['id']}").status_code == 403
    assert inscribir(cli_a, json={
        "student_id": estudiante_b["id"], "curso_id": CURSO_CON_CUPO,
    }).status_code == 403

    propia = inscribir(cli_b, json={
        "student_id": estudiante_b["id"], "curso_id": CURSO_CON_CUPO,
    })
    assert propia.status_code == 201, propia.text
    try:
        assert cli_a.get(f"/v1/enrollments/{propia.json()['id']}").status_code == 404
        assert cli_a.post(f"/v1/enrollments/{propia.json()['id']}/revert").status_code == 403
        listado = cli_a.get("/v1/enrollments").json()
        assert all(item["student_id"] == estudiante["id"] for item in listado["items"])
    finally:
        compensar_inscripcion(propia)


def test_password_incorrecta_da_401_y_api_key_admin_sigue_funcionando(api, estudiante):
    _token_para(api, estudiante)
    r = api.post("/v1/auth/token", data={"username": estudiante["email"], "password": "incorrecta"})
    assert r.status_code == 401
    assert api.get(f"/v1/students/{estudiante['id']}").status_code == 200
