"""Internal authenticated exposition. No metric route is added to Django URLs."""

import hmac
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess
from prometheus_client.core import GaugeMetricFamily


class SnapshotCollector:
    def __init__(self):
        self.cached = []
        self.last = 0
        self.lock = threading.Lock()

    def collect(self):
        with self.lock:
            if time.monotonic() - self.last >= 15:
                self.cached = self.refresh()
                self.last = time.monotonic()
            return iter(self.cached)

    def refresh(self):
        from django.db import close_old_connections, connection
        from django.db.models import Count, Q

        from jobs.models import Company, OperationalSnapshot, Source
        from jobs.services.operations import QUEUES

        def gauge(name, description, value):
            metric = GaugeMetricFamily(f"jobstream_{name}", description)
            metric.add_metric([], value)
            return metric

        result = []
        close_old_connections()
        try:
            # All aggregate queries share one short read-only transaction and timeout.
            from django.db import transaction

            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute("SET LOCAL statement_timeout = '1500ms'")
                snapshot = OperationalSnapshot.objects.filter(key="infrastructure").first()
                companies = Company.objects.filter(review_required=True).count()
                sources = Source.objects.aggregate(
                    pending=Count("pk", filter=Q(discovery_pending=True)),
                    enabled=Count("pk", filter=Q(enabled=True)),
                )
            result.extend(
                [
                    gauge("pending_company_reviews", "Companies with review_required", companies),
                    gauge(
                        "pending_source_reviews",
                        "Sources with discovery_pending",
                        sources["pending"],
                    ),
                    gauge("enabled_sources", "Enabled sources, stock count", sources["enabled"]),
                    gauge(
                        "snapshot_timestamp_seconds",
                        "Last stored infrastructure snapshot",
                        snapshot.observed_at.timestamp() if snapshot else 0,
                    ),
                    gauge(
                        "snapshot_present",
                        "Infrastructure snapshot exists",
                        int(snapshot is not None),
                    ),
                ]
            )
            if snapshot:
                payload = snapshot.payload
                result.append(
                    gauge(
                        "scheduler_publication_timestamp_seconds",
                        "Last observed scheduler publication",
                        payload.get("scheduler_published_at") or 0,
                    )
                )
                result.append(
                    gauge(
                        "worker_probe_success",
                        "Stored worker probe succeeded",
                        int(payload.get("worker_probe") == "healthy"),
                    )
                )
                depth = GaugeMetricFamily(
                    "jobstream_queue_depth", "Stored broker queue depth", labels=["queue"]
                )
                consumers = GaugeMetricFamily(
                    "jobstream_queue_consumers", "Stored subscribed consumers", labels=["queue"]
                )
                for queue in QUEUES:
                    depth.add_metric(
                        [queue],
                        sum(
                            q.get("depth", 0)
                            for q in payload.get("queues", [])
                            if q.get("name") == queue
                        ),
                    )
                    consumers.add_metric(
                        [queue],
                        sum(
                            queue in worker.get("queues", [])
                            for worker in payload.get("workers", [])
                        ),
                    )
                result.extend([depth, consumers])
            result.append(
                gauge("snapshot_exporter_success", "Snapshot exporter collection succeeded", 1)
            )
        except Exception:
            # Do not expose stale business values as fresh or log database exception text.
            result = [
                gauge("snapshot_exporter_success", "Snapshot exporter collection succeeded", 0)
            ]
        finally:
            close_old_connections()
        disk = GaugeMetricFamily(
            "jobstream_monitoring_disk_available_bytes",
            "Available bytes on the filesystem backing each dedicated volume",
            labels=["volume"],
        )
        for volume in ("prometheus", "loki", "alloy"):
            try:
                stats = os.statvfs(f"/monitoring-storage/{volume}")
                disk.add_metric([volume], stats.f_bavail * stats.f_frsize)
            except OSError:
                continue
        result.append(disk)
        return result


def serve(port=9100, snapshot=False):
    secret = Path(os.environ["JOBSTREAM_SCRAPE_TOKEN_FILE"]).read_text().strip()
    if len(secret) < 32:
        raise ValueError("Scrape credential too short")
    if snapshot:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        registry = CollectorRegistry()
        registry.register(SnapshotCollector())
    else:
        registry = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            if not hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), f"Bearer {secret}".encode()
            ):
                self.send_response(401)
                self.end_headers()
                return
            current = registry
            if current is None:
                current = CollectorRegistry()
                multiprocess.MultiProcessCollector(current)
            try:
                body = generate_latest(current)
            except Exception:
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(HTTPServer):
        def get_request(self):
            conn, address = super().get_request()
            conn.settimeout(5)
            return conn, address

    Server(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104 -- internal Compose only


if __name__ == "__main__":
    serve(snapshot=os.getenv("JOBSTREAM_SERVICE") == "snapshot-exporter")
