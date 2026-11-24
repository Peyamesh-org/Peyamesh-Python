import os
import tempfile

from peyamesh.testing import TEST_API_KEY, TEST_BASE_URL, TEST_WEBHOOK_SECRET

SECRET_KEY = "peyamesh-tests"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "shop.test"]
USE_TZ = True
TIME_ZONE = "Asia/Tehran"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "peyamesh.contrib.django",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "tests.django_project.urls"
# A file-based SQLite database (not :memory:) so the concurrency tests can use several connections.
# Set PEYAMESH_TEST_POSTGRES=postgres://user:pass@host:port/db to run the suite on PostgreSQL instead.
_pg = os.environ.get("PEYAMESH_TEST_POSTGRES")
if _pg:
    from urllib.parse import urlsplit

    _u = urlsplit(_pg)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _u.path.lstrip("/"),
            "USER": _u.username,
            "PASSWORD": _u.password,
            "HOST": _u.hostname,
            "PORT": _u.port,
            "TEST": {"NAME": "test_peyamesh_python"},
        }
    }
else:
    _db = os.path.join(tempfile.gettempdir(), f"peyamesh-tests-{os.getpid()}.sqlite3")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": _db,
            "TEST": {"NAME": _db},
            "OPTIONS": {"timeout": 20},
        }
    }
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

PEYAMESH = {
    "API_KEY": TEST_API_KEY,
    "BASE_URL": TEST_BASE_URL,
    "CURRENCY": "IRT",
    "WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
    "SUCCESS_URL": "shop:done",
    "FAILURE_URL": "/shop/failed/",
}
