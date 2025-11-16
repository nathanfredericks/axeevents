#!/bin/bash

uv run python manage.py migrate --noinput
uv run python manage.py collectstatic --noinput --clear

WORKERS=${GUNICORN_WORKERS:-4}
THREADS=${GUNICORN_THREADS:-2}
TIMEOUT=${GUNICORN_TIMEOUT:-60}
GRACEFUL_TIMEOUT=${GUNICORN_GRACEFUL_TIMEOUT:-30}
MAX_REQUESTS=${GUNICORN_MAX_REQUESTS:-1000}
MAX_REQUESTS_JITTER=${GUNICORN_MAX_REQUESTS_JITTER:-50}
WORKER_CLASS=${GUNICORN_WORKER_CLASS:-gthread}
KEEPALIVE=${GUNICORN_KEEPALIVE:-5}

exec uv run gunicorn axeevents.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers $WORKERS \
    --threads $THREADS \
    --worker-class $WORKER_CLASS \
    --worker-tmp-dir /dev/shm \
    --timeout $TIMEOUT \
    --graceful-timeout $GRACEFUL_TIMEOUT \
    --max-requests $MAX_REQUESTS \
    --max-requests-jitter $MAX_REQUESTS_JITTER \
    --keep-alive $KEEPALIVE \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    --capture-output \
    --enable-stdio-inheritance
