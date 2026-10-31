import os

from django.apps import AppConfig


class JobsConfig(AppConfig):
    name = "jobs"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.instrumentation.django import DjangoInstrumentor
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

            provider = TracerProvider(sampler=TraceIdRatioBased(0.05))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
            # OAuth and invite URLs contain secrets; exclude them entirely.
            DjangoInstrumentor().instrument(excluded_urls=".*gmail/.*callback.*,.*invite/.*")
