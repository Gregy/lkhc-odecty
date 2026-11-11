# LKHC Odečty – single container: FastAPI app + SQLite + photos in the /data volume.
#
#   docker build -t lkhc-odecty .
#   docker compose up -d          (see docker-compose.yml)
#
# The entrypoint runs `python -m app.migrate` (schema upgrade, with a DB snapshot before
# upgrading an existing database) and then the CMD (uvicorn, ONE worker: the photo
# extraction queue runs inside the app process and SQLite has a single writer).

ARG PYTHON_IMAGE=python:3.15-slim

# --- build: resolve the locked dependencies into /opt/venv with uv -------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- runtime ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
# tzdata: the app works in naive local time (TZ=Europe/Prague), also in summer time
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    DATA_DIR=/data \
    TZ=Europe/Prague \
    FORWARDED_ALLOW_IPS=127.0.0.1
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["docker-entrypoint.sh"]
# --proxy-headers: trust X-Forwarded-Proto/For from FORWARDED_ALLOW_IPS (set in docker-compose.yml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--no-server-header"]
