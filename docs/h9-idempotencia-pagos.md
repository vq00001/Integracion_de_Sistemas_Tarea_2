# H9: idempotencia de Pagos entre procesos

Las creaciones y los reembolsos reservan su operación en la base compartida antes
de llamar a Webpay. La clave primaria `(payment_id, operation)` de
`payment_operations` permite un solo emisor por pago y operación. Las transacciones
de reserva son cortas: no se mantiene un bloqueo de escritura de SQLite durante
el HTTP, por lo que otros pagos pueden usar los demás permisos del bulkhead.

Una petición concurrente obtiene el resultado ya guardado o `503 REINTENTAR` con
`Retry-After`. Una cancelación de un pago pendiente espera a que termine su creación;
el `UPDATE` de cancelación comprueba atómicamente que no haya una creación activa.

`payment_refund_keys` vincula cada clave de reembolso con la clave del cobro original.
La vinculación se confirma antes de modificar el pago o enviar el reembolso, incluso
para cancelaciones anticipadas y aliases de un replay. Reutilizarla para otro cobro
devuelve `422 IDEMPOTENCY_KEY_REUSED`, sin cambiar filas de pagos ni la pasarela.
La restricción de la base también protege dos peticiones de procesos distintos.

## Respuestas perdidas y reinicios

Las operaciones pasan por `RUNNING`, `UNCERTAIN` y `DONE`. Reiniciar un worker no
libera una reserva. Solo un rechazo conocido sin envío o una respuesta negativa
definitiva permiten volver a intentar la operación externa.

Si una creación pierde su respuesta, no se vuelve a enviar el `POST`: podría existir
una transacción cuyo token no se alcanzó a guardar. Las siguientes creaciones con
esa clave devuelven `503 REINTENTAR`; es necesario conciliarla con la pasarela antes
de autorizar un nuevo envío. El vencimiento del pago permite cerrar la Saga sin
confirmar un cobro desconocido. No se supone que un plazo o un reinicio prueben
que no existió un efecto externo.

Para reembolsos en curso o inciertos, el reintento y el reconciliador consultan el
token. Si Webpay confirma `REVERSED` o `NULLIFIED`, guardan `REEMBOLSADO` sin otro
`POST /refunds`. Mientras no exista esa confirmación, se conserva `COBRADO` y se
responde `503` al reintento. Un resultado que siga sin poder determinarse necesita
conciliación; no se responde falsamente `INEXISTENTE` ni se borra la reserva.

Esta recuperación cubre los reembolsos iniciados por la compensación normal.
El caso de una autorización con monto u orden inconsistentes corresponde a H10.

## Actualización del esquema

El arranque de Pagos ejecuta automáticamente la migración 1 y registra su aplicación
en `payment_schema_versions`. Es aditiva: crea las tres tablas de soporte sin cambiar
las columnas, índices, estados ni datos existentes de `payments`.

- Importa las claves `payments.refund_key` existentes a sus vinculaciones.
- Registra como inciertas las creaciones antiguas pendientes que no tengan token.
- Los arranques posteriores conservan estas reservas y no vuelven a migrar los pagos.

La migración se serializa mediante `BEGIN IMMEDIATE` en SQLite y un advisory lock
transaccional en PostgreSQL. Estos bloqueos solo cubren el arranque. Deben actualizarse
todos los workers de Pagos: una instancia del código anterior no conoce las reservas.
Las pruebas de actualización y arranque simultáneo utilizan SQLite temporal.

## Validación

Desde la raíz del repositorio, sin Docker ni servicios desplegados:

```bash
PYTHONDONTWRITEBYTECODE=1 API_KEY=h9-admin-local-testing-key-at-least-32 \
.venv/bin/python -m pytest \
  tests/integration/test_h9_idempotencia_pagos.py \
  tests/integration/test_h9_recuperacion_pagos.py \
  tests/unit/test_h9_migracion_pagos.py \
  tests/unit/test_pagos.py -q -p no:cacheprovider
```

La regresión general con Inscripción y Cupos aislados se ejecuta con:

```bash
.venv/bin/python tests/support/validar_h7.py
```

Ese ejecutor excluye las pruebas que administran Docker y el arranque limpio H5.
La evidencia integrada se regenerará después de reunir todos los frentes, usando
un identificador nuevo para conservar las corridas históricas.
