"""Privacy boundary for operational logs; arbitrary messages and extras are not exported."""

import atexit
import json
import logging
import logging.handlers
import os
import queue
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from .context import context, identifier
from .metrics import LOG_BUFFER, LOG_DROPS, observe

EVENT = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
ENUM = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,79}$")
ENUM_FIELDS = {
    "outcome",
    "state",
    "error_code",
    "error_category",
    "route",
    "method",
    "task_name",
    "queue",
    "provider",
    "model",
    "reason",
    "purpose",
}
NUMBER_FIELDS = {
    "status",
    "duration_seconds",
    "input_tokens",
    "output_tokens",
    "imported",
    "rejected",
    "failed",
    "inspected",
    "page",
    "retry_seconds",
}
ID_FIELDS = {
    "request_id",
    "workflow_id",
    "task_id",
    "execution_id",
    "attempt_id",
    "call_id",
    "source_id",
    "run_id",
}
SERVICES = {
    "web",
    "worker",
    "ingestion-worker",
    "preparation-worker",
    "delivery-worker",
    "browser-worker",
    "beat",
    "snapshot-exporter",
    "infinity",
}


SAFE_ERROR_CODES = frozenset(
    [
        "CrawlDeferred",
        "HTTPStatusError",
        "TimeoutError",
        "UnsafeURL",
        "ValueError",
        "agent_budget_accounting_inconsistent",
        "agent_busy",
        "agent_cancel_pending",
        "agent_evaluation",
        "agent_invalid_contract",
        "agent_invalid_metadata",
        "agent_invalid_result",
        "agent_invalid_usage",
        "agent_metadata_unavailable",
        "agent_not_configured",
        "agent_not_found",
        "agent_preference_override",
        "agent_recommendation_idx",
        "agent_request_rejected",
        "agent_response_too_large",
        "agent_service_error",
        "agent_timeout",
        "agent_unavailable",
        "agent_unsupported_claim",
        "ai_batch_enabled",
        "ai_batch_http_error",
        "ai_batch_invalid_result",
        "ai_batch_item_failed",
        "ai_batch_missing_result",
        "ai_batch_poll_unavailable",
        "ai_batch_response_too_large",
        "ai_batch_submission_unknown",
        "ai_batch_timeout",
        "ai_batch_unavailable",
        "ai_charge_observer",
        "ai_costs",
        "ai_daily_budget_exceeded",
        "ai_evaluation_budget_too_small",
        "ai_execution_superseded",
        "ai_failed",
        "ai_http_",
        "ai_http_400",
        "ai_http_401",
        "ai_http_403",
        "ai_incomplete_response",
        "ai_input_budget_exceeded",
        "ai_invalid_output_budget",
        "ai_invalid_provider_id",
        "ai_invalid_response",
        "ai_invalid_sections",
        "ai_model_price_unknown",
        "ai_monthly_budget_exceeded",
        "ai_normalization",
        "ai_normalization_enabled",
        "ai_not_configured",
        "ai_rate_limited",
        "ai_request_completed",
        "ai_request_failed",
        "ai_request_started",
        "ai_response_too_large",
        "ai_retry_scheduled",
        "ai_retry_window_requires_review",
        "ai_rollout_approved",
        "ai_selection",
        "ai_timeout",
        "ai_unavailable",
        "ai_unsupported_claim",
        "ai_user_daily_limit",
        "ai_version",
        "crawl_capacity_or_cooldown",
        "crawl_stats",
        "other",
        "source_due_enabled",
        "source_id",
        "source_kind_ownership",
        "source_labels",
        "source_lead",
        "source_line",
        "source_url",
    ]
)
SAFE_EVENTS = frozenset(
    [
        "Camunda delivery diagnostics",
        "Cato delivery diagnostics",
        "agent_budget_accounting_inconsistent",
        "agent_cancel_pending",
        "agent_evaluation",
        "agent_preference_override",
        "ai_request_completed",
        "ai_request_failed",
        "ai_request_started",
        "approval_finished",
        "approval_started",
        "delivery_deferred",
        "delivery_finished",
        "delivery_skipped",
        "delivery_started",
        "delivery_transition_finished",
        "approval_granted",
        "delivery_state_changed",
        "delivery_preflight_failed",
        "delivery_transition_started",
        "discovery_step_finished",
        "discovery_step_started",
        "http_completed",
        "http_exception",
        "ingestion_completed",
        "ingestion_started",
        "ingestion_failed",
        "normalization_finished",
        "normalization_started",
        "operations_scheduler_probe_unavailable",
        "preparation_finished",
        "preparation_started",
        "runtime_event",
        "scheduler_dispatched",
        "task_finished",
        "task_started",
    ]
)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        trusted = record.name.startswith(("jobs.", "jobstream."))
        event = (
            record.msg
            if trusted and isinstance(record.msg, str) and record.msg in SAFE_EVENTS
            else "runtime_event"
        )
        service = os.getenv("JOBSTREAM_SERVICE", "web")
        data = {
            "schema_version": 1,
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname
            if record.levelname in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
            else "INFO",
            "service": service if service in SERVICES else "worker",
            "environment": "development" if os.getenv("DEBUG", "0") == "1" else "production",
            "logger": record.name[:100] if trusted else "runtime",
            "event": event,
        }
        fields = {**context.get(), **record.__dict__} if trusted else context.get()
        revision = os.getenv("JOBSTREAM_BUILD_REVISION", "")
        if len(revision) == 40 and all(c in "0123456789abcdef" for c in revision):
            data["build_revision"] = revision
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry import trace

            span = trace.get_current_span().get_span_context()
            if span.is_valid:
                data["trace_id"] = f"{span.trace_id:032x}"
                data["span_id"] = f"{span.span_id:016x}"
        for key in ID_FIELDS:
            value = fields.get(key)
            if identifier(value):
                data[key] = value
            elif key in {"source_id", "run_id"} and type(value) is int and value > 0:
                data[key] = value
        for key in ENUM_FIELDS:
            value = fields.get(key)
            if isinstance(value, str) and ENUM.fullmatch(value):
                if key == "error_code":
                    value = value if value in SAFE_ERROR_CODES else "other"
                elif key == "model":
                    value = "fake" if value == "fake" else "configured"
                elif key == "purpose":
                    value = value if value in {"job_sections", "fact_selection"} else "other"
                elif key == "provider":
                    from .metrics import provider

                    value = provider(value)
                data[key] = value
        for key in NUMBER_FIELDS:
            value = fields.get(key)
            if type(value) in {int, float} and 0 <= value <= 1e15:
                data[key] = value
        if record.exc_info:
            # No exception string, source lines, arguments, locals, or chained payloads.
            data["error_category"] = "exception"
            import hashlib

            error_type = record.exc_info[0]
            data["error_fingerprint"] = hashlib.sha256(
                f"{error_type.__module__}.{error_type.__qualname__}".encode()
            ).hexdigest()[:16]
            frames = []
            tb = record.exc_info[2]
            while tb and len(frames) < 8:
                frames.append(
                    {"file": Path(tb.tb_frame.f_code.co_filename).name[:80], "line": tb.tb_lineno}
                )
                tb = tb.tb_next
            data["frames"] = frames
        return json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


class SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record):
        # Standard logging's fallback prints the original message/arguments.
        observe(lambda: LOG_DROPS.labels("console").inc())


class SafeRotatingHandler(logging.handlers.RotatingFileHandler):
    def handleError(self, record):
        observe(lambda: LOG_DROPS.labels("file").inc())


def prune(directory):
    """Bound dead-process history to 64 MiB/24h; retain live process files."""
    import time

    stale = []
    for path in directory.glob("events-*.jsonl*"):
        try:
            pid = int(path.name.split("-", 1)[1].split(".", 1)[0])
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                stat = path.stat()
                stale.append((stat.st_mtime, stat.st_size, path))
        except ValueError, OSError:
            continue
    total = sum(size for _, size, _ in stale)
    for modified, size, path in sorted(stale):
        if total <= 64 * 1024 * 1024 and time.time() - modified < 86400:
            break
        try:
            path.unlink(missing_ok=True)
            total -= size
        except OSError:
            observe(lambda: LOG_DROPS.labels("prune").inc())


class AsyncFileHandler(logging.Handler):
    """One bounded queue and writer per PID; no shared rotating file across forks."""

    def __init__(self):
        super().__init__()
        self.pid = None
        self.events = None
        self.writer = None
        self.thread = None

    def _start(self):
        self.pid = os.getpid()
        self.events = queue.Queue(maxsize=512)
        directory = Path(os.environ["JOBSTREAM_LOG_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        prune(directory)
        self.writer = SafeRotatingHandler(
            directory / f"events-{self.pid}.jsonl",
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
            delay=True,
        )
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()
        atexit.register(self.flush)

    def _drain(self):
        count = 0
        while True:
            try:
                value = self.events.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.writer.emit(logging.makeLogRecord({"msg": value, "levelno": logging.INFO}))
            except Exception:
                observe(lambda: LOG_DROPS.labels("file").inc())
            finally:
                self.events.task_done()
                observe(lambda: LOG_BUFFER.set(self.events.qsize()))
                count += 1
                if count % 100 == 0:
                    prune(Path(os.environ["JOBSTREAM_LOG_DIR"]))

    def emit(self, record):
        if not os.getenv("JOBSTREAM_LOG_DIR"):
            return
        try:
            if self.pid != os.getpid():
                self._start()
            self.events.put_nowait(self.format(record))
            observe(lambda: LOG_BUFFER.set(self.events.qsize()))
        except queue.Full:
            observe(lambda: LOG_DROPS.labels("queue").inc())
        except Exception:
            observe(lambda: LOG_DROPS.labels("file").inc())

    def flush(self):
        # A bounded shutdown wait; a broken disk must not hold up a worker.
        import time

        if self.pid != os.getpid() or self.events is None:
            return
        deadline = time.monotonic() + 0.25
        while self.events.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
