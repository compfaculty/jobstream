"""Minimal exporter-only Django settings; no application or provider secrets loaded."""

import os
import secrets

import dj_database_url

SECRET_KEY = secrets.token_urlsafe(48)
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "jobs"]
DATABASES = {"default": dj_database_url.parse(os.environ["DATABASE_URL"], conn_max_age=0)}
DATABASES["default"].setdefault("OPTIONS", {})["connect_timeout"] = 2
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"safe": {"()": "jobs.observability.logging.SafeFormatter"}},
    "handlers": {
        "console": {"class": "jobs.observability.logging.SafeStreamHandler", "formatter": "safe"}
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}
