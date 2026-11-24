# H5: arranque único y actualización de persistencia

Desde un clon sin `.env`, con Docker Compose disponible:

```bash
docker compose up --build -d
```

El servicio `inicializar` termina antes de PostgreSQL y de los consumidores de
credenciales. Genera ocho secretos independientes con `secrets.token_urlsafe` y
persiste el conjunto en `secretos_data`. Un lock y escrituras atómicas permiten
reinicios e inicializaciones concurrentes sin rotaciones accidentales. Un almacén
corrupto o incompleto detiene el arranque para evitar perder las claves existentes.

Los valores explícitos de un `.env` previo se adoptan; las entradas ausentes se
generan una vez. `scripts/generar_env.py` sigue disponible para configurar antes
del primer arranque. Después de inicializar, generar otro `.env` con claves nuevas
constituye una rotación: no es un paso de preparación necesario para levantar.

## Distribución de credenciales

El almacén maestro solo está montado en el inicializador. Este entrega archivos
JSON por volúmenes privados, montados read-only en sus destinatarios:

| Volumen | Destinatario | Datos |
|---|---|---|
| `config_db` | PostgreSQL | Contraseña para `POSTGRES_PASSWORD_FILE`. |
| `config_api` | Inscripción | URL de su base, claves API, JWT, interna y escritor. |
| `config_orquestador` | Orquestador | Claves de integración, escritor, Pagos y callback. |
| `config_pagos` | Pagos | Sus claves y la credencial interna. |
| `config_gateway` | Gateway | API keys, JWT y clave del orquestador. |
| `config_legacy` | CLI legado | Contraseña de su base. |
| `config_runner` | Perfiles tests/experimentos | Credenciales de preparación, integración e inyección de fallas. |

BFF y Cupos no reciben esos secretos. La clave de escritor de H1 no se entrega al
Gateway ni a Pagos. Las imágenes no incorporan `.env`. El cargador ejecuta el
proceso con su configuración sin incluir secretos en argumentos ni logs, y
permite conservar los overrides explícitos del entorno.

Los runners reciben las claves de pruebas porque son perfiles privilegiados con
acceso al socket Docker; no arrancan como servicios públicos de la aplicación.

Solo Gateway publica puerto, en `127.0.0.1:8000`. Los perfiles de pruebas siguen
siendo opcionales; su override publica los puertos internos únicamente en loopback.

## PostgreSQL: comprobar antes de reparar

```bash
python scripts/reparar_postgres.py --comprobar
```

Este modo solo lee la configuración, el contenedor y la imagen local, compara
usuario/base/origen de contraseña, prueba autenticación TCP y verifica la versión
mayor declarada. No cambia roles, no arranca ni recrea contenedores, no reescribe
`.env` y no imprime credenciales. Devuelve un código distinto de cero si detecta
desfase. Compara con la imagen local; no consulta actualizaciones del registry.

Si el problema es una contraseña del volumen distinta de la configuración actual:

```bash
python scripts/reparar_postgres.py
```

La reparación explícita comprueba TCP antes de cambiar la contraseña. Si falla,
sincroniza el rol mediante el socket local y vuelve a verificar TCP. Admite tanto
la configuración histórica por entorno como `POSTGRES_PASSWORD_FILE`. Después
reconstruye los consumidores con la versión actual del repo. No elimina volúmenes.
Los cambios de versión mayor, usuario o base requieren revisar una migración de
PostgreSQL; esta herramienta no realiza upgrades mayores ni crea roles nuevos.

El healthcheck de PostgreSQL también autentica por TCP: `pg_isready` por sí solo
no demuestra que la contraseña de la aplicación funcione.

## Base de sagas anterior a Webpay

El arranque actualizado del orquestador migra el esquema reconocido anterior a
Webpay antes de atender o conciliar. Conserva filas, pasos, claves de idempotencia,
índices y triggers. Añade `pago_url`, `pago_expira_at`, el estado `ESPERANDO_PAGO`
y su participación en el índice de sagas activas.

Antes de modificar tablas guarda una copia consistente SQLite en el mismo volumen:
`/data/sagas.db.pre-webpay.<fecha>-<id>.bak`, con permisos `0600`. La modificación
usa una transacción, bloqueo de escritura y exclusión entre migradores. Comprueba
integridad, referencias y recuento de sagas antes del commit; ante un error revierte.
Un esquema desconocido o de versión posterior se rechaza conservando los datos.
Repetir el arranque con el esquema actual no vuelve a migrar ni crea otro backup.

El diagnóstico `verificar_esquema` sigue siendo de solo lectura. Si se necesita
restaurar un backup, detener primero el orquestador y restaurar el archivo SQLite
con los mismos permisos, conservando el original para investigar. No es necesario
eliminar el volumen de sagas ni los volúmenes de los otros servicios.

## Pruebas

```bash
python -m pytest tests/integration/test_h5_arranque_limpio.py tests/unit/test_h5_diagnostico_persistencia.py tests/unit/test_h5_bootstrap_migracion.py -q --tb=short
```

El smoke usa Docker real con un proyecto aleatorio, volúmenes nuevos y puerto
efímero de Gateway; elimina solo sus propios recursos. Su caché de metadatos de
Buildx también es temporal. Las pruebas de diagnóstico simulan el control Docker,
y las de migración usan SQLite real con datos, incluyendo un fallo que exige
rollback y conservación del backup. No se modifica la instalación habitual para
ejecutar estas pruebas.

El runner incluye Git para exportar la copia limpia y el CLI Docker 29 con Compose
y Buildx de la misma imagen oficial. La sonda HTTP del smoke se ejecuta en Gateway
para funcionar también cuando pytest corre dentro del runner, sin extraer sus
credenciales. El puerto público y su interfaz se verifican mediante Docker.
