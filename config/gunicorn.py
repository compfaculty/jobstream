"""Safe runtime logging and live-gauge cleanup after worker death."""

import os

from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# Configure Gunicorn's first startup event as well as worker errors.
logconfig_dict = settings.LOGGING


def child_exit(server, worker):
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
