# Pagos — cobro con Transbank Webpay Plus

Servicio de la saga de matrícula que cobra y reembolsa con **Transbank Webpay Plus**. Por defecto usa el
**ambiente de integración gratuito** de Transbank con sus credenciales públicas de pruebas: no cobra dinero
real y no requiere contrato. Base propia (SQLite en el volumen `pagos_data`).

## Cómo encaja en la saga

Webpay Plus no cobra servidor a servidor: el comprador paga en un formulario de Transbank. Por eso la
saga `RESERVAR → COBRAR → INSCRIBIR` **espera** en `COBRAR`:

1. `POST /v1/matriculas` → el orquestador reserva el cupo y pide el cobro a Pagos → **202 `ESPERANDO_PAGO`** con `pago_url`.
2. El comprador abre `pago_url` (→ Webpay), paga y Webpay lo devuelve a `/v1/pagos/retorno`.
3. Pagos confirma (commit) con Transbank y avisa al orquestador; si el aviso se pierde, el orquestador consulta a Pagos solo.
4. `COBRADO` → se inscribe y la saga queda `COMPLETADA`. Pago rechazado, anulado o vencido → se libera el cupo (`COMPENSADA`). Si la inscripción falla tras pagar → **reembolso** en Transbank y compensación en orden inverso.

Diseño, contrato y diagramas: [`docs/saga.md`](../docs/saga.md) (ADR-8).

## Probar con el ambiente de integración de Transbank

Requiere internet desde los contenedores.

```bash
python scripts/generar_env.py
docker compose up --build -d
```

1. Crea un estudiante y obtén su JWT (o usa una API key M2M; ver `matriculas-api/README.md`) y matricula:

   ```bash
   curl -s -X POST http://localhost:8000/v1/matriculas \
     -H "X-API-Key: $MATRICULAS_API_KEYS" -H "Idempotency-Key: $(uuidgen)" \
     -H "Content-Type: application/json" \
     -d '{"alumno_id": "<ID_DEL_ESTUDIANTE>", "curso_id": 1, "monto": 15000}'
   ```

2. Abre en el navegador la `pago_url` de la respuesta (`http://localhost:8000/v1/pagos/ir/…`). Te lleva a Webpay.
3. Paga con las tarjetas de pruebas publicadas por Transbank (verifícalas en [transbankdevelopers.cl](https://www.transbankdevelopers.cl/documentacion/como_empezar#tarjetas-de-prueba)):

   | Resultado | Tarjeta | Datos |
   |---|---|---|
   | Aprobada | VISA `4051 8856 0044 6623` | CVV `123`, cualquier fecha futura; en la autenticación RUT `11.111.111-1`, clave `123` |
   | Rechazada | MasterCard `5186 0595 5959 0568` | CVV `123`, cualquier fecha futura |

   También puedes **anular** la compra en el propio formulario: la saga se compensa.
4. Consulta el resultado: `GET /v1/sagas/{id}` → `COMPLETADA` (aprobada) o `COMPENSADA` con `motivo_fallo` `COBRAR:RECHAZADO` / `COBRAR:ABORTADO`.

El plazo para pagar es `PAGOS_TTL_SECONDS` (600 s). `PAGOS_PUBLIC_BASE_URL` debe ser la URL del gateway **vista desde el navegador**
(por defecto `http://localhost:8000`): Webpay devuelve allí al comprador.

> Tiempos: `crear` y `reembolsar` ocurren dentro del timeout de la saga (`SAGA_TIMEOUT_SECONDS`, 2 s por defecto). Webpay real
> suele tardar 0,5–1,5 s; si ves `502`/circuito abierto sin haber inducido fallas, sube `SAGA_TIMEOUT_SECONDS=3` y `TBK_TIMEOUT_SECONDS=2.5` en `.env`.

## Sin internet: simulador de Transbank

`tests/support/fake_transbank.py` implementa el mismo contrato REST con un formulario "Aprobar / Rechazar / Anular":

```bash
python tests/support/fake_transbank.py --host 0.0.0.0 --port 9100 --public-url http://localhost:9100
# en .env:
#   TBK_BASE_URL=http://host.docker.internal:9100
```

(`TBK_COMMERCE_CODE` y `TBK_API_KEY` ya coinciden con los valores por defecto del simulador.) Es un doble de pruebas, no Transbank.

## Provocar las fallas (escenario de Transbank lento o caído)

Las rutas `/internal` no pasan por el gateway; se usan dentro de la red de Docker:

```bash
# Transbank lento: 3 s de latencia (mayor que el timeout → timeouts reales); falla=true simula Transbank caído
docker compose exec pagos python -c "import urllib.request,json,os; r=urllib.request.Request('http://localhost:8000/internal/fault', data=json.dumps({'latencia_ms':3000,'falla':False}).encode(), headers={'X-Internal-Key':os.environ['INTERNAL_KEY'],'Content-Type':'application/json'}, method='PUT'); print(urllib.request.urlopen(r).read())"
# Restablecer: latencia_ms 0 y falla false
```

Qué observar (`docker compose exec pagos python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode())"`):

- Los timeouts cuentan como fallas; tras `PAGOS_CB_THRESHOLD` (5) el **circuito de Transbank se abre** (`circuito_estado{dependencia="TRANSBANK",estado="OPEN"} 1`) y Pagos responde **503 de inmediato** en lugar de esperar.
- El orquestador compensa cada matrícula (cupo liberado, sin cobro) y, tras sus propios umbrales, abre su circuito hacia Pagos (`GET /metrics` del orquestador).
- El **bulkhead** (`PAGOS_BULKHEAD`, 8) rechaza el exceso de llamadas simultáneas a Transbank (`bulkhead_rechazos_total`).
- Pasado `PAGOS_CB_RECOVERY_SECONDS` el circuito deja pasar una sonda y, si Transbank responde, se cierra.

## API

| Ruta | Quién | Descripción |
|---|---|---|
| `POST /v1/payments` | orquestador (`X-API-Key`) | Crea el cobro (idempotente por `Idempotency-Key`). `PENDIENTE` + `redirect_url` |
| `POST /v1/payments/refund` | orquestador | Compensación: reembolsa, cancela un cobro abierto o deja una cancelación anticipada |
| `GET /v1/payments/by-key/{clave}` | orquestador | Estado del cobro (fuente de verdad) |
| `GET /v1/pagos/ir/{id}` · `GET/POST /v1/pagos/retorno` | comprador (públicas vía gateway) | Redirección a Webpay y retorno con `token_ws` |
| `PUT/GET /internal/fault` | operación (`X-Internal-Key`) | Latencia o caída inducida de Transbank |
| `GET /metrics` · `GET /v1/health` | operación | Pagos por estado, circuito, bulkhead y duración por operación |

## Seguridad y garantías

- No se guardan datos de tarjeta: solo el token de la transacción, códigos de resultado y estado.
- Las rutas del comprador no tienen credencial (Webpay devuelve al navegador): la autoridad es Transbank. El retorno **siempre se confirma con su API**; monto y orden de compra se verifican contra lo pedido.
- Las transiciones de estado son atómicas: un retorno y una compensación simultáneos no se contradicen; un pago cancelado o vencido nunca se confirma, aunque el comprador pague tarde.
- Un commit incierto (timeout tras confirmar) se resuelve consultando el estado de la transacción; nunca se reembolsa ni se declara cobrado sin confirmación.
- Claves propias sin valor por defecto (`PAGOS_API_KEY`, `PAGOS_CALLBACK_KEY`, `INTERNAL_KEY`); el servicio no arranca sin ellas, ni con credenciales de integración apuntando a producción.
- Producción: `TBK_BASE_URL=https://webpay3g.transbank.cl` con `TBK_COMMERCE_CODE` y `TBK_API_KEY` propios (otorgados por Transbank).

## Pruebas

```bash
python -m pytest tests/unit/test_pagos.py tests/unit/test_saga_pago.py tests/unit/test_gateway_pagos.py -q
python -m pytest tests/integration/test_pagos_procesos.py -q   # procesos reales, sin Docker
```
