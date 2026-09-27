# Entry point of the LLM gateway.
#
# The gateway sits between tenants and the Claude API, like a building's front
# desk: every request passes through here so we can check who is calling
# (auth), send the request on (forwarding), and record what it cost (metering).
# Each of those lives in its own module; this file wires them to HTTP routes.
#
# Run locally: uv run --env-file .env uvicorn gateway.main:app --reload

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway import forwarding


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One upstream client for the whole process, so connections get reused
    # (connection pooling). Closed on shutdown so no sockets are left open.
    app.state.upstream = forwarding.create_client()
    yield
    await app.state.upstream.aclose()


app = FastAPI(title="LLM Gateway", lifespan=lifespan)


def upstream_error(status: int, message: str) -> JSONResponse:
    # Same error shape Anthropic uses, so callers built on the Anthropic SDK
    # parse our errors the same way they parse Claude's.
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": "api_error", "message": message}},
    )


@app.get("/health")
def health() -> dict[str, str]:
    # Liveness only: "is this process up and answering?"
    # Deliberately no database call. If a DB blip failed this check, the
    # orchestrator would restart every gateway copy at once and turn a small
    # outage into a full one. DB readiness belongs in a separate check.
    return {"status": "ok"}


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    body = await request.body()
    try:
        upstream = await forwarding.forward_messages(
            request.app.state.upstream, body, request.headers
        )
    except httpx.TimeoutException:
        # 504 = "the server behind me was too slow". Distinct from 502 so the
        # caller (and later our dashboards) can tell slow from down.
        return upstream_error(504, "upstream timed out")
    except httpx.RequestError:
        # 502 = "the server behind me failed" (DNS, refused, reset...).
        return upstream_error(502, "upstream unreachable")

    # Pass the upstream answer through unchanged, errors included: a 400 for
    # a bad model name or a 429 rate limit is information the caller needs.
    # ponytail: buffers the whole reply, so "stream": true arrives all at once;
    # real streaming is Project 1's stretch goal.
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
