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
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from io import BytesIO
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import UploadedFile


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
    files = UploadedFile.objects.filter(session_id=session_id).order_by("uploaded_at", "id")
    missing_ids = []
    for uploaded_file in files:
        if not uploaded_file.file or not os.path.exists(uploaded_file.file.path):
            missing_ids.append(uploaded_file.id)
    if missing_ids:
        UploadedFile.objects.filter(id__in=missing_ids).delete()
    return UploadedFile.objects.filter(session_id=session_id).order_by("uploaded_at", "id")


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
                "type_label": (Path(original_name).suffix.replace(".", "") or "file").upper(),
                "mime_type": mime_type or "application/octet-stream",
                "download_url": reverse(
                    "download_file",
                    kwargs={"session_id": uploaded_file.session_id, "file_id": uploaded_file.id},
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


@require_POST
def cleanup(request):
    session_id = request.POST.get("session_id")
    delete_token = request.POST.get("delete_token")
    if not session_id or not delete_token:
        return JsonResponse({"error": "session_id and delete_token are required"}, status=400)

    files = UploadedFile.objects.filter(session_id=session_id, delete_token=delete_token)
    if not files.exists():
        return JsonResponse({"error": "Invalid session or delete token"}, status=403)

    _delete_uploaded_files(files)

    session_folder = os.path.join(settings.MEDIA_ROOT, "uploads", session_id)
    if os.path.isdir(session_folder):
        shutil.rmtree(session_folder, ignore_errors=True)

    return JsonResponse({"status": "ok"})



# Upload page (select files)
def upload_page(request):
    return render(request, "upload.html", {"nav": "send"})


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
    if request.method == "POST":
        session_id = request.POST.get("session_id") or str(uuid.uuid4())
        existing_token = (
            UploadedFile.objects.filter(session_id=session_id)
            .exclude(delete_token="")
            .values_list("delete_token", flat=True)
            .first()
        )
        delete_token = existing_token or request.POST.get("delete_token") or uuid.uuid4().hex
        files = request.FILES.getlist("files[]")
        validation_error = _validate_upload_request(files)
        if validation_error:
            return JsonResponse({"error": validation_error}, status=400)

        for f in files:
            UploadedFile.objects.create(
                session_id=session_id,
                delete_token=delete_token,
                original_name=f.name,
                file=f,
            )

        return JsonResponse({"status": "ok", "session_id": session_id, "delete_token": delete_token})

    return JsonResponse({"error": "Invalid request"}, status=400)


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
        },
    )


def receive(request):
    error = None

    if request.method == "POST":
        session_id = request.POST.get("session_id")
        if session_id:
            if not UploadedFile.objects.filter(session_id=session_id).exists():
                error = "Invalid session ID"
            else:
                return redirect("session_files", session_id=session_id)
        else:
            error = "Please enter a session code"

    return render(request, "receive_page.html", {"error": error, "nav": "receive"})


def session_files(request, session_id):
    files = _session_files_queryset(session_id)
    if not files.exists():
        return HttpResponse("No files found.", status=404)

    return render(
        request,
        "download_page.html",
        {
            "nav": "receive",
            "session_id": session_id,
            "files": _session_file_cards(files),
            "download_url": reverse("download", kwargs={"session_id": session_id}),
        },
    )


def download_file(request, session_id, file_id):
    uploaded_file = get_object_or_404(UploadedFile, id=file_id, session_id=session_id)
    if not uploaded_file.file or not os.path.exists(uploaded_file.file.path):
        uploaded_file.delete()
        return HttpResponse("File not found.", status=404)

    original_name = uploaded_file.original_name or Path(uploaded_file.file.name).name
    return FileResponse(uploaded_file.file.open("rb"), as_attachment=True, filename=original_name)


def download(request, session_id):
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

