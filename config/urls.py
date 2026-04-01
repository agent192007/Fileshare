from django.contrib import admin
from django.urls import include, path

from .env import load_env

load_env()

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("transfer_app.urls")),
]
