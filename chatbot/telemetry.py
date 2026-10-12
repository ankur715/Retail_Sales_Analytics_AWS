"""Traces and metrics for the chatbot (milestone 1: where does the latency go?).

Traces (OpenTelemetry): one trace per request, with a span per step --
  llm.route, db.known_values, llm.write_sql, sql.validate, db.query, db.data_as_of, llm.summarize
-- exported over OTLP to the collector -> Jaeger. Off unless OTEL_EXPORTER_OTLP_ENDPOINT is
set, so tests, CI and the laptop export nothing (the OpenTelemetry API is then a no-op).

Metrics (Prometheus, GET /metrics), the four golden signals:
  traffic     chatbot_requests_total{route,status}
  errors      the same counter, status >= 500; chatbot_step_errors_total{step}
  latency     chatbot_request_seconds{route}, chatbot_step_seconds{step}
  saturation  chatbot_requests_in_flight
plus chatbot_cache_total{result} for the answer cache.

Spans never carry question text, answers or result rows -- only the step, model, view
names, row counts and the validated SQL.
"""
import os
import time
from contextlib import contextmanager

from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE = "retail-chatbot"
tracer = trace.get_tracer("chatbot")

SECONDS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30)
REQUESTS = Counter("chatbot_requests_total", "HTTP requests handled", ["route", "status"])
LATENCY = Histogram("chatbot_request_seconds", "HTTP request latency", ["route"], buckets=SECONDS)
IN_FLIGHT = Gauge("chatbot_requests_in_flight", "Requests being handled right now")
STEP_SECONDS = Histogram("chatbot_step_seconds", "Time per chat step", ["step"], buckets=SECONDS)
STEP_ERRORS = Counter("chatbot_step_errors_total", "Chat steps that raised", ["step"])
CACHE = Counter("chatbot_cache_total", "Answer cache lookups", ["result"])
UNTRACKED = {"/metrics", "/api/health"}


@contextmanager
def step(name: str, **attributes):
    """A span plus a latency/error metric for one step of the chat pipeline."""
    started = time.perf_counter()
    with tracer.start_as_current_span(name, attributes={k: v for k, v in attributes.items() if v is not None}) as span:
        try:
            yield span
        except Exception:
            STEP_ERRORS.labels(name).inc()   # the span records the exception and sets ERROR itself
            raise
        finally:
            STEP_SECONDS.labels(name).observe(time.perf_counter() - started)


def _setup_tracing(app: FastAPI) -> None:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))   # endpoint from the env var
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls=",".join(UNTRACKED))


def setup(app: FastAPI) -> None:
    _setup_tracing(app)

    @app.middleware("http")
    async def golden_signals(request: Request, call_next):
        if request.url.path in UNTRACKED:
            return await call_next(request)
        IN_FLIGHT.inc()
        started, status = time.perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            IN_FLIGHT.dec()
            route = getattr(request.scope.get("route"), "path", "other")   # template, not raw path
            REQUESTS.labels(route, str(status)).inc()
            LATENCY.labels(route).observe(time.perf_counter() - started)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
