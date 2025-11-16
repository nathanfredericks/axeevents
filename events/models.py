import secrets
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone
from phonenumber_field.modelfields import PhoneNumberField


class User(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = PhoneNumberField(unique=True)
    name = models.CharField(max_length=100, blank=False)
    verification_code = models.CharField(max_length=6, blank=True)
    verification_code_sent_at = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def generate_verification_code(self):
        self.verification_code = str(secrets.randbelow(1000000)).zfill(6)
        self.verification_code_sent_at = timezone.now()
        self.save()
        return self.verification_code

    def is_verification_code_expired(self):
        if not self.verification_code_sent_at:
            return True
        expiry_time = self.verification_code_sent_at + timedelta(minutes=5)
        return timezone.now() > expiry_time

    def can_resend_code(self):
        if not self.verification_code_sent_at:
            return True
        cooldown_time = self.verification_code_sent_at + timedelta(minutes=1)
        return timezone.now() > cooldown_time

    def get_resend_cooldown_seconds(self):
        if not self.verification_code_sent_at:
            return 0
        cooldown_time = self.verification_code_sent_at + timedelta(minutes=1)
        remaining = (cooldown_time - timezone.now()).total_seconds()
        return max(0, int(remaining))

    def __str__(self):
        return str(self.phone_number)

    @property
    def formatted_phone(self):
        from events.utils import format_display_phone

        return format_display_phone(self.phone_number)


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField()
    location = models.CharField(max_length=300)
    event_state_date = models.DateTimeField(verbose_name="Event start time")
    event_end_date = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=100, default="UTC")
    cover_photo = models.ImageField(
        upload_to="event_covers/%Y/%m/%d/", null=True, blank=True
    )
    cover_photo_processing_status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("complete", "Complete"),
            ("failed", "Failed"),
        ],
        default="complete",
    )
    cover_photo_avif_url = models.TextField(blank=True)
    cover_photo_webp_url = models.TextField(blank=True)
    photo_album_url = models.URLField(blank=True, verbose_name="Photo Album URL")
    created_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="created_events"
    )
    organizers = models.ManyToManyField(
        User, related_name="organizing_events", blank=True
    )
    short_code = models.CharField(max_length=8, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    max_attendees = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    hide_attendee_count = models.BooleanField(default=False)
    is_listed = models.BooleanField(default=True, verbose_name="Listed?")

    allow_rsvp = models.BooleanField(default=True, verbose_name="Allow RSVP")
    allow_maybe_rsvp = models.BooleanField(
        default=True, verbose_name="Allow Maybe RSVP"
    )

    reminder_24h_sent = models.BooleanField(default=False)
    reminder_1h_sent = models.BooleanField(default=False)
    auto_reminders_enabled = models.BooleanField(
        default=True, verbose_name="Enable auto reminders"
    )

    text_blast_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-event_state_date"]
        indexes = [
            models.Index(
                fields=["event_state_date", "is_active"], name="event_date_active_idx"
            ),
            models.Index(fields=["created_by"], name="event_creator_idx"),
            models.Index(fields=["short_code"], name="event_shortcode_idx"),
            models.Index(fields=["-event_state_date"], name="event_date_desc_idx"),
        ]

    def __str__(self):
        return self.title

    @property
    def end_time(self):
        return self.event_end_date or self.event_state_date

    @property
    def is_past(self):
        return self.end_time < timezone.now()

    @property
    def attendee_count(self):
        if hasattr(self, "_attendee_count_cache"):
            return self._attendee_count_cache
        return self.rsvps.filter(status="attending").count()

    @attendee_count.setter
    def attendee_count(self, value):
        self._attendee_count_cache = value

    @property
    def is_full(self):
        if self.max_attendees:
            return self.attendee_count >= self.max_attendees
        return False

    def is_organizer(self, user):
        return self.created_by == user or self.organizers.filter(id=user.id).exists()

    def can_invite_organizer(self):
        return self.organizers.count() < 5

    def save(self, *args, **kwargs):
        if not self.short_code:
            while True:
                code = secrets.token_urlsafe(6)[:8]
                if not Event.objects.filter(short_code=code).exists():
                    self.short_code = code
                    break
        super().save(*args, **kwargs)

    def get_short_url(self):
        from django.conf import settings

        domain = getattr(settings, "SITE_DOMAIN", "localhost:8000")
        return f"http://{domain}/e/{self.short_code}"


class EventQuestion(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questions")
    text = models.CharField(max_length=255)
    is_required = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Questionnaire"
        verbose_name_plural = "Questionnaires"

    def __str__(self):
        return f"{self.event.title} - {self.text}"


class TextBlast(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="text_blasts"
    )
    sent_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="sent_text_blasts"
    )
    message = models.TextField(max_length=500)
    sent_to = models.CharField(
        max_length=20,
        choices=[("attending", "Going"), ("maybe", "Maybe"), ("both", "Both")],
    )
    recipient_count = models.PositiveIntegerField(default=0)
    display_on_page = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Text blast for {self.event.title} at {self.created_at}"


class RSVP(models.Model):
    STATUS_CHOICES = [
        ("attending", "Going"),
        ("maybe", "Maybe"),
        ("not_attending", "Can't Go"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="rsvps")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="rsvps")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="attending"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "RSVP"
        verbose_name_plural = "RSVPs"
        unique_together = ["user", "event"]
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "event"], name="rsvp_user_event_idx"),
            models.Index(fields=["event", "status"], name="rsvp_event_status_idx"),
            models.Index(fields=["user", "status"], name="rsvp_user_status_idx"),
            models.Index(fields=["-created_at"], name="rsvp_created_desc_idx"),
        ]

    def __str__(self):
        return f"{self.user.phone_number} - {self.event.title} ({self.status})"


class RSVPAnswer(models.Model):
    rsvp = models.ForeignKey(RSVP, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(
        EventQuestion, on_delete=models.CASCADE, related_name="answers"
    )
    answer = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("rsvp", "question")]
        ordering = ["question__order", "question__id"]
        verbose_name = "Questionnaire"
        verbose_name_plural = "Questionnaires"

    def __str__(self):
        return f"{self.rsvp} -> {self.question.text}: {self.answer[:30]}"


class EventInvitation(models.Model):
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="invitations"
    )
    phone_number = PhoneNumberField()
    invited_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="sent_invitations"
    )
    invited_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("event", "phone_number")]
        ordering = ["-invited_at"]

    def __str__(self):
        return f"{self.phone_number} invited to {self.event.title}"
