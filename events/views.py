import csv
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from urllib.error import URLError
from urllib.request import urlopen

import pytz
import qrcode
from django.conf import settings
from django.contrib import messages
from django.db.models import Count, ExpressionWrapper, F, Q, fields
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit
from ipware import get_client_ip
from phonenumbers import NumberParseException, is_valid_number, parse

from events.auth import AuthService
from events.image_utils import sanitize_and_save_image
from events.models import (
    RSVP,
    Event,
    EventInvitation,
    EventQuestion,
    RSVPAnswer,
    TextBlast,
    User,
)
from events.tasks import send_bulk_sms, send_single_sms
from events.templatetags.format_extras import format_datetime_with_conditional_tz
from events.utils import format_event_datetime

logger = logging.getLogger(__name__)

QUESTION_SLOTS = 5


@dataclass
class EventFormDefaults:
    title: str = ""
    description: str = ""
    location: str = ""
    event_state_date: datetime | None = None
    event_end_date: datetime | None = None
    timezone: str | None = None
    max_attendees: int | None = None
    allow_rsvp: bool = True
    allow_maybe_rsvp: bool = True
    hide_attendee_count: bool = False
    is_listed: bool = True
    auto_reminders_enabled: bool = True

    def __bool__(self):
        return False


def get_common_timezones():
    return [{"value": tz, "label": tz} for tz in pytz.all_timezones]


def get_timezone_from_ip(request):
    client_ip, is_routable = get_client_ip(request)

    if not is_routable or not client_ip:
        logger.debug("IP not routable or not found, defaulting to UTC")
        return "UTC"

    try:
        url = f"http://ip-api.com/json/{client_ip}?fields=timezone"
        with urlopen(url, timeout=2) as response:
            data = json.loads(response.read().decode())
            detected_tz = data.get("timezone", "UTC")

            if detected_tz in pytz.all_timezones:
                logger.info(f"Detected timezone {detected_tz} for IP {client_ip}")
                return detected_tz
            else:
                logger.warning(
                    f"Detected timezone {detected_tz} not in pytz, using UTC"
                )
                return "UTC"
    except (URLError, json.JSONDecodeError, KeyError, Exception) as e:
        logger.debug(f"Failed to detect timezone from IP {client_ip}: {e}")
        return "UTC"


def _build_event_user_state(request, event):
    user_phone = request.session.get("user_phone")
    user = None
    user_rsvp = None
    user_is_attending = False
    is_organizer = False
    is_creator = False
    is_co_organizer = False
    user_answers = {}

    questions = list(event.questions.order_by("order", "id"))

    if user_phone:
        try:
            user = User.objects.get(phone_number=user_phone)
            user_rsvp = RSVP.objects.filter(user=user, event=event).first()
            user_is_attending = bool(user_rsvp and user_rsvp.status == "attending")
            if user_rsvp:
                user_answers_qs = user_rsvp.answers.select_related("question")
                user_answers = {
                    answer.question_id: answer.answer for answer in user_answers_qs
                }
            is_organizer = event.is_organizer(user)
            is_creator = event.created_by == user
            is_co_organizer = event.organizers.filter(id=user.id).exists()
            if is_creator:
                user_is_attending = True
                if not user_rsvp or user_rsvp.status != "attending":
                    user_rsvp, _ = RSVP.objects.update_or_create(
                        user=user, event=event, defaults={"status": "attending"}
                    )
        except User.DoesNotExist:
            pass

    questionnaire = [
        {
            "id": question.id,
            "text": question.text,
            "required": question.is_required,
            "answer": user_answers.get(question.id, ""),
        }
        for question in questions
    ]

    return {
        "user_phone": user_phone,
        "user_rsvp": user_rsvp,
        "user_is_attending": user_is_attending,
        "is_organizer": is_organizer,
        "is_creator": is_creator,
        "is_co_organizer": is_co_organizer,
        "questionnaire": questionnaire,
    }


def _rsvp_response(request, event, trigger=None):
    if request.headers.get("HX-Request"):
        state = _build_event_user_state(request, event)
        context = {
            **state,
            "event": event,
            "primary_container": request.headers.get("HX-Target")
            or "rsvp-mobile-container",
            "mobile_container_id": "rsvp-mobile-container",
            "desktop_container_id": "rsvp-desktop-container",
            "mobile_form_id": "mobileRsvpForm",
            "desktop_form_id": "desktopRsvpForm",
            "mobile_wrapper": "mb-4 d-lg-none",
            "desktop_wrapper": "mb-4 d-none d-lg-block",
        }
        response = render(request, "events/partials/rsvp_refresh.html", context)
        if trigger:
            response["HX-Trigger"] = json.dumps(trigger)
        return response

    return redirect("event_detail", event_id=event.id)


def _normalize_lookup_prefix(prefix):
    if not prefix:
        return ""
    return prefix if prefix.endswith("__") else f"{prefix}__"


def _event_not_ended_q(prefix="", reference_time=None):
    now = reference_time or timezone.now()
    lookup_prefix = _normalize_lookup_prefix(prefix)
    has_end_time = Q(**{f"{lookup_prefix}event_end_date__gte": now})
    fallback_to_start = Q(**{f"{lookup_prefix}event_end_date__isnull": True}) & Q(
        **{f"{lookup_prefix}event_state_date__gte": now}
    )
    return has_end_time | fallback_to_start


def _event_has_ended_q(prefix="", reference_time=None):
    now = reference_time or timezone.now()
    lookup_prefix = _normalize_lookup_prefix(prefix)
    ended_with_end_time = Q(**{f"{lookup_prefix}event_end_date__lt": now})
    fallback_to_start = Q(**{f"{lookup_prefix}event_end_date__isnull": True}) & Q(
        **{f"{lookup_prefix}event_state_date__lt": now}
    )
    return ended_with_end_time | fallback_to_start


def parse_and_validate_event_state_date(date_str, event_timezone="UTC"):
    try:
        naive_dt = datetime.fromisoformat(date_str)

        event_tz = pytz.timezone(event_timezone)
        localized_dt = event_tz.localize(naive_dt)

        utc_dt = localized_dt.astimezone(pytz.UTC)

        if utc_dt <= datetime.now(pytz.UTC):
            raise ValueError("Event date must be in the future")

        return utc_dt
    except (ValueError, TypeError) as e:
        if "future" in str(e):
            raise
        raise ValueError("Invalid event date")


def validate_max_attendees(unlimited_attendees, post_data):
    if unlimited_attendees:
        return None
    try:
        max_attendees = int(post_data.get("max_attendees"))
        if max_attendees <= 0:
            raise ValueError("Maximum attendees must be positive")
        return max_attendees
    except (TypeError, ValueError):
        raise ValueError("Please enter a positive number for maximum attendees")


def normalize_phone_number(phone_number, default_region="US"):
    if not phone_number:
        return None

    try:
        parsed = parse(phone_number, default_region)

        if settings.DEBUG or is_valid_number(parsed):
            return f"+{parsed.country_code}{parsed.national_number}"

        return None
    except (NumberParseException, Exception):
        return None


def _build_question_form_rows(*, event=None, post_data=None):
    rows = []
    existing_questions = []
    if event is not None:
        existing_questions = list(event.questions.order_by("order", "id"))

    for index in range(1, QUESTION_SLOTS + 1):
        question = (
            existing_questions[index - 1]
            if index - 1 < len(existing_questions)
            else None
        )
        if post_data is not None:
            text = post_data.get(f"question_text_{index}", "").strip()
            required = post_data.get(f"question_required_{index}") == "on"
        else:
            text = question.text if question else ""
            required = question.is_required if question else False

        rows.append(
            {
                "index": index,
                "text": text,
                "required": required,
                "has_text": bool(text),
                "question_id": question.id if question else None,
            }
        )
    return rows


def _extract_question_entries(post_data):
    entries = []
    for index in range(1, QUESTION_SLOTS + 1):
        text = post_data.get(f"question_text_{index}", "").strip()
        required = post_data.get(f"question_required_{index}") == "on"
        if text:
            entries.append({"text": text, "required": required})
    return entries


def _save_event_questions(event, entries):
    existing = list(event.questions.order_by("order", "id"))

    for order, entry in enumerate(entries):
        if order < len(existing):
            question = existing[order]
            if (
                question.text != entry["text"]
                or question.is_required != entry["required"]
                or question.order != order
            ):
                question.text = entry["text"]
                question.is_required = entry["required"]
                question.order = order
                question.save()
        else:
            EventQuestion.objects.create(
                event=event,
                text=entry["text"],
                is_required=entry["required"],
                order=order,
            )

    if len(existing) > len(entries):
        for question in existing[len(entries) :]:
            question.delete()


def index(request):
    now = timezone.now()
    events = (
        Event.objects.filter(
            _event_not_ended_q(reference_time=now), is_active=True, is_listed=True
        )
        .select_related("created_by")
        .annotate(attendee_count=Count("rsvps", filter=Q(rsvps__status="attending")))
    )

    search_query = request.GET.get("search", "")
    if search_query:
        events = events.filter(
            Q(title__icontains=search_query)
            | Q(description__icontains=search_query)
            | Q(location__icontains=search_query)
        )

    events = events.order_by("event_state_date", "-attendee_count")[:12]

    context = {
        "events": events,
        "user_phone": request.session.get("user_phone"),
        "search_query": search_query,
    }

    if request.headers.get("HX-Request"):
        return render(request, "events/partials/event_list.html", context)

    return render(request, "events/index.html", context)


@ratelimit(key="ip", rate="5/h", method="POST", block=True)
def phone_login(request):
    if request.session.get("user_phone"):
        try:
            User.objects.get(phone_number=request.session.get("user_phone"))
            return redirect("index")
        except User.DoesNotExist:
            request.session.flush()

    if request.method == "POST":
        phone_number = request.POST.get("phone_number")

        formatted_number = normalize_phone_number(phone_number)

        if not formatted_number:
            logger.warning(f"Invalid phone number format attempted: {phone_number}")
            messages.error(request, "Please enter a valid phone number.")
            return render(request, "events/phone_login.html")

        existing_user = User.objects.filter(phone_number=formatted_number).first()
        if existing_user and not existing_user.can_resend_code():
            cooldown_seconds = existing_user.get_resend_cooldown_seconds()
            messages.error(
                request,
                f"Please wait {cooldown_seconds} seconds before requesting another code.",
            )
            return render(request, "events/phone_login.html")

        auth_service = AuthService()
        success, result = auth_service.send_verification_code(formatted_number)

        if success:
            request.session["verification_phone"] = formatted_number
            request.session.modified = True
            logger.info(f"Login attempt initiated for {formatted_number}")
            messages.success(request, "Verification code sent to your phone!")
            return redirect("verify_code")
        else:
            logger.error(
                f"Failed to send verification code to {formatted_number}: {result}"
            )
            messages.error(request, f"We couldn't send your code: {result}")

    return render(request, "events/phone_login.html")


def verify_code(request):
    phone_number = request.session.get("verification_phone")
    if not phone_number:
        return redirect("phone_login")

    existing_user = User.objects.filter(phone_number=phone_number).first()
    is_new_user = existing_user is None

    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        name = request.POST.get("name", "").strip()

        auth_service = AuthService()
        success, user, error_type = auth_service.verify_code(phone_number, code)

        if success:
            if name:
                user.name = name
                user.save()

            request.session["user_phone"] = phone_number
            request.session["user_id"] = str(user.id)
            request.session["user_name"] = user.name
            del request.session["verification_phone"]
            request.session.modified = True

            logger.info(f"User {phone_number} successfully logged in")

            user_greeting = f", {user.name}" if user.name else ""
            rsvp_data = request.session.get("rsvp_after_login")
            if rsvp_data:
                return redirect("event_detail", event_id=rsvp_data["event_id"])

            messages.success(
                request, f"Welcome{user_greeting}! Your phone has been verified."
            )
            return redirect("index")
        else:
            if error_type == "expired":
                messages.error(
                    request,
                    "Your verification code has expired. Please request a new code.",
                )
            elif error_type == "invalid":
                messages.error(
                    request, "That verification code is invalid. Please try again."
                )
            else:
                messages.error(
                    request, "We couldn't verify your code. Please try again."
                )

    return render(
        request,
        "events/verify_code.html",
        {"phone_number": phone_number, "is_new_user": is_new_user},
    )


@require_POST
def resend_code(request):
    phone_number = request.session.get("verification_phone")
    if not phone_number:
        messages.error(request, "We couldn't find your verification session.")
        if request.headers.get("HX-Request"):
            return render(request, "partials/messages.html")
        return redirect("phone_login")

    try:
        user = User.objects.get(phone_number=phone_number)

        if not user.can_resend_code():
            cooldown_seconds = user.get_resend_cooldown_seconds()
            messages.error(
                request,
                f"Please wait {cooldown_seconds} seconds before requesting another code.",
            )
            if request.headers.get("HX-Request"):
                return render(request, "partials/messages.html")
            return redirect("verify_code")

        auth_service = AuthService()
        success, result = auth_service.send_verification_code(phone_number)

        if success:
            messages.success(
                request, "We've sent a new verification code to your phone."
            )
        else:
            messages.error(request, f"We couldn't send your code: {result}")

    except User.DoesNotExist:
        messages.error(request, "We couldn't find that user.")

    if request.headers.get("HX-Request"):
        return render(request, "partials/messages.html")
    return redirect("verify_code")


def logout_view(request):
    user_phone = request.session.get("user_phone")
    if user_phone:
        logger.info(f"User {user_phone} logged out")
    request.session.flush()
    messages.success(request, "You've been logged out.")
    return redirect("index")


def event_short_url(request, short_code):
    event = get_object_or_404(Event, short_code=short_code, is_active=True)
    return redirect("event_detail", event_id=event.id)


def event_detail(request, event_id):
    event = get_object_or_404(
        Event.objects.select_related("created_by").prefetch_related(
            "organizers", "questions"
        ),
        id=event_id,
        is_active=True,
    )

    pending_questionnaire = None
    rsvp_data = request.session.get("rsvp_after_login")
    if rsvp_data and rsvp_data.get("event_id") == str(event_id):
        request.session.pop("rsvp_after_login", None)
        status_after_login = rsvp_data.get("status", "attending")
        saved_answers = rsvp_data.get("questions", {})
        should_prompt_questionnaire = (
            status_after_login == "attending" and event.questions.exists()
        )

        if should_prompt_questionnaire:
            pending_questionnaire = {
                "status": status_after_login,
                "answers": saved_answers,
            }
            messages.info(
                request, "Please answer the questionnaire to finish your RSVP."
            )
        else:
            from django.http import QueryDict

            fake_post = QueryDict("", mutable=True)
            fake_post["status"] = status_after_login
            for key, value in saved_answers.items():
                fake_post[key] = value
            request.POST = fake_post
            request.method = "POST"
            return rsvp_event(request, event_id)

    user_state = _build_event_user_state(request, event)
    if pending_questionnaire:
        answer_map = pending_questionnaire.get("answers", {})
        for question in user_state.get("questionnaire", []):
            answer_key = f"question_{question['id']}"
            if answer_key in answer_map:
                question["answer"] = answer_map[answer_key]

    text_blasts = event.text_blasts.filter(display_on_page=True).select_related(
        "sent_by"
    )
    organizers = list(event.organizers.all())
    organizer_names = [organizer.name for organizer in organizers if organizer.name]

    context = {
        "event": event,
        **user_state,
        "pending_questionnaire": pending_questionnaire,
        "organizers": organizers,
        "primary_organizer_name": event.created_by.name or "",
        "additional_organizer_names": organizer_names,
        "can_invite": event.can_invite_organizer(),
        "text_blasts": text_blasts,
    }
    return render(request, "events/event_detail.html", context)


@ratelimit(key="user", rate="20/h", method="POST", block=True)
def rsvp_event(request, event_id):
    if request.method != "POST":
        return _rsvp_response(request, event)

    user_phone = request.session.get("user_phone")
    if not user_phone:
        request.session["rsvp_after_login"] = {
            "event_id": str(event_id),
            "status": request.POST.get("status", "attending"),
        }
        messages.error(request, "Please log in to RSVP.")
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("phone_login")
            return response
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id, is_active=True)
    user = get_object_or_404(User, phone_number=user_phone)
    status = request.POST.get("status", "attending")

    if event.created_by == user:
        return _rsvp_response(request, event)

    if status not in {"attending", "maybe", "not_attending"}:
        messages.error(request, "Please select a valid RSVP status.")
        return _rsvp_response(request, event)

    user_existing_rsvp = RSVP.objects.filter(user=user, event=event).first()
    questions = list(event.questions.order_by("order", "id"))
    question_answers = {}
    for question in questions:
        answer_value = request.POST.get(f"question_{question.id}", "")
        question_answers[question.id] = answer_value.strip()

    if not event.allow_rsvp:
        if status != "not_attending" or not user_existing_rsvp:
            messages.error(request, "This event has closed RSVPs.")
            return _rsvp_response(request, event)

    if status == "maybe" and not event.allow_maybe_rsvp:
        messages.error(request, "This event doesn't allow maybe responses.")
        return _rsvp_response(request, event)

    requires_answers = status in {"attending", "maybe"}
    if requires_answers and questions:
        missing_questions = [
            question.text
            for question in questions
            if question.is_required and not question_answers.get(question.id)
        ]
        if missing_questions:
            if len(missing_questions) == 1:
                messages.error(
                    request,
                    f"Please answer the required question: {missing_questions[0]}",
                )
            else:
                messages.error(
                    request,
                    "Please answer all required questions before submitting your RSVP.",
                )
            return _rsvp_response(request, event)

    if (
        status == "attending"
        and event.is_full
        and not (user_existing_rsvp and user_existing_rsvp.status == "attending")
    ):
        messages.error(request, "This event is full.")
        return redirect("event_detail", event_id=event_id)

    rsvp, created = RSVP.objects.update_or_create(
        user=user, event=event, defaults={"status": status}
    )

    if status == "not_attending":
        rsvp.answers.all().delete()
    else:
        for question in questions:
            answer_text = question_answers.get(question.id, "")
            if answer_text or question.is_required:
                RSVPAnswer.objects.update_or_create(
                    rsvp=rsvp,
                    question=question,
                    defaults={"answer": answer_text},
                )
            else:
                RSVPAnswer.objects.filter(rsvp=rsvp, question=question).delete()

    action = "added" if created else "updated"
    logger.info(
        f"User {user.phone_number} RSVP {action} for event '{event.title}' (ID: {event.id}) with status '{status}'"
    )
    messages.success(request, f"We've {action} your RSVP!")

    message = f"You're RSVPed as '{status}' for {event.title} on {event.event_state_date.strftime('%B %d at %I:%M %p')}. {event.get_short_url()}"
    send_single_sms.delay(user_phone, message)

    return _rsvp_response(
        request,
        event,
        trigger={
            "rsvp-updated": {
                "status": status,
            }
        },
    )


def create_event(request):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to create events.")
        return redirect("phone_login")

    allowed_creator_ids = settings.ALLOWED_EVENT_CREATOR_IDS
    if allowed_creator_ids:
        user = get_object_or_404(User, phone_number=user_phone)
        allowed_ids_list = [
            uid.strip() for uid in allowed_creator_ids.split(",") if uid.strip()
        ]
        if str(user.id) not in allowed_ids_list:
            messages.error(request, "You don't have permission to create events.")
            return redirect("index")

    raw_question_rows = _build_question_form_rows(
        post_data=request.POST if request.method == "POST" else None
    )
    active_question_rows = [row for row in raw_question_rows if row["has_text"]]
    unlimited_attendees = (
        True
        if request.method != "POST"
        else request.POST.get("unlimited_attendees") == "on"
    )
    photo_album_prefill = (
        request.POST.get("photo_album_url", "").strip()
        if request.method == "POST"
        else ""
    )

    def render_create(extra_context=None):
        context = {
            "question_rows": active_question_rows,
            "question_slots": QUESTION_SLOTS,
            "unlimited_attendees": unlimited_attendees,
            "initial_photo_album_url": photo_album_prefill,
            "timezones": get_common_timezones(),
            "default_timezone": get_timezone_from_ip(request),
            "event": EventFormDefaults(),
            "detect_timezone_on_load": False,
            "event_photo_album": "",
        }
        if extra_context:
            context.update(extra_context)
        return render(request, "events/create_event.html", context)

    if request.method == "POST":
        user = get_object_or_404(User, phone_number=user_phone)

        event_state_date_str = request.POST.get("event_state_date")
        event_end_date_str = request.POST.get("event_end_date")
        event_timezone = request.POST.get("timezone", "UTC")

        try:
            event_state_date = parse_and_validate_event_state_date(
                event_state_date_str, event_timezone
            )
        except ValueError as e:
            messages.error(request, str(e))
            return render_create()

        try:
            naive_end = datetime.fromisoformat(event_end_date_str)
            event_tz = pytz.timezone(event_timezone)
            localized_end = event_tz.localize(naive_end)
            event_end_date = localized_end.astimezone(pytz.UTC)

            if event_end_date <= event_state_date:
                raise ValueError("Event end time must be after start time")
        except (ValueError, TypeError) as e:
            if "after start time" in str(e):
                messages.error(request, str(e))
            else:
                messages.error(request, "Invalid end time format")
            return render_create()

        if "cover_photo" not in request.FILES:
            messages.error(request, "Please upload a cover photo.")
            return render_create()

        temp_photo_path = None
        try:
            temp_photo_path = sanitize_and_save_image(request.FILES["cover_photo"])
        except ValueError as e:
            messages.error(request, f"There's an issue with your cover photo: {str(e)}")
            return render_create()

        try:
            max_attendees_value = validate_max_attendees(
                unlimited_attendees, request.POST
            )
        except ValueError as e:
            messages.error(request, str(e))
            return render_create()

        allow_rsvp = request.POST.get("allow_rsvp") == "on"
        allow_maybe_rsvp = allow_rsvp and request.POST.get("allow_maybe_rsvp") == "on"
        question_entries = _extract_question_entries(request.POST)
        photo_album_url = request.POST.get("photo_album_url", "").strip()

        event = Event.objects.create(
            title=request.POST.get("title"),
            description=request.POST.get("description"),
            location=request.POST.get("location"),
            event_state_date=event_state_date,
            event_end_date=event_end_date,
            timezone=request.POST.get("timezone", "UTC"),
            cover_photo="",
            cover_photo_processing_status="pending",
            photo_album_url=photo_album_url,
            created_by=user,
            max_attendees=max_attendees_value,
            hide_attendee_count=request.POST.get("hide_attendee_count") == "on",
            is_listed=request.POST.get("is_listed") == "on",
            allow_rsvp=allow_rsvp,
            allow_maybe_rsvp=allow_maybe_rsvp,
            auto_reminders_enabled=request.POST.get("auto_reminders_enabled") == "on",
        )

        from events.tasks import process_uploaded_image

        process_uploaded_image.delay("event", str(event.id), temp_photo_path)
        logger.info(f"Queued async cover photo processing for event {event.id}")

        _save_event_questions(event, question_entries)

        logger.info(
            f"Event '{event.title}' (ID: {event.id}) created by user {user.phone_number}"
        )
        messages.success(request, "You've created your event!")
        return redirect("event_detail", event_id=event.id)

    return render_create({"detect_timezone_on_load": True})


def edit_event(request, event_id):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to edit events.")
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id, is_active=True)
    user = get_object_or_404(User, phone_number=user_phone)

    if not event.is_organizer(user):
        messages.error(request, "Only organizers can edit this event.")
        return redirect("event_detail", event_id=event_id)

    raw_question_rows = _build_question_form_rows(
        event=event,
        post_data=request.POST if request.method == "POST" else None,
    )
    question_rows = [row for row in raw_question_rows if row["has_text"]]
    unlimited_attendees = (
        (request.POST.get("unlimited_attendees") == "on")
        if request.method == "POST"
        else event.max_attendees is None
    )

    def render_edit(extra_context=None):
        context = {
            "event": event,
            "user_phone": user_phone,
            "is_creator": event.created_by_id == user.id,
            "question_rows": question_rows,
            "question_slots": QUESTION_SLOTS,
            "unlimited_attendees": unlimited_attendees,
            "timezones": get_common_timezones(),
            "default_timezone": event.timezone,
            "detect_timezone_on_load": False,
            "event_photo_album": event.photo_album_url,
            "initial_photo_album_url": event.photo_album_url,
        }
        if extra_context:
            context.update(extra_context)
        return render(request, "events/edit_event.html", context)

    if request.method == "POST":
        event_state_date_str = request.POST.get("event_state_date")
        event_end_date_str = request.POST.get("event_end_date")
        event_timezone = request.POST.get("timezone", "UTC")

        try:
            event_state_date = parse_and_validate_event_state_date(
                event_state_date_str, event_timezone
            )
        except ValueError as e:
            messages.error(request, str(e))
            return render_edit()

        try:
            naive_end = datetime.fromisoformat(event_end_date_str)
            event_tz = pytz.timezone(event_timezone)
            localized_end = event_tz.localize(naive_end)
            event_end_date = localized_end.astimezone(pytz.UTC)

            if event_end_date <= event_state_date:
                raise ValueError("Event end time must be after start time")
        except (ValueError, TypeError) as e:
            if "after start time" in str(e):
                messages.error(request, str(e))
            else:
                messages.error(request, "Invalid end time format")
            return render_edit()

        if "cover_photo" in request.FILES:
            try:
                temp_photo_path = sanitize_and_save_image(request.FILES["cover_photo"])
                event.cover_photo = ""
                event.cover_photo_avif_url = ""
                event.cover_photo_processing_status = "pending"

                from events.tasks import process_uploaded_image

                process_uploaded_image.delay("event", str(event.id), temp_photo_path)
                logger.info(f"Queued async cover photo processing for event {event.id}")
            except ValueError as e:
                messages.error(request, str(e))
                return render_edit()
        elif not (event.cover_photo or event.cover_photo_avif_url):
            messages.error(request, "Please upload a cover photo.")
            return render_edit()

        try:
            max_attendees_value = validate_max_attendees(
                unlimited_attendees, request.POST
            )
        except ValueError as e:
            messages.error(request, str(e))
            return render_edit()

        allow_rsvp = request.POST.get("allow_rsvp") == "on"
        allow_maybe_rsvp = allow_rsvp and request.POST.get("allow_maybe_rsvp") == "on"
        question_entries = _extract_question_entries(request.POST)
        photo_album_url = request.POST.get("photo_album_url", "").strip()

        event.title = request.POST.get("title")
        event.description = request.POST.get("description")
        event.location = request.POST.get("location")
        event.event_state_date = event_state_date
        event.event_end_date = event_end_date
        event.timezone = request.POST.get("timezone", "UTC")
        event.max_attendees = max_attendees_value
        event.hide_attendee_count = request.POST.get("hide_attendee_count") == "on"
        event.is_listed = request.POST.get("is_listed") == "on"
        event.allow_rsvp = allow_rsvp
        event.allow_maybe_rsvp = allow_maybe_rsvp
        event.auto_reminders_enabled = (
            request.POST.get("auto_reminders_enabled") == "on"
        )
        event.photo_album_url = photo_album_url
        event.save()

        _save_event_questions(event, question_entries)

        logger.info(
            f"Event '{event.title}' (ID: {event.id}) updated by user {user.phone_number}"
        )
        messages.success(request, "You've updated your event!")
        return redirect("event_detail", event_id=event.id)

    return render_edit()


def my_events(request):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to view your events.")
        return redirect("phone_login")

    user = get_object_or_404(User, phone_number=user_phone)
    now = timezone.now()

    rsvps_upcoming_attending = (
        RSVP.objects.filter(
            _event_not_ended_q(prefix="event", reference_time=now),
            user=user,
            status="attending",
        )
        .select_related("event", "event__created_by")
        .order_by("event__event_state_date")
    )

    rsvps_upcoming_maybe = (
        RSVP.objects.filter(
            _event_not_ended_q(prefix="event", reference_time=now),
            user=user,
            status="maybe",
        )
        .select_related("event", "event__created_by")
        .order_by("event__event_state_date")
    )

    rsvps_upcoming_not = (
        RSVP.objects.filter(
            _event_not_ended_q(prefix="event", reference_time=now),
            user=user,
            status="not_attending",
        )
        .select_related("event", "event__created_by")
        .order_by("event__event_state_date")
    )

    rsvps_past = (
        RSVP.objects.filter(
            _event_has_ended_q(prefix="event", reference_time=now), user=user
        )
        .select_related("event", "event__created_by")
        .order_by("-event__event_state_date")
    )

    created_upcoming = (
        Event.objects.filter(_event_not_ended_q(reference_time=now), created_by=user)
        .prefetch_related("organizers")
        .order_by("event_state_date")
    )

    created_past = (
        Event.objects.filter(_event_has_ended_q(reference_time=now), created_by=user)
        .prefetch_related("organizers")
        .order_by("-event_state_date")
    )

    context = {
        "user_phone": user_phone,
        "rsvps_upcoming_attending": rsvps_upcoming_attending,
        "rsvps_upcoming_maybe": rsvps_upcoming_maybe,
        "rsvps_upcoming_not": rsvps_upcoming_not,
        "rsvps_past": rsvps_past,
        "created_upcoming": created_upcoming,
        "created_past": created_past,
    }
    return render(request, "events/my_events.html", context)


def export_ical(request, event_id):
    event = get_object_or_404(Event, id=event_id, is_active=True)

    end_time = (
        event.event_end_date
        if event.event_end_date
        else event.event_state_date + timedelta(hours=2)
    )

    ical_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//{settings.PLATFORM_NAME}//Event Calendar//EN
BEGIN:VEVENT
UID:{event.id}@{settings.SITE_DOMAIN}
DTSTAMP:{datetime.now().strftime("%Y%m%dT%H%M%SZ")}
DTSTART:{event.event_state_date.strftime("%Y%m%dT%H%M%S")}
DTEND:{end_time.strftime("%Y%m%dT%H%M%S")}
SUMMARY:{event.title}
DESCRIPTION:{event.description.replace(chr(10), "\\n")}
LOCATION:{event.location}
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR"""

    response = HttpResponse(ical_content, content_type="text/calendar")
    safe_filename = slugify(event.title) or str(event.id)
    response["Content-Disposition"] = f'attachment; filename="{safe_filename}.ics"'
    return response


def edit_profile(request):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to edit your profile.")
        return redirect("phone_login")

    user = get_object_or_404(User, phone_number=user_phone)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        new_phone = request.POST.get("phone_number", "").strip()

        if not name:
            messages.error(request, "Please enter your name.")
            return render(request, "events/edit_profile.html", {"user": user})

        user.name = name
        user.save()
        request.session["user_name"] = name
        request.session.modified = True

        if new_phone and new_phone != str(user.phone_number):
            try:
                parsed_number = parse(new_phone, "US")
                formatted_number = (
                    f"+{parsed_number.country_code}{parsed_number.national_number}"
                )

                if (
                    User.objects.filter(phone_number=formatted_number)
                    .exclude(id=user.id)
                    .exists()
                ):
                    messages.error(
                        request,
                        "Another account already uses this phone number.",
                    )
                    return render(request, "events/edit_profile.html", {"user": user})

                auth_service = AuthService()
                success, result = auth_service.send_verification_code(formatted_number)

                if success:
                    request.session["new_phone_verification"] = formatted_number
                    request.session["current_user_id"] = str(user.id)
                    request.session.modified = True
                    messages.success(
                        request,
                        "We've sent a verification code to your new phone number!",
                    )
                    return redirect("verify_phone_change")
                else:
                    messages.error(request, f"We couldn't send your code: {result}")

            except NumberParseException:
                messages.error(request, "Please enter a valid phone number.")
        else:
            messages.success(request, "You've updated your profile!")
            return redirect("edit_profile")

    context = {
        "user": user,
        "user_phone": user_phone,
    }
    return render(request, "events/edit_profile.html", context)


def verify_phone_change(request):
    new_phone = request.session.get("new_phone_verification")
    user_id = request.session.get("current_user_id")

    if not new_phone or not user_id:
        messages.error(request, "We couldn't find your verification session.")
        return redirect("edit_profile")

    user = get_object_or_404(User, id=user_id)
    context = {
        "phone_number": new_phone,
        "user_phone": request.session.get("user_phone"),
    }

    if request.method == "POST":
        code = request.POST.get("code", "").strip()

        if not code:
            messages.error(request, "Please enter your verification code.")
            return render(request, "events/verify_phone_change.html", context)

        auth_service = AuthService()

        success, verified_user, error_type = auth_service.verify_code(new_phone, code)

        if success:
            if verified_user.id != user.id:
                verified_user.delete()

            user.phone_number = new_phone
            user.is_verified = True
            user.verification_code = ""
            user.save()

            request.session["user_phone"] = new_phone
            del request.session["new_phone_verification"]
            del request.session["current_user_id"]
            request.session.modified = True

            messages.success(request, "You've updated your phone number!")
            return redirect("edit_profile")
        else:
            messages.error(request, "That verification code is invalid.")
    return render(request, "events/verify_phone_change.html", context)


@ratelimit(key="user", rate="10/h", method="POST", block=True)
def send_text_blast(request, event_id):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to send text blasts.")
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id, is_active=True)
    user = get_object_or_404(User, phone_number=user_phone)

    if not event.is_organizer(user):
        messages.error(request, "Only organizers can send text blasts.")
        return redirect("event_detail", event_id=event_id)

    if event.text_blast_count >= 20:
        messages.error(request, "You've reached the text blast limit (20 per event).")
        return redirect("event_detail", event_id=event_id)

    if request.method == "POST":
        message_text = request.POST.get("message", "").strip()
        send_to = request.POST.get("send_to", "attending")
        display_on_page = request.POST.get("display_on_page") == "on"

        if not message_text:
            messages.error(request, "Please enter a message.")
            return render(
                request,
                "events/text_blast.html",
                {
                    "event": event,
                    "user_phone": user_phone,
                    "remaining_blasts": 20 - event.text_blast_count,
                    "attending_count": RSVP.objects.filter(
                        event=event, status="attending"
                    ).count(),
                    "maybe_count": RSVP.objects.filter(
                        event=event, status="maybe"
                    ).count(),
                },
            )

        if send_to == "both":
            rsvps = RSVP.objects.filter(event=event, status__in=["attending", "maybe"])
        elif send_to == "maybe":
            rsvps = RSVP.objects.filter(event=event, status="maybe")
        else:
            rsvps = RSVP.objects.filter(event=event, status="attending")

        if not rsvps.exists():
            messages.error(
                request, "We couldn't find any recipients for this selection."
            )
            return render(
                request,
                "events/text_blast.html",
                {
                    "event": event,
                    "user_phone": user_phone,
                    "remaining_blasts": 20 - event.text_blast_count,
                    "attending_count": RSVP.objects.filter(
                        event=event, status="attending"
                    ).count(),
                    "maybe_count": RSVP.objects.filter(
                        event=event, status="maybe"
                    ).count(),
                },
            )

        full_message = f"[{event.title}] {message_text} {event.get_short_url()}"

        phone_numbers = [str(rsvp.user.phone_number) for rsvp in rsvps]

        TextBlast.objects.create(
            event=event,
            sent_by=user,
            message=message_text,
            sent_to=send_to,
            recipient_count=len(phone_numbers),
            display_on_page=display_on_page,
        )

        send_bulk_sms.delay(phone_numbers, full_message)

        event.text_blast_count += 1
        event.save()

        logger.info(
            f"Text blast sent by {user.phone_number} for event '{event.title}' (ID: {event.id}) to {len(phone_numbers)} recipients"
        )
        blast_word = "blast" if (20 - event.text_blast_count) == 1 else "blasts"
        attendee_word = "attendee" if len(phone_numbers) == 1 else "attendees"
        messages.success(
            request,
            f"We've queued your text blast for {len(phone_numbers)} {attendee_word}. You have {20 - event.text_blast_count} {blast_word} remaining.",
        )

        return redirect("event_detail", event_id=event_id)

    attending_count = RSVP.objects.filter(event=event, status="attending").count()
    maybe_count = RSVP.objects.filter(event=event, status="maybe").count()

    context = {
        "event": event,
        "user_phone": user_phone,
        "remaining_blasts": 20 - event.text_blast_count,
        "attending_count": attending_count,
        "maybe_count": maybe_count,
    }
    return render(request, "events/text_blast.html", context)


def invite_organizer(request, event_id):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to invite organizers.")
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id, is_active=True)
    user = get_object_or_404(User, phone_number=user_phone)

    if not event.is_organizer(user):
        messages.error(request, "Only organizers can invite other organizers.")
        return redirect("event_detail", event_id=event_id)

    if not event.can_invite_organizer():
        messages.error(
            request, "You've reached the maximum number of organizers (5 total)."
        )
        return redirect("event_detail", event_id=event_id)

    if request.method == "POST":
        phone_number = request.POST.get("phone_number", "").strip()

        if not phone_number:
            messages.error(request, "Please enter a phone number.")
            return render(
                request,
                "events/invite_organizer.html",
                {
                    "event": event,
                    "user_phone": user_phone,
                },
            )

        formatted_number = normalize_phone_number(phone_number)

        if not formatted_number:
            messages.error(request, "Please enter a valid phone number.")
            return render(
                request,
                "events/invite_organizer.html",
                {
                    "event": event,
                    "user_phone": user_phone,
                    "current_organizers": event.organizers.all(),
                    "remaining_slots": 5 - event.organizers.count(),
                },
            )

        try:
            invitee = User.objects.get(phone_number=formatted_number)

            if event.created_by == invitee:
                messages.error(request, "This person already created the event.")
                return redirect("event_detail", event_id=event_id)

            if event.organizers.filter(id=invitee.id).exists():
                messages.error(request, "This person is already an organizer.")
                return redirect("event_detail", event_id=event_id)

            event.organizers.add(invitee)

            message = f"You've been added as an organizer for '{event.title}' on {event.event_state_date.strftime('%B %d at %I:%M %p')}. {event.get_short_url()}"
            send_single_sms.delay(formatted_number, message)

            logger.info(
                f"User {formatted_number} added as organizer for event '{event.title}' (ID: {event.id}) by {user.phone_number}"
            )
            messages.success(request, "You've invited your organizer!")
            return redirect("event_detail", event_id=event_id)

        except User.DoesNotExist:
            messages.error(
                request,
                "We couldn't find a user with this phone number. They need to sign up first.",
            )

    context = {
        "event": event,
        "user_phone": user_phone,
        "current_organizers": event.organizers.all(),
        "remaining_slots": 5 - event.organizers.count(),
    }
    return render(request, "events/invite_organizer.html", context)


def leave_event(request, event_id):
    if request.method != "POST":
        return redirect("event_detail", event_id=event_id)

    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in.")
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id, is_active=True)
    user = get_object_or_404(User, phone_number=user_phone)

    if event.created_by == user:
        messages.error(
            request,
            "You created this event, so you can't leave it. Delete the event instead.",
        )
        return redirect("event_detail", event_id=event_id)

    if not event.organizers.filter(id=user.id).exists():
        messages.error(request, "You aren't an organizer of this event.")
        return redirect("event_detail", event_id=event_id)

    event.organizers.remove(user)
    messages.success(request, "You've left the event organizing team.")
    return redirect("my_events")


def delete_event(request, event_id):
    if request.method != "POST":
        return redirect("event_detail", event_id=event_id)

    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in.")
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id)
    user = get_object_or_404(User, phone_number=user_phone)

    if event.created_by != user:
        messages.error(request, "Only the event creator can delete this event.")
        return redirect("event_detail", event_id=event_id)

    event_title = event.title

    if event.cover_photo:
        try:
            import os

            from django.conf import settings

            if not event.cover_photo.name.startswith("http"):
                file_path = os.path.join(settings.MEDIA_ROOT, str(event.cover_photo))
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception:
            pass

    event.delete()

    logger.info(f"Event '{event_title}' deleted by user {user.phone_number}")
    messages.success(request, f'You\'ve deleted "{event_title}".')
    return redirect("my_events")


def event_qr_code(request, event_id):
    event = get_object_or_404(Event, id=event_id, is_active=True)

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=1,
    )
    qr.add_data(event.get_short_url())
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    response = HttpResponse(buffer, content_type="image/png")
    return response


def invite_to_event(request, event_id):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to invite people.")
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id, is_active=True)
    user = get_object_or_404(User, phone_number=user_phone)

    is_organizer = event.is_organizer(user)
    is_attendee = RSVP.objects.filter(
        user=user, event=event, status__in=["attending", "maybe"]
    ).exists()

    if not is_organizer and not is_attendee:
        messages.error(request, "Only organizers and attendees can invite others.")
        return redirect("event_detail", event_id=event_id)

    if request.method == "POST":
        phone_numbers = request.POST.get("phone_numbers", "").strip()

        if not phone_numbers:
            messages.error(request, "Please enter at least one phone number.")
            return render(
                request,
                "events/invite_to_event.html",
                {
                    "event": event,
                    "user_phone": user_phone,
                },
            )

        phone_list = re.split(r"[,\n]+", phone_numbers)
        phone_list = [p.strip() for p in phone_list if p.strip()]

        if len(phone_list) > 20:
            messages.error(request, "You can send a maximum of 20 invites at once.")
            return render(
                request,
                "events/invite_to_event.html",
                {
                    "event": event,
                    "user_phone": user_phone,
                },
            )

        valid_numbers = []
        already_invited = []

        for phone in phone_list:
            normalized = normalize_phone_number(phone)
            if normalized:
                if EventInvitation.objects.filter(
                    event=event, phone_number=normalized
                ).exists():
                    already_invited.append(normalized)
                else:
                    valid_numbers.append(normalized)

        invitations_to_create = [
            EventInvitation(event=event, phone_number=phone, invited_by=user)
            for phone in valid_numbers
        ]
        EventInvitation.objects.bulk_create(invitations_to_create)

        inviter_name = user.name or "Someone"
        event_datetime_text = format_event_datetime(event, "%b %d at %I:%M %p %Z")
        message = f"{inviter_name} invited you to '{event.title}' on {event_datetime_text}. RSVP: {event.get_short_url()}"

        if valid_numbers:
            send_bulk_sms.delay(valid_numbers, message)

        logger.info(
            f"User {user.phone_number} invited {len(valid_numbers)} people to event '{event.title}' (ID: {event.id})"
        )
        invalid_count = len(phone_list) - len(valid_numbers) - len(already_invited)
        if valid_numbers:
            invite_word = "invite" if len(valid_numbers) == 1 else "invites"
            messages.success(
                request, f"You've sent {len(valid_numbers)} {invite_word}!"
            )
        if already_invited:
            number_word = "number was" if len(already_invited) == 1 else "numbers were"
            messages.warning(
                request,
                f"{len(already_invited)} {number_word} already invited to this event.",
            )
        if invalid_count > 0:
            number_word = "number was" if invalid_count == 1 else "numbers were"
            messages.warning(request, f"{invalid_count} phone {number_word} invalid.")

        return redirect("event_detail", event_id=event_id)

    context = {
        "event": event,
        "user_phone": user_phone,
        "short_url": event.get_short_url(),
    }
    return render(request, "events/invite_to_event.html", context)


def attendee_list(request, event_id):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to view the attendee list.")
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id, is_active=True)
    user = get_object_or_404(User, phone_number=user_phone)

    if not event.is_organizer(user):
        messages.error(request, "Only organizers can view the attendee list.")
        return redirect("event_detail", event_id=event_id)

    questions = list(event.questions.order_by("order", "id"))

    rsvps = list(
        RSVP.objects.filter(event=event)
        .select_related("user")
        .prefetch_related("answers__question")
        .order_by("status", "user__name")
    )

    user_timezone = request.session.get("user_timezone", "UTC")

    for rsvp in rsvps:
        answer_map = {
            answer.question_id: answer.answer for answer in rsvp.answers.all()
        }
        rsvp.answer_list = [answer_map.get(question.id, "") for question in questions]

        rsvp.updated_at_formatted = format_datetime_with_conditional_tz(
            rsvp.updated_at, event.timezone, user_timezone
        )

    attending = [rsvp for rsvp in rsvps if rsvp.status == "attending"]
    maybe = [rsvp for rsvp in rsvps if rsvp.status == "maybe"]
    not_attending = [rsvp for rsvp in rsvps if rsvp.status == "not_attending"]

    attending_count = len(attending)
    maybe_count = len(maybe)
    not_attending_count = len(not_attending)

    context = {
        "event": event,
        "user_phone": user_phone,
        "attending": attending,
        "maybe": maybe,
        "not_attending": not_attending,
        "attending_count": attending_count,
        "maybe_count": maybe_count,
        "not_attending_count": not_attending_count,
        "total_count": len(rsvps),
        "questions": questions,
    }
    return render(request, "events/attendee_list.html", context)


def download_attendee_list(request, event_id):
    user_phone = request.session.get("user_phone")
    if not user_phone:
        messages.error(request, "Please log in to download the attendee list.")
        return redirect("phone_login")

    event = get_object_or_404(Event, id=event_id, is_active=True)
    user = get_object_or_404(User, phone_number=user_phone)

    if not event.is_organizer(user):
        messages.error(request, "Only organizers can download the attendee list.")
        return redirect("event_detail", event_id=event_id)

    response = HttpResponse(content_type="text/csv")
    safe_event_slug = slugify(event.title) or str(event.id)
    response["Content-Disposition"] = (
        f'attachment; filename="{safe_event_slug}-attendees.csv"'
    )

    writer = csv.writer(response)
    questions = list(event.questions.order_by("order", "id"))
    header = ["Name", "Phone Number", "Status"]
    header.extend([question.text for question in questions])
    writer.writerow(header)

    rsvps = (
        RSVP.objects.filter(event=event)
        .select_related("user")
        .prefetch_related("answers__question")
        .order_by("status", "user__name")
    )

    try:
        event_tz = pytz.timezone(event.timezone)
    except pytz.UnknownTimeZoneError:
        logger.warning(
            f"Invalid timezone {event.timezone} for event {event.id}, using UTC"
        )
        event_tz = pytz.UTC

    for rsvp in rsvps:
        answer_map = {
            answer.question_id: answer.answer for answer in rsvp.answers.all()
        }

        rsvp_time_local = rsvp.updated_at.astimezone(event_tz)
        row = [
            rsvp.user.name or "N/A",
            str(rsvp.user.phone_number),
            rsvp.get_status_display(),
            rsvp_time_local.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        row.extend([answer_map.get(question.id, "") for question in questions])
        writer.writerow(row)

    return response


@require_POST
def add_question_row(request):
    existing_indexes = set()
    for key in request.POST.keys():
        if key.startswith("question_text_") or key.startswith("question_required_"):
            try:
                existing_indexes.add(int(key.split("_")[-1]))
            except (TypeError, ValueError):
                continue

    next_index = None
    for idx in range(1, QUESTION_SLOTS + 1):
        if idx not in existing_indexes:
            next_index = idx
            break

    if next_index is None:
        return HttpResponse("", status=204)

    row = {
        "index": next_index,
        "text": "",
        "required": False,
        "has_text": False,
        "question_id": None,
    }
    html = render_to_string("events/partials/question_row.html", {"row": row})
    return HttpResponse(html)


@require_POST
def remove_question_row(request):
    return HttpResponse("")


@require_POST
def set_user_timezone(request):
    try:
        data = json.loads(request.body)
        user_timezone = data.get("timezone")

        if user_timezone and user_timezone in pytz.all_timezones:
            request.session["user_timezone"] = user_timezone
            return HttpResponse(status=200)
        else:
            logger.warning(f"Invalid timezone received: {user_timezone}")
            return HttpResponse(status=400)
    except json.JSONDecodeError:
        return HttpResponse(status=400)
    except Exception as e:
        logger.error(f"Error setting user timezone: {e}")
        return HttpResponse(status=500)


@require_GET
def cover_photo_status(request, event_id):
    event = get_object_or_404(Event, id=event_id, is_active=True)

    response_data = {
        "status": event.cover_photo_processing_status,
        "avif_url": event.cover_photo_avif_url or "",
        "webp_url": event.cover_photo_webp_url or "",
        "original_url": event.cover_photo.url if event.cover_photo else "",
    }

    return HttpResponse(json.dumps(response_data), content_type="application/json")
