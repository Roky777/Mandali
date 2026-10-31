import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from .environment import canonical_url, allowed_hosts, database_config

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            "Set DJANGO_SECRET_KEY or enable local DJANGO_DEBUG=1."
        )
    SECRET_KEY = "local-development-only-never-use-in-production"
if not DEBUG and (len(SECRET_KEY) < 50 or SECRET_KEY.startswith("django-insecure-")):
    raise ImproperlyConfigured(
        "Production DJANGO_SECRET_KEY must be a strong random value of at least 50 characters."
    )
MANDALI_BASE_URL = canonical_url(os.environ, DEBUG)
ALLOWED_HOSTS = allowed_hosts(os.environ, DEBUG, MANDALI_BASE_URL)
CSRF_TRUSTED_ORIGINS = [MANDALI_BASE_URL]
for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(","):
    if origin.strip():
        CSRF_TRUSTED_ORIGINS.append(canonical_url({"MANDALI_BASE_URL": origin}, DEBUG))
# Enable only behind Cloud Run's managed ingress, never on an exposed container.
TRUST_CLOUD_RUN_PROXY = os.getenv("DJANGO_TRUST_CLOUD_RUN_PROXY", "0") == "1"
if TRUST_CLOUD_RUN_PROXY and not os.getenv("K_SERVICE"):
    raise ImproperlyConfigured(
        "DJANGO_TRUST_CLOUD_RUN_PROXY requires a Cloud Run service environment."
    )
if TRUST_CLOUD_RUN_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
TRUSTED_PROXY_HOPS = int(os.getenv("DJANGO_TRUSTED_PROXY_HOPS", "0"))
if (
    TRUSTED_PROXY_HOPS < 0
    or TRUSTED_PROXY_HOPS > 5
    or (TRUSTED_PROXY_HOPS and not TRUST_CLOUD_RUN_PROXY)
):
    raise ImproperlyConfigured(
        "Proxy hop trust must be explicitly configured for Cloud Run (0 to 5 hops)."
    )
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "billing",
    "checkout",
    "integrations",
    "recurring",
    "portal",
    "reports",
    "operations",
]
MIDDLEWARE = [
    "operations.monitoring.RequestTimingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.PrivateResponseMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": database_config(os.environ, DEBUG, BASE_DIR)}
AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 9},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "core.validators.PasswordCompositionValidator"},
]
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "workspaces"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 8
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
PASSWORD_RESET_TIMEOUT = 3600
EMAIL_BACKEND = os.getenv(
    "DJANGO_EMAIL_BACKEND",
    (
        "django.core.mail.backends.console.EmailBackend"
        if DEBUG
        else "django.core.mail.backends.smtp.EmailBackend"
    ),
)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Mandali <noreply@example.com>")
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "1") == "1"
EMAIL_TIMEOUT = 10

# First key encrypts new values; retain old keys to decrypt pending orders/links.
PAYMENT_ENCRYPTION_KEYS = os.getenv("PAYMENT_ENCRYPTION_KEYS", "")
# Development only: lets integrations reach a sample website on localhost.
MANDALI_ALLOW_LOCAL_INTEGRATIONS = (
    DEBUG and os.getenv("MANDALI_ALLOW_LOCAL_INTEGRATIONS", "0") == "1"
)
# Structured logs on stdout, one JSON object per line, for Cloud Logging.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "operations.monitoring.JsonFormatter"}},
    "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["stdout"], "level": "WARNING"},
    "loggers": {
        "mandali.requests": {
            "handlers": ["stdout"],
            "level": os.getenv("MANDALI_REQUEST_LOG_LEVEL", "WARNING" if DEBUG else "INFO"),
            "propagate": False,
        },
        "django.request": {"handlers": ["stdout"], "level": "ERROR", "propagate": False},
    },
}
# Controlled pilot: comma-separated emails and @domains allowed to create
# accounts and businesses. Empty means open signup.
MANDALI_PILOT_ALLOWLIST = os.getenv("MANDALI_PILOT_ALLOWLIST", "")

