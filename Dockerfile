FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY fanvaaz ./fanvaaz
RUN pip install ".[gcp]" && useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app /app
USER app

# SQLite by default; set FANVAAZ_DATABASE_URL (postgresql://...) for Cloud SQL / docker compose.
ENV FANVAAZ_DB_PATH=/app/data/fanvaaz.db FANVAAZ_RAW_DIR=/app/data/raw \
    FANVAAZ_CHECKPOINT_PATH=/app/data/checkpoints.db FANVAAZ_LOG_JSON=true
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health').status==200 else 1)"
# No seed data: the database starts empty and fills from live YouTube collections
# (admin page, Cloud Scheduler, or `fanvaaz run`). Schema migrations run on startup.
CMD ["sh", "-c", "exec uvicorn fanvaaz.api:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
