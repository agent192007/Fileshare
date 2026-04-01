import uuid

from django.db import models

from config.env import load_env

load_env()

def upload_path(instance, filename):
    return f"uploads/{instance.session_id}/{filename}"

class UploadedFile(models.Model):
    session_id = models.CharField(max_length=100, default=uuid.uuid4)
    delete_token = models.CharField(max_length=32, blank=True, default="")
    original_name = models.CharField(max_length=255, blank=True, default="")
    file = models.FileField(upload_to=upload_path)
    uploaded_at = models.DateTimeField(auto_now_add=True)
