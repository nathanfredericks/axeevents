import logging
from typing import Union

import phonenumbers
import pytz
from django.utils import timezone
from phonenumbers import NumberParseException, PhoneNumber

logger = logging.getLogger(__name__)


def format_display_phone(phone_number: Union[str, PhoneNumber, None]) -> str:
    if not phone_number:
        return ""

    raw = str(phone_number)
    try:
        parsed = phonenumbers.parse(raw, None)
    except NumberParseException:
        return raw

    if parsed.country_code == 1:
        national = phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.NATIONAL
        )
        return f"+1 {national}"

    return phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )


def _get_event_local_datetime(event):
    event_dt = getattr(event, "event_state_date", None)
    if not event_dt:
        return None

    if timezone.is_naive(event_dt):
        event_dt = timezone.make_aware(event_dt, pytz.UTC)

    tz_name = getattr(event, "timezone", None) or "UTC"
    try:
        target_tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        logger.warning(
            "Unknown timezone '%s' for event %s; defaulting to UTC",
            tz_name,
            getattr(event, "id", "unknown"),
        )
        target_tz = pytz.UTC

    return event_dt.astimezone(target_tz)


def format_event_datetime(event, fmt="%b %d at %I:%M %p %Z") -> str:
    local_dt = _get_event_local_datetime(event)
    if not local_dt:
        return ""

    formatted = local_dt.strftime(fmt).strip()
    return " ".join(formatted.split())
