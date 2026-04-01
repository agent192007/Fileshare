from django.apps import AppConfig

from config.env import load_env

load_env()


class TransferAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "transfer_app"
    label = "File"
    verbose_name = "File Transfer"
