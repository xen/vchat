# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM node:22-bookworm-slim AS frontend-chat
WORKDIR /src/frontend_chat
COPY frontend_chat/package*.json ./
RUN npm ci
COPY frontend_chat/ ./
RUN npm run build

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/venv/bin:${PATH}" \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus_multiproc

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libpq5 \
        libyaml-0-2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/requirements.txt requirements/requirements.txt
COPY requirements/req_linux.txt requirements/req_linux.txt
RUN python -m venv /app/venv \
    && /app/venv/bin/python -m pip install --upgrade pip uv \
    && /app/venv/bin/uv pip sync requirements/requirements.txt --link-mode=copy \
    && /app/venv/bin/uv pip install -r requirements/req_linux.txt --link-mode=copy

COPY . .
COPY --from=frontend /src/frontend/dist frontend/dist
COPY --from=frontend-chat /src/frontend_chat/dist frontend_chat/dist

ARG DOWNLOAD_MODELS=false
RUN if [ "${DOWNLOAD_MODELS}" = "true" ]; then python entry.py --model; fi

RUN groupadd --system --gid 10001 vchat \
    && useradd --system --uid 10001 --gid vchat --home-dir /app --shell /usr/sbin/nologin vchat \
    && mkdir -p /tmp/prometheus_multiproc \
    && chown -R vchat:vchat /app /tmp/prometheus_multiproc

USER vchat
EXPOSE 9080

CMD ["python", "entry.py", "--host", "0.0.0.0", "--port", "9080"]
