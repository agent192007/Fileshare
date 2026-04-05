import base64
import io
import mimetypes
import os
import shutil
import uuid
import zipfile
from pathlib import Path

import qrcode
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from io import BytesIO
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from config.env import load_env

from .models import UploadedFile

load_env()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _build_zip_entry_name(filename, seen_names):
    candidate = Path(filename).name or "file"
    stem = Path(candidate).stem or "file"
    suffix = Path(candidate).suffix
    index = 1

    while candidate in seen_names:
        candidate = f"{stem} ({index}){suffix}"
        index += 1

    seen_names.add(candidate)
    return candidate


def _format_bytes(size):
    thresholds = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in thresholds:
        if value < 1024 or unit == thresholds[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"


def _session_files_queryset(session_id):
    """Return live files for a session, pruning expired and orphaned records."""
    UploadedFile.objects.filter(
        session_id=session_id, expires_at__lte=timezone.now()
    ).delete()

    files = UploadedFile.objects.filter(session_id=session_id).order_by(
        "uploaded_at", "id"
    )
    missing_ids = []
    for uploaded_file in files:
        if not uploaded_file.file or not os.path.exists(uploaded_file.file.path):
            missing_ids.append(uploaded_file.id)
    if missing_ids:
        UploadedFile.objects.filter(id__in=missing_ids).delete()
    return UploadedFile.objects.filter(session_id=session_id).order_by(
        "uploaded_at", "id"
    )


def _session_file_cards(files):
    cards = []
    for uploaded_file in files:
        original_name = uploaded_file.original_name or Path(uploaded_file.file.name).name
        mime_type, _ = mimetypes.guess_type(original_name)
        cards.append(
            {
                "id": uploaded_file.id,
                "name": original_name,
                "size_label": _format_bytes(uploaded_file.file.size),
                "type_label": (
                    Path(original_name).suffix.replace(".", "") or "file"
                ).upper(),
                "mime_type": mime_type or "application/octet-stream",
                "download_url": reverse(
                    "download_file",
                    kwargs={
                        "session_id": uploaded_file.session_id,
                        "file_id": uploaded_file.id,
                    },
                ),
            }
        )
    return cards


def _delete_uploaded_files(files):
    for uploaded_file in files:
        file_path = os.path.join(settings.MEDIA_ROOT, uploaded_file.file.name)
        if os.path.exists(file_path):
            uploaded_file.file.delete(save=False)
        uploaded_file.delete()


def _check_rate_limit(request):
    """Return True if the client has exceeded the upload rate limit."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else request.META.get("REMOTE_ADDR", "")
    )
    cache_key = f"upload_rate:{ip}"
    current = cache.get(cache_key, 0)
    if current >= settings.UPLOAD_RATE_LIMIT:
        return True
    cache.set(cache_key, current + 1, settings.UPLOAD_RATE_WINDOW)
    return False


def _validate_file_extensions(files):
    """Return error message if any file has a blocked extension."""
    blocked = {ext.lower() for ext in settings.BLOCKED_FILE_EXTENSIONS}
    for f in files:
        ext = Path(f.name).suffix.lower()
        if ext in blocked:
            return f"File type '{ext}' is not allowed: {f.name}"
    return None


def _scan_files_for_malware(files):
    """Scan files with ClamAV if available. Returns error message or None."""
    try:
        import pyclamd

        cd = pyclamd.ClamdUnixSocket()
        cd.ping()
    except Exception:
        return None  # ClamAV not available, skip

    for f in files:
        try:
            result = cd.scan_stream(f.read())
            f.seek(0)
            if result:
                return f"{f.name} was flagged by the virus scanner."
        except Exception:
            f.seek(0)
    return None


def _session_password_hash(session_id):
    """Return the password hash for a session, or empty string."""
    return (
        UploadedFile.objects.filter(session_id=session_id)
        .exclude(password_hash="")
        .values_list("password_hash", flat=True)
        .first()
        or ""
    )


def _session_is_encrypted(session_id):
    return UploadedFile.objects.filter(
        session_id=session_id, is_encrypted=True
    ).exists()


def _password_verified(request, session_id):
    pw_hash = _session_password_hash(session_id)
    if not pw_hash:
        return True
    return request.session.get(f"pw_ok:{session_id}", False)


# ── Views ────────────────────────────────────────────────────────────────────


@require_POST
def cleanup(request):
    session_id = request.POST.get("session_id")
    delete_token = request.POST.get("delete_token")
    if not session_id or not delete_token:
        return JsonResponse(
            {"error": "session_id and delete_token are required"}, status=400
        )

    files = UploadedFile.objects.filter(
        session_id=session_id, delete_token=delete_token
    )
    if not files.exists():
        return JsonResponse({"error": "Invalid session or delete token"}, status=403)

    _delete_uploaded_files(files)

    session_folder = os.path.join(settings.MEDIA_ROOT, "uploads", session_id)
    if os.path.isdir(session_folder):
        shutil.rmtree(session_folder, ignore_errors=True)

    return JsonResponse({"status": "ok"})


def upload_page(request):
    return render(
        request,
        "upload.html",
        {
            "nav": "send",
            "blocked_extensions_json": list(settings.BLOCKED_FILE_EXTENSIONS),
        },
    )


def _validate_upload_request(files):
    if not files:
        return "No files uploaded"

    if len(files) > settings.MAX_FILES_PER_SESSION:
        return f"You can upload at most {settings.MAX_FILES_PER_SESSION} files at a time."

    total_size = 0
    for uploaded_file in files:
        total_size += uploaded_file.size
        if uploaded_file.size > settings.MAX_FILE_SIZE_BYTES:
            return (
                f"{uploaded_file.name} exceeds the per-file limit of "
                f"{settings.MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."
            )

    if total_size > settings.MAX_TOTAL_UPLOAD_BYTES:
        return (
            "The total upload exceeds the session limit of "
            f"{settings.MAX_TOTAL_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    return None


def upload_file(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=400)

    # Rate limiting
    if _check_rate_limit(request):
        return JsonResponse(
            {"error": "Too many uploads. Please wait before trying again."},
            status=429,
        )

    session_id = request.POST.get("session_id") or str(uuid.uuid4())
    existing_token = (
        UploadedFile.objects.filter(session_id=session_id)
        .exclude(delete_token="")
        .values_list("delete_token", flat=True)
        .first()
    )
    delete_token = (
        existing_token or request.POST.get("delete_token") or uuid.uuid4().hex
    )
    files = request.FILES.getlist("files[]")
    is_encrypted = request.POST.get("is_encrypted") == "true"
    password = request.POST.get("password", "").strip()

    # Validation
    validation_error = _validate_upload_request(files)
    if validation_error:
        return JsonResponse({"error": validation_error}, status=400)

    # File extension check
    ext_error = _validate_file_extensions(files)
    if ext_error:
        return JsonResponse({"error": ext_error}, status=400)

    # Virus scan (only for non-encrypted uploads — encrypted content is opaque)
    if not is_encrypted:
        malware_error = _scan_files_for_malware(files)
        if malware_error:
            return JsonResponse({"error": malware_error}, status=400)

    password_hash = make_password(password) if password else ""

    for f in files:
        UploadedFile.objects.create(
            session_id=session_id,
            delete_token=delete_token,
            original_name=f.name,
            file=f,
            is_encrypted=is_encrypted,
            password_hash=password_hash,
        )

    return JsonResponse(
        {"status": "ok", "session_id": session_id, "delete_token": delete_token}
    )


def show_qr(request, session_id):
    files = _session_files_queryset(session_id)
    if not files.exists():
        raise Http404("No files found for this session.")

    session_url = request.build_absolute_uri(
        reverse("session_files", kwargs={"session_id": session_id})
    )

    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(session_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()

    is_encrypted = _session_is_encrypted(session_id)
    first_file = files.first()
    expires_at = first_file.expires_at if first_file else None

    return render(
        request,
        "show_qr.html",
        {
            "nav": "send",
            "session_id": session_id,
            "qr_code": qr_base64,
            "files": _session_file_cards(files),
            "session_url": session_url,
            "download_url": reverse("download", kwargs={"session_id": session_id}),
            "is_encrypted": is_encrypted,
            "expires_at": expires_at,
        },
    )


def receive(request):
    error = None

    if request.method == "POST":
        raw_code = request.POST.get("session_id", "").strip()
        if raw_code:
            # Support combined session_id#key format — extract just the session_id
            session_id = raw_code.split("#")[0] if "#" in raw_code else raw_code
            if not UploadedFile.objects.filter(session_id=session_id).exists():
                error = "Invalid session ID"
            elif UploadedFile.objects.filter(
                session_id=session_id, expires_at__lte=timezone.now()
            ).exists():
                error = "This transfer has expired"
            else:
                return redirect("session_files", session_id=session_id)
        else:
            error = "Please enter a session code"

    return render(request, "receive_page.html", {"error": error, "nav": "receive"})


def session_files(request, session_id):
    files = _session_files_queryset(session_id)
    if not files.exists():
        return HttpResponse("No files found.", status=404)

    is_encrypted = _session_is_encrypted(session_id)
    pw_hash = _session_password_hash(session_id)

    # Password gate
    if pw_hash and not request.session.get(f"pw_ok:{session_id}"):
        error = None
        if request.method == "POST":
            password = request.POST.get("password", "")
            if check_password(password, pw_hash):
                request.session[f"pw_ok:{session_id}"] = True
                # Fall through to render file list (preserves URL hash fragment)
            else:
                error = "Incorrect password. Please try again."
                return render(
                    request,
                    "password_required.html",
                    {
                        "nav": "receive",
                        "session_id": session_id,
                        "error": error,
                        "is_encrypted": is_encrypted,
                    },
                )
        else:
            return render(
                request,
                "password_required.html",
                {
                    "nav": "receive",
                    "session_id": session_id,
                    "is_encrypted": is_encrypted,
                },
            )

    first_file = files.first()
    expires_at = first_file.expires_at if first_file else None

    return render(
        request,
        "download_page.html",
        {
            "nav": "receive",
            "session_id": session_id,
            "files": _session_file_cards(files),
            "download_url": reverse("download", kwargs={"session_id": session_id}),
            "is_encrypted": is_encrypted,
            "expires_at": expires_at,
        },
    )


def download_file(request, session_id, file_id):
    if not _password_verified(request, session_id):
        return JsonResponse({"error": "Password required"}, status=403)

    uploaded_file = get_object_or_404(UploadedFile, id=file_id, session_id=session_id)

    if uploaded_file.is_expired:
        uploaded_file.delete()
        return HttpResponse("This transfer has expired.", status=410)

    if not uploaded_file.file or not os.path.exists(uploaded_file.file.path):
        uploaded_file.delete()
        return HttpResponse("File not found.", status=404)

    original_name = uploaded_file.original_name or Path(uploaded_file.file.name).name
    return FileResponse(
        uploaded_file.file.open("rb"), as_attachment=True, filename=original_name
    )


def download(request, session_id):
    if not _password_verified(request, session_id):
        return JsonResponse({"error": "Password required"}, status=403)

    files = _session_files_queryset(session_id)
    if not files.exists():
        return HttpResponse("No files found.", status=404)

    zip_buffer = BytesIO()
    seen_names = set()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for f in files:
            file_path = os.path.join(settings.MEDIA_ROOT, f.file.name)
            if os.path.exists(file_path):
                display_name = f.original_name or os.path.basename(f.file.name)
                file_name = _build_zip_entry_name(display_name, seen_names)
                with open(file_path, "rb") as file_obj:
                    zip_file.writestr(file_name, file_obj.read())

    zip_buffer.seek(0)

    response = HttpResponse(zip_buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="files_{session_id}.zip"'
    return response
