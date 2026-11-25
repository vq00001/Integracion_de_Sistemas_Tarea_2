# H11: circuitos Pagos → Transbank por operación

`Guard` mantiene circuitos independientes para `crear`, `confirmar`, `estado` y
`reembolsar`. Cada operación conserva su contador de fallas, plazo de recuperación
y sonda `HALF_OPEN`. El nombre de operación que ya enviaba `Webpay` selecciona
el circuito; no se cambian las rutas ni el protocolo HTTP de la pasarela.

Con `PAGOS_CB_THRESHOLD=5`, cinco fallas de creación abren su circuito aunque
entre ellas haya consultas, confirmaciones o reembolsos exitosos. Las siguientes
creaciones reciben `503` y `Retry-After`, sin ejecutar el cliente. Los rechazos
locales conservan `seguro_no_enviado=True`, para que la idempotencia de H9 permita
continuar una petición cuando se recupere el circuito.

Abrir creación no bloquea las otras operaciones. Sus resultados tampoco abren,
cierran ni restablecen el circuito de creación. La misma independencia se aplica
en las otras direcciones. Una respuesta de negocio 4xx mantiene el comportamiento
anterior: acredita disponibilidad de su propia operación y no se cuenta como caída.

## Recuperación y recursos

Después de `PAGOS_CB_RECOVERY_SECONDS`, cada circuito admite una sola sonda de su
operación. Un éxito ajeno no sustituye esa sonda. Un éxito propio cierra el circuito;
una nueva falla lo reabre y comienza otro plazo de recuperación.

Los umbrales y plazos existentes se aplican a los cuatro circuitos. El estado sigue
siendo local a cada instancia de Pagos, como antes. No cambia la persistencia ni
se agrega una migración.

El bulkhead sigue siendo compartido: `PAGOS_BULKHEAD` limita el total de llamadas
en curso, no concede esa capacidad a cada operación. La configuración de timeouts
y la inyección de fallas también se mantienen. Los reembolsos y las deudas de H10
conservan sus reservas persistentes y reglas de conciliación.

## Métricas y compatibilidad

Las familias nuevas exponen el estado y los rechazos por operación:

```text
transbank_circuito_estado{operacion="crear",estado="OPEN"} 1
transbank_circuito_estado{operacion="estado",estado="CLOSED"} 1
transbank_circuito_rechazos_total{operacion="crear"} 2
```

Cada operación publica las tres series `CLOSED`, `OPEN` y `HALF_OPEN`, con una
sola en 1. El exportador copia los contadores bajo el lock del Guard y toma una
lectura de estado por circuito, evitando recorrer diccionarios que cambian durante
las peticiones.

`guard.breaker`, `guard.available` y la serie histórica
`circuito_estado{dependencia="TRANSBANK",...}` representan **crear**. Esa serie
permite que el experimento actual siga midiendo la dependencia de creación sin
confundir una consulta abierta con una falla de creación. No se agregan estados
por operación a esa misma familia; se publican en `transbank_circuito_estado`.

`circuito_rechazos_total{dependencia="TRANSBANK"}` conserva el total de rechazos
de todos los circuitos. Es igual a la suma de los nuevos contadores por operación.
El contador de rechazos del bulkhead conserva su significado y familia anteriores.

H13 separa admisiones, fallas artificiales e inicios del cliente y muestrea los
cuatro circuitos; sus fronteras y la compatibilidad histórica se describen en
[Métricas de Transbank](h13-metricas-transbank.md). Los CSV e informes históricos
se conservan. La evidencia integrada se regenerará al reunir los frentes.

## Validación

Desde la raíz del repositorio, con simuladores y bases temporales:

```bash
PYTHONDONTWRITEBYTECODE=1 API_KEY=h11-admin-local-testing-key-at-least-32 \
.venv/bin/python -m pytest \
  tests/unit/test_h11_breaker_transbank.py \
  tests/integration/test_h11_breaker_pagos.py \
  tests/unit/test_saga_fallas_experimento.py -q -p no:cacheprovider
```

La regresión general utiliza procesos propios de Inscripción y Cupos y excluye
H5 y las pruebas que administran Docker:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/support/validar_h7.py
```
