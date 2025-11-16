from django.contrib import admin
from django.utils.html import format_html_join

from events.models import RSVP, Event, EventInvitation, TextBlast, User

admin.site.site_header = "AxeEvents administration"
admin.site.site_title = "AxeEvents administration"
admin.site.index_title = "AxeEvents administration"


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["name", "phone_number", "is_verified", "created_at"]
    list_filter = ["is_verified", "created_at"]
    search_fields = ["phone_number", "name"]
    readonly_fields = ["created_at"]
    ordering = ["-created_at"]
    fields = ["name", "phone_number", "created_at"]


class TextBlastInline(admin.StackedInline):
    model = TextBlast
    extra = 0
    can_delete = False
    show_change_link = False
    readonly_fields = ["message", "sent_to", "display_on_page", "created_at"]
    fields = [
        "message",
        "sent_to",
        "display_on_page",
        "created_at",
    ]

    def has_add_permission(self, request, obj=None):
        return False


class RSVPInline(admin.StackedInline):
    model = RSVP
    extra = 0
    can_delete = False
    show_change_link = False
    readonly_fields = [
        "user",
        "status",
        "answers_display",
        "created_at",
        "updated_at",
    ]
    fields = [
        "user",
        "status",
        "answers_display",
        "created_at",
        "updated_at",
    ]

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.select_related("user").prefetch_related("answers__question")

    def answers_display(self, obj):
        answers = list(obj.answers.all())
        if not answers:
            return "No answers"
        return format_html_join(
            "",
            "<div><strong>{}</strong>: {}</div>",
            ((answer.question.text, answer.answer or "—") for answer in answers),
        )

    answers_display.short_description = "Questionnaire"


class EventInvitationInline(admin.TabularInline):
    model = EventInvitation
    extra = 0
    can_delete = False
    show_change_link = False
    verbose_name = "Invitation"
    verbose_name_plural = "Invitations"
    readonly_fields = ["phone_number", "invited_by", "invited_at"]
    fields = ["phone_number", "invited_by", "invited_at"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "event_state_date",
        "location",
        "created_by",
        "is_active",
        "is_listed",
        "attendee_count",
    ]
    list_filter = [
        "is_active",
        "is_listed",
        "auto_reminders_enabled",
        "event_state_date",
        "created_at",
    ]
    search_fields = ["title", "description", "location", "short_code"]
    readonly_fields = [
        "created_at",
        "updated_at",
        "attendee_count",
        "text_blast_count",
        "reminder_24h_sent",
        "reminder_1h_sent",
    ]
    date_hierarchy = "event_state_date"
    filter_horizontal = ["organizers"]
    raw_id_fields = ["created_by"]
    inlines = [RSVPInline, EventInvitationInline, TextBlastInline]

    fieldsets = (
        (
            "Event Information",
            {
                "fields": (
                    "cover_photo",
                    "title",
                    "description",
                    "location",
                    "photo_album_url",
                )
            },
        ),
        (
            "Date and Time",
            {"fields": ("event_state_date", "event_end_date", "timezone")},
        ),
        (
            "Organization",
            {"fields": ("created_by", "organizers")},
        ),
        (
            "Settings",
            {
                "fields": (
                    "is_listed",
                    "allow_rsvp",
                    "allow_maybe_rsvp",
                    "hide_attendee_count",
                    "auto_reminders_enabled",
                    "max_attendees",
                )
            },
        ),
        (
            "Statistics",
            {
                "fields": (
                    "attendee_count",
                    "text_blast_count",
                    "reminder_24h_sent",
                    "reminder_1h_sent",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    def attendee_count(self, obj):
        return obj.attendee_count

    attendee_count.short_description = "Attendees"

    class Media:
        css = {"all": ("events/admin.css",)}
