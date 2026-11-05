"""Lectura de cursos y disponibilidad (hallazgo 10: la API no exponía ninguna consulta)."""
import uuid

from tests.conftest import CURSO_CON_CUPO


def test_listar_cursos_requiere_autenticacion(api_sin_auth):
    assert api_sin_auth.get("/v1/courses").status_code == 401


def test_listar_cursos_trae_la_semilla(api):
    r = api.get("/v1/courses")
    assert r.status_code == 200
    ids = {c["curso_id"] for c in r.json()["items"]}
    assert {1, 2, 3, 4, 5} <= ids
    c = next(c for c in r.json()["items"] if c["curso_id"] == CURSO_CON_CUPO)
    assert set(c) == {"curso_id", "nombre", "cupos_totales", "cupos_libres"}


def test_filtro_disponibles_excluye_cursos_llenos(api, cupos_stub, curso_lleno):
    # `curso_lleno` ocupó los cupos DIRECTO en Cupos (sin pasar por la API), así que el caché de
    # la API no se invalidó: es la consistencia eventual acotada por TTL que declara D1. Se espera
    # a que venza el TTL que la propia API anuncia en Cache-Control.
    import re, time
    ttl = int(re.search(r"max-age=(\d+)", api.get("/v1/courses").headers["Cache-Control"]).group(1))
    time.sleep(ttl + 0.5)
    r = api.get("/v1/courses", params={"disponibles": "true"})
    assert r.status_code == 200
    ids = {c["curso_id"] for c in r.json()["items"]}
    assert curso_lleno not in ids and CURSO_CON_CUPO in ids


def test_obtener_un_curso_y_curso_inexistente(api):
    curso = api.get(f"/v1/courses/{CURSO_CON_CUPO}")
    assert curso.status_code == 200, curso.text
    assert curso.json()["curso_id"] == CURSO_CON_CUPO
    r = api.get("/v1/courses/999999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "CURSO_NO_ENCONTRADO"


def test_un_estudiante_con_clave_propia_puede_descubrir_cursos(api, estudiante):
    clave = api.post(f"/v1/students/{estudiante['id']}/api-key").json()["api_key"]
    from tests.conftest import Api, BASE_URL
    assert Api(BASE_URL, clave).get("/v1/courses").status_code == 200


def test_inscripcion_local_no_cambia_disponibilidad(api, estudiante):
    inicial = api.get("/v1/courses")
    assert inicial.status_code == 200, inicial.text
    assert api.get("/v1/courses").headers["X-Cache"] == "HIT"
    antes = api.get(f"/v1/courses/{CURSO_CON_CUPO}").json()["cupos_libres"]
    r = api.post("/v1/enrollments", json={"student_id": estudiante["id"], "curso_id": CURSO_CON_CUPO},
                 headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 201
    try:
        # tras matricular, el caché de esta réplica se invalidó: el dato ya refleja el cupo ocupado
        resp = api.get(f"/v1/courses/{CURSO_CON_CUPO}")
        assert resp.headers["X-Cache"] in ("HIT", "MISS")
        assert resp.json()["cupos_libres"] == antes
    finally:
        api.post(f"/v1/enrollments/{r.json()['id']}/revert")
