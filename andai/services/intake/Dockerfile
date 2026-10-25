# syntax=docker/dockerfile:1
# andai-intake — build context is andai/ (compose: context andai/, dockerfile services/intake/Dockerfile).

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# The whole uv workspace definition is needed to resolve the lock; only andai-intake is installed.
COPY pyproject.toml uv.lock ./
COPY services/core/pyproject.toml services/core/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
COPY services/intake/pyproject.toml services/intake/pyproject.toml
COPY services/intake/andai_intake services/intake/andai_intake
RUN uv sync --frozen --no-dev --no-editable --package andai-intake

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin andai
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ANDAI_VERSION=dev
USER 10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "andai_intake.main:app", "--host", "0.0.0.0", "--port", "8000"]
