# Tarea 2 — Integración de Sistemas

## Descripción

El Centro de Formación Técnica AprendeMás utiliza dos sistemas desarrollados de forma independiente:

- **Cupos:** administra el catálogo de cursos y su disponibilidad. Es la fuente de verdad sobre los cupos restantes.
- **Matrículas:** registra estudiantes y matrículas. Sin integración con Cupos, puede registrar estudiantes en cursos que ya alcanzaron su capacidad.

Este proyecto integra ambos sistemas y evalúa el comportamiento de la solución ante concurrencia, latencia y fallas mediante pruebas automatizadas y experimentos.

La integración se implementa en `matriculas-api/`, una API REST desarrollada con **Python y FastAPI**. Antes de confirmar una matrícula, la API consulta mediante **gRPC** al servicio Cupos. El cliente de línea de comandos de Node.js en `matriculas/` se conserva como referencia del funcionamiento anterior a la integración y no se inicia de forma predeterminada.

## Arquitectura y estructura del repositorio

```text
.
├── docker-compose.yml                              # orquesta TODOS los servicios (cupos + matriculas-db + matriculas-api [+ matriculas, legacy])
├── .env.example                                    # plantilla SIN secretos; el .env real se genera con scripts/generar_env.py
├── scripts/                                        # generar_env.py, ejecutar_tests.py, verificar_compatibilidad.py
├── runner/                                         # imagen y dependencias de los ejecutores de pruebas y experimentos
├── contrato-base/                                  # último contrato PUBLICADO (.proto y OpenAPI): base de la verificación de compatibilidad
├── proto/
│   └── cupos.proto                                 # contrato gRPC de Cupos, paquete cupo.v1 (fuente de verdad de la integración)
├── docs/adr/                                       # ADR D1–D4 + D5 (autenticación, transporte, privacidad)
├── experiment/                                     # scripts, datos crudos (data/raw) y figuras de los experimentos
├── tests/                                          # grpc/, integration/, contract/ y unit/ (unitarias en proceso, sin Docker)
├── cupos/                                          # servicio Cupos (Python + gRPC + SQLite)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── server.py
│       └── generar.py                              # genera los stubs (*_pb2.py) desde proto/cupos.proto
├── matriculas-api/                                 # API REST de Matrículas (Python + FastAPI) - la integración
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── README.md                                   # autenticación, resiliencia, endpoints, cómo correrla
│   ├── generar_stubs.py / exportar_openapi.py
│   ├── docs/openapi.{json,yaml}                    # contrato REST versionado
│   └── app/
│       ├── main.py, config.py, database.py, models.py, schemas.py
│       ├── security.py                             # autenticación por API Key
│       ├── errors.py                               # errores JSON uniformes
│       ├── grpc_client.py + circuit_breaker.py     # cliente gRPC resiliente hacia Cupos
│       ├── reconciler.py                           # cierra matrículas PENDIENTES (resultado incierto tras un timeout)
│       ├── rut.py                                  # validación del RUT (módulo 11)
│       ├── cursos_cache.py                         # caché en memoria (TTL corto) de la disponibilidad de cursos
│       └── routers/                                # students, enrollments, courses, health
└── matriculas/                                     # sistema legado de referencia (Node.js + Postgres), "antes de la integración"
    ├── Dockerfile
    ├── package.json
    └── src/
        ├── index.js                                # bootstrap: conecta y sincroniza el esquema (no es un servidor)
        ├── config/database.js
        ├── models/                                 # Student, Enrollment (sin Course - ver mas abajo)
        ├── database/seed.js
        └── cli/
```

La capa de exposición vive en `api-gateway/` y `bff/`: el gateway es el único servicio publicado,
valida JWT de usuario o API keys M2M, aplica rate limiting y enruta hacia los servicios internos. El
BFF web agrega el catálogo de cursos y las matrículas para el portal. El contrato está en
[`docs/gateway-bff.md`](docs/gateway-bff.md).

### Responsabilidades y persistencia

| Componente | Tecnología | Responsabilidad |
|---|---|---|
| API Gateway | Python y Flask | Único punto HTTP publicado; valida JWT/API key, aplica rate limit y enruta solicitudes. |
| BFF web | Python y Flask | Compone cursos de Cupos e inscripciones de `matriculas-api` para el portal. |
| Cupos | Python, gRPC y SQLite | Catálogo de cursos, disponibilidad y operaciones sobre cupos. |
| Matrículas API | Python y FastAPI | API REST, autenticación, matrícula y comunicación resiliente con Cupos. |
| Orquestador | Python y FastAPI | Coordina la Saga persistente de cupo, pago e inscripción con compensaciones. |
| Base de datos de Matrículas | PostgreSQL | Persistencia de estudiantes y matrículas. |
| Matrículas legado | Node.js | Sistema de referencia que representa el estado anterior a la integración. |

Cada sistema es dueño de sus datos: Cupos utiliza SQLite en el volumen `cupos_data` y Matrículas utiliza PostgreSQL en `matriculas_db_data`. Ningún servicio consulta directamente la base de datos del otro.

Matrículas no mantiene una tabla local de cursos. La API integrada almacena el `curso_id` correspondiente a Cupos y verifica la existencia y disponibilidad del curso por gRPC. En cambio, el CLI legado registra un `codigo_curso` de texto libre, sin validación de disponibilidad.

La API conserva las API keys para integraciones máquina a máquina y añade autenticación local OAuth2/JWT para usuarios, con permisos acotados al estudiante autenticado. También incluye errores JSON uniformes, cliente gRPC con circuit breaker, conciliación de matrículas `PENDIENTE`, validación de RUT y caché de disponibilidad con TTL corto. Los contratos se encuentran en `proto/cupos.proto` y `matriculas-api/docs/openapi.{json,yaml}`; las decisiones arquitectónicas se documentan en `docs/adr/`.

## Instalación y ejecución

### Requisitos

- Docker con Docker Compose.
- Python para generar la configuración y ejecutar los scripts desde el anfitrión.

### Levantar la integración

Desde la raíz del repositorio:

```bash
python scripts/generar_env.py
docker compose up --build -d
```

El primer comando genera `.env` con claves aleatorias sin sobrescribir un archivo existente. El segundo inicia Cupos, PostgreSQL, `matriculas-api`, el orquestador de Saga, el BFF y el gateway. Solo el gateway publica el puerto `8000`; los servicios de dominio quedan dentro de la red de Compose.

El gateway está disponible en `http://localhost:8000`. `GET /v1/bff/dashboard` devuelve cursos disponibles e inscripciones en una respuesta. La operación Saga se expone en `POST /v1/matriculas` y su consulta en `GET /v1/sagas/{id}`. Las rutas y ejemplos de autenticación están descritos en [`docs/gateway-bff.md`](docs/gateway-bff.md) y [`matriculas-api/README.md`](matriculas-api/README.md).

El puerto `8000` se publica únicamente en `127.0.0.1` mediante `API_BIND`. Como el gateway utiliza HTTP sin TLS, debe ubicarse detrás de un proxy TLS si se necesita acceso desde otra red. La documentación interactiva puede desactivarse con `DOCS_ENABLED=false`. Los límites, timeouts y demás valores del gateway y el BFF están en `.env.example`.

Cupos (gRPC) y PostgreSQL permanecen en la red interna de Docker. Para acceder a PostgreSQL:

```bash
docker compose exec matriculas-db psql -U matriculas_user -d matriculas_db
```

Para iniciar únicamente Cupos:

```bash
docker compose up --build cupos
```

### Ejecutar el CLI legado

El CLI reproduce el comportamiento anterior a la integración: permite registrar matrículas sin consultar los cupos disponibles. Se inicia exclusivamente mediante el perfil `legacy`:

```bash
docker compose --profile legacy run --rm matriculas node src/index.js
docker compose --profile legacy run --rm -it matriculas npm run cli
```

## Pruebas automatizadas y verificación de contratos

La suite contempla pruebas de los contratos gRPC y REST, autenticación, manejo de errores, reserva y liberación de cupos, concurrencia, idempotencia, fallas o lentitud de Cupos y compatibilidad OpenAPI. Las pruebas se organizan en `tests/grpc/`, `tests/integration/`, `tests/contract/` y `tests/unit/`.

### Ejecución recomendada con Docker

Para ejecutar la suite completa en un contenedor:

```bash
docker compose run --rm --build tests
```

Para una ejecución rápida, que excluye las pruebas que detienen contenedores:

```bash
docker compose run --rm tests --rapido
```

Para ejecutar solamente un subconjunto de pruebas, se puede enviar cualquier argumento válido de pytest. Por ejemplo, para seleccionar las pruebas relacionadas con idempotencia:

```bash
docker compose run --rm tests -k idempotency
```

El ejecutor inicia los servicios necesarios, espera a que la API responda, ejecuta pytest y elimina los archivos de caché de la corrida. Devuelve el código de salida de pytest (`0` cuando todas las pruebas pasan).

**El restablecimiento de datos está desactivado por defecto.** Para eliminar contenedores y volúmenes asociados a una corrida y comenzar desde cero, utilizar `--reset-estado` o `RESET_ESTADO=1` únicamente en un entorno de pruebas sin datos que sea necesario conservar. Los cupos retenidos deben resolverse mediante el conciliador o reinicializando expresamente los volúmenes de pruebas; `docker compose down -v` borra datos persistentes.

### Ejecución local con pytest

Para preparar el entorno local, ejecutar estos comandos una sola vez o cuando cambien las dependencias:

```bash
python scripts/generar_env.py
python -m pip install -r tests/requirements.txt
docker compose -f docker-compose.yml -f docker-compose.test.yml up --build -d
```

Luego, ejecutar los siguientes comandos según el objetivo:


Para ejecutar la suite completa, incluidas las pruebas de falla:

```bash
python -m pytest tests -q
```

Para ejecutar las pruebas normales, excluyendo las pruebas que detienen contenedores:

```bash
python -m pytest tests -m "not falla" -q
```

Para ejecutar únicamente las pruebas que verifican escenarios de falla:

```bash
python -m pytest tests -m falla -q
```

Estas pruebas detienen y reinician servicios, por lo que deben ejecutarse sin paralelismo.


Para ejecutar solamente las pruebas unitarias:

```bash
python -m pytest tests/unit -q
```

El archivo `docker-compose.test.yml` expone el gRPC de Cupos exclusivamente en `127.0.0.1:50051` para las pruebas de contrato. Las pruebas obtienen la clave administrativa desde `API_KEY`, `MATRICULAS_API_KEYS` o `.env`, sin utilizar una clave predeterminada. Las pruebas marcadas como `falla` detienen y reinician servicios, por lo que deben ejecutarse sin paralelismo.

También puede utilizarse el ejecutor sin contenedor, siempre que el entorno esté preparado. En este caso, elegir una sola opción:

Para una ejecución rápida:

```bash
python scripts/ejecutar_tests.py --rapido
```

Para restablecer explícitamente el estado antes de ejecutar:

```bash
python scripts/ejecutar_tests.py --reset-estado
```

Para verificar la compatibilidad de los contratos actuales con las versiones publicadas en `contrato-base/`:

```bash
python scripts/verificar_compatibilidad.py
```

## Experimentos de resiliencia y rendimiento

Los experimentos evalúan el comportamiento de la integración ante distintas configuraciones de los servicios. Cada escenario recrea los contenedores con una configuración explícita y registra los valores efectivos para facilitar la reproducibilidad.

### Ejecución con Docker

```bash
docker compose run --rm --build experimentos
```

La ejecución normal utiliza tres repeticiones y el TTL real; puede tomar aproximadamente entre 45 y 60 minutos. Si se quiere ejecutar algún experimento en específico se debe hacer con:

```bash
docker compose run --rm experimentos --rapido
docker compose run --rm experimentos --reps 5
docker compose run --rm experimentos --solo-graficar
```

dónde la opción `--rapido` es un smoke con TTL reducido a seis segundos (aproximadamente diez minutos) y no sustituye los experimentos con la configuración real. `--reps` ajusta el número de repeticiones y `--solo-graficar` genera nuevamente las figuras a partir de los CSV existentes.

Con el sistema preparado también pueden lanzarse desde el anfitrión:

```bash
python experiment/ejecutar_todo.py --reps 3
python experiment/ejecutar_todo.py --rapido
```

Los resultados se organizan de la siguiente forma:

| Ruta | Contenido |
|---|---|
| `experiment/data/raw/` | Datos crudos de cada escenario. |
| `experiment/results/` | Figuras y visualizaciones. |
| `experiment/results/resumen_<fecha>.txt` | Tablas y resultados resumidos. |
| `experiment/data/archivo_rev/` | Resultados de una revisión anterior, no directamente comparables con los actuales. |

### Consideraciones del entorno de pruebas

Los ejecutores de pruebas de fallas y experimentos requieren acceso al socket Docker del anfitrión (`/var/run/docker.sock`) para administrar contenedores. **Este acceso concede privilegios elevados sobre Docker y no debe habilitarse en servidores compartidos o entornos no confiables.** Los ejecutores no arrancan con `docker compose up` normal.

El proyecto Compose utiliza el nombre fijo `integracion-sistemas-t1`, de modo que todos los ejecutores administran los mismos servicios. Si existen contenedores levantados con una configuración anterior, deben detenerse antes de iniciar la actual para evitar conflictos en el puerto `8000`.

El repositorio se monta en `/repo` en los ejecutores, lo que permite reflejar cambios de código y guardar resultados en el directorio local.

## Historial de versiones de los contratos

| Fecha | Contrato | Cambio principal | Compatibilidad |
|---|---|---|---|
| 2026-09-25 | REST `/v1`, gRPC `cupo` | Versión inicial. | — |
| 2026-10-21 | gRPC `cupo.v1` | Versionado del paquete gRPC. | Cambio de nombre del servicio; ambos extremos se regeneran conjuntamente. |
| 2026-10-21 | REST `/v1` | `Idempotency-Key`, estado `PENDIENTE`, respuestas `502`/`504`, ajuste de `403` a `401` para claves inválidas y validación de RUT. | Cambios de comportamiento documentados en `docs/adr/`. |
| 2026-10-29 | gRPC `cupo.v1` y REST `/v1` (1.1.0) | RPC `consultarOperacion`; consultas de cursos; eliminación de estudiantes; expiración de claves; respuestas `409` y `429`; validaciones adicionales de liberación e idempotencia. | Adiciones al esquema y cambios de comportamiento documentados en `docs/adr/`. |

## Uso de inteligencia artificial

Durante el desarrollo del proyecto se utilizó inteligencia artificial como apoyo para revisar aspectos sintácticos del código, analizar alternativas de implementación, simplificar secciones repetitivas y mejorar la documentación.

En las revisiones del proyecto se utilizó **Claude (Anthropic)** para examinar la implementación de Cupos, la API y las pruebas; apoyar la revisión de scripts de experimentos, mediciones de mensajes, decisiones arquitectónicas y documentación del repositorio. También se empleó asistencia de IA para mejorar la redacción y el formato LaTeX del informe.

La revisión asistida incluyó pruebas ejecutadas contra procesos locales y simulaciones del control de Docker. Estas verificaciones **no equivalen** a ejecutar los contenedores reales ni a validar PostgreSQL en Docker. Los resultados experimentales reales deben distinguirse de cualquier ejecución simulada.
