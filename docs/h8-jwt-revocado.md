# H8 — Revocación de JWT tras anonimización

## Riesgo

El Gateway valida la firma y el vencimiento localmente. La anonimización elimina la
credencial de estudiante en Inscripción, por lo que Inscripción rechaza el JWT; sin
embargo, si el Gateway no confirma la vigencia, puede sustituirlo por la clave
interna del orquestador y permitir nuevas matrículas.

## Regresión

`tests/integration/test_h8_gateway_jwt_revocado.py` recorre el Gateway y usa la
ruta existente de Inscripción para comprobar que un JWT continúa asociado a un
estudiante vigente. Incluye JWT válido, vencido, con firma inválida, revocado por
anonimización y un intento de matricular a otro estudiante.

Ejecutar con:

```sh
.venv/bin/python -m pytest -q tests/integration/test_h8_gateway_jwt_revocado.py
```

El caso del JWT revocado debe fallar en la versión defectuosa: el Gateway responde
con el resultado del orquestador en vez de detenerse con 401 antes de delegar.
