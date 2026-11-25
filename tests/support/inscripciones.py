"""Preparación y compensación de inscripciones de prueba por las rutas internas.

No se usa la baja pública para limpiar datos. La clave original viaja en la
petición de alta y se recupera de esa petición para cancelar exactamente su fila.
"""
import os
import uuid

from dotenv import dotenv_values

from tests.conftest import Api, ROOT


def inscribir(api, **kwargs):
    headers = dict(kwargs.pop('headers', {}))
    headers.setdefault('Idempotency-Key', str(uuid.uuid4()))
    return api.inscribir(headers=headers, **kwargs)


def compensar_inscripcion(alta):
    assert alta.status_code in (200, 201), alta.text
    row = alta.json()
    original_key = alta.request.headers.get('Idempotency-Key')
    assert original_key, 'La preparación debe usar inscribir() y conservar su respuesta'
    internal_key = os.getenv('INTERNAL_KEY') or dotenv_values(ROOT / '.env').get('INTERNAL_KEY')
    if not internal_key:
        raise RuntimeError('Define INTERNAL_KEY para compensar las inscripciones de prueba')
    target = os.getenv('INSCRIPCION_TEST_URL', 'http://localhost:8001')
    response = Api(target, None).post('/internal/enrollments/cancel',
        headers={'X-Internal-Key': internal_key, 'Idempotency-Key': original_key},
        json={'student_id': row['student_id'], 'curso_id': row['curso_id']})
    assert response.status_code == 200, response.text
    assert response.json()['id'] == row['id'], 'La compensación debe anular la misma inscripción'
    assert response.json()['estado'] == 'ANULADA'
    return response
