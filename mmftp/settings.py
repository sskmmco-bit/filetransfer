"""
Django settings for MMFileTransfer (mmftp).

Phase 0 / Phase 1 starter. Runtime-editable values belong in the SiteSettings
singleton (apps.config); deployment constants live here and are read from the
environment via django-environ.
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1", "[::1]"]),
    TRUSTED_PROXY_IPS=(list, ["127.0.0.1", "::1"]),
    DJANGO_SECURE_SSL=(bool, False),
)

# Read a .env file if present. On a server install the systemd units pass the
# environment in via EnvironmentFile; this covers local runs where the shell
# hasn't sourced .env.
environ.Env.read_env(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# Public base URL for share links (e.g. "https://files.company.com"). We store
# only a link's token and build "<PUBLIC_BASE_URL>/s/<token>" on demand — when
# empty we fall back to the current request host (§5.4).
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", default="")

# Secret used to Fernet-encrypt SMTP / LDAP secrets at rest (§5.11). Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRETS_ENCRYPTION_KEY = env("SECRETS_ENCRYPTION_KEY", default="")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",  # trigram search operators + GIN index support (file search)
    # Project apps (§3) — app labels: accounts, core, config, files,
    # notifications, audit, public
    "apps.accounts",
    "apps.core",
    "apps.config",
    "apps.files",
    "apps.notifications",
    "apps.audit",
    "apps.public",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mmftp.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.app_chrome",
            ],
        },
    },
]

WSGI_APPLICATION = "mmftp.wsgi.application"
ASGI_APPLICATION = "mmftp.asgi.application"

# ---------------------------------------------------------------------------
# Database (PostgreSQL)
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="mmftp"),
        "USER": env("POSTGRES_USER", default="mmftp"),
        "PASSWORD": env("POSTGRES_PASSWORD", default="mmftp"),
        "HOST": env("POSTGRES_HOST", default="postgres"),
        "PORT": env("POSTGRES_PORT", default="5432"),
        "CONN_MAX_AGE": env.int("DJANGO_CONN_MAX_AGE", default=60),
    }
}

# ---------------------------------------------------------------------------
# Cache (local in-process — no Redis)
# ---------------------------------------------------------------------------
# Background jobs run as management commands on systemd timers (no Celery/broker)
# and the app has no cross-process cache needs (login throttle is DB-backed,
# sessions use the database), so the default cache is Django's in-memory
# LocMemCache. No external cache service is required.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "mmftp-default",
    }
}

# ---------------------------------------------------------------------------
# Object storage (MinIO via the S3 API) + static files
# ---------------------------------------------------------------------------
MINIO_ENDPOINT_URL = env("MINIO_ENDPOINT_URL", default="http://127.0.0.1:9000")
# Browser-reachable endpoint used when MINTING presigned URLs (the loopback
# 127.0.0.1:9000 host is not reachable from a user's browser). In production this
# is the public S3/MinIO domain behind nginx.
MINIO_PUBLIC_ENDPOINT_URL = env("MINIO_PUBLIC_ENDPOINT_URL", default="http://localhost:9000")
MINIO_BUCKET = env("MINIO_BUCKET", default="mmftp-files")
MINIO_ACCESS_KEY = env("MINIO_ACCESS_KEY", default="minioadmin")
MINIO_SECRET_KEY = env("MINIO_SECRET_KEY", default="minioadmin")
MINIO_USE_SSL = env.bool("MINIO_USE_SSL", default=False)

STORAGES = {
    # File blobs (media) live in MinIO. Presigned URLs are used for delivery
    # (querystring_auth=True), so the bucket stays private.
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": MINIO_BUCKET,
            "endpoint_url": MINIO_ENDPOINT_URL,
            "access_key": MINIO_ACCESS_KEY,
            "secret_key": MINIO_SECRET_KEY,
            "addressing_style": "path",       # required for MinIO
            "querystring_auth": True,          # presigned URLs
            "querystring_expire": 3600,
            "file_overwrite": False,
            "default_acl": None,
            "use_ssl": MINIO_USE_SSL,
            "region_name": env("MINIO_REGION", default="us-east-1"),
        },
    },
    # Static assets are served by WhiteNoise (and nginx in prod).
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Source static files (project-level). Equivalent of Next.js's public/ folder —
# put images, fonts, etc. here. collectstatic gathers these into STATIC_ROOT.
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"

# ---------------------------------------------------------------------------
# Uploads — large/chunked transfers
# ---------------------------------------------------------------------------
# Chunks are file fields (streamed to temp, exempt from the data-size cap), but
# raise the non-file POST cap and remove the field-count cap so the chunked
# uploader is never rejected with a 400/413 HTML page.
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = None
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # spill to a temp file beyond this

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    # Multi-identifier login: employee ID / email / username (§5.7.1).
    "apps.accounts.backends.MultiIdentifierBackend",
    "django.contrib.auth.backends.ModelBackend",
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Session timeout (idle) — full enforcement comes in Phase 1 (§5.9).
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=60 * 60 * 8)
SESSION_SAVE_EVERY_REQUEST = True

# ---------------------------------------------------------------------------
# Trusted reverse proxy + client IP (§3)
# ---------------------------------------------------------------------------
TRUSTED_PROXY_IPS = env("TRUSTED_PROXY_IPS")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

if env("DJANGO_SECURE_SSL"):
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS", default=["http://localhost", "http://127.0.0.1:8000"])

# ---------------------------------------------------------------------------
# Email (transactional inline; deferred via the NotificationLog queue + drain command)
# ---------------------------------------------------------------------------
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="mmftp@example.com")

# ---------------------------------------------------------------------------
# i18n / tz (English only — §1.1)
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", default="UTC")
USE_I18N = False
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Logging — simple console logging (captured by systemd/journald in production)
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
}
