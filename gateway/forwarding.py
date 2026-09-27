# Forwarding: sends the caller's request on to the upstream LLM provider
# (OpenRouter today, Anthropic directly later) and hands back its reply.
#
# This is deliberately a pass-through. We don't parse or rebuild the body, so
# when the API gains a new field it reaches Claude without a gateway change.
# Auth (Day 3) and metering (Day 4) wrap around this; they don't live in it.

import os

import httpx
from starlette.datastructures import Headers

# Read at import so a missing variable crashes on startup, not on the first
# real request an hour later.
UPSTREAM_BASE_URL = os.environ["UPSTREAM_BASE_URL"]
UPSTREAM_API_KEY = os.environ["UPSTREAM_API_KEY"]

# httpx gives up after 5s by default, but a long LLM answer can take 30s+.
# Cutting it off early would waste a request we still pay for. Connecting is
# kept short so a dead upstream fails fast instead of hanging for 2 minutes.
TIMEOUT = httpx.Timeout(120.0, connect=5.0)

# Caller headers we pass on. An allowlist, not "copy everything": the
# caller's own Authorization header is a *gateway* key and must never leak
# upstream, and headers like Host describe the caller's hop, not ours.
PASSTHROUGH_HEADERS = ("anthropic-version", "anthropic-beta")

# Anthropic direct rejects requests without this header (OpenRouter doesn't
# care). Defaulting it means switching providers won't break callers that
# leave it out. A caller's own value still wins.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# Upstream reply headers we pass back, also an allowlist. retry-after tells
# the caller's SDK how long to wait after a 429; request-id is what the
# provider's support asks for when debugging. Not copied: content-encoding
# and content-length, because httpx has already decompressed the body, so
# they'd describe bytes the caller never receives.
RESPONSE_HEADERS = ("retry-after", "request-id")
RESPONSE_HEADER_PREFIXES = ("anthropic-ratelimit-",)


def create_client() -> httpx.AsyncClient:
    # Created once per process and shared (see main.py lifespan). Reusing it
    # keeps TLS connections open, saving ~100ms of handshake per request.
    return httpx.AsyncClient(base_url=UPSTREAM_BASE_URL, timeout=TIMEOUT)


def upstream_headers(caller_headers: Headers) -> dict[str, str]:
    headers = {
        # OpenRouter wants Bearer auth. Anthropic direct uses x-api-key, so
        # switching providers means changing this line plus the .env values.
        "authorization": f"Bearer {UPSTREAM_API_KEY}",
        "content-type": "application/json",
        "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
    }
    for name in PASSTHROUGH_HEADERS:
        if name in caller_headers:
            headers[name] = caller_headers[name]
    return headers


async def forward_messages(
    client: httpx.AsyncClient, body: bytes, caller_headers: Headers
) -> httpx.Response:
    # Waits for the whole reply, then returns it.
    return await client.post(
        "/v1/messages", content=body, headers=upstream_headers(caller_headers)
    )


async def open_stream(
    client: httpx.AsyncClient, body: bytes, caller_headers: Headers
) -> httpx.Response:
    # Returns as soon as the reply's status and headers arrive; the body is
    # read piece by piece later. The caller must close it (aclose), which is
    # also what tells the provider to stop generating.
    request = client.build_request(
        "POST", "/v1/messages", content=body, headers=upstream_headers(caller_headers)
    )
    return await client.send(request, stream=True)


def response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in upstream.headers.items()
        if name in RESPONSE_HEADERS or name.startswith(RESPONSE_HEADER_PREFIXES)
    }
