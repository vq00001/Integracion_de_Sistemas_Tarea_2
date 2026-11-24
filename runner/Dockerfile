# Imagen para ejecutar la suite de pruebas y los experimentos DENTRO de Docker.
# Se comunica con el daemon de Docker del anfitrión (socket montado por docker-compose.yml)
# porque las pruebas de falla y los experimentos apagan/reinician contenedores.
FROM python:3.12-slim

WORKDIR /repo
COPY runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# El código NO se copia: el repositorio se monta en /repo, así ves los cambios sin reconstruir
# y los resultados (CSV y figuras) quedan directamente en tu carpeta.
ENV PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    INSCRIPCION_TEST_URL=http://matriculas-api:8000 \
    BASE_URL=http://matriculas-api:8000 \
    CUPOS_GRPC_ADDR=cupos:50051

# H5 exporta una copia limpia de las fuentes para probar Compose sin .env.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Compose 2.33 bloqueaba --dry-run con service_completed_successfully. Usar
# el CLI y ambos plugins de la misma imagen oficial, también para builds Bake.
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=docker:29-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
