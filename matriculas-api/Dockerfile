FROM python:3.12-slim

WORKDIR /srv

# Se construye con context: . (raíz del monorepo), igual que cupos/ y
# matriculas/, para poder copiar proto/ además del código del servicio.
COPY matriculas-api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY matriculas-api/app ./app
COPY matriculas-api/generar_stubs.py ./generar_stubs.py
COPY proto ./proto

# Los stubs gRPC se generan desde proto/cupos.proto (única fuente de verdad);
# no se versionan en git, así que el build nunca usa stubs desactualizados.
RUN python generar_stubs.py

# Usuario sin privilegios (la API solo escucha el puerto 8000 y habla con Postgres/Cupos por red).
RUN useradd --system --uid 10001 --no-create-home app
RUN mkdir -p /data && chown app:app /data
USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
