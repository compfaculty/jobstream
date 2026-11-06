"""Check production-mode settings with ephemeral, non-deployment credentials.

No database, storage, or provider requests are made. This validates settings, not
the deployed proxy, IAM policy, or suitability for HSTS preload registration.
"""

import base64
import os
import secrets
import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ.update(
        DJANGO_SETTINGS_MODULE="config.settings",
        DEBUG="0",
        SECRET_KEY=secrets.token_urlsafe(64),
        TOKEN_ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        S3_BUCKET="synthetic-private-deployment-check",
        ALLOWED_HOSTS="jobstream.example.invalid",
        SENTRY_DSN="",
        OTEL_EXPORTER_OTLP_ENDPOINT="",
    )
    import django
    from django.conf import settings
    from django.core.management import call_command

    django.setup()
    # Preload is an operator/domain commitment, not a safe automatic default.
    # Suppress only that advisory in this synthetic check; all other warnings fail.
    settings.SILENCED_SYSTEM_CHECKS = ["security.W021"]
    call_command("check", deploy=True, fail_level="WARNING")


if __name__ == "__main__":
    main()
