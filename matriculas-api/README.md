 Matrículas API (REST, Python/FastAPI)

API REST pública del sistema de Matrículas de AprendeMás. Gestiona
estudiantes y sus matrículas, validando en tiempo real la disponibilidad
de cupos contra el servicio **Cupos** (gRPC, ya existente en `cupos/`)
antes de confirmar cada matrícula.

```
matriculas-api/
├── Dockerfile
├── requirements.txt
├── .env.example
└── app/
    ├── main.py            # bootstrap FastAPI, OpenAPI, exception handlers
    ├── config.py          # settings vía variables de entorno
    ├── database.py        # engine/session SQLAlchemy
    ├── models.py           # ORM: Student, Enrollment
    ├── schemas.py          # Pydantic: request/response, documentados para OpenAPI
    ├── security.py         # API Key: admin (entorno) y por estudiante (tabla api_keys, hasheadas)
    ├── ratelimit.py        # límite de intentos de autenticación fallidos y de escrituras por estudiante
    ├── cursos_cache.py     # caché en memoria (TTL corto) de la disponibilidad de cursos
    ├── errors.py           # excepciones de dominio + sobre JSON uniforme de error
    ├── circuit_breaker.py  # circuit breaker en memoria hacia Cupos
    ├── grpc_client.py      # cliente gRPC con timeout + circuit breaker
    ├── grpc_stubs/         # cupos_pb2*.py: GENERADOS desde proto/cupos.proto (no se versionan)
    └── routers/
        ├── students.py
        ├── enrollments.py
        ├── courses.py       # lectura de cursos y disponibilidad (referencial)
        └── health.py
```

## Por qué este servicio y no el CLI de Node existente

El repositorio traía un CLI en Node (`matriculas/`) que reproduce a
propósito el problema original: "se matricula sin verificar cupos". Esta
API en Python es la pieza de integración que la tarea pide: expone
matrícula como **API REST pública** y, del lado del servidor, consulta
el gRPC de Cupos para decidir si el curso está disponible antes de
persistir la matrícula. El CLI de Node se deja en el repo sin levantarse
por defecto (`profiles: ["legacy"]` en `docker-compose.yml`) solo como
referencia de "antes de la integración"; el modelo de datos de esta API
reemplaza `codigo_curso` (texto libre, sin validar) por `curso_id`
(entero, siempre el mismo id que usa Cupos), porque ahora sí existe una
fuente de verdad contra la cual validarlo.

## Cómo correrlo

### Con Docker Compose (recomendado)

Desde la raíz del repo:

```bash
python scripts/generar_env.py        # una vez: crea .env con claves aleatorias
docker compose up --build -d
```

(`.env` es **obligatorio**: ya no hay claves por defecto. `scripts/generar_env.py` crea la clave
admin y la contraseña de Postgres aleatorias; el archivo `.env` real **no se versiona ni debe ir en
el ZIP de entrega**.)

La API queda en `http://localhost:8000`. Documentación interactiva
(Swagger UI) en `http://localhost:8000/docs`, esquema OpenAPI crudo en
`http://localhost:8000/openapi.json`.

Todos los endpoints de negocio requieren la cabecera `X-API-Key`. Con tu clave admin
(`MATRICULAS_API_KEYS` en `.env`):

```bash
ADMIN_KEY=$(grep ^MATRICULAS_API_KEYS= .env | cut -d= -f2)
curl -X POST http://localhost:8000/v1/students \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ADMIN_KEY" \
  -d '{"rut":"19011022-3","nombre":"Ana","apellido":"Pérez","email":"ana@example.com"}'
```

### Localmente, sin Docker

```bash
cd matriculas-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Generar los stubs de gRPC desde proto/cupos.proto (una vez, o cuando
# cambie el .proto). El script también corrige el import relativo:
python generar_stubs.py

cp .env.example .env   # ajustar CUPOS_GRPC_TARGET si Cupos corre en otro host/puerto
uvicorn app.main:app --reload --port 8000
```

Requiere que el servicio Cupos esté corriendo (`docker compose up cupos`,
o `python cupos/src/server.py` localmente tras generar sus propios
stubs con `cupos/src/generar.py`).

## Endpoints

Todos bajo `/v1`, todos requieren `X-API-Key` salvo `/v1/health`.

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/v1/students` | Crear estudiante (valida RUT con dígito verificador) → `201` / `409` / `422` |
| GET | `/v1/students/{id}` | Consultar estudiante → `200` / `404` |
| GET | `/v1/students` | Listar (paginado, `offset` ≤ 100 000, filtro `rut`) → `200` |
| DELETE | `/v1/students/{id}` | Anonimizar al estudiante (derecho de supresión; solo admin) → `204` / `403` / `404` |
| GET | `/v1/courses` | Cursos con su disponibilidad, referencial y con caché (`?disponibles=true`) → `200` / `502` / `503` / `504` |
| GET | `/v1/courses/{curso_id}` | Un curso y su disponibilidad (referencial) → `200` / `404` |
| POST | `/v1/enrollments` | Matricular (valida cupos vía Cupos; cabecera opcional `Idempotency-Key`) → `201` / `404` / `409` / `422` / `429` / `502` / `503` / `504` |
| POST | `/v1/enrollments/{id}/revert` | Revertir matrícula (libera el cupo) → `200` / `404` / `409` / `429` / `500` / `502` / `503` / `504` |
| GET | `/v1/enrollments/{id}` | Consultar matrícula → `200` / `404` |
| GET | `/v1/enrollments` | Listar (paginado, filtros `student_id`, `curso_id`, `estado`) → `200` |
| GET | `/v1/health` | Estado del servicio y del circuit breaker hacia Cupos (sin auth) |

El contrato completo, con schemas, ejemplos y todos los códigos de
respuesta, está en el OpenAPI servido en `/openapi.json` / `/docs`, y
versionado en `docs/openapi.json` y `docs/openapi.yaml` (se regeneran con
`python exportar_openapi.py --desde-url http://localhost:8000/openapi.json` (exporta lo que sirve la API real)). Una prueba de contrato (`tests/contract`)
falla si el archivo versionado difiere del que publica la API, y valida que
las respuestas reales cumplan el esquema declarado.

### Formato de error uniforme

Todas las respuestas de error (validación, 401, negocio, 502/503/504, 500)
usan el mismo sobre:

```json
{"error": {"code": "SIN_CUPOS", "message": "El curso 5 no tiene cupos disponibles", "details": null}}
```

Códigos usados: `VALIDATION_ERROR`, `UNAUTHORIZED`, `NOT_FOUND`,
`STUDENT_ALREADY_EXISTS`, `ALREADY_ENROLLED`, `ENROLLMENT_IN_PROGRESS`,
`ENROLLMENT_EXPIRED`, `ENROLLMENT_NOT_CONFIRMED`, `ALREADY_REVERTED`,
`IDEMPOTENCY_KEY_REUSED`, `ENROLLMENT_ANNULLED` (409), `STUDENT_ANONYMIZED` (409),
`TOO_MANY_REQUESTS` (429), `SIN_CUPOS`, `CURSO_NO_ENCONTRADO`,
`CUPOS_NO_DISPONIBLE` (503), `CUPOS_TIMEOUT` (504),
`CUPOS_RESPUESTA_INVALIDA` (502), `PERSISTENCE_ERROR`, `INTERNAL_ERROR`.
(`FORBIDDEN` ya no se usa: ver `docs/adr/D5-autenticacion-api-key.md`.)

## Autenticación y permisos: API Key + OAuth2/JWT local

La API acepta en paralelo las API keys existentes (`X-API-Key`) y tokens de usuario
(`Authorization: Bearer <JWT>`). Las API keys conservan su comportamiento para integraciones
maquina a maquina. El flujo JWT se valida y firma dentro de esta API, sin servicios externos.

| Credencial | Emisión/almacenamiento | Alcance |
|---|---|---|
| API key admin | Variable `API_KEYS` (`MATRICULAS_API_KEYS` en `.env`), obligatoria, ≥24 caracteres | Acceso administrativo total |
| API key estudiante | `POST /v1/students/{id}/api-key`; se almacena solo hash SHA-256 | Recursos propios del estudiante |
| JWT estudiante | OAuth2 password local; contraseña configurada por admin y hash PBKDF2 en `student_credentials` | Recursos propios; token firmado HS256 con expiración |

El administrador puede establecer o reiniciar una contraseña con `PUT /v1/students/{id}/password`
(JSON `{"password":"..."}`, mínimo 12 caracteres). El correo del estudiante es el `username`.
La contraseña no se devuelve y se almacena con PBKDF2-SHA256 y sal aleatoria. Eliminar el acceso
OAuth2 (incluido anonimizar el estudiante) borra la credencial y hace que sus JWT dejen de servir.
La base nueva `student_credentials` se crea al iniciar; las instalaciones existentes reciben la
tabla sin alterar `students` ni requerir una migración destructiva.

**Inicio de sesión** (`application/x-www-form-urlencoded`):

```bash
curl -X PUT localhost:8000/v1/students/<id>/password \
  -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"password":"ClaveSeguraLocal-123"}'
curl -X POST localhost:8000/v1/auth/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=ana@example.com&password=ClaveSeguraLocal-123'
# usar access_token en Authorization: Bearer <access_token>
```

`MATRICULAS_JWT_SECRET` debe ser aleatorio y tener al menos 32 caracteres. `python
scripts/generar_env.py` lo genera junto con la clave admin; `MATRICULAS_JWT_TTL_MINUTES`
define la vigencia (60 por defecto). Un token incluye `sub` (id del estudiante), emisor,
audiencia y expiración. La API verifica la firma HMAC-SHA256 y que la cuenta siga activa.

**Qué puede hacer cada una**

| Operación | Admin | Estudiante (API key personal o JWT) |
|---|---|---|
| `POST /v1/students`, `GET /v1/students` (listar) | ✅ | ❌ 403 |
| `GET /v1/students/{id}` | ✅ cualquiera | ✅ solo el suyo (403 si es otro) |
| `POST /v1/students/{id}/api-key` (emitir), `DELETE` (revocar) | ✅ | ❌ 403 |
| `GET /v1/students/{id}/api-key` (metadatos, sin secreto) | ✅ | ✅ solo la suya |
| `POST /v1/enrollments` | ✅ | ✅ solo con su propio `student_id` (403 si es otro) |
| `GET`/`revert` `/v1/enrollments/{id}` | ✅ | ✅ solo las suyas (una ajena responde **404**) |
| `GET /v1/enrollments` | ✅ todas | ✅ se acota automáticamente a las suyas |

Códigos: `401` = la clave falta o no autentica (inexistente o revocada);
`403` = autenticado pero sin permiso sobre ese recurso. Las matrículas ajenas
responden `404` para no revelar qué ids existen.

**Flujo típico**

```bash
# 1) secretaría (admin) crea al estudiante y le emite su clave
curl -X POST localhost:8000/v1/students/<id>/api-key -H "X-API-Key: $ADMIN_KEY"
#  -> {"api_key": "mat_...", "key_prefix": "mat_Ab3k", ...}   (guardarla: no se vuelve a mostrar)

# 2) el estudiante opera solo sobre lo suyo
curl -X POST localhost:8000/v1/enrollments -H "X-API-Key: mat_..." \
     -H "Content-Type: application/json" -d '{"student_id": "<id>", "curso_id": 1}'

# rotar: revocar y emitir otra
curl -X DELETE localhost:8000/v1/students/<id>/api-key -H "X-API-Key: $ADMIN_KEY"
```

**Sin secretos por defecto (fail-closed).** La API **no arranca** si `API_KEYS` falta, es un valor
público conocido (incluido el `dev-matriculas-key-123` que traían versiones anteriores del repositorio) o mide
menos de 24 caracteres. Genera una con `python scripts/generar_env.py` o
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

**Formato de la clave de estudiante:** `mat_<id público>.<secreto>`. `key_prefix` es el id público (8 hex
aleatorios): se puede mostrar en listados y logs sin revelar nada del secreto.

**Abuso:** los intentos de autenticación fallidos se limitan por IP (`AUTH_FAIL_LIMIT` por
`AUTH_FAIL_WINDOW_SECONDS`) y un estudiante con clave propia tiene un tope de escrituras por minuto
(`WRITE_RATE_LIMIT_PER_MINUTE`); ambos responden `429` con `Retry-After`. El estado es por proceso (ver D5).

**Sin TLS.** La API habla HTTP: por defecto se publica solo en `127.0.0.1`. Para exponerla hay que poner un
proxy TLS delante (D5). **Privacidad:** `DELETE /v1/students/{id}` anonimiza los datos personales de un
estudiante conservando sus matrículas como historial.

Implementación: `app/routers/auth.py` emite los tokens; `app/security.py` valida API key o JWT
y crea el mismo `Principal` para ambos. `require_admin`, `exigir_acceso_a_estudiante` y las rutas
de matrículas aplican las reglas de propietario existentes. Las tablas `api_keys` y
`student_credentials` se crean con `create_all`.

## Manejo de fallas del gRPC de Cupos

Requisito: la API debe comportarse de forma razonable si Cupos está caído
o lento. El razonamiento completo está en `docs/adr/D4_resiliencia_y_modos_de_falla.md`;
aquí el resumen operativo (implementado en `grpc_client.py`,
`circuit_breaker.py`, `routers/enrollments.py` y `reconciler.py`).

| Situación | HTTP | Código | ¿Qué pasó con el cupo? |
|---|---|---|---|
| Cupos no responde en `CUPOS_GRPC_TIMEOUT_SECONDS` (2 s) | **504** | `CUPOS_TIMEOUT` | **Incierto**: Cupos pudo ocuparlo. La matrícula queda `PENDIENTE`. |
| Cupos inalcanzable: conexión rechazada / sin resolver | **503** | `CUPOS_NO_DISPONIBLE` | Ninguno: la solicitud nunca salió; se borra la intención (no queda `PENDIENTE`). |
| Cupos inalcanzable: conexión rota a mitad de la llamada | **503** | `CUPOS_NO_DISPONIBLE` | Incierto (queda `PENDIENTE`). |
| Circuito abierto (fail-fast) | **503** | `CUPOS_NO_DISPONIBLE` | Ninguno: ni se intentó la llamada; no se persiste nada. |
| Error interno de Cupos | **502** | `CUPOS_RESPUESTA_INVALIDA` | Se trata como incierto **por precaución** (queda `PENDIENTE`; ver D4: el ADR anterior decía "ninguno" y el código hacía otra cosa). |

Mecanismos:

1. **Timeout por llamada**: ninguna matrícula espera indefinidamente.
2. **Circuit breaker** (`CUPOS_CB_FAILURE_THRESHOLD` fallas consecutivas →
   `OPEN` durante `CUPOS_CB_RECOVERY_SECONDS`). Solo cuentan las fallas de
   *disponibilidad* (timeout, `UNAVAILABLE`, `RESOURCE_EXHAUSTED`); los
   errores de dominio e internos de Cupos significan que sí respondió.
3. **Fail-closed**: sin confirmación de Cupos, la matrícula nunca queda
   `CONFIRMADA`.
4. **Matrícula en dos pasos**: se registra una intención `PENDIENTE` (un
   índice único parcial sobre `(student_id, curso_id)` de los estados activos
   impide duplicados concurrentes sin tocar Cupos) y luego se ocupa el cupo.
   Rechazos definitivos (sin cupos, curso inexistente) borran la intención.
5. **Idempotencia de punta a punta**: cada matrícula tiene una clave interna
   que se envía a `ocuparCupo`/`liberarCupo`, y el cliente puede enviar
   `Idempotency-Key` para reintentar: la misma clave devuelve la misma
   matrícula (`Idempotent-Replayed: true`) y nunca consume otro cupo.
6. **Conciliación**: una matrícula `PENDIENTE` se resuelve reintentando con la
   misma `Idempotency-Key` dentro de `PENDING_TTL_SECONDS / 2`; si no, pasado
   `PENDING_TTL_SECONDS` un hilo en segundo plano (`reconciler.py`, cada
   `RECONCILE_INTERVAL_SECONDS`) la anula (`estado=ANULADA`, con `motivo`) y
   se asegura de liberar el cupo.
7. **Compensación**: si falla el guardado local después de ocupar el cupo, se
   libera el cupo con la misma clave.
8. **Lecturas sin dependencia**: consultar o listar estudiantes y matrículas
   funciona aunque Cupos esté caído.

`GET /v1/health` expone el estado del circuit breaker
(`CLOSED`/`OPEN`/`HALF_OPEN`).

### Cómo se probó esto

La suite `tests/` (ver README raíz) cubre, contra el sistema real:

- Matrícula, rechazo por falta de cupos, curso inexistente, duplicado y
  reversión; **matricular → revertir → matricular → revertir** el mismo curso.
- **Concurrencia**: 6 matrículas simultáneas del mismo estudiante → un `201` y
  cinco `409`, con un solo cupo consumido en Cupos; 6 reversiones simultáneas →
  un `200`, cinco `409` y un solo cupo liberado; estudiantes distintos
  compitiendo por un curso de capacidad baja no lo sobrepasan.
- **Idempotencia**: repetir la `Idempotency-Key` no duplica; reutilizarla con
  otro curso da `422`.
- **Fallas**: Cupos apagado → `503`/`504` sin colgarse y sin matrícula
  `CONFIRMADA`; Cupos lento → `504`, el cupo huérfano se concilia, y tres
  reintentos con la misma clave consumen un solo cupo.
- **Contrato**: el OpenAPI versionado coincide con el publicado y las
  respuestas reales cumplen el esquema declarado.

## Notas de diseño adicionales

- **Privacidad**: Cupos nunca recibe datos personales (solo `curso_id` y una
  clave opaca); el RUT se valida (módulo 11) y se guarda normalizado; los
  errores de validación no devuelven el dato enviado; Postgres y el gRPC de
  Cupos no se publican en el host; una clave de estudiante solo accede a los
  datos de ese estudiante (no puede listar ni leer a otros). Ver "Consideraciones de contexto" del informe.
- **Esquema existente**: al arrancar sobre una base Postgres de la versión
  anterior, `database.py` migra la tabla `enrollments` (quita la restricción
  única antigua, agrega `client_key` y los índices). Con SQLite local, borra
  el archivo `matriculas.db` para recrearlo.
- **Aislamiento de datos (principio T5 del repo)**: esta API usa su
  propia base de datos (`matriculas-db`, Postgres, vía `DATABASE_URL`;
  también soporta SQLite para desarrollo local rápido) y nunca accede
  directo a la base de Cupos — todo pasa por el contrato gRPC.
- **Semántica HTTP**: `POST` para creación (`201` + recurso creado),
  `GET` para lectura (`200`/`404`), acciones de negocio no
  idempotentes-por-URL como revertir se modelan como sub-recurso de
  acción (`POST /v1/enrollments/{id}/revert`) en vez de sobrecargar
  `DELETE` con semántica de "anular" (la matrícula no se borra: queda
  auditada con `estado=ANULADA`).
- **Paginación** simple por `limit`/`offset` en los listados, con
  `total` en la respuesta.
