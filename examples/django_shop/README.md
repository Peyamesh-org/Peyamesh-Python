# Django example

Drop these pieces into an existing project:

1. `settings_snippet.py` → your `settings.py`
2. `urls.py` → include in your root `urls.py`
3. `views.py` and `receivers.py` → your `orders` app (import `receivers` in `OrdersConfig.ready()`)
4. `python manage.py migrate`, then schedule `python manage.py peyamesh_reconcile` every 5 minutes.
