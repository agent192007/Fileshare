from django.urls import path

from config.env import load_env

from . import views

load_env()

urlpatterns = [
    path("upload/", views.upload_file, name="upload_file"),
    path("", views.upload_page, name="upload_page"),
    path("qr/<str:session_id>/", views.show_qr, name="show_qr"),
    path("receive/", views.receive, name="receive"),
    path("files/<str:session_id>/", views.session_files, name="session_files"),
    path("files/<str:session_id>/<int:file_id>/download/", views.download_file, name="download_file"),
    path("download/<str:session_id>/", views.download, name="download"),
    path("cleanup/", views.cleanup, name="cleanup"),
]
