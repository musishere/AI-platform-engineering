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


def create_client() -> httpx.AsyncClient:
    # Created once per process and shared (see main.py lifespan). Reusing it
    # keeps TLS connections open, saving ~100ms of handshake per request.
    return httpx.AsyncClient(base_url=UPSTREAM_BASE_URL, timeout=TIMEOUT)


async def forward_messages(
    client: httpx.AsyncClient, body: bytes, caller_headers: Headers
) -> httpx.Response:
    headers = {
        # OpenRouter wants Bearer auth. Anthropic direct uses x-api-key, so
        # switching providers means changing this line plus the .env values.
        "authorization": f"Bearer {UPSTREAM_API_KEY}",
        "content-type": "application/json",
    }
    for name in PASSTHROUGH_HEADERS:
        if name in caller_headers:
            headers[name] = caller_headers[name]
    return await client.post("/v1/messages", content=body, headers=headers)
