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
| `GET /v1/pagos/ir/{id}` | Pagos | **Pública** (sin credencial): el comprador abre `pago_url` y se le redirige a Webpay. Rate limit por IP. |
| `GET`/`POST /v1/pagos/retorno` | Pagos | **Pública**: Webpay devuelve aquí al comprador con `token_ws`. Pagos confirma con Transbank; el contenido de la petición nunca se da por cierto. Rate limit por IP. |
| `GET /health` | Gateway | Salud del gateway. |

El BFF llama en paralelo al RPC `listarCursosDisponibles` de Cupos por gRPC y a
`GET /v1/enrollments?limit=100` de Inscripción por REST. Así compone datos de dos servicios de
dominio distintos. Reenvía el Bearer JWT o API key a Inscripción, para que el servicio mantenga sus
controles de autorización y alcance por estudiante.

`BFF_UPSTREAM_TIMEOUT_SECONDS` es el presupuesto total de la agregación, medido
con un único deadline monotónico desde la entrada al dashboard. Incluye el tiempo
de cola y la espera de ambos resultados; no se reinicia al pasar de Cupos a
Inscripción. Al vencer responde `504` con código `BFF_DEPENDENCY_TIMEOUT`.

Cada transporte recibe como timeout el 80 % del tiempo restante. Además, HTTP
usa `httpx.AsyncClient` bajo `asyncio.timeout` para limitar la operación completa,
incluidos redirecciones, cabeceras y cuerpo. Así, una respuesta que envía bytes
continuamente no puede prolongar la llamada reiniciando el timeout de lectura.
El BFF cancela la tarea HTTP y el RPC nativo de gRPC cuando vence el presupuesto
o falla la otra dependencia. `Future.cancel()` se usa únicamente para evitar
que comiencen los trabajos que aún estén en cola.

`BFF_MAX_WORKERS` acota el pool (mínimo 2). Se admiten como máximo
`floor(BFF_MAX_WORKERS / 2)` agregaciones simultáneas por proceso, reservando dos
workers por dashboard. Sin capacidad se responde `503`, código `BFF_OVERLOADED`
y `Retry-After: 1`, sin iniciar nuevas llamadas. El permiso se devuelve cuando
ambos trabajos realmente terminan, aunque el dashboard ya haya respondido con
error. `/health` no utiliza ese pool. Estos límites no se comparten entre réplicas.

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
- `PAGOS_URL`, `SAGA_PAGO_POLL_SECONDS`, `SAGA_TIMEOUT_SECONDS`, `SAGA_BULKHEAD`, `SAGA_CB_THRESHOLD`,
  `SAGA_CB_RECOVERY_SECONDS`, `SAGA_RETRIES`, `SAGA_BACKOFF_SECONDS` y valores de reconciliación.

El servicio Pagos está en [`../pagos/`](../pagos/README.md) (Transbank Webpay Plus). El gateway solo enruta hacia él las dos rutas
públicas del comprador; su API de servicio (`/v1/payments…`) y `/internal` nunca se exponen. Variables: `PAGOS_SERVICE_URL`
(gateway), `PAGOS_API_KEY`, `PAGOS_CALLBACK_KEY`, `PAGOS_PUBLIC_BASE_URL`, `TBK_*` y `PAGOS_TTL_SECONDS`; contrato en [`saga.md`](saga.md).

## Verificación de H4

```bash
python -m pytest tests/integration/test_h4_bff_deadline.py tests/integration/test_gateway_bff.py -q --tb=short
docker compose up -d --build bff
```

Las pruebas usan HTTP y gRPC locales con los clientes reales. Comprueban el plazo
total, cancelación durante cabeceras y cuerpo lentos, liberación de ambos workers,
cancelación del RPC ante una falla HTTP, rechazo de sobrecarga, recuperación e
integridad de datos y credenciales. Generan los stubs en una carpeta temporal;
no necesitan Docker ni modifican las bases existentes. No requieren variables
nuevas. El rebuild instala el cliente HTTP indicado en `bff/requirements.txt`.
