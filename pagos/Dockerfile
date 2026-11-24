FROM python:3.12-slim

WORKDIR /srv

# Se construye con context: . (raíz del monorepo), igual que los demás servicios.
COPY pagos/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY pagos/app ./app

# Usuario sin privilegios; /data guarda la base propia del servicio (SQLite en el volumen pagos_data).
RUN useradd --system --uid 10001 --no-create-home app
RUN mkdir -p /data && chown app:app /data
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
