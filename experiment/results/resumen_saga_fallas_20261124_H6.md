# Evidencia de cascada H6 (20261124_H6)

Todas las cifras se calculan desde los CSV de esta corrida. Las tablas agregadas contienen medianas entre repeticiones; los CSV por repetición conservan dispersión y máximos.

## E1/E2: éxito y compensación después del pago

- E1_exito: 3 matrículas; invariantes OK=1; pasos `RESERVAR>COBRAR>INSCRIBIR`.

- E2_falla_ultimo_paso: 3 matrículas; invariantes OK=1; pasos `RESERVAR>COBRAR>comp:INSCRIBIR>comp:COBRAR>comp:RESERVAR`.

## Resultado principal: lentitud aislada de Pagos

Medianas de las repeticiones: 87.3% de fail-fast comprobado con patrón; 9 intentos reales de crear pago frente a 20 sin patrón. p50 de la solicitud 0.040 s frente a 2.228 s; p95 2.312 s frente a 2.311 s. Ocupación máxima muestreada de cobros en el orquestador: 4 frente a 4.

El circuito reduce los intentos después del umbral; las primeras llamadas aún pagan el timeout. La p95 puede seguir cerca del timeout si esas llamadas superan el 5% de solicitudes. La carga de bucle cerrado ofrece más solicitudes cuando el rechazo es rápido, por lo que no se comparan volúmenes como si fueran una tasa fija de llegada.


## Parámetros efectivamente ejecutados

| variante   | fase               |   workers |   pagos_latencia_ms |   tbk_latencia_ms |   tbk_caido |   saga_timeout_s |   tbk_timeout_s | comprador   |
|:-----------|:-------------------|----------:|--------------------:|------------------:|------------:|-----------------:|----------------:|:------------|
| con_patron | antes              |         4 |                   0 |                 0 |           0 |                2 |             1.5 | aprobar     |
| con_patron | pagos_lento        |         4 |                3000 |                 0 |           0 |                2 |             1.5 | aprobar     |
| con_patron | pagos_recuperacion |         4 |                   0 |                 0 |           0 |                2 |             1.5 | aprobar     |
| con_patron | lenta              |         4 |                   0 |              3000 |           0 |                2 |             1.5 | aprobar     |
| con_patron | alivio             |         4 |                   0 |                 0 |           0 |                2 |             1.5 | aprobar     |
| con_patron | bulkhead           |        24 |                   0 |              1200 |           0 |                2 |             1.5 | aprobar     |
| con_patron | pico               |        16 |                   0 |              3000 |           0 |                2 |             1.5 | aprobar     |
| con_patron | caida              |         4 |                   0 |                 0 |           1 |                2 |             1.5 | aprobar     |
| con_patron | recuperacion       |         4 |                   0 |                 0 |           0 |                2 |             1.5 | aprobar     |
| sin_patron | antes              |         4 |                   0 |                 0 |           0 |                2 |             1.5 | aprobar     |
| sin_patron | pagos_lento        |         4 |                3000 |                 0 |           0 |                2 |             1.5 | aprobar     |
| sin_patron | pagos_recuperacion |         4 |                   0 |                 0 |           0 |                2 |             1.5 | aprobar     |
| sin_patron | lenta              |         4 |                   0 |              3000 |           0 |                2 |             1.5 | aprobar     |
| sin_patron | alivio             |         4 |                   0 |                 0 |           0 |                2 |             1.5 | aprobar     |
| sin_patron | bulkhead           |        24 |                   0 |              1200 |           0 |                2 |             1.5 | aprobar     |
| sin_patron | pico               |        16 |                   0 |              3000 |           0 |                2 |             1.5 | aprobar     |
| sin_patron | caida              |         4 |                   0 |                 0 |           1 |                2 |             1.5 | aprobar     |
| sin_patron | recuperacion       |         4 |                   0 |                 0 |           0 |                2 |             1.5 | aprobar     |

Las variantes conservan los timeouts. Sin patrón usa umbral y capacidad elevados; conserva idempotencia, callbacks, reconciliación y timeouts.

## Comparación por fase

| variante   | fase               |   n_reps |   peticiones |   p50_s |   p95_s |   p95_s_min |   p95_s_max |   pct_429 |   pct_fail_fast_orq |   intentos_a_pagos |   compensaciones_orq |   orq_cobros_en_vuelo_max |   rechazos_admision_orq |   intentos_a_tbk |   sonda_orq_p95_s |
|:-----------|:-------------------|---------:|-------------:|--------:|--------:|------------:|------------:|----------:|--------------------:|-------------------:|---------------------:|--------------------------:|------------------------:|-----------------:|------------------:|
| con_patron | antes              |        3 |           37 |   0.196 |   0.317 |       0.175 |       0.66  |     0     |               0     |                 37 |                    0 |                         1 |                       0 |               74 |             0.072 |
| con_patron | pagos_lento        |        3 |          110 |   0.04  |   2.312 |       2.208 |       2.42  |     0     |              87.273 |                  9 |                   23 |                         4 |                       0 |                0 |             0.031 |
| con_patron | pagos_recuperacion |        3 |          132 |   0.046 |   0.189 |       0.186 |       0.225 |     0     |              75.182 |                 33 |                    0 |                         2 |                       0 |               66 |             0.042 |
| con_patron | lenta              |        3 |           96 |   0.041 |   1.782 |       1.713 |       1.847 |     0     |              91.667 |                  8 |                   16 |                         4 |                       0 |                8 |             0.026 |
| con_patron | alivio             |        3 |           82 |   0.167 |   0.272 |       0.245 |       0.305 |     0     |               9.756 |                 74 |                    2 |                         1 |                       0 |              148 |             0.041 |
| con_patron | bulkhead           |        3 |          284 |   0.055 |   1.368 |       1.324 |       1.385 |     0     |               0     |                  0 |                    0 |                         0 |                       0 |               40 |             0.021 |
| con_patron | pico               |        3 |          293 |   0.06  |   0.467 |       0.452 |       0.564 |    24.232 |              65.529 |                  9 |                   33 |                         8 |                      72 |                9 |             0.047 |
| con_patron | caida              |        3 |           84 |   0.036 |   0.106 |       0.067 |       0.112 |     0     |             100     |                  0 |                    0 |                         0 |                       0 |                0 |             0.034 |
| con_patron | recuperacion       |        3 |           95 |   0.175 |   0.298 |       0.253 |       0.3   |     0     |               8.421 |                 86 |                    1 |                         2 |                       0 |              172 |             0.043 |
| sin_patron | antes              |        3 |           38 |   0.192 |   0.267 |       0.238 |       0.302 |     0     |               0     |                 38 |                    0 |                         2 |                       0 |               76 |             0.037 |
| sin_patron | pagos_lento        |        3 |           20 |   2.228 |   2.311 |       2.308 |       2.407 |     0     |               0     |                 20 |                   40 |                         4 |                       0 |                0 |             0.024 |
| sin_patron | pagos_recuperacion |        3 |           68 |   0.21  |   0.342 |       0.256 |       0.38  |     0     |               0     |                 68 |                    0 |                         2 |                       0 |              136 |             0.047 |
| sin_patron | lenta              |        3 |           20 |   1.726 |   1.868 |       1.833 |       1.892 |     0     |               0     |                 20 |                   40 |                         4 |                       0 |               20 |             0.026 |
| sin_patron | alivio             |        3 |           72 |   0.2   |   0.31  |       0.231 |       0.422 |     0     |               0     |                 72 |                    0 |                         1 |                       0 |              144 |             0.048 |
| sin_patron | bulkhead           |        3 |           97 |   1.32  |   1.608 |       1.434 |       1.652 |     0     |               0     |                  0 |                    0 |                         0 |                       0 |               97 |             0.013 |
| sin_patron | pico               |        3 |           63 |   1.879 |   2.906 |       2.837 |       2.989 |     0     |               0     |                 63 |                  126 |                        14 |                       0 |               63 |             0.392 |
| sin_patron | caida              |        3 |           37 |   0.283 |   0.483 |       0.353 |       0.526 |     0     |               0     |                 37 |                   74 |                         1 |                       0 |               37 |             0.043 |
| sin_patron | recuperacion       |        3 |           94 |   0.176 |   0.262 |       0.259 |       0.288 |     0     |               0     |                 94 |                    0 |                         2 |                       0 |              188 |             0.043 |

Fail-fast del orquestador exige HTTP 503, motivo PAGOS_CIRCUITO_ABIERTO y latencia <200 ms. El porcentaje de respuestas rápidas incluye éxitos y otros rechazos: se conserva separado. Intentos y compensaciones son deltas entre límites de fase; ocupación es el máximo muestreado de cobros simultáneos, no CPU.

## Apertura por frontera y recuperación

- con_patron, antes: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
  El circuito Orquestador→Pagos no abrió en esta fase; no se atribuye a él contención observada en la otra frontera.
- sin_patron, antes: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- con_patron, pagos_lento: Orquestador→Pagos (crear) abrió en 3 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- sin_patron, pagos_lento: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- con_patron, pagos_recuperacion: Orquestador→Pagos (crear) abrió en 3 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- sin_patron, pagos_recuperacion: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- con_patron, lenta: Orquestador→Pagos (crear) abrió en 3 de 3 repeticiones; Pagos→Transbank abrió en 3 de 3.
- sin_patron, lenta: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- con_patron, alivio: Orquestador→Pagos (crear) abrió en 3 de 3 repeticiones; Pagos→Transbank abrió en 3 de 3.
- sin_patron, alivio: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- con_patron, bulkhead: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
  El circuito Orquestador→Pagos no abrió en esta fase; no se atribuye a él contención observada en la otra frontera.
- sin_patron, bulkhead: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- con_patron, pico: Orquestador→Pagos (crear) abrió en 3 de 3 repeticiones; Pagos→Transbank abrió en 3 de 3.
- sin_patron, pico: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- con_patron, caida: Orquestador→Pagos (crear) abrió en 3 de 3 repeticiones; Pagos→Transbank abrió en 3 de 3.
- sin_patron, caida: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.
- con_patron, recuperacion: Orquestador→Pagos (crear) abrió en 3 de 3 repeticiones; Pagos→Transbank abrió en 3 de 3.
- sin_patron, recuperacion: Orquestador→Pagos (crear) abrió en 0 de 3 repeticiones; Pagos→Transbank abrió en 0 de 3.

Estados observados por operación:

| variante   |   rep | orq_cb_COBRAR_forward   | orq_cb_COBRAR_compensacion   | orq_cb_COBRAR_consulta   |
|:-----------|------:|:------------------------|:-----------------------------|:-------------------------|
| con_patron |     1 | CLOSED,HALF_OPEN,OPEN   | CLOSED                       | CLOSED                   |
| con_patron |     2 | CLOSED,HALF_OPEN,OPEN   | CLOSED                       | CLOSED                   |
| con_patron |     3 | CLOSED,HALF_OPEN,OPEN   | CLOSED                       | CLOSED                   |
| sin_patron |     1 | CLOSED                  | CLOSED                       | CLOSED                   |
| sin_patron |     2 | CLOSED                  | CLOSED                       | CLOSED                   |
| sin_patron |     3 | CLOSED                  | CLOSED                       | CLOSED                   |

Rechazos por circuito de Pagos verificados por saga: 1744; sin pasos firmes ni peticiones a Pagos: 1744.

- Recuperación pagos_recuperacion, con_patron, rep 1: circuito de creación termina CLOSED; 33 matrículas COMPLETADA de esta fase.
- Recuperación pagos_recuperacion, con_patron, rep 2: circuito de creación termina CLOSED; 34 matrículas COMPLETADA de esta fase.
- Recuperación pagos_recuperacion, con_patron, rep 3: circuito de creación termina CLOSED; 30 matrículas COMPLETADA de esta fase.
- Recuperación pagos_recuperacion, sin_patron, rep 1: circuito de creación termina CLOSED; 66 matrículas COMPLETADA de esta fase.
- Recuperación pagos_recuperacion, sin_patron, rep 2: circuito de creación termina CLOSED; 84 matrículas COMPLETADA de esta fase.
- Recuperación pagos_recuperacion, sin_patron, rep 3: circuito de creación termina CLOSED; 68 matrículas COMPLETADA de esta fase.
- Recuperación recuperacion, con_patron, rep 1: circuito de creación termina CLOSED; 86 matrículas COMPLETADA de esta fase.
- Recuperación recuperacion, con_patron, rep 2: circuito de creación termina CLOSED; 93 matrículas COMPLETADA de esta fase.
- Recuperación recuperacion, con_patron, rep 3: circuito de creación termina CLOSED; 83 matrículas COMPLETADA de esta fase.
- Recuperación recuperacion, sin_patron, rep 1: circuito de creación termina CLOSED; 94 matrículas COMPLETADA de esta fase.
- Recuperación recuperacion, sin_patron, rep 2: circuito de creación termina CLOSED; 94 matrículas COMPLETADA de esta fase.
- Recuperación recuperacion, sin_patron, rep 3: circuito de creación termina CLOSED; 91 matrículas COMPLETADA de esta fase.

Resultados de Saga por fase de origen (estado observado al terminar el drenaje):

| variante   |   rep | fase               | estado_final   |   sagas |
|:-----------|------:|:-------------------|:---------------|--------:|
| con_patron |     1 | alivio             | COMPENSADA     |      10 |
| con_patron |     1 | alivio             | COMPLETADA     |      71 |
| con_patron |     1 | antes              | COMPLETADA     |      25 |
| con_patron |     1 | caida              | COMPENSADA     |      83 |
| con_patron |     1 | lenta              | COMPENSADA     |      96 |
| con_patron |     1 | pagos_lento        | COMPENSADA     |     109 |
| con_patron |     1 | pagos_recuperacion | COMPENSADA     |      99 |
| con_patron |     1 | pagos_recuperacion | COMPLETADA     |      33 |
| con_patron |     1 | pico               | COMPENSADA     |     222 |
| con_patron |     1 | recuperacion       | COMPENSADA     |       9 |
| con_patron |     1 | recuperacion       | COMPLETADA     |      86 |
| con_patron |     2 | alivio             | COMPENSADA     |       7 |
| con_patron |     2 | alivio             | COMPLETADA     |      81 |
| con_patron |     2 | antes              | COMPLETADA     |      65 |
| con_patron |     2 | caida              | COMPENSADA     |      88 |
| con_patron |     2 | lenta              | COMPENSADA     |     103 |
| con_patron |     2 | pagos_lento        | COMPENSADA     |     116 |
| con_patron |     2 | pagos_recuperacion | COMPENSADA     |     103 |
| con_patron |     2 | pagos_recuperacion | COMPLETADA     |      34 |
| con_patron |     2 | pico               | COMPENSADA     |     226 |
| con_patron |     2 | recuperacion       | COMPENSADA     |       9 |
| con_patron |     2 | recuperacion       | COMPLETADA     |      93 |
| con_patron |     3 | alivio             | COMPENSADA     |       8 |
| con_patron |     3 | alivio             | COMPLETADA     |      74 |
| con_patron |     3 | antes              | COMPLETADA     |      37 |
| con_patron |     3 | caida              | COMPENSADA     |      84 |
| con_patron |     3 | lenta              | COMPENSADA     |      94 |
| con_patron |     3 | pagos_lento        | COMPENSADA     |     110 |
| con_patron |     3 | pagos_recuperacion | COMPENSADA     |      96 |
| con_patron |     3 | pagos_recuperacion | COMPLETADA     |      30 |
| con_patron |     3 | pico               | COMPENSADA     |     208 |
| con_patron |     3 | recuperacion       | COMPENSADA     |       8 |
| con_patron |     3 | recuperacion       | COMPLETADA     |      83 |
| sin_patron |     1 | alivio             | COMPLETADA     |      72 |
| sin_patron |     1 | antes              | COMPLETADA     |      37 |
| sin_patron |     1 | caida              | COMPENSADA     |      38 |
| sin_patron |     1 | lenta              | COMPENSADA     |      20 |
| sin_patron |     1 | pagos_lento        | COMPENSADA     |      20 |
| sin_patron |     1 | pagos_recuperacion | COMPLETADA     |      66 |
| sin_patron |     1 | pico               | COMPENSADA     |      63 |
| sin_patron |     1 | recuperacion       | COMPLETADA     |      94 |
| sin_patron |     2 | alivio             | COMPLETADA     |      86 |
| sin_patron |     2 | antes              | COMPLETADA     |      43 |
| sin_patron |     2 | caida              | COMPENSADA     |      36 |
| sin_patron |     2 | lenta              | COMPENSADA     |      20 |
| sin_patron |     2 | pagos_lento        | COMPENSADA     |      20 |
| sin_patron |     2 | pagos_recuperacion | COMPLETADA     |      84 |
| sin_patron |     2 | pico               | COMPENSADA     |      59 |
| sin_patron |     2 | recuperacion       | COMPLETADA     |      94 |
| sin_patron |     3 | alivio             | COMPLETADA     |      64 |
| sin_patron |     3 | antes              | COMPLETADA     |      38 |
| sin_patron |     3 | caida              | COMPENSADA     |      37 |
| sin_patron |     3 | lenta              | COMPENSADA     |      20 |
| sin_patron |     3 | pagos_lento        | COMPENSADA     |      20 |
| sin_patron |     3 | pagos_recuperacion | COMPLETADA     |      68 |
| sin_patron |     3 | pico               | COMPENSADA     |      63 |
| sin_patron |     3 | recuperacion       | COMPLETADA     |      91 |

Bulkhead aislado: la fase de carga directa a Pagos cancela cada pago por su clave, incluso después de un timeout. No representa una matrícula y se etiqueta como destino pagos.

## Estado final y cierre de negocio

- con_patron, repetición 2: convergencia 0.002 s (negativo = sin cierre); COMPLETADA 274, ESPERANDO_PAGO 0, COMPENSADA 652, COMPENSACION_PENDIENTE 0; cupos huérfanos 0, cobros huérfanos 0.
- sin_patron, repetición 2: convergencia 0.005 s (negativo = sin cierre); COMPLETADA 308, ESPERANDO_PAGO 0, COMPENSADA 135, COMPENSACION_PENDIENTE 0; cupos huérfanos 0, cobros huérfanos 0.
- con_patron, repetición 3: convergencia 0.006 s (negativo = sin cierre); COMPLETADA 225, ESPERANDO_PAGO 0, COMPENSADA 608, COMPENSACION_PENDIENTE 0; cupos huérfanos 0, cobros huérfanos 0.
- sin_patron, repetición 3: convergencia 0.006 s (negativo = sin cierre); COMPLETADA 262, ESPERANDO_PAGO 0, COMPENSADA 140, COMPENSACION_PENDIENTE 0; cupos huérfanos 0, cobros huérfanos 0.
- con_patron, repetición 1: convergencia 0.005 s (negativo = sin cierre); COMPLETADA 216, ESPERANDO_PAGO 0, COMPENSADA 628, COMPENSACION_PENDIENTE 0; cupos huérfanos 0, cobros huérfanos 0.
- sin_patron, repetición 1: convergencia 0.005 s (negativo = sin cierre); COMPLETADA 270, ESPERANDO_PAGO 0, COMPENSADA 141, COMPENSACION_PENDIENTE 0; cupos huérfanos 0, cobros huérfanos 0.

## Límites y calidad de la medición

- Telemetría requerida ausente: ninguna. Valores ausentes se mantienen desconocidos; un contador que disminuye invalida el cálculo y detiene el informe.
- Transbank y el comprador son simulados. En Pagos lento, un proxy experimental retrasa únicamente POST /v1/payments; reembolsos y consultas llegan al servicio real. El proxy registra incluso llamadas que continúan después del timeout. Ambas variantes usan el mismo proxy.
- El comprador intenta aprobar los pagos aceptados. El drenaje exige cero ESPERANDO_PAGO, pagos pendientes y llamadas activas; si no se alcanza, el informe declara falta de cierre.
- Procesos locales en una máquina, bases SQLite independientes, un proceso por servicio; no mide Kubernetes, PostgreSQL bajo carga, red distribuida ni Webpay real.
- Carga de bucle cerrado: cada worker espera respuesta y comprador/cancelación antes de pausar y repetir. El volumen admitido difiere entre variantes; comparar también intentos y ocupación, sin atribuir throughput a igual tasa de llegada.
- HALF_OPEN puede durar menos que el intervalo de muestreo de 0,5 s. La transición controlada se verifica en tests H2; la corrida demuestra recuperación mediante CLOSED y matrículas completas posteriores.
- No se deducen causas de código desde un estado de circuito aislado. CSV históricos conservan sus conclusiones originales y no sustituyen esta evidencia.
