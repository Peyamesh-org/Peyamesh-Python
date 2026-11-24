from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

from peyamesh.contrib.django import get_returned_payment, start_payment
from peyamesh.errors import PeyameshError


def checkout(request):
    try:
        payment = start_payment(
            request, amount=int(request.POST.get("amount", "10000")), order_id=request.POST.get("order", "1")
        )
    except PeyameshError as exc:
        return HttpResponse(exc.message, status=402)
    return payment.redirect()


def done(request):
    payment = get_returned_payment(request)
    return HttpResponse("paid" if payment and payment.is_paid else "not-paid")


shop = (
    [
        path("checkout/", checkout, name="checkout"),
        path("done/", done, name="done"),
        path("failed/", done, name="failed"),
    ],
    "shop",
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("shop/", include(shop)),
    path("peyamesh/", include("peyamesh.contrib.django.urls")),
]
