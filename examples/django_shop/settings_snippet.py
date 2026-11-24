import os

INSTALLED_APPS = [
    # ... your apps
    "peyamesh.contrib.django",
]

PEYAMESH = {
    "API_KEY": os.environ["PEYAMESH_API_KEY"],
    "CURRENCY": "IRT",
    "WEBHOOK_SECRET": os.environ.get("PEYAMESH_WEBHOOK_SECRET"),
    "SUCCESS_URL": "orders:payment-result",
    "FAILURE_URL": "orders:payment-result",
}
