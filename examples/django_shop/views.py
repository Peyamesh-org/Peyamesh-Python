from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from peyamesh import PeyameshError
from peyamesh.contrib.django import get_returned_payment, start_payment

from .models import Order


@login_required
@require_POST
def pay(request, order_id):
    order = get_object_or_404(Order, pk=order_id, user=request.user, status="unpaid")
    try:
        payment = start_payment(request, amount=order.total, order_id=order.pk, mobile=order.phone)
    except PeyameshError as exc:
        messages.error(request, exc.message)
        return redirect("cart")
    return payment.redirect()


def payment_result(request):
    payment = get_returned_payment(request)
    return render(
        request,
        "orders/payment_result.html",
        {
            "payment": payment,
            "paid": bool(payment and payment.is_paid),
            "pending": bool(payment and payment.is_pending),
        },
    )
