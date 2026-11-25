# H10: devolución de autorizaciones inconsistentes

Si se solicitaron $15.000 y Webpay autoriza $18.500, Pagos conserva los $15.000
solicitados y registra una deuda de $18.500 sobre el mismo token. También registra
la orden autorizada si difiere de la esperada. Esta obligación se confirma en la
base **antes** del primer intento de devolución.

`payment_refund_debts` guarda el monto, la orden observada y el motivo. No se
reescribe el payload original ni se borra esta información después del reembolso.
Mientras haya deuda, `payments.estado` permanece `PROCESANDO`; la API, las métricas
y la página muestran `REEMBOLSO_PENDIENTE`. Se reutiliza el estado interno existente
para conservar los datos y el CHECK de `payments`, sin reconstruir la tabla.

La autorización inconsistente no se publica como `COBRADO` ni como `RECHAZADO`.
La página indica que se está gestionando la devolución; no afirma que no hubo cobro.
Si la compensación no puede cerrar la deuda, devuelve `503 REINTENTAR` y `Retry-After`.
Si confirma la devolución, devuelve `REEMBOLSADO` y deja el pago en ese estado.
Una autorización tardía también prevalece sobre un rechazo o cancelación anterior;
una devolución ya confirmada permanece cerrada ante respuestas repetidas.

## Recuperación e idempotencia

El retorno, el reconciliador y la compensación pasan por la misma operación
`REFUND` persistente de H9. La devolución automática tiene una clave estable por
pago. Una clave recibida del orquestador queda vinculada al mismo cobro y no crea
otra operación externa. Los bloqueos de la BD solo cubren las reservas y las
transiciones; no se mantienen durante el HTTP.

- Un rechazo definitivo sin devolución libera la operación para reintentar.
  El reconciliador vuelve a intentar el monto autorizado, también después de reiniciar.
- Una devolución procesada cuya respuesta se pierde se recupera por consulta:
  `REVERSED` o `NULLIFIED` cierran la deuda sin enviar otro `POST /refunds`.
- Un resultado incierto que todavía no se puede confirmar conserva su reserva.
  Una consulta que siga mostrando `AUTHORIZED` no prueba que una petición anterior
  nunca vaya a ejecutarse. En ese caso se conserva la deuda y se necesita conciliación
  antes de autorizar un nuevo envío; no se responde `INEXISTENTE` ni se reenvía a ciegas.

Las consultas pendientes no vuelven a confirmar el pago ni permiten matricularlo.
El orquestador actual no trata `REEMBOLSADO` como una resolución inmediata de su
espera: usa su plazo y la compensación existentes para cerrar la Saga. Esta
corrección no cambia ese contrato del orquestador ni sus archivos.

## Actualización del esquema

El arranque aplica la migración 2 bajo el mismo bloqueo de migración de H9.
Agrega `payment_refund_debts` y registra la versión sin alterar las columnas ni
los índices de `payments`.

Los registros antiguos con `RECHAZADO`, `tbk_status` igual a `AUTHORIZED`/`CAPTURED`
y `response_code=0` conservan evidencia de autorización. Se recuperan como
`PROCESANDO`, con deuda `LEGACY_AUTHORIZED` y operación `REFUND UNCERTAIN`.
Se conservan identidad, payload, token, claves, metadata y fechas anteriores.
El monto autorizado antiguo no se inventa: se obtiene por consulta cuando sea posible.
Un rechazo real no se modifica.

La migración no llama a la pasarela. Si la devolución antigua ya ocurrió, el
reconciliador la confirma por consulta. Si el resultado antiguo sigue incierto,
queda registrado para conciliación. Un fallo durante la migración revierte tablas,
cambios de estado y marcador de versión en la misma transacción.

Todos los workers de Pagos deben usar la versión nueva, igual que en H9. Las
pruebas de actualización y arranque concurrente usan SQLite temporal.

## Comprobaciones

Desde la raíz del repositorio:

```bash
PYTHONDONTWRITEBYTECODE=1 API_KEY=h10-admin-local-testing-key-at-least-32 \
.venv/bin/python -m pytest \
  tests/integration/test_h10_reembolso_pendiente.py \
  tests/unit/test_h10_migracion_pagos.py \
  tests/integration/test_h9_idempotencia_pagos.py \
  tests/integration/test_h9_recuperacion_pagos.py \
  tests/unit/test_h9_migracion_pagos.py \
  tests/unit/test_pagos.py -q -p no:cacheprovider
```

Regresión general con servicios y bases aislados, excluyendo H5 y las pruebas que
administran Docker:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/support/validar_h7.py
```

La evidencia integrada se regenerará después de reunir los frentes, con un
identificador nuevo. No se reemplazan resultados históricos.
