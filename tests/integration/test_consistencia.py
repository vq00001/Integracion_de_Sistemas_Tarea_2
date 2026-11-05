"""Concurrencia e idempotencia del servicio local de Inscripción."""
import uuid
from concurrent.futures import ThreadPoolExecutor


def parallel(fn):
    with ThreadPoolExecutor(6) as pool:
        return list(pool.map(lambda _:fn(),range(6)))


def test_solo_una_inscripcion_activa_por_alumno_y_curso(api,estudiante):
    body={'student_id':estudiante['id'],'curso_id':1}
    responses=parallel(lambda:api.post('/v1/enrollments',json=body))
    assert sorted(r.status_code for r in responses)==[201,409,409,409,409,409]
    first=next(r.json() for r in responses if r.status_code==201)
    api.post(f"/v1/enrollments/{first['id']}/revert")


def test_bajas_simultaneas_son_noop_200(api,estudiante):
    first=api.post('/v1/enrollments',json={'student_id':estudiante['id'],'curso_id':1}).json()
    responses=parallel(lambda:api.post(f"/v1/enrollments/{first['id']}/revert"))
    assert [r.status_code for r in responses]==[200]*6
    assert all(r.json()['estado']=='ANULADA' for r in responses)


def test_misma_clave_simultanea_devuelve_la_misma_inscripcion(api,estudiante):
    h={'Idempotency-Key':str(uuid.uuid4())}
    body={'student_id':estudiante['id'],'curso_id':1}
    responses=parallel(lambda:api.post('/v1/enrollments',json=body,headers=h))
    assert all(r.status_code==201 for r in responses)
    assert len({r.json()['id'] for r in responses})==1
    api.post(f"/v1/enrollments/{responses[0].json()['id']}/revert")


def test_inscribir_baja_reinscribir_repite_ciclo(api,estudiante):
    ids=[]
    for _ in range(3):
        r=api.post('/v1/enrollments',json={'student_id':estudiante['id'],'curso_id':1},
                   headers={'Idempotency-Key':str(uuid.uuid4())})
        assert r.status_code==201
        ids.append(r.json()['id'])
        assert api.post(f"/v1/enrollments/{ids[-1]}/revert").status_code==200
    assert len(set(ids))==3
