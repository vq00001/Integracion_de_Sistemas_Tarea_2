# H15 — API keys personales a través del Gateway

## Riesgo

Inscripción emite API keys personales y valida su propietario, expiración y
revocación. El Gateway, en cambio, reconoce solo las claves administrativas
configuradas, por lo que rechaza claves personales válidas antes de que Inscripción
pueda aplicar esos controles.

## Regresión

`tests/integration/test_h15_gateway_api_keys_personales.py` recorre el Gateway y
usa un doble temporal de Inscripción con claves válidas, inválidas, vencidas y
revocadas. También comprueba el acceso a datos propios y ajenos y que las claves
administrativas configuradas sigan funcionando.

Ejecutar con:

```sh
.venv/bin/python -m pytest -q tests/integration/test_h15_gateway_api_keys_personales.py
```

En la versión defectuosa, el Gateway rechaza la clave personal vigente antes de
reenviarla a Inscripción.

## Mitigación

El Gateway reconoce el formato emitido para claves personales y reenvía la
credencial original solo por el proxy a Inscripción. No deriva una identidad del
contenido de la clave: Inscripción sigue verificando su hash, propietario,
expiración y revocación. La clave personal no se considera M2M y no habilita las
rutas de BFF, Saga, métricas ni el inicio de matrícula delegado por el
Orquestador. Las claves administrativas configuradas mantienen su flujo.

La regresión completa de H15 pasa junto con las pruebas de Gateway para H8 y
H14: 43 pruebas aprobadas.
