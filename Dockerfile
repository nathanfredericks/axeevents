FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_CACHE_DIR=/app/.cache/uv

WORKDIR /app

RUN apt-get update && apt-get install -y \
    postgresql-client \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen

COPY . .

RUN mkdir -p logs media staticfiles .cache/uv

COPY docker-entrypoint.sh docker-entrypoint-prod.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    /usr/local/bin/docker-entrypoint-prod.sh

RUN groupadd -r axeevents && useradd -r -g axeevents axeevents && \
    chown -R axeevents:axeevents /app

USER axeevents

EXPOSE 8000