"""Run the local k6 baseline with an ephemeral, unusable-password account/session."""

import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from uuid import uuid4

import django


def main():
    ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.backends.db import SessionStore

    if not settings.DEBUG or not os.getenv("SECRET_KEY"):
        raise SystemExit("Export the same .env as the running local web app (DEBUG=1, SECRET_KEY).")
    user = get_user_model().objects.create_user(username=f"benchmark-{uuid4().hex}", password=None)
    session = SessionStore()
    session["_auth_user_id"] = str(user.pk)
    session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session["_auth_user_hash"] = user.get_session_auth_hash()
    session.save()
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/jobs/?q=Python",
            headers={"Cookie": f"sessionid={session.session_key}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise SystemExit("Load test authentication preflight failed")
        print("Authenticated local preflight passed", flush=True)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "jobstream_default",
                "-e",
                "SESSION_ID",
                "-e",
                "BASE_URL=http://web:8000",
                "-e",
                "HOST_HEADER=localhost",
                "-v",
                f"{ROOT / 'benchmarks'}:/scripts:ro",
                "grafana/k6:2.3.0",
                "run",
                "--quiet",
                "/scripts/search.js",
            ],
            env={**os.environ, "SESSION_ID": session.session_key},
            check=True,
        )
    finally:
        session.delete()
        user.delete()


if __name__ == "__main__":
    main()
