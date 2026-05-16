FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "psycopg[binary]>=3.2,<4"
COPY app.py /app/app.py

EXPOSE 8080
CMD ["python", "/app/app.py"]
