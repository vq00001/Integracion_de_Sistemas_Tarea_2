"""Inscripción debe seguir funcionando cuando Cupos está caído."""
import pytest
from tests.conftest import compose, API_KEY, CUPOS_GRPC_ADDR
from tests.support.servicios import esperar_cupos, esperar_catalogo

pytestmark=pytest.mark.falla


@pytest.fixture
def cupos_caido(api):
    compose('stop','cupos')
    try:
        yield
    finally:
        compose('start','cupos')
        esperar_cupos(CUPOS_GRPC_ADDR)
        esperar_catalogo(api.base_url, API_KEY)


def test_inscripcion_y_alumnos_no_dependen_de_cupos(api,estudiante,cupos_caido):
    assert api.get(f"/v1/students/{estudiante['id']}").status_code==200
    assert api.get('/v1/students').status_code==200
    r=api.post('/v1/enrollments',json={'student_id':estudiante['id'],'curso_id':1})
    assert r.status_code==201
    for _ in range(2):
        assert api.post(f"/v1/enrollments/{r.json()['id']}/revert").status_code==200
