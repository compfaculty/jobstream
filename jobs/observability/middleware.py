"""HTTP completion telemetry without reading request data or buffering streams."""

import logging
import time

from .context import context, new_context
from .metrics import HTTP, INFLIGHT, LATENCY, PAYLOAD, UNKNOWN, observe

logger = logging.getLogger(__name__)


class TelemetryMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        values = new_context({"request_id": request.headers.get("X-Request-ID")})
        token = context.set(values)
        started = time.monotonic()
        method = (
            request.method
            if request.method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
            else "OTHER"
        )
        observe(INFLIGHT.inc)
        finished = False

        def finish(response, size=None):
            nonlocal finished
            if finished:
                return
            finished = True
            route = (
                getattr(getattr(request, "resolver_match", None), "view_name", None) or "unknown"
            )
            # view_name comes only from installed URL patterns, never request paths.
            elapsed = time.monotonic() - started
            status = response.status_code if response else 500
            observe(lambda: HTTP.labels(route, method, str(status)).inc())
            observe(lambda: LATENCY.labels(route, method).observe(elapsed))
            observe(INFLIGHT.dec)
            observe(UNKNOWN.inc if size is None else lambda: PAYLOAD.inc(size))
            current = context.set(values)
            try:
                logger.info(
                    "http_completed",
                    extra={
                        "route": route,
                        "method": method,
                        "status": status,
                        "duration_seconds": elapsed,
                    },
                )
            finally:
                context.reset(current)

        try:
            response = self.get_response(request)
            response["X-Request-ID"] = values["request_id"]
            if response.streaming:
                original = response.streaming_content
                if response.is_async:

                    async def asynchronous():
                        iterator = original.__aiter__()
                        try:
                            while True:
                                current = context.set(values)
                                try:
                                    chunk = await anext(iterator)
                                except StopAsyncIteration:
                                    break
                                finally:
                                    context.reset(current)
                                yield chunk
                        finally:
                            finish(response)

                    response.streaming_content = asynchronous()
                else:

                    def synchronous():
                        iterator = iter(original)
                        try:
                            while True:
                                current = context.set(values)
                                try:
                                    chunk = next(iterator)
                                except StopIteration:
                                    break
                                finally:
                                    context.reset(current)
                                yield chunk
                        finally:
                            finish(response)

                    response.streaming_content = synchronous()
                # Existing resource closers run on disconnect as well as normal completion.
                response._resource_closers.append(lambda: finish(response))
            else:
                finish(response, len(response.content))
            return response
        except BaseException:
            finish(None)
            raise
        finally:
            context.reset(token)

    def process_exception(self, request, exception):
        logger.error(
            "http_exception", exc_info=(type(exception), exception, exception.__traceback__)
        )
