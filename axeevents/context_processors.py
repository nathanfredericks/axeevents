from django.conf import settings


def platform(request):
    return {
        "PLATFORM_NAME": settings.PLATFORM_NAME,
        "user_phone": request.session.get("user_phone"),
    }
