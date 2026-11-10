# API Gateway y BFF

## Topología y rutas

`gateway` es el único servicio con `ports:`. Enruta las rutas de negocio a `matriculas-api`,
`POST /v1/matriculas` y `GET /v1/sagas/{id}` al `orquestador`, y `/v1/bff/*` al BFF web.
`cupos` permanece interno en gRPC y el perfil `legacy` (`matriculas`) no participa en este flujo.

| Ruta pública | Destino | Uso |
|---|---|---|
| `POST /v1/auth/token` | `matriculas-api` | Emite JWT OAuth2; rate limit por IP. |
| `/v1/students`, `/v1/courses`, `/v1/enrollments` | `matriculas-api` | API de dominio. |
| `GET /v1/bff/dashboard` | BFF web | Agrega cursos disponibles y matrículas del usuario. |
| `POST /v1/matriculas` | Orquestador | Ejecuta Saga; el gateway agrega la API key interna del orquestador. |
| `GET /v1/sagas/{id}` | Orquestador | Consulta el estado de una Saga. |
| `GET /health` | Gateway | Salud del gateway. |

El BFF llama en paralelo al RPC `listarCursosDisponibles` de Cupos por gRPC y a
`GET /v1/enrollments?limit=100` de Inscripción por REST. Así compone datos de dos servicios de
dominio distintos. Reenvía el Bearer JWT o API key a Inscripción, para que el servicio mantenga sus
controles de autorización y alcance por estudiante. Si una dependencia excede
`BFF_UPSTREAM_TIMEOUT_SECONDS`, responde `504`.

Para una Saga iniciada con JWT, el gateway exige que `alumno_id` sea el `sub` del token y oculta
con `404` las sagas que pertenecen a otro estudiante. La API key M2M permite operaciones de
administración e integración; `/metrics` requiere esa credencial.

Ejemplo de llamada del portal con una sesión existente:

```bash
curl -H "Authorization: Bearer $JWT" \
  http://localhost:8000/v1/bff/dashboard
```

El inicio de sesión usa el formulario OAuth2 (`username` es el correo del estudiante):

```bash
curl -X POST http://localhost:8000/v1/auth/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=$EMAIL" \
  --data-urlencode "password=$PASSWORD"
```

## Autenticación y límites

El gateway valida localmente la firma HS256, emisor, audiencia y expiración de los JWT emitidos por
`matriculas-api`. Para M2M compara `X-API-Key` con las claves definidas en `MATRICULAS_API_KEYS`.
El servicio vuelve a validar las credenciales al recibirlas; la verificación de gateway establece la
puerta pública y la del servicio conserva autorización por recurso. `POST /v1/auth/token` es la única
ruta de negocio sin credencial previa y queda limitada por IP.

Las solicitudes autenticadas se limitan por identidad en una ventana móvil en memoria del proceso:
`GATEWAY_RATE_LIMIT` solicitudes cada `GATEWAY_RATE_WINDOW_SECONDS`. Con una instancia, este límite
es compartido por sus rutas; para varias réplicas se necesitaría un almacén distribuido. Las llamadas
HTTP del gateway tienen límite `GATEWAY_PROXY_TIMEOUT_SECONDS`.

## Variables de entorno

`python scripts/generar_env.py` crea los secretos de API, JWT y Saga localmente. No versionar `.env`.
Los valores configurables están en `.env.example`:

- `MATRICULAS_API_KEYS`, `MATRICULAS_JWT_SECRET`, `JWT_ISSUER`, `JWT_AUDIENCE`.
- `ORCHESTRATOR_API_KEY` (credencial interna inyectada al enrutar la Saga).
- `GATEWAY_RATE_LIMIT`, `GATEWAY_RATE_WINDOW_SECONDS`, `GATEWAY_PROXY_TIMEOUT_SECONDS`, `API_BIND`.
- `BFF_UPSTREAM_TIMEOUT_SECONDS`, `BFF_MAX_WORKERS`.
- `PAGOS_URL`, `SAGA_TIMEOUT_SECONDS`, `SAGA_BULKHEAD`, `SAGA_CB_THRESHOLD`,
  `SAGA_CB_RECOVERY_SECONDS`, `SAGA_RETRIES`, `SAGA_BACKOFF_SECONDS` y valores de reconciliación.

El servicio Pagos no está implementado en este repositorio. La Saga espera que `PAGOS_URL` apunte a
un servicio compatible con los endpoints de cobro y reembolso documentados en [`saga.md`](saga.md).
