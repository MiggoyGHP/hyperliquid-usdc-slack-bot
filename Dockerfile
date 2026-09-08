FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hl_usdc_bot/ ./hl_usdc_bot/
COPY wsgi.py .

# Cloud Run injects PORT. One worker is plenty: this serves one request an hour.
ENV PORT=8080
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 60 wsgi:app
