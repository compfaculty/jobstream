"""Isolated lifecycle-test application; never consumes Jobstream business queues."""

import os
import time
from pathlib import Path

import django
from celery import Celery
from django.http import HttpResponse
from django.test import RequestFactory

from jobs.observability.middleware import TelemetryMiddleware

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

app = Celery(
    "observability_probe", broker=os.environ.get("PROBE_BROKER", "redis://127.0.0.1:6379/15")
)
app.conf.update(
    worker_hijack_root_logger=False,
    task_ignore_result=True,
    worker_send_task_events=False,
    broker_transport_options={"global_keyprefix": os.getenv("PROBE_KEY_PREFIX", "observability:")},
)


@app.task(name="observability.probe", bind=True, max_retries=1)
def probe(self, retry=False, pause=0):
    if retry and self.request.retries == 0:
        raise self.retry(countdown=0)
    if pause and os.getenv("PROBE_PID_FILE"):
        Path(os.environ["PROBE_PID_FILE"]).write_text(str(os.getpid()))
    time.sleep(min(pause, 10))


def response(request):
    if request.path == "/slow":
        time.sleep(10)
    return HttpResponse(str(os.getpid()))


middleware = TelemetryMiddleware(response)


def application(environ, start_response):
    request = RequestFactory().get(environ.get("PATH_INFO", "/"))
    result = middleware(request)
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [result.content]
