# syntax=docker/dockerfile:1

# ---- build: resolve the locked deps into a venv with uv ----
FROM python:3.14-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Deps only (cached layer); dev group (pytest/ruff/playwright) is not needed at runtime.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# ---- runtime: python + venv + app code, nothing else ----
FROM python:3.14-slim AS runtime

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY app/ ./app/

# Read-only USDA nutrition store (usda.db) is mounted here; app state lives in Postgres.
RUN mkdir -p /app/data && chown app:app /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    USDA_DB_PATH=/app/data/usda.db

USER app

VOLUME ["/app/data"]

# uvicorn's default port; the app has no port setting of its own.
EXPOSE 8000

# Migrate first (transactional; a failure leaves the schema untouched and uvicorn never starts),
# then serve. Single worker: in-process background planner thread + process-wide plan lock.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
