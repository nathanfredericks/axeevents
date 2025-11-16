import logging
from datetime import timedelta
from io import BytesIO

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from events.auth import AuthService
from events.image_utils import (
    generate_avif_image,
    generate_webp_image,
    remove_gps_exif_data,
    resize_image,
)
from events.models import RSVP, Event
from events.utils import format_event_datetime

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def send_event_reminders(self):
    now = timezone.now()
    auth_service = AuthService()

    sent_count = 0
    error_count = 0

    try:
        reminder_24h_start = now + timedelta(hours=23, minutes=30)
        reminder_24h_end = now + timedelta(hours=24, minutes=30)

        events_24h = Event.objects.filter(
            event_state_date__gte=reminder_24h_start,
            event_state_date__lte=reminder_24h_end,
            is_active=True,
            reminder_24h_sent=False,
            auto_reminders_enabled=True,
        )

        for event in events_24h:
            rsvps = RSVP.objects.filter(event=event, status="attending")
            event_time_text = format_event_datetime(event, "%I:%M %p %Z")
            if not event_time_text and event.event_state_date:
                event_time_text = event.event_state_date.strftime("%I:%M %p")

            for rsvp in rsvps:
                try:
                    message = (
                        f"Reminder: {event.title} is tomorrow at {event_time_text}. "
                        f"Location: {event.location} {event.get_short_url()}"
                    )
                    success, result = auth_service.send_event_update(
                        rsvp.user.phone_number, message
                    )

                    if success:
                        logger.info(
                            f"Sent 24h reminder to {rsvp.user.phone_number} for {event.title}"
                        )
                        sent_count += 1
                    else:
                        logger.error(f"Failed to send 24h reminder: {result}")
                        error_count += 1
                except Exception as e:
                    logger.error(
                        f"Error sending 24h reminder to {rsvp.user.phone_number}: {str(e)}"
                    )
                    error_count += 1

            event.reminder_24h_sent = True
            event.save()
            logger.info(f"Marked 24h reminder sent for {event.title}")

        reminder_1h_start = now + timedelta(minutes=30)
        reminder_1h_end = now + timedelta(hours=1, minutes=30)

        events_1h = Event.objects.filter(
            event_state_date__gte=reminder_1h_start,
            event_state_date__lte=reminder_1h_end,
            is_active=True,
            reminder_1h_sent=False,
            auto_reminders_enabled=True,
        )

        for event in events_1h:
            rsvps = RSVP.objects.filter(event=event, status="attending")
            event_time_text = format_event_datetime(event, "%I:%M %p %Z")
            if not event_time_text and event.event_state_date:
                event_time_text = event.event_state_date.strftime("%I:%M %p")

            for rsvp in rsvps:
                try:
                    message = (
                        f"Starting soon: {event.title} at {event_time_text}. "
                        f"Location: {event.location} {event.get_short_url()}"
                    )
                    success, result = auth_service.send_event_update(
                        rsvp.user.phone_number, message
                    )

                    if success:
                        logger.info(
                            f"Sent 1h reminder to {rsvp.user.phone_number} for {event.title}"
                        )
                        sent_count += 1
                    else:
                        logger.error(f"Failed to send 1h reminder: {result}")
                        error_count += 1
                except Exception as e:
                    logger.error(
                        f"Error sending 1h reminder to {rsvp.user.phone_number}: {str(e)}"
                    )
                    error_count += 1

            event.reminder_1h_sent = True
            event.save()
            logger.info(f"Marked 1h reminder sent for {event.title}")

        result_msg = f"Reminder task completed: {sent_count} sent, {error_count} errors"
        logger.info(result_msg)
        return result_msg

    except Exception as exc:
        logger.error(f"Error in send_event_reminders task: {str(exc)}")
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries))


@shared_task(bind=True, max_retries=3)
def send_single_sms(self, phone_number, message):
    import sys

    from django.conf import settings

    if settings.DEBUG:
        if "test" not in sys.argv:
            logger.info(message)
        return {"success": True, "message": "SMS sent successfully"}

    try:
        auth_service = AuthService()
        success, result = auth_service.send_event_update(phone_number, message)

        if success:
            logger.info(f"Successfully sent SMS to {phone_number}")
            return {"success": True, "message": "SMS sent successfully"}
        else:
            logger.error(f"Failed to send SMS to {phone_number}: {result}")
            raise self.retry(exc=Exception(result), countdown=60)

    except Exception as e:
        logger.error(f"Error sending SMS to {phone_number}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (2**self.request.retries))
        return {"success": False, "error": str(e)}


@shared_task
def send_bulk_sms(phone_numbers, message):
    sent_count = 0
    failed_count = 0

    for phone_number in phone_numbers:
        try:
            send_single_sms.delay(phone_number, message)
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to queue SMS for {phone_number}: {str(e)}")
            failed_count += 1

    logger.info(f"Bulk SMS queued: {sent_count} queued, {failed_count} failed to queue")
    return {"queued": sent_count, "failed": failed_count}


@shared_task(bind=True, max_retries=3)
def process_uploaded_image(self, model_type, instance_id, temp_file_path):
    import os
    import uuid

    from django.conf import settings
    from django.core.files.base import ContentFile
    from PIL import Image

    from events.models import Event

    logger.info(
        f"Starting image processing for {model_type} {instance_id} from {temp_file_path}"
    )

    try:
        if model_type == "event":
            instance = Event.objects.get(id=instance_id)
            status_field = "cover_photo_processing_status"
            avif_field = "cover_photo_avif_url"
            webp_field = "cover_photo_webp_url"
            upload_dir = "event_covers"
        else:
            raise ValueError(f"Invalid model_type: {model_type}")

        setattr(instance, status_field, "processing")
        instance.save(update_fields=[status_field])

        if not os.path.exists(temp_file_path):
            raise FileNotFoundError(f"Temp file not found: {temp_file_path}")

        img = Image.open(temp_file_path)
        img.verify()
        img = Image.open(temp_file_path)

        try:
            img = remove_gps_exif_data(img)
        except Exception as e:
            logger.warning(f"Failed to remove GPS EXIF data: {str(e)}")

        img = resize_image(img, max_dimension=2048)

        avif_buffer = generate_avif_image(img, quality=80)
        avif_filename = f"{uuid.uuid4()}.avif"

        webp_buffer = generate_webp_image(img, quality=85)
        webp_filename = f"{uuid.uuid4()}.webp"

        if os.environ.get("ENVIRONMENT") == "production":
            import boto3
            from botocore.exceptions import ClientError

            try:
                s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                    region_name=settings.AWS_S3_REGION_NAME,
                )

                bucket_name = settings.AWS_STORAGE_BUCKET_NAME

                avif_key = f"{upload_dir}/{avif_filename}"
                s3_client.upload_fileobj(
                    avif_buffer,
                    bucket_name,
                    avif_key,
                    ExtraArgs={"ContentType": "image/avif"},
                )
                avif_url = f"https://{bucket_name}.s3.{settings.AWS_S3_REGION_NAME}.amazonaws.com/{avif_key}"

                webp_key = f"{upload_dir}/{webp_filename}"
                s3_client.upload_fileobj(
                    webp_buffer,
                    bucket_name,
                    webp_key,
                    ExtraArgs={"ContentType": "image/webp"},
                )
                webp_url = f"https://{bucket_name}.s3.{settings.AWS_S3_REGION_NAME}.amazonaws.com/{webp_key}"

                logger.info(f"Uploaded to S3: {avif_key}, {webp_key}")

            except ClientError as e:
                logger.error(f"S3 upload failed: {str(e)}")
                raise

        else:
            from datetime import datetime

            date_path = datetime.now().strftime("%Y/%m/%d")
            full_upload_dir = os.path.join(settings.MEDIA_ROOT, upload_dir, date_path)
            os.makedirs(full_upload_dir, exist_ok=True)

            avif_path = os.path.join(full_upload_dir, avif_filename)
            with open(avif_path, "wb") as f:
                f.write(avif_buffer.read())
            avif_url = f"{settings.MEDIA_URL}{upload_dir}/{date_path}/{avif_filename}"

            webp_path = os.path.join(full_upload_dir, webp_filename)
            with open(webp_path, "wb") as f:
                f.write(webp_buffer.read())
            webp_url = f"{settings.MEDIA_URL}{upload_dir}/{date_path}/{webp_filename}"

            logger.info(f"Saved to local media: {avif_path}, {webp_path}")

        setattr(instance, avif_field, avif_url)
        setattr(instance, webp_field, webp_url)
        setattr(instance, status_field, "complete")
        instance.save(update_fields=[avif_field, webp_field, status_field])

        try:
            os.remove(temp_file_path)
            logger.info(f"Removed temp file: {temp_file_path}")
        except Exception as e:
            logger.warning(f"Failed to remove temp file: {str(e)}")

        result_msg = f"Image processing completed for {model_type} {instance_id}"
        logger.info(result_msg)
        return {"success": True, "avif_url": avif_url, "webp_url": webp_url}

    except Exception as exc:
        logger.error(
            f"Error processing image for {model_type} {instance_id}: {str(exc)}"
        )

        try:
            if model_type == "event":
                instance = Event.objects.get(id=instance_id)
                instance.cover_photo_processing_status = "failed"
                instance.save(update_fields=["cover_photo_processing_status"])
        except Exception as e:
            logger.error(f"Failed to update status to failed: {str(e)}")

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries))

        return {"success": False, "error": str(exc)}
