"""Hallazgos de la revisión 3: validación de entrada, privacidad, claves y idempotencia."""
import uuid

import pytest

from tests.conftest import CURSO_CON_CUPO, Api, BASE_URL, rut_valido


def _alta(api, **cambios):
    sufijo = uuid.uuid4().hex[:8]
    cuerpo = {"rut": rut_valido(), "nombre": "Ana", "apellido": "Pérez",
              "email": f"e{sufijo}@example.com", "telefono": "+56912345678", **cambios}
    return api.post("/v1/students", json=cuerpo), cuerpo


# ---------- Validación de entrada (hallazgo 11) ----------

@pytest.mark.parametrize("campo,valor", [
    ("nombre", "<script>alert(1)</script>"),
    ("nombre", "      "),
    ("apellido", 'Pérez"; DROP TABLE students;--'),
    ("telefono", "abc;DROP"),
    ("telefono", "12"),
])
def test_entradas_peligrosas_o_vacias_dan_422(api, campo, valor):
    r, _ = _alta(api, **{campo: valor})
    assert r.status_code == 422, r.text
    # el 422 no devuelve el dato enviado (privacidad)
    assert valor.strip() == "" or valor not in r.text


@pytest.mark.parametrize("nombre", ["José María", "O'Brien", "Núñez-Pérez", "Ana Sofía"])
def test_nombres_reales_se_aceptan(api, nombre):
    r, _ = _alta(api, nombre=nombre)
    assert r.status_code == 201, r.text
    assert r.json()["nombre"] == nombre


def test_el_correo_se_canonicaliza_y_no_admite_duplicado_por_mayusculas(api):
    local = f"mix{uuid.uuid4().hex[:8]}"
    r1, _ = _alta(api, email=f"{local.capitalize()}@Example.COM")
    assert r1.status_code == 201
    assert r1.json()["email"] == f"{local}@example.com"
    r2, _ = _alta(api, email=f"{local}@example.com")
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "STUDENT_ALREADY_EXISTS"


# ---------- Privacidad: supresión / anonimización (hallazgo 12) ----------

def test_anonimizar_estudiante_borra_datos_personales_y_revoca_su_clave(api):
    r, cuerpo = _alta(api)
    est = r.json()
    clave = api.post(f"/v1/students/{est['id']}/api-key").json()["api_key"]
    assert api.delete(f"/v1/students/{est['id']}").status_code == 204
    anon = api.get(f"/v1/students/{est['id']}").json()
    texto = str(anon)
    for dato in (cuerpo["rut"], cuerpo["email"], cuerpo["telefono"], cuerpo["nombre"], cuerpo["apellido"]):
        assert dato not in texto, f"quedó el dato personal {dato!r}"
    assert anon["nombre"] == "Anonimizado" and anon["telefono"] is None
    assert Api(BASE_URL, clave).get(f"/v1/students/{est['id']}").status_code == 401   # clave revocada
    assert api.delete(f"/v1/students/{est['id']}").status_code == 204                 # idempotente
    assert api.post(f"/v1/students/{est['id']}/api-key").status_code == 409          # ya no se le emiten claves


def test_anonimizar_conserva_la_matricula_sin_datos_personales(api):
    est = _alta(api)[0].json()
    m = api.post("/v1/enrollments", json={"student_id": est["id"], "curso_id": CURSO_CON_CUPO})
    assert m.status_code == 201
    try:
        assert api.delete(f"/v1/students/{est['id']}").status_code == 204
        g = api.get(f"/v1/enrollments/{m.json()['id']}")
        assert g.status_code == 200 and g.json()["estado"] == "CONFIRMADA"
    finally:
        api.post(f"/v1/enrollments/{m.json()['id']}/revert")


def test_solo_el_admin_puede_anonimizar(api):
    est = _alta(api)[0].json()
    clave = api.post(f"/v1/students/{est['id']}/api-key").json()["api_key"]
    assert Api(BASE_URL, clave).delete(f"/v1/students/{est['id']}").status_code == 403
    assert api.delete("/v1/students/no-existe").status_code == 404


# ---------- Claves de estudiante (hallazgo 19) ----------

def test_el_prefijo_publico_no_revela_el_secreto_y_la_clave_caduca(api):
    est = _alta(api)[0].json()
    d = api.post(f"/v1/students/{est['id']}/api-key").json()
    prefijo, secreto = d["api_key"].split(".", 1)
    assert d["key_prefix"] == prefijo and len(secreto) >= 40
    assert secreto[:4] not in d["key_prefix"]
    assert d["expires_at"] is not None


# ---------- Idempotency-Key acotada por estudiante (hallazgo 18) ----------

def test_dos_estudiantes_pueden_usar_la_misma_idempotency_key_sin_enterarse(api):
    a, b = _alta(api)[0].json(), _alta(api)[0].json()
    clave = {"Idempotency-Key": f"compartida-{uuid.uuid4()}"}
    ra = api.post("/v1/enrollments", json={"student_id": a["id"], "curso_id": CURSO_CON_CUPO}, headers=clave)
    rb = api.post("/v1/enrollments", json={"student_id": b["id"], "curso_id": CURSO_CON_CUPO}, headers=clave)
    try:
        # antes: 422 IDEMPOTENCY_KEY_REUSED (oráculo que confirmaba que la clave existía)
        assert ra.status_code == 201 and rb.status_code == 201, (ra.text, rb.text)
        assert ra.json()["id"] != rb.json()["id"]
    finally:
        for r in (ra, rb):
            if r.status_code == 201:
                api.post(f"/v1/enrollments/{r.json()['id']}/revert")


def test_repetir_clave_anulada_devuelve_la_misma_inscripcion_sin_reactivarla(api):
    est = _alta(api)[0].json()
    h = {'Idempotency-Key':str(uuid.uuid4())}
    body = {'student_id':est['id'],'curso_id':CURSO_CON_CUPO}
    first=api.post('/v1/enrollments',json=body,headers=h)
    assert first.status_code == 201
    api.post(f"/v1/enrollments/{first.json()['id']}/revert")
    r=api.post('/v1/enrollments',json=body,headers=h)
    assert r.status_code == 201 and r.json()['estado'] == 'ANULADA'
    assert r.json()['id'] == first.json()['id']


# ---------- Listados: el total se cuenta en la base ----------

def test_el_total_de_los_listados_es_correcto_con_filtros(api):
    est = _alta(api)[0].json()
    ids = []
    try:
        for curso in (1, 2):
            r = api.post("/v1/enrollments", json={"student_id": est["id"], "curso_id": curso})
            assert r.status_code == 201
            ids.append(r.json()["id"])
        p = api.get("/v1/enrollments", params={"student_id": est["id"], "limit": 1}).json()
        assert p["total"] == 2 and len(p["items"]) == 1
        p = api.get("/v1/enrollments", params={"student_id": est["id"], "curso_id": 2}).json()
        assert p["total"] == 1
    finally:
        for i in ids:
            api.post(f"/v1/enrollments/{i}/revert")


def test_offset_gigante_se_rechaza(api):
    assert api.get("/v1/students", params={"offset": 10_000_000}).status_code == 422
