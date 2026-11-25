# H13: qué se cuenta antes de Transbank

El Guard publica tres contadores acumulados por `operacion`: `crear`, `confirmar`,
`estado` y `reembolsar`. Las doce series existen desde el arranque, inicialmente
en cero. Los contadores se copian bajo el mismo lock antes de exportarlos.

| Familia en `/metrics` | Cuándo aumenta | Columna total en el CSV de muestras | Delta por fase |
| --- | --- | --- | --- |
| `transbank_intentos_protegidos_total` | Después de adquirir bulkhead y permiso del circuito, antes de la simulación | `pagos_tbk_protegidos` | `intentos_protegidos_tbk` |
| `transbank_fallas_simuladas_total` | La simulación interrumpe la operación antes del cliente | `pagos_tbk_fallas_simuladas` | `fallas_simuladas_tbk` |
| `transbank_cliente_llamadas_iniciadas_total` | Justo antes de ejecutar el callable del cliente | `pagos_tbk_cliente_inicios` | `inicios_cliente_tbk` |

El CSV conserva además cada operación mediante el sufijo `_crear`, `_confirmar`,
`_estado` o `_reembolsar`. La tabla por fase mantiene esos mismos sufijos. El total
se calcula solo si están presentes y son válidas las cuatro operaciones.

Una llamada iniciada al cliente no demuestra recepción en la pasarela ni cobro.
Incluye ejecuciones que fallan antes de enviar HTTP, timeouts, respuestas 4xx/5xx
y llamadas aún en curso. La transacción autorizada y el estado de negocio se
comprueban por separado en los escenarios y las bases de los simuladores.

## Ejemplos y relación con resiliencia

| Caso | Protegidos | Simuladas | Inicios de cliente |
| --- | ---: | ---: | ---: |
| Circuito abierto o bulkhead lleno | 0 | 0 | 0 |
| Caída artificial o demora artificial mayor que el timeout | 1 | 1 | 0 |
| Demora artificial menor que el timeout, seguida de cliente | 1 | 0 | 1 |
| Cliente ejecutado con éxito, error HTTP o timeout | 1 | 0 | 1 |

Al terminar las operaciones se cumple `protegidos = simuladas + inicios de
cliente`. Durante la simulación puede haber admisiones aún sin desenlace. Una
operación puede cruzar el límite de una fase; sus deltas por fase no tienen que
balancear. Los rechazos locales siguen en sus contadores de circuito/bulkhead.

Las fallas artificiales siguen abriendo el circuito de su operación, pero ahora
indican `seguro_no_enviado=True`: nunca se ejecutó el cliente. Al crear un pago,
la API responde `503` con `Retry-After` y permite reintentar la misma clave al
retirar la falla, siguiendo la exclusión persistente de H9. Los timeouts reales
del cliente conservan su tratamiento incierto. Las deudas de H10 y los circuitos
independientes de H11 se mantienen. H13 no cambia el esquema de persistencia.

`transbank_llamadas_total` sigue contando operaciones finalizadas dentro del Guard,
incluidos rechazos de circuito. `transbank_duracion_seconds_sum` incluye espera
artificial; no es una medida aislada de latencia de red.

## Ausencias, reinicios e históricos

Desde H13, `transbank_intentos_total` es un alias del total de inicios del cliente.
El sampler conserva esa serie como `pagos_tbk_intentos` para diagnóstico, pero no
la usa para inferir ninguno de los tres contadores explícitos. En CSV anteriores
puede incluir fallas previas al cliente y su semántica es ambigua.

Una serie ausente, duplicada, negativa o no finita queda desconocida; no se
completa con ceros ni con el alias. Los deltas usan las muestras explícitas de
inicio y fin; sin marcadores, usan la última lectura temporal de cada frontera,
sin sustituir una lectura ausente por otra anterior. Una disminución del contador
invalida los deltas y detiene el informe. Las tablas agregadas exigen telemetría
de todas las repeticiones y publican cobertura `n_reps_*`.

Las figuras muestran las tres curvas separadas. En datos antiguos, la curva
ambigua se etiqueta como histórica y se declara desconocida la telemetría del
cliente. El informe explica las fronteras de medición y muestra inicios por
operación. También se muestrean los cuatro circuitos de H11; la serie histórica
`pagos_cb` sigue representando `crear`. El manifest identifica la versión 2 del
contrato de métricas y conserva la revisión del código ejecutado.

Los CSV, imágenes y resúmenes históricos se conservan, incluida `20261124_H6`.
H13 se verifica con datos sintéticos en directorios temporales; no se genera
evidencia integrada hasta reunir los frentes.

## Validación y nueva evidencia

Desde la raíz del repositorio:

```bash
PYTHONDONTWRITEBYTECODE=1 API_KEY=h13-admin-local-testing-key-at-least-32 \
MPLCONFIGDIR=/tmp/h13-matplotlib-tests .venv/bin/python -m pytest \
  tests/unit/test_h13_metricas_transbank.py \
  tests/unit/test_h13_evidencia_metricas.py \
  tests/unit/test_pagos.py \
  tests/unit/test_h11_breaker_transbank.py \
  tests/integration/test_h11_breaker_pagos.py \
  tests/integration/test_h10_reembolso_pendiente.py \
  tests/unit/test_h10_migracion_pagos.py \
  tests/unit/test_h9_migracion_pagos.py \
  tests/integration/test_h9_idempotencia_pagos.py \
  tests/integration/test_h9_recuperacion_pagos.py \
  tests/unit/test_saga_fallas_experimento.py \
  tests/unit/test_h6_evidencia_cascada.py -q -p no:cacheprovider
```

Regresión general con servicios y bases temporales, excluyendo las pruebas de
H5/Docker y el marcador `falla`:

```bash
PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/tmp/h13-matplotlib-tests \
  .venv/bin/python tests/support/validar_h7.py
```

Después de integrar y confirmar todos los frentes, una nueva corrida completa
con ambos modos y tres repeticiones genera CSV, figuras y su interpretación:

```bash
.venv/bin/python experiment/scripts/saga_fallas.py --repeticiones 3 \
  --ts "$(date +%Y%m%d_%H%M%S)_integrado_H13"
```

Usar un identificador nuevo en cada corrida; no reutilizar el de una evidencia
histórica ni ejecutar el graficador sobre ella para actualizar sus conclusiones.
El experimento usa servicios propios y una pasarela simulada, sin administrar
los contenedores y volúmenes actuales.
