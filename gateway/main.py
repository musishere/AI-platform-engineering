# Entry point of the LLM gateway.
#
# The gateway sits between tenants and the Claude API, like a building's front
# desk: every request passes through here so we can check who is calling
# (auth), send the request on (forwarding), and record what it cost (metering).
# Each of those lives in its own module; this file wires them to HTTP routes.
#
# Run locally: uv run --env-file .env uvicorn gateway.main:app --reload

import json
import os
import time
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway import auth, forwarding, metering, streaming
from gateway.db import DB_ERRORS


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One upstream client and one DB pool for the whole process, so
    # connections get reused instead of re-opened on every request.
    # Both are closed on shutdown so no sockets are left open.
    app.state.upstream = forwarding.create_client()
    app.state.db = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    yield
    await app.state.db.close()
    await app.state.upstream.aclose()


app = FastAPI(title="LLM Gateway", lifespan=lifespan)


def error_response(status: int, error_type: str, message: str) -> JSONResponse:
    # Same error shape Anthropic uses, so callers built on the Anthropic SDK
    # parse our errors the same way they parse Claude's.
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


# Same cap as Claude's own API. Without one, a single tenant sending a huge
# body could exhaust the gateway's memory and take every tenant down.
MAX_BODY_BYTES = 32 * 1024 * 1024


async def read_body_limited(request: Request) -> bytes | None:
    # Returns None when the body is too large.
    # Cheap early exit when the caller declares its size up front...
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    # ...but count while reading anyway, because a body can arrive without a
    # declared size (chunked upload).
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def parse_json_object(body: bytes) -> dict:
    # Peek at a few fields (model, stream) without changing what we forward.
    # Invalid JSON gives {}: the upstream will reject it with a proper 400.
    try:
        payload = json.loads(body)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


@app.get("/health")
def health() -> dict[str, str]:
    # Liveness only: "is this process up and answering?"
    # Deliberately no database call. If a DB blip failed this check, the
    # orchestrator would restart every gateway copy at once and turn a small
    # outage into a full one. DB readiness belongs in a separate check.
    return {"status": "ok"}


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    # x-api-key is the header the Anthropic SDK already sends, so a caller
    # only swaps base URL + key to go through us.
    try:
        tenant_id = await auth.find_tenant_id(
            request.app.state.db, request.headers.get("x-api-key")
        )
    except DB_ERRORS:
        # Fail closed: if we can't check the key, nobody gets in. 503 says
        # "a service I depend on is down", so it's retryable, unlike a 401.
        return error_response(503, "api_error", "auth database unavailable")
    if tenant_id is None:
        # Same message for "no key" and "wrong key": telling them apart
        # would help an attacker probe which keys exist.
        return error_response(401, "authentication_error", "invalid x-api-key")

    body = await read_body_limited(request)
    if body is None:
        return error_response(413, "request_too_large", "request body exceeds 32 MB")

    payload = parse_json_object(body)
    model = payload.get("model")
    if not isinstance(model, str):
        model = None

    try:
        event_id = await metering.start(request.app.state.db, tenant_id, model)
    except DB_ERRORS:
        # Nothing has been spent yet, so refusing here is safe. Better than
        # letting a call through that we'd have no record of.
        return error_response(503, "api_error", "usage database unavailable")

    # perf_counter is a monotonic clock: it only moves forward, unlike the
    # wall clock, which can jump when the machine syncs its time.
    started = time.perf_counter()
    try:
        if payload.get("stream"):
            upstream = await forwarding.open_stream(
                request.app.state.upstream, body, request.headers
            )
            if streaming.is_event_stream(upstream):
                # From here the stream relays itself and writes the usage
                # row when it ends, however it ends.
                return streaming.MeteredStream(
                    upstream, request.app.state.db, event_id, model, started
                )
            # Not a stream after all (e.g. a 400 sent up front): read it
            # whole and handle it exactly like a normal reply below.
            await upstream.aread()
        else:
            upstream = await forwarding.forward_messages(
                request.app.state.upstream, body, request.headers
            )
        # Pass the upstream answer through unchanged, errors included: a 400
        # for a bad model name or a 429 rate limit is information the caller
        # needs.
        response = Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=forwarding.response_headers(upstream),
            media_type=upstream.headers.get("content-type"),
        )
    except httpx.TimeoutException:
        # 504 = "the server behind me was too slow". Distinct from 502 so the
        # caller (and later our dashboards) can tell slow from down.
        response = error_response(504, "api_error", "upstream timed out")
    except httpx.RequestError:
        # 502 = "the server behind me failed" (DNS, refused, reset...).
        response = error_response(502, "api_error", "upstream unreachable")
    latency_ms = round((time.perf_counter() - started) * 1000)

    # Errors are metered too: "tenant X got 200 errors today" is exactly what
    # a usage table should show.
    await metering.finish_buffered(
        request.app.state.db, event_id, model, response.status_code, response.body, latency_ms
    )
    return response
