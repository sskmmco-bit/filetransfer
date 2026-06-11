FROM python:3.12-slim

# System deps:
#   libmagic1        -> python-magic content sniffing (§5.6.1b)
#   netcat-openbsd   -> wait-for-service in the entrypoint
#   curl             -> container healthchecks
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libmagic1 \
        netcat-openbsd \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . /app/

# Entrypoints
RUN chmod +x /app/docker/entrypoint.web.sh /app/docker/entrypoint.celery.sh

ENTRYPOINT ["/app/docker/entrypoint.web.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
