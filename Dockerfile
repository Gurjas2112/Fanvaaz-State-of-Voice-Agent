FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY atompulse ./atompulse
RUN pip install . && useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app /app
USER app

ENV ATOMPULSE_DB_PATH=/app/data/atompulse.db ATOMPULSE_RAW_DIR=/app/data/raw
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
# Seed the demo run on first boot if the volume is empty, then serve.
CMD ["sh", "-c", "python -c \"from atompulse.store import Store; from atompulse.config import get_settings; import sys; sys.exit(0 if Store(get_settings().db_path).latest_run() else 1)\" || atompulse run --source ${ATOMPULSE_BOOT_SOURCE:-demo}; exec uvicorn atompulse.api:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2}"]
