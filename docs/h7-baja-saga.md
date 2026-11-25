# H7: baja pública deshabilitada

Una inscripción creada por la Saga solo conserva su coherencia si la baja se
coordina con el cobro y la reserva. La ruta pública
`POST /v1/enrollments/{enrollment_id}/revert` podía anular la inscripción y dejar
la Saga COMPLETADA, el pago COBRADO y el cupo reservado. El índice de Saga activa
impedía una nueva matrícula para ese alumno y curso.

La ruta permanece para responder explícitamente al consumidor, pero está marcada
como deprecated y devuelve 403 a cualquier credencial autenticada, incluso una
credencial de escritor. Sin autenticación devuelve 401. No consulta la existencia
de la matrícula ni modifica Inscripción, Saga, Cupos o Pagos. El Gateway conserva
su proxy: la protección está en Inscripción y también se aplica a accesos directos.
No se implementa una nueva cancelación global ni se modifica una Saga completada.

Las compensaciones siguen usando `POST /internal/enrollments/cancel`, con
`X-Internal-Key` y la clave original INSCRIBIR en `Idempotency-Key`. Esta operación
continúa siendo interna, idempotente y apta para cancelaciones anticipadas; el
Gateway no la expone. El orquestador mantiene el orden inverso de compensación.
Una petición de alta tardía con una clave ya cancelada no reactiva la inscripción.

## Contrato y actualización

Los contratos OpenAPI de Inscripción declaran el 403 y no anuncian una respuesta
exitosa para la baja pública. Se retiran también el 404 y la consulta por ID de esa
operación. El verificador de compatibilidad admite exclusivamente esas retiradas
intencionales en POST /v1/enrollments/{enrollment_id}/revert; conserva la línea
base histórica y sigue detectando rupturas en otras operaciones.

Los consumidores que usaban la baja pública deben dejar de invocarla. Las claves
administrativas o de escritor no habilitan una cancelación pública. La limpieza
de datos de prueba usa la compensación interna y conserva la clave de cada alta;
no se enseña al cliente público a usar esa operación.

Esta corrección evita nuevas bajas descoordinadas. No repara automáticamente
estados inconsistentes que hubieran sido creados antes de actualizar la API.
No se modifican datos ni volúmenes durante la actualización del código.

## Pruebas

Regresión de H7 en proceso, sin Docker ni servicios previos:

```bash
API_KEY=h7-test-admin-key-at-least-32-characters .venv/bin/python -m pytest tests/integration/test_h7_baja_saga.py -q
```

La clave del comando es ficticia y solo permite cargar la configuración de los
tests; las credenciales del sistema en proceso se preparan de forma aislada.

Regresión amplia con procesos HTTP/gRPC y bases temporales:

```bash
.venv/bin/python tests/support/validar_h7.py
```

El ejecutor genera stubs propios, usa puertos libres y prepara credenciales y
bases temporales. Finaliza únicamente los procesos que creó. Omite los dos casos
marcados `falla` que administran contenedores y el módulo de arranque limpio H5
que usa Docker; no modifica la instalación existente. Acepta una selección de
pytest como argumentos si solo se desea comprobar un subconjunto.
