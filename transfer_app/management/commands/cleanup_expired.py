import os
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from transfer_app.models import UploadedFile


class Command(BaseCommand):
    help = "Delete expired file-transfer sessions and their files from disk."

    def handle(self, *args, **options):
        expired = UploadedFile.objects.filter(expires_at__lte=timezone.now())
        count = expired.count()

        if count == 0:
            self.stdout.write("No expired sessions found.")
            return

        session_ids = set(expired.values_list("session_id", flat=True))

        for f in expired:
            if f.file and os.path.exists(f.file.path):
                f.file.delete(save=False)

        expired.delete()

        for sid in session_ids:
            folder = os.path.join(settings.MEDIA_ROOT, "uploads", sid)
            if os.path.isdir(folder):
                shutil.rmtree(folder, ignore_errors=True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleaned up {count} expired file(s) across {len(session_ids)} session(s)."
            )
        )
