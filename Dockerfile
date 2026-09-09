# One tick, then exit. Cloud Run Jobs treats a zero exit as a delivered tick and
# anything else as a task to retry, so nothing here may swallow an error.
#
# Pinned to match the local interpreter.
FROM python:3.14-slim

# Unbuffered, or the log line from runner.py arrives in Cloud Logging only after
# the process exits -- which is exactly when a crashed tick would lose it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copied and installed before the source so an edit to the bot does not
# invalidate the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hl_usdc_bot/ ./hl_usdc_bot/
# wsgi.py is not used by the job. It is kept so the same image can still serve
# the Cloud Run *Service* path if it is ever wanted:
#   gcloud run deploy ... --command gunicorn \
#     --args=--bind,:8080,--workers,1,--threads,4,--timeout,60,wsgi:app
COPY wsgi.py .

# Nothing here writes to disk -- state lives in GCS -- so the container has no
# reason to run as root.
RUN useradd --create-home --uid 1001 bot
USER bot

ENTRYPOINT ["python", "-m", "hl_usdc_bot.tick_once"]
