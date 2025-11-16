import os
import uuid
from io import BytesIO

from django.conf import settings
from PIL import Image
from PIL.ExifTags import TAGS


def remove_gps_exif_data(img):
    if not hasattr(img, "getexif"):
        return img

    exif = img.getexif()
    gps_tags_to_remove = []

    for tag_id in list(exif.keys()):
        tag_name = TAGS.get(tag_id, tag_id)
        if tag_name == "GPSInfo" or tag_id == 34853:
            gps_tags_to_remove.append(tag_id)

    for tag in gps_tags_to_remove:
        if tag in exif:
            del exif[tag]

    if gps_tags_to_remove:
        temp_buffer = BytesIO()
        img.save(
            temp_buffer,
            format=img.format or "JPEG",
            exif=exif,
            quality=85,
            optimize=True,
        )
        temp_buffer.seek(0)
        img = Image.open(temp_buffer)

    return img


def resize_image(img, max_dimension=2048):
    if max(img.size) > max_dimension:
        img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    return img


def generate_avif_image(img, quality=80):
    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3] if len(img.split()) == 4 else None)
        img = background
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buffer = BytesIO()
    img.save(buffer, format="AVIF", quality=quality, optimize=True)
    buffer.seek(0)
    return buffer


def generate_webp_image(img, quality=85):
    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3] if len(img.split()) == 4 else None)
        img = background
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buffer = BytesIO()
    img.save(buffer, format="WEBP", quality=quality, optimize=True)
    buffer.seek(0)
    return buffer


def sanitize_and_save_image(uploaded_file):
    allowed_extensions = [".jpg", ".jpeg", ".png", ".gif", ".webp"]
    file_ext = os.path.splitext(uploaded_file.name)[1].lower()

    if file_ext not in allowed_extensions:
        raise ValueError(
            f"Invalid file type. Allowed types: {', '.join(allowed_extensions)}"
        )

    max_size = 10 * 1024 * 1024
    if uploaded_file.size > max_size:
        raise ValueError("File size too large. Maximum size is 10MB")

    try:
        img = Image.open(uploaded_file)
        img.verify()
        uploaded_file.seek(0)

        temp_dir = os.path.join(settings.MEDIA_ROOT, "temp_uploads")
        os.makedirs(temp_dir, exist_ok=True)

        temp_filename = f"{uuid.uuid4()}{file_ext}"
        temp_path = os.path.join(temp_dir, temp_filename)

        with open(temp_path, "wb") as temp_file:
            for chunk in uploaded_file.chunks():
                temp_file.write(chunk)

        return temp_path
    except Exception as exc:
        raise ValueError(f"Invalid or corrupted image file: {str(exc)}") from exc
