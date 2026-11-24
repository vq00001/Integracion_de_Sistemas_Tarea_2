# H2: circuitos independientes para cobro, reembolso y consulta

El cliente de cada dependencia mantiene tres circuitos: `forward`,
`compensacion` y `consulta`. Cada uno tiene su contador de fallas, cooldown y
una única sonda HALF_OPEN. Los tres conservan los umbrales y tiempos configurados
por `SAGA_CB_THRESHOLD` y `SAGA_CB_RECOVERY_SECONDS`.

Para Pagos, el circuito forward gobierna la creación del cobro. Un reembolso o
una consulta exitosos no reinician sus fallas ni se usan como sonda para cerrarlo.
Esas operaciones pueden seguir atendiendo pagos anteriores mientras el alta está
abierta, si sus propios circuitos están sanos. Sus errores tampoco abren el circuito
de cobro ni cierran los otros circuitos.

H2 separó los circuitos conservando entonces un bulkhead compartido. La corrección
[H3](h3-bulkhead-saga.md) añade admisión antes de reservar y capacidad separada para
forward, compensaciones y consultas. El circuito interno Pagos -> Transbank conserva
su política.

## Admisión y recuperación

`Client.available` observa exclusivamente el forward. Tras cinco fallas secuenciales
de COBRAR (umbral predeterminado), la siguiente saga responde 503 con estado
COMPENSADA y motivo PAGOS_CIRCUITO_ABIERTO. No registra pasos ni reserva cupos.
El id permite consultar el resultado. Para volver a intentar después de la
recuperación se usa una nueva Idempotency-Key; repetir la anterior devuelve su estado.

Al vencer el cooldown, se admite una sola sonda de creación de pago. Si falla, se
reabre el circuito y reinicia el cooldown; si responde correctamente, se cierra.
Una consulta o reembolso que termine durante esa sonda no libera su exclusión ni
cambia el estado del circuito forward. Los rechazos de negocio siguen indicando
una respuesta de una dependencia alcanzable, según la política previa.

## Observabilidad

`circuito_estado{dependencia="COBRAR",estado="OPEN"}` mantiene el significado anterior:
indica el estado del forward, usado para el fail-fast antes de reservar.
Se agrega `saga_circuito_operacion_estado`, con etiquetas dependencia, operacion
(forward/compensacion/consulta) y estado (CLOSED/OPEN/HALF_OPEN).
Los contadores de llamadas, duración y rechazos del bulkhead mantienen su alcance
agregado por dependencia. Los circuitos siguen siendo locales al proceso; reiniciar
el orquestador reinicia sus estados. No se cambian las tablas ni el OpenAPI.

## Verificación

```bash
python -m pytest tests/unit/test_h2_breaker_pagos.py tests/unit/test_saga_pago.py -q --tb=short
```

Las regresiones RED se conservan sin xfail y deben pasar. Se prueban el adaptador
productivo, la API y la saga con SQLite temporal, timeouts simulados y reloj virtual;
no son mediciones de latencia del despliegue. Incluyen que la sexta saga no ejecute
reservas ni cobros, el retorno al servicio tras el cooldown, la sonda única concurrente,
el aislamiento de errores auxiliares y la separación de métricas.

Los informes del experimento de noviembre de 2026 se conservan como evidencia del
defecto anterior. No representan una nueva ejecución posterior a esta corrección.
Para aplicar el código, reconstruir el servicio `orquestador`; no requiere claves,
variables nuevas ni migraciones de datos.
