"""Environment-driven settings; insecure defaults are confined to explicit local mode."""

import json
import os
from pathlib import Path
from typing import Any

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DEBUG", "0") == "1"
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("SECRET_KEY is required outside DEBUG=1")
    # Explicit DEBUG-only fallback; production refuses to start without a secret.
    SECRET_KEY = "local-development-only-not-for-deployment"  # noqa: S105
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
CSRF_TRUSTED_ORIGINS = list(filter(None, os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")))
INSTALLED_APPS = [
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "drf_spectacular",
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "two_factor",
    "axes",
    "jobs",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
    "jobs.middleware.SecurityHeadersMiddleware",
    "jobs.middleware.PrivilegedMFAMiddleware",
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
DATABASES = {
    "default": dj_database_url.parse(
        os.getenv("DATABASE_URL", "postgresql://jobstream:local-only@localhost:5432/jobstream"),
        conn_max_age=60,
    )
}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
LOGIN_URL = "two_factor:login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/account/login/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 28800
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
MEDIA_ROOT = BASE_DIR / "private-media"
STORAGES: dict[str, Any] = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "config.static_storage.AdminStaticFilesStorage"},
}
if os.getenv("S3_BUCKET"):
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": os.environ["S3_BUCKET"],
            "endpoint_url": os.getenv("S3_ENDPOINT_URL"),
            "region_name": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            "default_acl": None,
            "querystring_auth": True,
            "file_overwrite": False,
        },
    }
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BEAT_SCHEDULE = {
    "batches": {"task": "jobs.tasks.dispatch_batches", "schedule": 30.0},
    "company-discovery": {"task": "jobs.tasks.schedule_company_discovery", "schedule": 60.0},
    "sources": {"task": "jobs.tasks.schedule_sources", "schedule": 60.0},
    "outbox": {"task": "jobs.tasks.dispatch_submissions", "schedule": 30.0},
    "reconciliation": {"task": "jobs.tasks.reconcile_submissions", "schedule": 300.0},
}
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Jobstream API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "ENUM_NAME_OVERRIDES": {
        "JobLifecycleEnum": [
            ("open", "Open"),
            ("closed", "Closed"),
            ("filled", "Filled (employer confirmed)"),
            ("archived", "Archived"),
        ]
    },
}
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/gmail/callback/")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
if not DEBUG and (not TOKEN_ENCRYPTION_KEY or not os.getenv("S3_BUCKET")):
    raise ImproperlyConfigured("Production requires TOKEN_ENCRYPTION_KEY and private S3_BUCKET")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        }
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.server": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "CRITICAL", "propagate": False},
    },
}


def scrub_event(event, hint):
    # Event messages and exception text may contain provider payloads. Export only categories.
    return {
        "event_id": event.get("event_id"),
        "level": event.get("level", "error"),
        "message": "Application error; inspect redacted local audit events",
        "tags": {"component": "jobstream"},
    }


if os.getenv("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(
        dsn=os.environ["SENTRY_DSN"],
        send_default_pii=False,
        traces_sample_rate=0,
        before_send=scrub_event,
    )

# Durable worker-wide delivery spacing, in seconds.
SEND_PROVIDER_INTERVAL = float(os.getenv("SEND_PROVIDER_INTERVAL", "0.5"))
SEND_ACCOUNT_INTERVAL = float(os.getenv("SEND_ACCOUNT_INTERVAL", "2"))
SEND_DOMAIN_INTERVAL = float(os.getenv("SEND_DOMAIN_INTERVAL", "5"))
CV_FONT_PATH = os.getenv("CV_FONT_PATH", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low")
JOB_NORMALIZATION_MODEL = os.getenv("JOB_NORMALIZATION_MODEL", OPENAI_MODEL)
JOB_NORMALIZATION_REASONING_EFFORT = os.getenv("JOB_NORMALIZATION_REASONING_EFFORT", "none")
JOB_AI_NORMALIZATION_ENABLED = os.getenv("JOB_AI_NORMALIZATION_ENABLED", "0") == "1"
JOB_NORMALIZATION_MAX_OUTPUT_TOKENS = 800
JOB_NORMALIZATION_TIMEOUT_SECONDS = 20

AI_MAX_INPUT_CHARACTERS = int(os.getenv("AI_MAX_INPUT_CHARACTERS", "30000"))
AI_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "2000"))
AI_DAILY_TOKEN_BUDGET = int(os.getenv("AI_DAILY_TOKEN_BUDGET", "200000"))

# Control tasks stay on the default queue; expensive stages have independent workers.
CELERY_TASK_ROUTES = {
    "jobs.tasks.discover_company_careers": {"queue": "ingestion"},
    "jobs.tasks.ingest_source": {"queue": "ingestion"},
    "jobs.tasks.prepare_batch_item": {"queue": "preparation"},
    "jobs.tasks.send_application": {"queue": "delivery"},
    "jobs.tasks.send_browser_application": {"queue": "browser-delivery"},
    "jobs.tasks.reconcile_submissions": {"queue": "delivery"},
}

SECRET_TLV_APPLY_INTERVAL = float(os.getenv("SECRET_TLV_APPLY_INTERVAL", "10"))

# Public career collection: shared database limits apply across worker processes.
CRAWL_GLOBAL_CONCURRENCY = int(os.getenv("CRAWL_GLOBAL_CONCURRENCY", "8"))
CRAWL_HOST_CONCURRENCY = int(os.getenv("CRAWL_HOST_CONCURRENCY", "2"))
CRAWL_HOST_INTERVAL = float(os.getenv("CRAWL_HOST_INTERVAL", "0"))
CRAWL_DISPATCH_BATCH = int(os.getenv("CRAWL_DISPATCH_BATCH", "100"))


# Jazzmin is a Python package with bundled static assets: no npm/Node build step.
JAZZMIN_SETTINGS = {
    "site_title": "JobStream Admin",
    "site_header": "JobStream",
    "site_brand": "JobStream",
    "welcome_sign": "JobStream administration",
    "copyright": "JobStream",
    "show_ui_builder": False,
    "show_theme_chooser": False,
    "use_google_fonts_cdn": False,
    "navigation_expanded": True,
    "changeform_format": "single",
    "order_with_respect_to": ["jobs", "jobs.Job", "jobs.Company", "jobs.Source"],
    "icons": {
        "jobs.Job": "fas fa-briefcase",
        "jobs.Company": "fas fa-building",
        "jobs.Source": "fas fa-satellite-dish",
        "jobs.IngestionRun": "fas fa-history",
        "jobs.AuditEvent": "fas fa-shield-alt",
        "jobs.Invitation": "fas fa-user-plus",
    },
    "topmenu_links": [{"name": "JobStream", "url": "/", "new_window": False}],
}
JAZZMIN_UI_TWEAKS = {"theme": "default", "default_theme_mode": "dark"}

# Experimental vacancy evaluation only; legacy AI paths are independent.
AGENT_BACKEND = os.getenv("AGENT_BACKEND", "disabled")
if AGENT_BACKEND not in {"disabled", "infinity"}:
    raise ImproperlyConfigured("AGENT_BACKEND must be disabled or infinity")
INFINITY_URL = os.getenv("INFINITY_URL", "http://127.0.0.1:8091")
INFINITY_SERVICE_TOKEN = os.getenv("INFINITY_SERVICE_TOKEN", "")
INFINITY_TOOL_SECRET = os.getenv("INFINITY_TOOL_SECRET", "")
INFINITY_MODEL = os.getenv("INFINITY_MODEL", OPENAI_MODEL)
INFINITY_REASONING_EFFORT = os.getenv("INFINITY_REASONING_EFFORT", "low")
INFINITY_INPUT_COST_PER_MILLION = os.getenv("INFINITY_INPUT_COST_PER_MILLION", "")
INFINITY_OUTPUT_COST_PER_MILLION = os.getenv("INFINITY_OUTPUT_COST_PER_MILLION", "")
CELERY_BEAT_SCHEDULE["agent-evaluations"] = {
    "task": "jobs.tasks.dispatch_evaluations",
    "schedule": 10.0,
}
CELERY_TASK_ROUTES["jobs.tasks.evaluate_vacancy"] = {"queue": "preparation"}

CELERY_BEAT_SCHEDULE["operations"] = {
    "task": "jobs.tasks.collect_operations",
    "schedule": 30.0,
    "options": {"expires": 25},
}

# Pilot application-side budgets; prices are USD per million uncached tokens.
AI_MONTHLY_BUDGET_USD = os.getenv("AI_MONTHLY_BUDGET_USD", "25")
AI_USER_DAILY_COMPARISONS = int(os.getenv("AI_USER_DAILY_COMPARISONS", "20"))
AI_MODEL_PRICES = json.loads(
    os.getenv(
        "AI_MODEL_PRICES", '{"gpt-6-luna": ["0.10", "0.50"], "gpt-4.1-mini": ["0.40", "1.60"]}'
    )
)
AI_PRICING_VERSION = "2026-11-05"
JOB_NORMALIZATION_BATCH_ENABLED = os.getenv("JOB_NORMALIZATION_BATCH_ENABLED", "0") == "1"
JOB_NORMALIZATION_BATCH_SIZE = min(
    100, max(1, int(os.getenv("JOB_NORMALIZATION_BATCH_SIZE", "50")))
)
CV_REPRESENTATIONS_ENABLED = os.getenv("CV_REPRESENTATIONS_ENABLED", "0") == "1"
CELERY_BEAT_SCHEDULE.update(
    {
        "ai-normalization-submit": {
            "task": "jobs.tasks.submit_normalization_batch",
            "schedule": 3600.0,
        },
        "ai-normalization-poll": {
            "task": "jobs.tasks.poll_normalization_batches",
            "schedule": 60.0,
        },
        "cv-profiles": {"task": "jobs.tasks.dispatch_cv_profiles", "schedule": 60.0},
    }
)
AI_PILOT_ROLLOUT_APPROVED = os.getenv("AI_PILOT_ROLLOUT_APPROVED", "0") == "1"
CELERY_TASK_ROUTES.update(
    {
        "jobs.tasks.build_cv_profile": {"queue": "preparation"},
        "jobs.tasks.submit_normalization_batch": {"queue": "ingestion"},
        "jobs.tasks.poll_normalization_batches": {"queue": "ingestion"},
    }
)

# Public company discovery ships disabled in its database campaign.
BRAVE_SEARCH_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "")
CELERY_BEAT_SCHEDULE["employer-discovery"] = {
    "task": "jobs.tasks.schedule_employer_discovery",
    "schedule": 60.0,
}
CELERY_TASK_ROUTES["jobs.tasks.discover_employers"] = {"queue": "ingestion"}
