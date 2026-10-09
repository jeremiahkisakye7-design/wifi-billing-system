FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WIFI_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data

EXPOSE 5002
CMD ["gunicorn", "app:app", "--workers", "1", "--threads", "8", "--timeout", "30", "--bind", "0.0.0.0:5002"]