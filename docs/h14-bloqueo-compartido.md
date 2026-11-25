# H14 — Aislar el límite de fallos de autenticación tras el Gateway

## Riesgo

Inscripción cuenta los fallos por la dirección del socket. Cuando las solicitudes
llegan desde el Gateway, varios clientes comparten esa dirección; los intentos de
login incorrectos de uno pueden causar respuestas 429 a otro que presenta un JWT
válido.

## Regresión

`tests/integration/test_h14_gateway_rate_limit_auth.py` envía solicitudes a través
del Gateway hacia un doble temporal de Inscripción con el límite de fallos en
memoria. Comprueba que 20 fallos de login y el bloqueo del cliente A no afectan al
JWT válido del cliente B. A también falsifica `X-Forwarded-For` para comprobar que
el Gateway no confíe en una IP proporcionada por el navegador.

## Mitigación

El Gateway toma la IP del socket de entrada y adjunta esa IP a las solicitudes de
Inscripción junto con una firma HMAC-SHA256 basada en el secreto JWT compartido.
Inscripción usa la IP recibida solo si la firma es válida y el valor tiene formato
de IP; de lo contrario conserva la IP del socket. El Gateway no reenvía la cabecera
`X-Forwarded-For` aportada por el cliente.

Ejecutar con:

```sh
.venv/bin/python -m pytest -q tests/integration/test_h14_gateway_rate_limit_auth.py
```

En la versión defectuosa, la solicitud válida final de B recibe 429 debido a los
fallos de autenticación de A que Inscripción asocia a la conexión compartida del
Gateway.
