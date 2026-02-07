import os
from pathlib import Path
from urllib.parse import urlparse
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def _env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _unique(items):
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _normalize_samesite(value, fallback):
    raw = (value or "").strip().lower()
    if raw == "none":
        return "None"
    if raw == "lax":
        return "Lax"
    if raw == "strict":
        return "Strict"
    return fallback


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS")
if "canvaslens.uniwebprod.co.uk" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("canvaslens.uniwebprod.co.uk")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "directory",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "canvaslens.middleware.StaffAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "canvaslens.middleware.FrameAncestorsMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "canvaslens.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "directory" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "canvaslens.wsgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/canvaslens")
parsed_db = urlparse(DATABASE_URL)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed_db.path.lstrip("/"),
        "USER": parsed_db.username,
        "PASSWORD": parsed_db.password,
        "HOST": parsed_db.hostname,
        "PORT": parsed_db.port or "5432",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# CSRF
default_csrf_trusted_origins = [
    "https://canvaslens.uniwebprod.co.uk",
    "https://canvaslens.uniwebdev.co.uk",
    "https://canvas.liverpool.ac.uk",
    "https://*.instructure.com",
]
CSRF_TRUSTED_ORIGINS = _unique(
    default_csrf_trusted_origins + _env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
)

# Reverse proxy / TLS
if _env_bool("DJANGO_USE_X_FORWARDED_PROTO", True):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = _env_bool("DJANGO_USE_X_FORWARDED_HOST", True)

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Allow embedding in iframe contexts (Canvas).
X_FRAME_OPTIONS = "ALLOWALL"
FRAME_ANCESTORS = _unique(
    [
        "'self'",
        "https://canvas.liverpool.ac.uk",
        "https://*.instructure.com",
    ]
    + _env_list("DJANGO_FRAME_ANCESTORS")
)

# Cookies for iframe compatibility.
SESSION_COOKIE_SECURE = _env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = _env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_SAMESITE = _normalize_samesite(
    os.getenv("DJANGO_SESSION_COOKIE_SAMESITE", "None"),
    "None",
)
CSRF_COOKIE_SAMESITE = _normalize_samesite(
    os.getenv("DJANGO_CSRF_COOKIE_SAMESITE", "None"),
    "None",
)
# Browsers reject SameSite=None cookies without Secure.
if SESSION_COOKIE_SAMESITE == "None" and not SESSION_COOKIE_SECURE:
    SESSION_COOKIE_SAMESITE = "Lax"
if CSRF_COOKIE_SAMESITE == "None" and not CSRF_COOKIE_SECURE:
    CSRF_COOKIE_SAMESITE = "Lax"

# Auth
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/admin-dashboard/"
LOGOUT_REDIRECT_URL = "/"

# Celery
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_BEAT_SCHEDULE = {
    "purge-expired-submission-reports": {
        "task": "directory.tasks.purge_expired_submission_reports",
        "schedule": timedelta(minutes=5),
    }
}

# Canvas
CANVAS_URL = "https://canvas.liverpool.ac.uk"
