"""Mount with ``path("peyamesh/", include("peyamesh.contrib.django.urls"))``."""

from django.urls import path

from . import views

app_name = "peyamesh"

urlpatterns = [
    path("callback/", views.callback, name="callback"),
    path("webhook/", views.webhook, name="webhook"),
]
