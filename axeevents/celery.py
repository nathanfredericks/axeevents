import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "axeevents.settings")

app = Celery("axeevents")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()

app.conf.beat_schedule = {
    "send-event-reminders-every-30-minutes": {
        "task": "events.tasks.send_event_reminders",
        "schedule": crontab(minute="*/30"),
    },
}

app.conf.timezone = "UTC"
