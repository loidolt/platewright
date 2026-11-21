# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY platewright ./platewright
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
# DejaVu Sans for the labels Platewright draws.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 1000 platewright \
 && useradd --system --uid 1000 --gid 1000 --home-dir /app platewright \
 && mkdir -p /data && chown platewright /data
WORKDIR /app
COPY --from=build --chown=platewright /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PLATEWRIGHT_DATA_DIR=/data
USER platewright
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
# --proxy-headers: the Cloudflare tunnel terminates TLS in front of us.
CMD ["uvicorn", "platewright.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
