# Integridad y bulkhead con dependencias sanas (20261122_163339)

## A) POST /v1/enrollments evita la saga

| Caso | Código | Estado | Esperado |
|---|---|---|---|
| POST /v1/enrollments curso 5 (curso con 2 cupos) | 201 | CONFIRMADA | rechazo (la regla exige cupo y pago) |
| POST /v1/enrollments curso 999 (curso INEXISTENTE) | 201 | CONFIRMADA | rechazo (la regla exige cupo y pago) |

Una inscripción `CONFIRMADA` sin reservar cupo ni cobrar rompe las dos reglas de negocio del enunciado.

## B) Bulkhead del orquestador (capacidad 8) con dependencias SANAS

| Simultáneas | COMPLETADA | COMPENSADA | COMPENSACION_PENDIENTE | % completadas | Rechazos del bulkhead |
|---:|---:|---:|---:|---:|---:|
| 4 | 4 | 0 | 0 | 100.0 | 0 |
| 8 | 8 | 0 | 0 | 100.0 | 0 |
| 12 | 8 | 0 | 4 | 66.7 | 16 |
| 24 | 14 | 6 | 4 | 58.3 | 31 |
| 48 | 38 | 5 | 5 | 79.2 | 29 |

Hasta la capacidad (8) todas se completan; por encima, el rechazo del bulkhead se trata como falla incierta y la matrícula se compensa aunque ninguna dependencia esté caída.
