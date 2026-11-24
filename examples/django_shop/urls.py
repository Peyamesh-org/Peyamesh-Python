from django.urls import path

from . import views

app_name = "orders"

urlpatterns = [
    path("orders/<int:order_id>/pay/", views.pay, name="pay"),
    path("orders/payment/result/", views.payment_result, name="payment-result"),
]

# In the project's root urls.py:
#     path("peyamesh/", include("peyamesh.contrib.django.urls")),
#     path("", include("orders.urls")),
