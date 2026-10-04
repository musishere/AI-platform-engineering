# Tracing: records a timeline ("trace") of each request, so one slow call can
# be taken apart step by step: auth, limits, Postgres, Redis, the upstream.
#
# Metrics (metrics.py) answer "how many, how fast overall". A trace answers
# "where did THIS request spend its time". Each timed step is a "span"; spans
# nest, and together they form the trace.
#
# Unlike Prometheus, which PULLS metrics, spans are PUSHED: batched in memory
# and sent in the background (OTLP over HTTP) to Tempo in the monitoring
# namespace, then viewed in Grafana.
#
# Most spans come free from instrumentation libraries that patch FastAPI,
# httpx, asyncpg and redis. Our own steps get named spans with the `tracer`
# below (used as a decorator in auth, limits and metering), so the timeline
# reads "limits.reserve", not just "EVALSHA".
#
# Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (k8s/config.yaml sets it), so
# a local run needs no Tempo. When off, the decorators cost next to nothing.

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

# Safe to create at import, before setup(): it forwards to whatever provider
# is set later, and does nothing if none ever is.
tracer = trace.get_tracer("gateway")


def setup(app, exporter: SpanExporter | None = None) -> TracerProvider | None:
    """Turns tracing on. Returns the provider (to flush on shutdown), or None if off.

    `exporter` is only for the self-check below; the gateway always uses OTLP.
    """
    if exporter is None and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    # Resource = "who sent these spans". Resource.create() reads
    # OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES (the pod name, from
    # k8s/gateway.yaml), so a trace shows WHICH pod served it: canary or stable.
    provider = TracerProvider(resource=Resource.create())
    # Batch: spans are sent from a background thread every few seconds, never
    # inside a request. If Tempo is down, spans are dropped and requests carry
    # on (fail open): losing a trace is fine, failing a tenant's call is not.
    # ponytail: 100% of requests are traced; add a sampler (keep ~1-10%)
    # when traffic is big enough that storing every trace costs real money.
    provider.add_span_processor(BatchSpanProcessor(exporter or OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(
        app,
        # Probes hit /health every few seconds on every pod: thousands of
        # useless traces a day that would bury the real ones.
        excluded_urls="health",
        # Without this, every ASGI send becomes its own span: one per chunk,
        # so a long stream would produce hundreds of spans of pure noise.
        exclude_spans=["receive", "send"],
    )
    # These patch the libraries' classes, so the clients main.py creates
    # later in lifespan are covered too. The httpx one also adds a
    # `traceparent` header to upstream calls: random ids only, no secrets.
    HTTPXClientInstrumentor().instrument()
    AsyncPGInstrumentor().instrument()
    RedisInstrumentor().instrument()
    return provider


if __name__ == "__main__":
    # Self-check: a tiny app through the same setup, spans kept in memory.
    #   uv run python -m gateway.tracing
    import asyncio

    import httpx
    from fastapi import FastAPI
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    @tracer.start_as_current_span("step")
    async def step() -> None:
        await asyncio.sleep(0.05)

    demo = FastAPI()

    @demo.get("/work")
    async def work() -> dict:
        trace.get_current_span().set_attribute("tenant.id", 7)
        await step()
        return {}

    @demo.get("/health")
    async def health() -> dict:
        return {}

    memory = InMemorySpanExporter()
    provider = setup(demo, exporter=memory)

    async def run() -> None:
        transport = httpx.ASGITransport(app=demo)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            await client.get("/work")
            await client.get("/health")

    asyncio.run(run())
    provider.force_flush()
    spans = {s.name: s for s in memory.get_finished_spans()}

    # Exactly two spans: the request and our step. /health and the per-send
    # spans were left out.
    assert set(spans) == {"GET /work", "step"}, set(spans)
    server, child = spans["GET /work"], spans["step"]
    # The step sits inside the request, in the same trace.
    assert child.parent.span_id == server.context.span_id
    assert child.context.trace_id == server.context.trace_id
    # The decorator timed the whole async function, not just its start.
    assert (child.end_time - child.start_time) / 1e9 >= 0.05
    assert server.attributes["tenant.id"] == 7
    print("tracing self-check ok")
