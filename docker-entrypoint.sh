#!/bin/bash
uv run python manage.py migrate --noinput
uv run python manage.py collectstatic --noinput --clear

exec uv run python manage.py runserver 0.0.0.0:8000
