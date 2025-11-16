from django.urls import path

from events import views

urlpatterns = [
    path("", views.index, name="index"),
    path("login/", views.phone_login, name="phone_login"),
    path("verify/", views.verify_code, name="verify_code"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.edit_profile, name="edit_profile"),
    path(
        "profile/verify-phone/", views.verify_phone_change, name="verify_phone_change"
    ),
    path("e/<str:short_code>/", views.event_short_url, name="event_short_url"),
    path("event/<uuid:event_id>/", views.event_detail, name="event_detail"),
    path("event/<uuid:event_id>/edit/", views.edit_event, name="edit_event"),
    path("event/<uuid:event_id>/qr-code/", views.event_qr_code, name="event_qr_code"),
    path("event/<uuid:event_id>/rsvp/", views.rsvp_event, name="rsvp_event"),
    path("event/<uuid:event_id>/export/", views.export_ical, name="export_ical"),
    path(
        "event/<uuid:event_id>/text-blast/",
        views.send_text_blast,
        name="send_text_blast",
    ),
    path(
        "event/<uuid:event_id>/invite-organizer/",
        views.invite_organizer,
        name="invite_organizer",
    ),
    path(
        "event/<uuid:event_id>/invite/", views.invite_to_event, name="invite_to_event"
    ),
    path("event/<uuid:event_id>/attendees/", views.attendee_list, name="attendee_list"),
    path(
        "event/<uuid:event_id>/attendees/download/",
        views.download_attendee_list,
        name="download_attendee_list",
    ),
    path("event/<uuid:event_id>/leave/", views.leave_event, name="leave_event"),
    path("event/<uuid:event_id>/delete/", views.delete_event, name="delete_event"),
    path(
        "event/<uuid:event_id>/cover-photo-status/",
        views.cover_photo_status,
        name="cover_photo_status",
    ),
    path("event/create/", views.create_event, name="create_event"),
    path("login/resend-code/", views.resend_code, name="resend_code"),
    path("my-events/", views.my_events, name="my_events"),
    path("questionnaire/add/", views.add_question_row, name="question_row_add"),
    path(
        "questionnaire/remove/", views.remove_question_row, name="question_row_remove"
    ),
    path("set-timezone/", views.set_user_timezone, name="set_user_timezone"),
]
