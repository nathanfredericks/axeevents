import pytz
from django import template
from django.utils import timezone as django_timezone

from events.utils import format_display_phone

register = template.Library()


def format_datetime_with_conditional_tz(
    dt, event_timezone, user_timezone=None, date_format="%b %d, %Y %I:%M %p"
):
    if not dt:
        return ""

    if not user_timezone:
        user_timezone = "UTC"

    try:
        event_tz = pytz.timezone(event_timezone)
    except pytz.UnknownTimeZoneError:
        event_tz = pytz.UTC

    try:
        user_tz = pytz.timezone(user_timezone)
    except pytz.UnknownTimeZoneError:
        user_tz = pytz.UTC

    if django_timezone.is_aware(dt):
        dt_local = dt.astimezone(event_tz)
    else:
        dt_local = event_tz.localize(dt)

    formatted = dt_local.strftime(date_format)

    if event_tz.zone != user_tz.zone:
        tz_abbr = dt_local.strftime("%Z")
        formatted = f"{formatted} {tz_abbr}"

    return formatted


@register.filter(name="format_phone")
def format_phone(value):
    return format_display_phone(value)


@register.simple_tag(takes_context=True)
def format_event_time(context, event_datetime, event_timezone, date_format="g:i A"):
    if not event_datetime:
        return ""

    request = context.get("request")
    user_tz_name = None
    if request:
        user_tz_name = request.session.get("user_timezone")

    if not user_tz_name:
        user_tz_name = "UTC"

    try:
        event_tz = pytz.timezone(event_timezone)
    except pytz.UnknownTimeZoneError:
        event_tz = pytz.UTC

    try:
        user_tz = pytz.timezone(user_tz_name)
    except pytz.UnknownTimeZoneError:
        user_tz = pytz.UTC

    if django_timezone.is_aware(event_datetime):
        event_time_local = event_datetime.astimezone(event_tz)
    else:
        event_time_local = event_tz.localize(event_datetime)

    from django.utils.dateformat import format as date_format_func

    formatted_time = date_format_func(event_time_local, date_format)

    if event_tz.zone != user_tz.zone:
        tz_abbr = event_time_local.strftime("%Z")
        formatted_time = f"{formatted_time} {tz_abbr}"

    return formatted_time


@register.simple_tag(takes_context=True)
def format_event_date(context, event_datetime, event_timezone, date_format="l, F j, Y"):
    if not event_datetime:
        return ""

    request = context.get("request")
    user_tz_name = None
    if request:
        user_tz_name = request.session.get("user_timezone")

    if not user_tz_name:
        user_tz_name = "UTC"

    try:
        event_tz = pytz.timezone(event_timezone)
    except pytz.UnknownTimeZoneError:
        event_tz = pytz.UTC

    try:
        user_tz = pytz.timezone(user_tz_name)
    except pytz.UnknownTimeZoneError:
        user_tz = pytz.UTC

    if django_timezone.is_aware(event_datetime):
        event_time_local = event_datetime.astimezone(event_tz)
    else:
        event_time_local = event_tz.localize(event_datetime)

    from django.utils.dateformat import format as date_format_func

    formatted_date = date_format_func(event_time_local, date_format)

    return formatted_date


@register.simple_tag(takes_context=True)
def format_datetime_conditional_tz(
    context, dt, event_timezone, date_format="F j, Y, g:i A"
):
    if not dt:
        return ""

    request = context.get("request")
    user_tz_name = None
    if request:
        user_tz_name = request.session.get("user_timezone")

    if not user_tz_name:
        user_tz_name = "UTC"

    try:
        event_tz = pytz.timezone(event_timezone)
    except pytz.UnknownTimeZoneError:
        event_tz = pytz.UTC

    try:
        user_tz = pytz.timezone(user_tz_name)
    except pytz.UnknownTimeZoneError:
        user_tz = pytz.UTC

    if django_timezone.is_aware(dt):
        dt_local = dt.astimezone(event_tz)
    else:
        dt_local = event_tz.localize(dt)

    from django.utils.dateformat import format as date_format_func

    formatted = date_format_func(dt_local, date_format)

    if event_tz.zone != user_tz.zone:
        tz_abbr = dt_local.strftime("%Z")
        formatted = f"{formatted} {tz_abbr}"

    return formatted
