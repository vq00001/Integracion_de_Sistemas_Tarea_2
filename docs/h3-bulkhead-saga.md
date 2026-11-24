# H3: admisión antes de efectos y compensaciones con capacidad reservada

`SAGA_BULKHEAD` (8 por defecto, mayor que cero) limita las ejecuciones de saga
simultáneas por proceso. Una nueva `POST /v1/matriculas` obtiene permiso antes de
crear la saga o llamar a Cupos. Sin capacidad responde `429`, sobre JSON de error
con código `TOO_MANY_REQUESTS` y `Retry-After: 1`. No registra saga, reserva cupo,
crea cobro ni confirma inscripción. Se puede reintentar el mismo payload y la misma
`Idempotency-Key` después del plazo sugerido.

Una clave ya registrada se consulta antes de exigir capacidad: devuelve el estado
existente, con `Idempotent-Replayed`, sin repetir efectos. Reutilizarla para otro
payload conserva el rechazo 422. Los índices únicos siguen resolviendo las carreras
entre altas; todos los permisos se liberan también ante conflictos y excepciones.

El permiso se libera al llegar a `ESPERANDO_PAGO`; no se mantiene mientras el
comprador usa Webpay. Para continuar tras un cobro confirmado se exige nuevamente
capacidad. Si está llena, se conserva `ESPERANDO_PAGO` para que un sondeo posterior
reanude la inscripción: la carga no provoca reembolso ni liberación de ese cupo.

## Rechazos conocidos y resultados inciertos

`LocalRejection`, subclase de `DependencyFailure`, informa `enviado=False` cuando
el bulkhead está lleno o el circuito impide la llamada. No incrementa las fallas
del circuito. El paso forward queda `FALLIDO` y no se intenta compensarlo; se
deshacen únicamente los pasos anteriores con efectos posibles o confirmados.

Un timeout después de invocar al destino conserva `enviado=True` y `INCIERTO`:
se compensa también ese paso, usando la clave original. Una intención `PENDIENTE`
recuperada tras una caída sigue siendo incierta. Si se rechaza localmente una
compensación, su intento queda `FALLIDO`, pero sigue sujeto a los reintentos y al
reconciliador hasta completar la limpieza.

## Aislamiento y límites

Cada cliente tiene tres semáforos acotados, uno para `forward`, otro para
`compensacion` y otro para `consulta`, cada uno con capacidad `SAGA_BULKHEAD`.
Cobros o consultas saturados no consumen la capacidad reservada a reembolsos.
Las consultas tampoco desplazan los pasos de una saga admitida. Se mantienen los
tres circuitos independientes de H2 y las métricas agregadas por dependencia.
El máximo de llamadas simultáneas de un cliente es tres veces `SAGA_BULKHEAD`.

Estos límites son locales al proceso; no constituyen una cuota global entre
réplicas. Una dependencia realmente caída o compensaciones cuyo propio límite
está agotado todavía pueden requerir reconciliación. H3 evita que la saturación
del forward, por sí sola, impida limpiar recursos con dependencias sanas.

## Verificación y despliegue

```bash
python -m pytest tests/unit/test_h3_bulkhead_saga.py -q --tb=short
python -m pytest tests/unit/test_h3_bulkhead_saga.py tests/unit/test_h2_breaker_pagos.py tests/unit/test_saga_pago.py tests/unit/test_saga.py tests/integration/test_h1_inscripcion_saga.py -q --tb=short
docker compose up -d --build orquestador
```

Las pruebas usan la API, la máquina de estados, los clientes y SQLite reales,
con dependencias simuladas y saturación controlada mediante eventos. Verifican
rechazo sin efectos, capacidad recuperada, ausencia de recursos huérfanos y de
compensaciones pendientes en ráfagas sanas, reembolsos durante saturación,
idempotencia, aislamiento de consultas, espera y reanudación de Webpay, y la
distinción entre rechazo local y timeout incierto.

El contrato versionado del orquestador incluye el nuevo 429. No requiere variables,
claves ni migraciones nuevas. Los informes de carga históricos se conservan como
evidencia del defecto anterior; estos tests no sustituyen una nueva medición del
despliegue con Docker.
