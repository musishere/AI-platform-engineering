# Metrics: the gateway's dashboard gauges, read by Prometheus.
#
# Prometheus PULLS: every ~15s it visits each gateway pod's /metrics page and
# reads the current numbers. This module only keeps counters in memory and
# serves them. Each pod counts on its own; Prometheus adds the pods up.
#
# /metrics lives on its own port (9100), not the API port: it shows tenant ids
# and usage, and the gateway Service (later the public load balancer) only
# exposes 8000, so outsiders can never read it.
#
# Label rule (cardinality): every distinct label combination is a separate
# time series that Prometheus stores for weeks. Labels only take values from a
# small known set: tenant ids (a handful), known models, status codes. Never
# anything a caller can make up freely, or one caller could create millions
# of series and take Prometheus down.

import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from gateway.metering import PRICES_PER_MTOK

METRICS_PORT = 9100

# LLM calls take seconds, not milliseconds. The client's default buckets top
# out at 10s, so every slow-but-normal answer would land in "+Inf" and p99
# would be unreadable. These go up to our 120s upstream timeout.
DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120)
TTFT_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30)

REQUESTS = Counter(
    "gateway_requests_total",
    "Requests to /v1/messages, by outcome. Rate and error rate come from this.",
    ["tenant", "model", "status"],
)
DURATION = Histogram(
    "gateway_request_duration_seconds",
    "Time from request in to last byte out (whole stream, for streaming).",
    ["tenant", "model"],
    buckets=DURATION_BUCKETS,
)
# ponytail: tenant on a histogram = tenants x models x 13 buckets series.
# Fine for a handful of tenants; drop the tenant label here if that grows.
TTFT = Histogram(
    "gateway_time_to_first_token_seconds",
    "Streams only: time until the first content arrives. What users feel as 'fast'.",
    ["model"],
    buckets=TTFT_BUCKETS,
)
TOKENS = Counter(
    "gateway_tokens_total",
    "Tokens used, from the provider's counts (estimated for broken streams).",
    ["tenant", "model", "direction"],
)
LIMIT_REJECTIONS = Counter(
    "gateway_limit_rejections_total",
    "Requests refused with 429 by our own limits (not the provider's).",
    ["tenant", "reason"],
)
IN_FLIGHT = Gauge(
    "gateway_requests_in_flight",
    "Requests being handled by this pod right now (streams count until they end).",
)


def start_server() -> None:
    # A small HTTP server on its own thread, separate from uvicorn, so a
    # scrape never competes with API requests for the event loop.
    start_http_server(METRICS_PORT)


def model_label(model: str | None) -> str:
    # The model name comes from the caller. Only models we know (the price
    # table) become a label; anything else is "other", so random model names
    # can't explode the number of series.
    return model if model in PRICES_PER_MTOK else "other"


def record_tokens(tenant_id: int, model: str | None, input_tokens: int, output_tokens: int) -> None:
    labels = (str(tenant_id), model_label(model))
    TOKENS.labels(*labels, "input").inc(input_tokens)
    TOKENS.labels(*labels, "output").inc(output_tokens)


def record_rejection(tenant_id: int, reason: str) -> None:
    LIMIT_REJECTIONS.labels(str(tenant_id), reason).inc()


def record_ttft(model: str | None, seconds: float) -> None:
    TTFT.labels(model_label(model)).observe(seconds)


class MetricsMiddleware:
    """Counts every /v1/messages request once, however it ends.

    A plain ASGI middleware, not FastAPI's @app.middleware("http"): that one
    sees the response when its headers go out, which for a stream is the
    first second of a two-minute answer. Watching the raw `send` calls lets
    us stop the clock at the real last byte.

    The route puts tenant and model into request.state as soon as it knows
    them; a 401 never learns a tenant, so it's counted as tenant "none".
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] != "/v1/messages":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500  # if the app crashes before answering, that's what the caller gets
        IN_FLIGHT.inc()

        async def send_and_watch(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_watch)
        finally:
            # Runs on success, on a crash, and when a streaming caller
            # disconnects (the task is cancelled). No awaits in here, so a
            # cancellation can't stop it halfway.
            IN_FLIGHT.dec()
            state = scope.get("state", {})
            tenant = str(state.get("tenant_id", "none"))
            model = model_label(state.get("model"))
            REQUESTS.labels(tenant, model, str(status)).inc()
            DURATION.labels(tenant, model).observe(time.perf_counter() - started)


if __name__ == "__main__":
    # Self-check: run a fake app through the middleware and check the counts.
    #   uv run python -m gateway.metrics
    import asyncio

    from prometheus_client import REGISTRY

    def sample(name: str, **labels: str) -> float:
        return REGISTRY.get_sample_value(name, labels) or 0.0

    async def fake_app(scope, receive, send):
        scope["state"]["tenant_id"] = 7
        scope["state"]["model"] = "no-such-model"
        await send({"type": "http.response.start", "status": 429, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def crashing_app(scope, receive, send):
        raise RuntimeError("bug")

    async def noop_send(message):
        pass

    async def run(app, path="/v1/messages"):
        scope = {"type": "http", "path": path, "state": {}}
        try:
            await MetricsMiddleware(app)(scope, None, noop_send)
        except RuntimeError:
            pass

    asyncio.run(run(fake_app))
    asyncio.run(run(crashing_app))
    asyncio.run(run(fake_app, path="/health"))  # must not be counted

    # Unknown model collapsed to "other"; status taken from the response.
    assert sample("gateway_requests_total", tenant="7", model="other", status="429") == 1
    # A crash before any response counts as a 500 with no tenant.
    assert sample("gateway_requests_total", tenant="none", model="other", status="500") == 1
    assert sample("gateway_request_duration_seconds_count", tenant="7", model="other") == 1
    assert sample("gateway_requests_in_flight") == 0  # every inc had its dec
    record_tokens(7, "anthropic/claude-haiku-4.5", 10, 5)
    assert sample("gateway_tokens_total", tenant="7", model="anthropic/claude-haiku-4.5", direction="output") == 5
    print("metrics self-check ok")
