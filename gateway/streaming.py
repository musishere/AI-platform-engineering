# Streaming: relays a streamed (SSE) reply to the caller piece by piece,
# while metering reads the token counts on the way through.
#
# The hard part is the ending. A stream can finish normally, break on the
# provider's side, or the caller can leave midway. Whatever happens we (1)
# close the upstream, so Claude stops writing tokens nobody will read but we'd
# pay for, and (2) write the usage row, marking whether its counts are exact.

import time
from datetime import datetime

import anyio
import asyncpg
import httpx
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from gateway import forwarding, limits, metering


def is_event_stream(upstream: httpx.Response) -> bool:
    # A streaming request can still get a plain JSON reply, e.g. a 400 for a
    # bad model name, sent before any streaming starts. Only 200 + SSE is a
    # real stream; anything else is handled like a normal reply.
    return upstream.status_code == 200 and upstream.headers.get(
        "content-type", ""
    ).startswith("text/event-stream")


class MeteredStream(StreamingResponse):
    def __init__(
        self,
        upstream: httpx.Response,
        db: asyncpg.Pool,
        limiter: limits.Limiter,
        tenant_id: int,
        event_id: int,
        model: str | None,
        started: float,
        started_at: datetime,
        quota_hold: int,
    ) -> None:
        self.upstream = upstream
        self.db = db
        self.limiter = limiter
        self.tenant_id = tenant_id
        self.event_id = event_id
        self.model = model
        self.started = started
        self.started_at = started_at
        self.quota_hold = quota_hold
        self.meter = metering.StreamUsage()
        self.upstream_done = False    # we read the provider's stream to the end
        self.upstream_failed = False  # the provider's connection broke midway
        super().__init__(
            self._relay(),
            status_code=upstream.status_code,
            headers=forwarding.response_headers(upstream),
            media_type=upstream.headers.get("content-type"),
        )

    async def _relay(self):
        try:
            async for chunk in self.upstream.aiter_bytes():
                self.meter.feed(chunk)
                yield chunk  # to the caller immediately, unchanged
        except httpx.HTTPError:
            # The provider's side broke. The caller sees its stream end early;
            # there's no clean way to send an HTTP error once 200 has gone out.
            self.upstream_failed = True
            return
        self.upstream_done = True

    def _outcome(self) -> str:
        if self.upstream_failed or self.meter.errored:
            return "upstream_error"
        if self.upstream_done:
            return "complete" if self.meter.stopped else "upstream_error"
        # We never reached the end of the provider's stream, so the caller left.
        # (uvicorn reports ASGI 2.3: Starlette handles a disconnect by quietly
        # cancelling the relay, not by raising, so this is how we detect it.)
        return "client_disconnected"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Shielded: if this request is being cancelled, a plain `await`
            # here would be cancelled too, and the row would never be written.
            # The shield lets this cleanup finish no matter what.
            with anyio.CancelScope(shield=True):
                # Closing the upstream is what tells the provider to stop
                # generating, so a caller who leaves stops costing money.
                await self.upstream.aclose()
                input_tokens, output_tokens = self.meter.usage()
                tokens = await metering.finish(
                    self.db,
                    self.event_id,
                    self.model,
                    self.status_code,
                    round((time.perf_counter() - self.started) * 1000),
                    input_tokens,
                    output_tokens,
                    self.meter.provider_cost,
                    self._outcome(),
                )
                # Estimated tokens count too: a disconnect mustn't be a way
                # around the quota any more than around billing.
                await self.limiter.settle(
                    self.tenant_id, self.started_at, self.quota_hold, tokens
                )
