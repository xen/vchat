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
    HF_HOME=/app/models/huggingface \
    PATH="/app/venv/bin:${PATH}" \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus_multiproc

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libldap2-dev \
        libpq5 \
        libsasl2-dev \
        libyaml-0-2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/requirements.txt requirements/requirements.txt
COPY requirements/req_linux.txt requirements/req_linux.txt
ARG INSTALL_LINUX_EXTRAS=true
RUN python -m venv /app/venv \
    && /app/venv/bin/python -m pip install --no-cache-dir --upgrade pip uv \
    && /app/venv/bin/uv pip sync requirements/requirements.txt --link-mode=copy --index-strategy unsafe-best-match --no-cache \
    && if [ "${INSTALL_LINUX_EXTRAS}" = "true" ]; then /app/venv/bin/uv pip install -r requirements/req_linux.txt --link-mode=copy --index-strategy unsafe-best-match --no-cache; fi \
    && apt-mark manual ca-certificates curl libldap-2.5-0 libpq5 libsasl2-2 libyaml-0-2 \
    && apt-get purge -y --auto-remove build-essential git libldap2-dev libsasl2-dev \
    && rm -rf /root/.cache/uv

COPY . .
COPY --from=frontend /src/frontend/dist frontend/dist
COPY --from=frontend-chat /src/frontend_chat/dist frontend_chat/dist

RUN groupadd --gid 10001 vchat \
    && useradd --uid 10001 --gid vchat --home-dir /app --shell /usr/sbin/nologin --no-create-home vchat \
    && mkdir -p /app/models /tmp/prometheus_multiproc \
    && chown vchat:vchat /app/models /tmp/prometheus_multiproc

USER vchat

RUN python entry.py --model

EXPOSE 9080

CMD ["python", "entry.py", "--host", "0.0.0.0", "--port", "9080"]
