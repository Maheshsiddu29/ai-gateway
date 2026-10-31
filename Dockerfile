# syntax=docker/dockerfile:1

# Base images are pinned by digest for reproducible builds; .github/dependabot.yml
# proposes updates.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# Resolve dependencies from the lockfile into a self-contained virtual environment.
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Linux regression environment using the Redis 7.0 package from Debian bookworm.
# This stage is never part of the runtime image.
FROM builder AS validation
RUN apt-get update \
    && apt-get install --yes --no-install-recommends redis-server openssl \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --locked
COPY tests ./tests
COPY tools ./tools
COPY config ./config
ENV GATEWAY_REQUIRE_REDIS_TESTS=1
CMD ["uv", "run", "pytest", "-q"]

# Shared runtime: the virtual environment and an unprivileged user, no build tools.
FROM ${PYTHON_IMAGE} AS base
RUN groupadd --system --gid 10001 gateway \
    && useradd --system --uid 10001 --gid gateway --no-create-home --shell /usr/sbin/nologin gateway
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER gateway

# Mock provider for local development and demonstrations only.
FROM base AS mock-upstream
COPY tools/mock_upstream.py /opt/mock/mock_upstream.py
EXPOSE 9000
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=2)"]
CMD ["uvicorn", "mock_upstream:app", "--app-dir", "/opt/mock", "--host", "0.0.0.0", "--port", "9000", "--no-access-log"]

# The gateway. Configuration and secrets are supplied at run time: mount a provider
# configuration file at GATEWAY_CONFIG_FILE and pass keys as environment variables.
FROM base AS runtime
ENV GATEWAY_CONFIG_FILE=/etc/gateway/gateway.toml
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)"]
# Exec form keeps uvicorn as PID 1 so it receives SIGTERM directly; it then stops
# accepting connections and waits up to 30 seconds for in-flight requests.
CMD ["uvicorn", "gateway.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--no-server-header", "--timeout-graceful-shutdown", "30"]
