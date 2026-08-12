# Multi-stage: the build tools that resolve dependencies have no business in
# the image that faces the internet.
FROM python:3.12-slim-bookworm AS builder

# Pinned, like every other dependency. A floating installer is a floating
# build.
RUN pip install --no-cache-dir uv==0.12.3

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies before source, so a code change does not re-resolve them.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime

# Non-root: the container boundary is not a substitute for least privilege,
# and nothing this service does needs uid 0.
RUN useradd --create-home --uid 10001 catalogue

WORKDIR /app
COPY --from=builder --chown=catalogue:catalogue /app/.venv /app/.venv
COPY --from=builder --chown=catalogue:catalogue /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER catalogue
EXPOSE 8000

# Cloud Run injects PORT and expects it honoured; the default is for local runs.
# --proxy-headers: Cloud Run terminates TLS, so without this uvicorn believes
# it is serving plain HTTP and every redirect it issues points at http://,
# which a client on https:// then refuses.
CMD ["sh", "-c", "exec uvicorn api.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
