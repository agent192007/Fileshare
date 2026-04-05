import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from config.env import load_env

load_env()


def upload_path(instance, filename):
    return f"uploads/{instance.session_id}/{filename}"


def default_expiry():
    hours = getattr(settings, "SESSION_TTL_HOURS", 24)
    return timezone.now() + timedelta(hours=hours)


class UploadedFile(models.Model):
    session_id = models.CharField(max_length=100, default=uuid.uuid4)
    delete_token = models.CharField(max_length=32, blank=True, default="")
    original_name = models.CharField(max_length=255, blank=True, default="")
    file = models.FileField(upload_to=upload_path)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_expiry)
    password_hash = models.CharField(max_length=128, blank=True, default="")
    is_encrypted = models.BooleanField(default=False)

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at
