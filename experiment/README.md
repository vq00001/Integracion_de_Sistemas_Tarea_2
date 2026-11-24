# Experimentos de resiliencia y rendimiento

Esta carpeta contiene los experimentos utilizados para evaluar la integración
entre **Matrículas-API** y **Cupos**. El objetivo no es solamente comprobar
que la API responda ante condiciones normales, sino observar su comportamiento
cuando una dependencia presenta latencia, cuando una operación queda en un
estado incierto y cuando aumenta la concurrencia.

Los experimentos se ejecutan contra los servicios reales levantados mediante
Docker Compose. Por tanto, los resultados dependen del hardware, de la carga
del equipo y de la configuración utilizada en cada corrida. Los datos crudos
se conservan en `data/raw/` para que las tablas y figuras puedan regenerarse.


## 1. Preparación del entorno

Desde la raíz del repositorio, levantar los servicios necesarios:

```bash
python scripts/generar_env.py        # una vez (crea .env con claves aleatorias)
docker compose up -d --build         # cupos + matriculas-db + matriculas-api
```

Instalar las dependencias de los scripts:

```bash
python -m pip install -r tests/requirements.txt
```

La API debe estar disponible en `http://localhost:8000`. Los scripts utilizan
la clave admin de `API_KEY`/`MATRICULAS_API_KEYS`/`.env` (sin valor por defecto) y no
necesitan un proxy para inyectar latencia: Cupos ofrece `SIMULAR_LATENCIA_MS` y
`SIMULAR_LATENCIA_MODO` (`antes` | `despues` de confirmar la operación).

## 2. Diseño experimental

Cada experimento define una hipótesis, una variable independiente y métricas
observables. Las variables controladas se mantienen constantes dentro de una
corrida para que los cambios en las mediciones puedan atribuirse, en la medida
de lo posible, al factor estudiado.

### 2.1. Timeout y circuito de protección

**Script:** `scripts/timeout.py`

**Pregunta:** ¿cómo cambia la latencia y el código HTTP de
`POST /v1/enrollments` cuando Cupos se vuelve progresivamente más lento?

**Hipótesis:** mientras la latencia de Cupos permanezca bajo el límite
configurado, las solicitudes deberían completarse normalmente. Al superar
`CUPOS_GRPC_TIMEOUT_SECONDS`, Matrículas-API debería interrumpir la espera y
responder `504 CUPOS_TIMEOUT`. Después de varias fallas consecutivas, el
circuit breaker debería pasar de `CLOSED` a `OPEN` y responder rápidamente con
`503 CUPOS_NO_DISPONIBLE`, sin volver a esperar a Cupos.

**Variables y controles:**

- **Independiente:** latencia artificial de Cupos
  (`SIMULAR_LATENCIA_MS`).
- **Dependientes:** código HTTP, latencia de la solicitud y estado del
  circuito.
- **Controladas (fijadas y verificadas en cada escenario, y escritas en el CSV):**
  `CUPOS_GRPC_TIMEOUT_SECONDS`, `CUPOS_CB_FAILURE_THRESHOLD` (5),
  `CUPOS_CB_RECOVERY_SECONDS` (15), TTL y conciliador por defecto; cursos 1 a 5 y
  número de solicitudes por escenario.

El barrido predeterminado utiliza latencias de `0`, `250`, `1000`, `1500`,
`1900`, `2050`, `2500` y `4000 ms`, con 25 solicitudes por escenario.
(fracciones del timeout, de modo que otro `--timeout-s` escala el barrido).
Los valores de `1900` y `2050 ms` permiten observar el comportamiento
inmediatamente antes y después del timeout predeterminado de 2 segundos.
**Cada escenario recrea Cupos y la API** con su configuración, así el circuito
comienza `CLOSED`. El control funcional (curso `999`, que no existe; respuestas `404`)
se ejecuta **una vez por repetición en su propia fase** (`fase = control_404`, Cupos sin latencia), no
intercalado con las mediciones (`fase = medicion`). Además, antes de medir se calibra la deriva de
reloj contenedor/anfitrión (`data/raw/reloj_<fecha>.csv`).

Ejecutar tres repeticiones del barrido:

```bash
python experiment/scripts/timeout.py --label timeout2s --reps 3
```

Para estudiar otro timeout ya no hay que tocar `.env`: el script lo recibe y lo aplica a cada escenario:

```bash
python experiment/scripts/timeout.py --label timeout5s --timeout-s 5 --reps 3
```

### 2.2. Consistencia de cupos después de un timeout

**Script:** `scripts/consistencia.py`

**Pregunta:** ¿qué ocurre con el cupo reservado por Cupos cuando Matrículas-API
deja de esperar y la matrícula no alcanza a confirmarse?

**Hipótesis** (la tercera es nueva):
- **H1.** Si Cupos confirma la operación y la *respuesta* llega tarde (`SIMULAR_LATENCIA_MODO=despues`), cada
  timeout deja un cupo huérfano.
- **H2.** Sin conciliador esos huérfanos persisten; con conciliador vuelven a cero en ≈ TTL + intervalo.
- **H3.** Si Cupos es lento *antes* de procesar (`antes`), como ahora verifica el deadline del cliente y hace
  `ROLLBACK`, los huérfanos son 0 aunque haya N timeouts; solo quedan matrículas `PENDIENTE` (sin cupo retenido)
  hasta que el conciliador las cierre.

**Variables y controles:**

- **Independiente:** estrategia — `sin_conciliador` (latencia `despues`), `con_conciliador` (latencia `despues`,
  conciliador cada 10 s) y `deadline_en_origen` (latencia `antes`, sin conciliador) — y N (`--niveles-n`, p. ej. `4,8,12`).
- **Dependientes:** cupos huérfanos inmediatamente después del timeout, matrículas `PENDIENTE`, huérfanos y
  pendientes finales, y segundos hasta resolver.
- **Controladas:** latencia de Cupos de 3000 ms, timeout de 2000 ms, curso 4 (15 cupos), concurrencia de cuatro,
  **TTL real de 30 s y conciliador cada 10 s** (antes TTL 6 s / 1 s; con `--rapido` vuelven a esos valores y
  queda anotado en el CSV), y umbral del circuito en 1000 **solo** para que cada solicitud llegue a Cupos (queda
  registrado en `cb_threshold`). Cada corrida parte de un estado limpio (`limpiar`): espera a que el conciliador
  cierre lo que dejó la corrida anterior, porque Cupos ya no permite "devolver" cupos con claves inventadas.

```bash
python experiment/scripts/consistencia.py --label consistencia --reps 3 --niveles-n 4,8,12
```

La métrica de cupos huérfanos se calcula como la diferencia entre las ocupaciones observadas en Cupos y las
nuevas matrículas `CONFIRMADA` (medición de caja blanca). Resultados idénticos entre repeticiones son esperables:
el mecanismo es determinista; la variación útil está entre estrategias y entre valores de N.

### 2.3. Throughput bajo carga

**Script:** `scripts/throughput.py`

**Pregunta:** ¿cómo se comportan el throughput y la latencia cuando aumenta
la cantidad de solicitudes simultáneas?

**Hipótesis:** con poca concurrencia, la latencia debería mantenerse
relativamente estable. Al aparecer un cuello de botella —por ejemplo, en el
pool de conexiones de PostgreSQL, en los hilos gRPC o en las escrituras de
SQLite— el throughput debería dejar de crecer y el p95 debería aumentar.

**Variables y controles:**

- **Independiente:** concurrencia, con niveles de `1`, `5`, `10`, `20` y `40`.
- **Dependientes:** **ciclos por segundo** y llamadas HTTP por segundo, latencia p50 y p95 y tasa de éxito.
- **Controladas:** endpoint, configuración por defecto verificada (contenedores recreados al inicio de cada
  repetición), 100 ciclos por nivel tras un calentamiento descartado, cursos 1 a 4. El equipo (SO, CPUs) se
  registra en el CSV.

**Qué es una "solicitud".** Cada iteración es un **ciclo de 3 llamadas HTTP**: crear estudiante → matricular →
revertir (se revierte para no agotar cupos). `ciclos_por_s` cuenta ciclos y `llamadas_http_por_s` = 3 × ciclos
(antes se llamaba `rps`, lo que sugería una sola solicitud por iteración). La latencia p50/p95 es **solo** la de
`POST /v1/enrollments`, la operación que cruza a Cupos por gRPC.

```bash
python experiment/scripts/throughput.py --reps 3 --n 100
```

Se ejecutan **3 repeticiones** y se reporta media ± desviación. **Limitación declarada:** el generador de carga
corre en el mismo equipo (y, con `docker compose run`, en el mismo host Docker) que la API, Cupos y Postgres, y
compiten por CPU; las cifras absolutas son de ese equipo. El nivel de saturación se identifica cuando los
ciclos/s dejan de crecer o el p95 sube de forma pronunciada. `graficar_experimentos.py` genera `throughput.png`.

## 3. Datos y repetibilidad

Los scripts escriben los CSV en `data/raw/` con una etiqueta y una marca
temporal. Se recomienda ejecutar al menos tres repeticiones para reducir el
efecto de variaciones accidentales del entorno:

```bash
python experiment/scripts/timeout.py --label timeout2s --reps 3
python experiment/scripts/consistencia.py --label consistencia --reps 3
python experiment/scripts/throughput.py --reps 3
python experiment/scripts/tamano_mensaje.py          # D2: protobuf vs JSON (no requiere Docker)
```

El graficador reconoce cada CSV por sus columnas y también escribe las **tablas** (`results/exp*.csv|md`):

```bash
python experiment/graficar_experimentos.py \
  experiment/data/raw/timeout2s_<fecha>.csv \
  experiment/data/raw/consistencia_<fecha>.csv \
  experiment/data/raw/throughput_<fecha>.csv
```

Cuando existen varias repeticiones, las figuras muestran promedios por
escenario y, en la figura de latencia, la desviación estándar. Los CSV
generados por las corridas se versionan en `data/raw/`; esto permite conservar
la evidencia utilizada para elaborar el informe.

## 4. Figuras generadas

Todas las figuras y tablas se guardan en `results/` cuando se ejecuta `graficar_experimentos.py` sobre
datos nuevos. **La carpeta se entrega vacía de resultados vigentes** (ver el aviso del inicio): las imágenes
de abajo se generarán allí; las anteriores a la revisión 3 están en `results/archivo_rev2/` y no son
comparables. Qué muestra cada una:

### Latencia en función del delay

**Archivo:** `results/latencia_vs_delay.png`

Compara la latencia media de las respuestas `201` con la latencia introducida en Cupos. Las barras de error
son la desviación estándar entre repeticiones y la línea vertical marca el timeout configurado. El control `404`
ya no está en estos datos (fase aparte). Si `reloj_*.csv` muestra deriva (factor ≠ 1), la latencia medida
será ese factor por el retraso inyectado: no es un error del sistema, es del reloj de la VM.

### Transición del circuit breaker

**Archivo:** `results/circuit_breaker_transition.png`

Muestra, para el primer escenario sobre el timeout, la latencia de cada solicitud y el estado del circuito. El
**título se arma con lo observado** (antes decía "CLOSED → OPEN" aunque los datos mostraran el circuito siempre
cerrado). El color es el estado del circuito y la forma, el código HTTP (`504`, `503`, `201`).

### Distribución de códigos HTTP

**Archivo:** `results/status_por_delay.png`

Presenta el número promedio de respuestas de cada código por escenario: de `201` a `504` por timeout y luego a
`503` por circuito abierto.

### Consistencia de cupos

**Archivo:** `results/consistencia_cupos.png`

Compara, por estrategia (`sin_conciliador`, `con_conciliador`, `deadline_en_origen`), los huérfanos justo después
del timeout, los huérfanos finales y las matrículas pendientes finales. Hay además una figura `throughput.png` y
tablas `exp1_*`, `exp2_*`, `exp3_*`.

## 5. Interpretación y límites

Los resultados deben interpretarse como evidencia del comportamiento de esta
implementación bajo la configuración indicada, no como una caracterización
universal del rendimiento del sistema. En particular, el throughput depende
del hardware y de la carga concurrente disponible, mientras que los umbrales
del circuit breaker, el timeout y el TTL determinan la forma de las curvas.
Por ello, cualquier conclusión del informe debe acompañarse de la
configuración, el número de repeticiones y los CSV utilizados para generarla.

**Límites concretos de estos experimentos** (para no sobre-interpretar):
- El timeout (experimento 1) se evalúa con un solo valor por corrida (2 s por defecto; `--timeout-s` para otros): no es un barrido de timeouts.
- La latencia es **inyectada** (`sleep` en Cupos), no una degradación real (CPU, red, disco): modela un Cupos lento, no todas las formas de lentitud.
- El experimento 2 mide huérfanos por caja blanca (consulta directa a la base de Cupos): válido para el
  entorno Compose; no sería observable así en producción.
- Un reloj de VM con deriva altera las latencias medidas respecto de las inyectadas; se calibra y se registra, no se corrige en los datos.
- La caché de disponibilidad (`GET /v1/courses`) **no** se midió; el diseño la justifica (D1) pero no hay datos de cuánto reduce la carga.


## Experimento `saga_fallas` (Unidad 3): evidencia antes y después de los tres escenarios

Responde a lo que pide el encargo U3: provocar las fallas, mostrar **qué hace el sistema antes y después** y **qué habría pasado sin el patrón**.
Los experimentos anteriores de esta carpeta miden el flujo de la Tarea 1; este mide la saga de matrícula con Pagos (Transbank Webpay Plus).

| Escenario | Qué se hace | Qué se mide |
|---|---|---|
| **E1 Éxito** | 5 matrículas completas (el comprador paga en Webpay simulado) | Estado de Cupos, Inscripción y Pagos antes/después; pasos firmes en orden |
| **E2 Fallo con compensación** | Falla el último paso (Inscripción) tras pagar | Reembolso, cupo liberado, sin inscripción; orden inverso de la compensación |
| **E3 Cascada contenida** | Una carga de bucle cerrado atraviesa `antes → pagos_lento → pagos_recuperacion → lenta → alivio → bulkhead → pico → caida → recuperacion` (Pagos lento, Transbank lento, pico y caída) | Latencias, circuitos por frontera y operación, admisión, bulkhead, intentos reales a Pagos y Transbank, sagas por estado y **el resto del sistema** (sondas) |

E3 usa el mismo plan de workers, pausas, fallas y timeouts por variante: **con patrón** (configuración normal) y **sin patrón** (Circuit Breaker y Bulkhead inertes, con los mismos timeouts). La sonda `GET /v1/sagas/{id}` comparte proceso con la saga (ahí se ve la cascada), `GET /v1/health` de Pagos comparte proceso con la llamada lenta a Transbank (ahí se ve si el bulkhead protege sus hilos) y `GET /v1/enrollments` es otro servicio (ahí se ve el aislamiento).

### Cómo ejecutarlo

Corre con procesos locales (Cupos gRPC, Inscripción, Orquestador y Pagos reales; Transbank y comprador simulados, más un proxy experimental para demorar crear pagos), sin Docker ni internet:

```bash
pip install -r matriculas-api/requirements.txt -r pagos/requirements.txt -r tests/requirements.txt
python experiment/scripts/saga_fallas.py --repeticiones 3     # completo: ≈ 15 min (E1/E2 + 3 × (con y sin patrón))
python experiment/scripts/saga_fallas.py --rapido             # fases cortas para probar el script (≈ 4 min)
python experiment/scripts/saga_fallas.py --solo-base          # solo E1 y E2
```

Para correrlo por partes con el mismo `--ts` (los CSV se fusionan): `--solo-cascada --variantes con_patron --desde-rep 2 --repeticiones 1 --ts <ts>`.

### Qué produce

- `data/raw/saga_fallas_<ts>_manifest.json`: revisión Git, estado de trabajo, hash de fuentes y parámetros de ejecución.
- `data/raw/saga_fallas_<ts>_frontera.csv`: llamadas recibidas y reenviadas por el proxy, con operación, clave opaca y tiempos; no contiene credenciales ni cuerpos.
- `data/raw/saga_fallas_<ts>_*.csv`: peticiones, sondas, métricas muestreadas cada 0,5 s, fases, estados antes/después por fase y escenarios E1/E2.
- `results/saga_fallas_cascada_<ts>.png`: línea de tiempo con y sin patrón (latencia, resto del sistema, circuitos y bulkhead, sagas por estado).
- `results/saga_fallas_fases_<ts>.png|csv|md`: comparación por fase (medianas y rango entre repeticiones).
- `results/saga_fallas_antes_despues_<ts>.png`: estado de los tres servicios antes/después en E1 y E2.
- `results/resumen_saga_fallas_<ts>.md`: lectura de la evidencia **con los números de la corrida** y los hallazgos que se detectan en los datos.

Evidencia vigente de H6: [`20261124_H6`](results/resumen_saga_fallas_20261124_H6.md), con tres repeticiones por variante y comprador activo. `20261121_completo` queda como evidencia histórica anterior a H2/H3/H6.

Para reproducir la corrida incluida:

```bash
python experiment/scripts/saga_fallas.py --repeticiones-base 3 --repeticiones 3 --ts <identificador_nuevo>
python -m pytest tests/unit/test_h6_evidencia_cascada.py tests/unit/test_saga_fallas_experimento.py tests/unit/test_h2_breaker_pagos.py -q
```

En `pagos_lento`, cuatro workers y una demora de 3000 ms en el proxy superan el timeout del orquestador (2 s), sin agotar la admisión (8). Transbank permanece sano; las compensaciones y consultas no sufren esa demora. El proxy reenvía incluso tras el timeout y registra la petición tardía, para comprobar que la cancelación por clave evita cobros huérfanos. Cada variante usa el mismo proxy.

El comprador aprueba los pagos aceptados. En la fase de carga directa a Pagos se cancela cada pago por su clave; ese trabajo no es una matrícula. El drenaje solo declara cierre cuando no quedan sagas en ejecución, compensaciones pendientes, esperas de comprador, pagos pendientes ni peticiones activas del proxy. Los rechazos `PAGOS_CIRCUITO_ABIERTO` se comprueban por saga: sin pasos firmes y sin peticiones al proxy.

`pct_fail_fast_orq` exige HTTP 503, motivo de circuito de Pagos abierto y respuesta en menos de 200 ms. Se distingue de `pct_rapidas` y de HTTP 429. Los contadores de intentos se incrementan únicamente al ejecutar la dependencia; un rechazo local no cuenta. El informe se detiene si un contador disminuye dentro de una repetición, y no convierte telemetría ausente en ceros.

### Cómo se lee frente a la rúbrica

- *Escenarios claros y reproducibles*: el script y los parámetros (`plan()` y `PATRONES`) son la definición; `tests/unit/test_saga_fallas_experimento.py` verifica sus piezas deterministas.
- *Evidencia antes/después*: figuras y CSV anteriores, con el estado leído directamente de la base de cada servicio.
- *Interpretación y qué habría pasado sin el patrón*: la comparación con/sin patrón del resumen y sus **límites** (una máquina, SQLite, Transbank simulado).

### Límites

Transbank y el comprador están simulados. La lentitud de Pagos se induce en el proxy y la de Transbank mediante `PUT /internal/fault`. Todo corre en una máquina con SQLite y un proceso por servicio. La carga espera respuesta y comprador/cancelación antes de repetir: no es una tasa fija de llegada. Los volúmenes de trabajo difieren entre variantes y los máximos de ocupación son muestreados; no representan CPU ni capacidad de producción. HALF_OPEN puede pasar entre muestras; su transición controlada se verifica en H2. La recuperación medida exige CLOSED y matrículas completas posteriores. Un resultado con espera de pago o compensación pendiente no demuestra cierre.

### Evidencia complementaria: integridad y bulkhead con dependencias sanas

La corrida histórica `20261122_163339` documentó H1 (alta pública sin Saga) y H3 (compensaciones provocadas por el bulkhead). Sus CSV e informe se conservan como diagnóstico previo a las correcciones. La protección vigente se verifica con `tests/integration/test_h1_inscripcion_saga.py` y `tests/unit/test_h3_bulkhead_saga.py`; esa corrida histórica no describe el comportamiento actual.
