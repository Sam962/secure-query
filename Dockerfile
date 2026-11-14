# Production image: pinned dependencies, non-root, no demo data and no dev defaults.
# Configuration comes from the environment at run time (see .env.example); with
# SECURE_QUERY_ENV=production the service refuses to start without a signing secret,
# real authentication and an approved catalog.
# The local demo adds its own settings in docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
RUN pip install --no-deps . \
    && useradd --system --uid 10001 --create-home secure-query \
    && mkdir -p /app/data && chown secure-query /app/data

USER secure-query
ENV SECURE_QUERY_ENV=production
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "secure_query.api:app", "--host", "0.0.0.0", "--port", "8000"]
