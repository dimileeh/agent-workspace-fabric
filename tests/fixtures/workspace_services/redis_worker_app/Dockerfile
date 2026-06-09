FROM python:3.12-alpine

WORKDIR /app
COPY app.py worker.py redis_client.py /app/
COPY scripts /app/scripts

EXPOSE 8080
CMD ["python", "/app/app.py"]
