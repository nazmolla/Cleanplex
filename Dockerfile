# syntax=docker/dockerfile:1
# Cleanplex image (#194): app + ffmpeg + the nginx front door. See README "Docker".

FROM node:22-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# vite writes to ../cleanplex/web/static
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg nginx libnginx-mod-stream nftables tini curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml LICENSE ./
# Dependencies in their own layer: rebuilding after a code change does not reinstall them.
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/deps.txt \
    && pip install --no-cache-dir -r /tmp/deps.txt
COPY cleanplex ./cleanplex
COPY --from=frontend /build/cleanplex/web/static ./cleanplex/web/static
RUN pip install --no-cache-dir --no-deps .
COPY docker/entrypoint.sh /entrypoint.sh

ENV CLEANPLEX_DATA=/data \
    CLEANPLEX_HOST=0.0.0.0 \
    CLEANPLEX_PROXY_HOST=127.0.0.1 \
    PYTHONUNBUFFERED=1
VOLUME /data
# 7979: web UI. 32480: the front door (publish it as Plex's public port, 32400).
EXPOSE 7979 32480
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD curl -fsS http://127.0.0.1:7979/api/settings >/dev/null || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
