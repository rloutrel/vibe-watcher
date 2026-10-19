"""
Django settings for vibe-watcher.

Configuration follows 12-factor principles: everything tunable comes from
environment variables so the same image/unit file works in compose, systemd
system mode and systemd user mode.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


SECRET_KEY = os.environ.get(
    "VIBE_WATCHER_SECRET_KEY", "django-insecure-dev-only-change-me"
)
DEBUG = env_bool("VIBE_WATCHER_DEBUG", True)
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("VIBE_WATCHER_ALLOWED_HOSTS", "*").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "watcher",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vibe_watcher.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "vibe_watcher.wsgi.application"

sqlite_path = os.environ.get("VIBE_WATCHER_DB_PATH", str(BASE_DIR / "db.sqlite3"))
if os.environ.get("VIBE_WATCHER_DB") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "vibewatcher"),
            "USER": os.environ.get("POSTGRES_USER", "vibewatcher"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "vibewatcher"),
            "HOST": os.environ.get("POSTGRES_HOST", "db"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": sqlite_path,
        }
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("VIBE_WATCHER_TIMEZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_REDIRECT_URL = "watcher:index"
LOGOUT_REDIRECT_URL = "watcher:index"

# --- Watcher / notifications configuration -------------------------------

# How stale (seconds) the watcher heartbeat may be before the UI flags it.
WATCHER_HEARTBEAT_STALE_AFTER = env_int("VIBE_WATCHER_HEARTBEAT_STALE_AFTER", 180)

# Default poll interval for folders (seconds).
WATCHER_DEFAULT_POLL_INTERVAL = env_int("VIBE_WATCHER_DEFAULT_POLL_INTERVAL", 30)

# Base directory scanned for project discovery in the folder form
# (compose sets /projects, the mount of VIBE_WATCHER_PROJECTS_DIR).
WATCHER_PROJECTS_BASE = os.environ.get("VIBE_WATCHER_PROJECTS_BASE", "~")

# Fetch the remote before each scan so origin/* refs are current.
WATCHER_FETCH_BEFORE_SCAN = env_bool("VIBE_WATCHER_FETCH_BEFORE_SCAN", True)

# Maximum subprocess time for a single git invocation.
WATCHER_GIT_TIMEOUT = env_int("VIBE_WATCHER_GIT_TIMEOUT", 120)

# Desktop notification backend:
#   dbus   - org.freedesktop.Notifications on the session bus (default)
#   none   - notifications disabled (logging only)
WATCHER_NOTIFY_BACKEND = os.environ.get("VIBE_WATCHER_NOTIFY_BACKEND", "dbus")

WATCHER_NOTIFY_APP_NAME = os.environ.get("VIBE_WATCHER_NOTIFY_APP_NAME", "vibe-watcher")

# Extra args passed verbatim to notify-send (e.g. "--icon=git --transient").
WATCHER_NOTIFY_SEND_ARGS = os.environ.get("VIBE_WATCHER_NOTIFY_SEND_ARGS", "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "vibe_watcher.logging.JSONFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("VIBE_WATCHER_LOG_LEVEL", "INFO"),
    },
}
