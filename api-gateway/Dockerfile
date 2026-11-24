FROM python:3.12-slim
WORKDIR /app
COPY api-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api-gateway/app.py .
COPY scripts/service_env.py /usr/local/bin/service_env.py
EXPOSE 8080
ENTRYPOINT ["python", "/usr/local/bin/service_env.py"]
CMD ["python", "app.py"]
