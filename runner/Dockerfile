# Imagen para ejecutar la suite de pruebas y los experimentos DENTRO de Docker.
# Se comunica con el daemon de Docker del anfitrión (socket montado por docker-compose.yml)
# porque las pruebas de falla y los experimentos apagan/reinician contenedores.
FROM python:3.12-slim

# CLI de Docker y plugin compose copiados de la imagen oficial (sin instalar nada con apt)
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

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
