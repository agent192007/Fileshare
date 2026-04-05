import io
import tempfile
import zipfile
from datetime import timedelta

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from config.env import load_env

from .models import UploadedFile

load_env()


@override_settings(UPLOAD_RATE_LIMIT=1000)
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

    def setUp(self):
        cache.clear()

    # ── Upload tests ─────────────────────────────────────────────

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
        self.assertEqual(
            UploadedFile.objects.filter(session_id="session-123").count(), 1
        )

    @override_settings(MAX_FILES_PER_SESSION=1, UPLOAD_RATE_LIMIT=1000)
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

    @override_settings(MAX_FILE_SIZE_BYTES=4, UPLOAD_RATE_LIMIT=1000)
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

    # ── Cleanup tests ────────────────────────────────────────────

    def test_cleanup_requires_valid_delete_token(self):
        uploaded_file = UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        invalid_response = self.client.post(
            reverse("cleanup"),
            {
                "session_id": uploaded_file.session_id,
                "delete_token": "wrong-token",
            },
        )
        self.assertEqual(invalid_response.status_code, 403)
        self.assertTrue(UploadedFile.objects.filter(pk=uploaded_file.pk).exists())

        valid_response = self.client.post(
            reverse("cleanup"),
            {
                "session_id": uploaded_file.session_id,
                "delete_token": uploaded_file.delete_token,
            },
        )
        self.assertEqual(valid_response.status_code, 200)
        self.assertFalse(UploadedFile.objects.filter(pk=uploaded_file.pk).exists())

    # ── QR tests ─────────────────────────────────────────────────

    def test_show_qr_contains_canonical_download_url(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(
            reverse("show_qr", kwargs={"session_id": "session-123"})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/files/session-123/")

    def test_show_qr_includes_exit_cleanup_script(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(
            reverse("show_qr", kwargs={"session_id": "session-123"})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "navigator.sendBeacon")
        self.assertContains(response, "pagehide")
        self.assertContains(response, reverse("cleanup"))

    # ── Download tests ───────────────────────────────────────────

    def test_download_returns_zip_archive(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(
            reverse("download", kwargs={"session_id": "session-123"})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")

        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(archive.namelist(), ["hello.txt"])
        self.assertEqual(archive.read("hello.txt"), b"hello world")

    def test_download_file_returns_single_file(self):
        uploaded_file = UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(
            reverse(
                "download_file",
                kwargs={
                    "session_id": "session-123",
                    "file_id": uploaded_file.id,
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'attachment; filename="hello.txt"', response["Content-Disposition"]
        )
        self.assertEqual(b"".join(response.streaming_content), b"hello world")

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

        response = self.client.get(
            reverse("download", kwargs={"session_id": "session-123"})
        )

        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(
            sorted(archive.namelist()), ["report (1).txt", "report.txt"]
        )

    # ── Receive tests ────────────────────────────────────────────

    def test_receive_redirects_to_file_list_page(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.post(
            reverse("receive"), {"session_id": "session-123"}
        )

        self.assertRedirects(
            response,
            reverse("session_files", kwargs={"session_id": "session-123"}),
        )

    def test_receive_strips_encryption_key_from_code(self):
        UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.post(
            reverse("receive"), {"session_id": "session-123#someEncryptionKey"}
        )

        self.assertRedirects(
            response,
            reverse("session_files", kwargs={"session_id": "session-123"}),
        )

    # ── File list tests ──────────────────────────────────────────

    def test_session_file_list_page_contains_individual_download_link(self):
        uploaded_file = UploadedFile.objects.create(
            session_id="session-123",
            delete_token="token-123",
            original_name="hello.txt",
            file=SimpleUploadedFile("hello.txt", b"hello world"),
        )

        response = self.client.get(
            reverse("session_files", kwargs={"session_id": "session-123"})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse(
                "download_file",
                kwargs={
                    "session_id": "session-123",
                    "file_id": uploaded_file.id,
                },
            ),
        )

    # ── Expiration tests ─────────────────────────────────────────

    def test_expired_files_are_not_accessible(self):
        UploadedFile.objects.create(
            session_id="session-expired",
            delete_token="token-123",
            original_name="old.txt",
            file=SimpleUploadedFile("old.txt", b"old data"),
            expires_at=timezone.now() - timedelta(hours=1),
        )

        response = self.client.get(
            reverse("session_files", kwargs={"session_id": "session-expired"})
        )
        self.assertEqual(response.status_code, 404)

    def test_expired_session_rejected_at_receive(self):
        UploadedFile.objects.create(
            session_id="session-expired",
            delete_token="token-123",
            original_name="old.txt",
            file=SimpleUploadedFile("old.txt", b"old data"),
            expires_at=timezone.now() - timedelta(hours=1),
        )

        response = self.client.post(
            reverse("receive"), {"session_id": "session-expired"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "expired")

    def test_download_file_returns_410_for_expired(self):
        uploaded_file = UploadedFile.objects.create(
            session_id="session-expired",
            delete_token="token-123",
            original_name="old.txt",
            file=SimpleUploadedFile("old.txt", b"old data"),
            expires_at=timezone.now() - timedelta(hours=1),
        )

        response = self.client.get(
            reverse(
                "download_file",
                kwargs={
                    "session_id": "session-expired",
                    "file_id": uploaded_file.id,
                },
            )
        )
        self.assertEqual(response.status_code, 410)

    # ── Rate limiting tests ──────────────────────────────────────

    @override_settings(UPLOAD_RATE_LIMIT=2, UPLOAD_RATE_WINDOW=60)
    def test_rate_limiting_blocks_excessive_uploads(self):
        for i in range(2):
            resp = self.client.post(
                reverse("upload_file"),
                {
                    "session_id": f"session-rl-{i}",
                    "delete_token": "token",
                    "files[]": [
                        SimpleUploadedFile(f"f{i}.txt", b"data")
                    ],
                },
            )
            self.assertEqual(resp.status_code, 200)

        # Third should be rate limited
        resp = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-rl-3",
                "delete_token": "token",
                "files[]": [SimpleUploadedFile("f3.txt", b"data")],
            },
        )
        self.assertEqual(resp.status_code, 429)

    # ── File extension blocking tests ────────────────────────────

    @override_settings(
        BLOCKED_FILE_EXTENSIONS=[".exe", ".bat"],
        UPLOAD_RATE_LIMIT=1000,
    )
    def test_blocked_extension_rejected(self):
        response = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-ext",
                "delete_token": "token",
                "files[]": [SimpleUploadedFile("virus.exe", b"bad")],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not allowed", response.json()["error"])

    @override_settings(
        BLOCKED_FILE_EXTENSIONS=[".exe"],
        UPLOAD_RATE_LIMIT=1000,
    )
    def test_allowed_extension_accepted(self):
        response = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-ext-ok",
                "delete_token": "token",
                "files[]": [SimpleUploadedFile("doc.pdf", b"pdf data")],
            },
        )
        self.assertEqual(response.status_code, 200)

    # ── Password protection tests ────────────────────────────────

    def test_password_protected_session_requires_password(self):
        from django.contrib.auth.hashers import make_password

        UploadedFile.objects.create(
            session_id="session-pw",
            delete_token="token-123",
            original_name="secret.txt",
            file=SimpleUploadedFile("secret.txt", b"secret data"),
            password_hash=make_password("mypassword"),
        )

        # GET without password should show password form
        response = self.client.get(
            reverse("session_files", kwargs={"session_id": "session-pw"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Password Protected")

    def test_wrong_password_rejected(self):
        from django.contrib.auth.hashers import make_password

        UploadedFile.objects.create(
            session_id="session-pw2",
            delete_token="token-123",
            original_name="secret.txt",
            file=SimpleUploadedFile("secret.txt", b"secret data"),
            password_hash=make_password("correct"),
        )

        response = self.client.post(
            reverse("session_files", kwargs={"session_id": "session-pw2"}),
            {"password": "wrong"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incorrect password")

    def test_correct_password_grants_access(self):
        from django.contrib.auth.hashers import make_password

        UploadedFile.objects.create(
            session_id="session-pw3",
            delete_token="token-123",
            original_name="secret.txt",
            file=SimpleUploadedFile("secret.txt", b"secret data"),
            password_hash=make_password("correct"),
        )

        response = self.client.post(
            reverse("session_files", kwargs={"session_id": "session-pw3"}),
            {"password": "correct"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "secret.txt")
        self.assertNotContains(response, "Password Protected")

    def test_password_verified_persists_in_session(self):
        from django.contrib.auth.hashers import make_password

        UploadedFile.objects.create(
            session_id="session-pw4",
            delete_token="token-123",
            original_name="secret.txt",
            file=SimpleUploadedFile("secret.txt", b"secret data"),
            password_hash=make_password("correct"),
        )

        # Enter correct password
        self.client.post(
            reverse("session_files", kwargs={"session_id": "session-pw4"}),
            {"password": "correct"},
        )

        # Subsequent GET should work without password
        response = self.client.get(
            reverse("session_files", kwargs={"session_id": "session-pw4"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "secret.txt")
        self.assertNotContains(response, "Password Protected")

    def test_download_blocked_without_password_verification(self):
        from django.contrib.auth.hashers import make_password

        uploaded_file = UploadedFile.objects.create(
            session_id="session-pw5",
            delete_token="token-123",
            original_name="secret.txt",
            file=SimpleUploadedFile("secret.txt", b"secret data"),
            password_hash=make_password("pass"),
        )

        response = self.client.get(
            reverse(
                "download_file",
                kwargs={
                    "session_id": "session-pw5",
                    "file_id": uploaded_file.id,
                },
            )
        )
        self.assertEqual(response.status_code, 403)

    # ── Encryption flag tests ────────────────────────────────────

    def test_upload_with_encryption_flag(self):
        response = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-enc",
                "delete_token": "token",
                "is_encrypted": "true",
                "files[]": [SimpleUploadedFile("enc.bin", b"ciphertext")],
            },
        )

        self.assertEqual(response.status_code, 200)
        f = UploadedFile.objects.get(session_id="session-enc")
        self.assertTrue(f.is_encrypted)

    def test_encrypted_session_shows_e2ee_badge(self):
        UploadedFile.objects.create(
            session_id="session-enc2",
            delete_token="token-123",
            original_name="enc.bin",
            file=SimpleUploadedFile("enc.bin", b"ciphertext"),
            is_encrypted=True,
        )

        response = self.client.get(
            reverse("session_files", kwargs={"session_id": "session-enc2"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "End-to-end encrypted")

    def test_upload_with_password(self):
        response = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-pwd-upload",
                "delete_token": "token",
                "password": "secret123",
                "files[]": [SimpleUploadedFile("f.txt", b"data")],
            },
        )

        self.assertEqual(response.status_code, 200)
        f = UploadedFile.objects.get(session_id="session-pwd-upload")
        self.assertTrue(f.password_hash)

    # ── Expiry field tests ───────────────────────────────────────

    def test_uploaded_file_has_expiry(self):
        response = self.client.post(
            reverse("upload_file"),
            {
                "session_id": "session-ttl",
                "delete_token": "token",
                "files[]": [SimpleUploadedFile("f.txt", b"data")],
            },
        )

        self.assertEqual(response.status_code, 200)
        f = UploadedFile.objects.get(session_id="session-ttl")
        self.assertIsNotNone(f.expires_at)
        self.assertFalse(f.is_expired)
