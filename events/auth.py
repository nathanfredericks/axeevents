import logging
import sys

import boto3
from botocore.exceptions import ClientError
from django.conf import settings

from events.models import User

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self):
        self.client = boto3.client(
            "pinpoint-sms-voice-v2",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
        )
        self.pool_id = settings.AWS_SMS_POOL_ID
        self.origination_number = settings.AWS_SMS_ORIGINATION_NUMBER

    def send_verification_code(self, phone_number: str):
        user, created = User.objects.get_or_create(phone_number=phone_number)
        code = user.generate_verification_code()

        message_body = f"Your verification code is {code}."

        try:
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                response = self.client.send_text_message(
                    DestinationPhoneNumber=str(phone_number),
                    OriginationIdentity=self.origination_number or self.pool_id,
                    MessageBody=message_body,
                    MessageType="TRANSACTIONAL",
                )
                message_id = response["MessageId"]
                logger.info(
                    f"Verification code sent to {phone_number}, MessageId: {message_id}"
                )
                return True, message_id
            else:
                if "test" not in sys.argv:
                    logger.info(
                        "Your verification code is %s",
                        code,
                        extra={"phone_number": phone_number},
                    )
                return True, "debug"
        except ClientError as e:
            logger.error(
                f"Failed to send verification code to {phone_number}: {str(e)}"
            )
            return False, str(e)
        except Exception as e:
            logger.error(
                f"Failed to send verification code to {phone_number}: {str(e)}"
            )
            return False, str(e)

    def verify_code(self, phone_number, code):
        try:
            user = User.objects.get(phone_number=phone_number)

            if user.is_verification_code_expired():
                logger.warning(f"Expired verification code for {phone_number}")
                return False, None, "expired"

            if user.verification_code == code:
                user.is_verified = True
                user.verification_code = ""
                user.verification_code_sent_at = None
                user.save()
                logger.info(f"User {phone_number} successfully verified")
                return True, user, None

            logger.warning(f"Invalid verification code attempt for {phone_number}")
            return False, None, "invalid"
        except User.DoesNotExist:
            logger.error(f"Verification attempt for non-existent user {phone_number}")
            return False, None, "not_found"

    def send_event_update(self, phone_number, message):
        try:
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                response = self.client.send_text_message(
                    DestinationPhoneNumber=str(phone_number),
                    OriginationIdentity=self.origination_number or self.pool_id,
                    MessageBody=message,
                    MessageType="TRANSACTIONAL",
                )
                message_id = response["MessageId"]
                logger.info(
                    f"Event update sent to {phone_number}, MessageId: {message_id}"
                )
                return True, message_id
            else:
                if "test" not in sys.argv:
                    logger.debug(message, extra={"phone_number": phone_number})
                return True, "debug"
        except ClientError as e:
            logger.error(f"Failed to send event update to {phone_number}: {str(e)}")
            return False, str(e)
        except Exception as e:
            logger.error(f"Failed to send event update to {phone_number}: {str(e)}")
            return False, str(e)
