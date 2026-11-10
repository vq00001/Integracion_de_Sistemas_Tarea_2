FROM python:3.12-slim
WORKDIR /app
COPY bff/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bff/app.py ./app.py
COPY bff/generar_stubs.py ./generar_stubs.py
COPY proto ./proto
RUN python generar_stubs.py --destino /app/grpc_stubs
EXPOSE 8090
CMD ["python", "app.py"]
