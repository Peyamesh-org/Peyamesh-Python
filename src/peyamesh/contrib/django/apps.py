from django.apps import AppConfig


class PeyameshConfig(AppConfig):
    name = "peyamesh.contrib.django"
    label = "peyamesh"
    verbose_name = "پیامش"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from . import checks  # noqa: F401  (registers system checks)
