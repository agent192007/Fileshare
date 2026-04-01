import io
import tempfile
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from config.env import load_env

from .models import UploadedFile

load_env()


class FileShareFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.temp_media_dir = tempfile.TemporaryDirectory()
        cls.override = override_settings(MEDIA_ROOT=cls.temp_media_dir.name)
        cls.override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        cls.temp_media_dir.cleanup()
        super().tearDownClass()

    def test_upload_returns_delete_token_and_saves_files(self):
        response = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-123",
                "delete_token": "token-123",
                "files[]": [SimpleUploadedFile("hello.txt", b"hello world")],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["session_id"], "session-123")
        self.assertEqual(payload["delete_token"], "token-123")
        self.assertEqual(UploadedFile.objects.filter(session_id="session-123").count(), 1)

    def test_cleanup_requires_valid_delete_token(self):
        uploaded_file = UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        invalid_response = self.client.post(
            reverse("cleanup"),
            {"session_id": uploaded_file.session_id, "delete_token": "wrong-token"},
        )
        self.assertEqual(invalid_response.status_code, 403)
        self.assertTrue(UploadedFile.objects.filter(pk=uploaded_file.pk).exists())

        valid_response = self.client.post(
            reverse("cleanup"),
            {"session_id": uploaded_file.session_id, "delete_token": uploaded_file.delete_token},
        )
        self.assertEqual(valid_response.status_code, 200)
        self.assertFalse(UploadedFile.objects.filter(pk=uploaded_file.pk).exists())

    def test_show_qr_contains_canonical_download_url(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(reverse("show_qr", kwargs={"session_id": "session-123"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/files/session-123/")

    def test_show_qr_includes_exit_cleanup_script(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(reverse("show_qr", kwargs={"session_id": "session-123"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "navigator.sendBeacon")
        self.assertContains(response, "pagehide")
        self.assertContains(response, reverse("cleanup"))

    def test_download_returns_zip_archive(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(reverse("download", kwargs={"session_id": "session-123"}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")

        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(archive.namelist(), ["hello.txt"])
        self.assertEqual(archive.read("hello.txt"), b"hello world")

    def test_receive_redirects_to_file_list_page(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.post(reverse("receive"), {"session_id": "session-123"})

        self.assertRedirects(response, reverse("session_files", kwargs={"session_id": "session-123"}))

    def test_session_file_list_page_contains_individual_download_link(self):
        uploaded_file = UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(reverse("session_files", kwargs={"session_id": "session-123"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("download_file", kwargs={"session_id": "session-123", "file_id": uploaded_file.id}),
        )

    def test_download_file_returns_single_file(self):
        uploaded_file = UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(
            reverse("download_file", kwargs={"session_id": "session-123", "file_id": uploaded_file.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="hello.txt"', response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"hello world")

    @override_settings(MAX_FILES_PER_SESSION=1)
    def test_upload_rejects_too_many_files(self):
        response = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-123",
                "delete_token": "token-123",
                "files[]": [
                    SimpleUploadedFile("one.txt", b"one"),
                    SimpleUploadedFile("two.txt", b"two"),
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("at most 1 files", response.json()["error"])

    @override_settings(MAX_FILE_SIZE_BYTES=4)
    def test_upload_rejects_file_over_size_limit(self):
        response = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-123",
                "delete_token": "token-123",
                "files[]": [SimpleUploadedFile("hello.txt", b"hello world")],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("exceeds the per-file limit", response.json()["error"])

    def test_download_renames_duplicate_zip_entries(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="report.txt",
            file=SimpleUploadedFile("report.txt", b"first"),
        )
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="report.txt",
            file=SimpleUploadedFile("report.txt", b"second"),
        )

        response = self.client.get(reverse("download", kwargs={"session_id": "session-123"}))

        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(sorted(archive.namelist()), ["report (1).txt", "report.txt"])
