"""The two endpoints mounted by ``include("peyamesh.contrib.django.urls")``."""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from ...errors import PeyameshError, SignatureVerificationError
from ...webhooks import SIGNATURE_HEADER, construct_event
from . import services
from .conf import get_settings

logger = logging.getLogger("peyamesh.django")


@csrf_exempt
@never_cache
@require_http_methods(["GET", "POST"])
def callback(request: HttpRequest) -> HttpResponse:
    """The buyer returns here from the payment page; we verify, then redirect to your result page."""
    params = request.GET.copy()
    if request.method == "POST":
        params.update(request.POST)
    record, outcome = services.process_callback(params)
    return HttpResponseRedirect(services.return_url_for(record, outcome))


@csrf_exempt
@require_POST
def webhook(request: HttpRequest) -> HttpResponse:
    """Receives signed events. Answers 2xx once processed, so the sender stops retrying."""
    conf = get_settings()
    if not conf.webhook_secret:
        logger.error("peyamesh: webhook received but PEYAMESH['WEBHOOK_SECRET'] is not set")
        return JsonResponse({"error": "webhook_not_configured"}, status=404)
    try:
        event = construct_event(
            request.body,
            request.headers.get(SIGNATURE_HEADER),
            list(conf.webhook_secret),
            tolerance=conf.webhook_tolerance,
        )
    except SignatureVerificationError as exc:
        logger.warning("peyamesh: rejected webhook (%s)", exc.code)
        return JsonResponse({"error": exc.code}, status=400)
    try:
        services.process_event(event)
    except PeyameshError as exc:
        logger.warning("peyamesh: webhook %s will be retried: %s", event.id, exc)
        return JsonResponse({"error": "retry_later"}, status=503)
    return JsonResponse({"received": True})
