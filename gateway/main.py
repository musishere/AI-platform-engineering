# Entry point of the LLM gateway.
#
# The gateway sits between tenants and the Claude API, like a building's front
# desk: every request passes through here so we can check who is calling
# (auth), send the request on (forwarding), and record what it cost (metering).
# Those pieces get their own modules as we build them. For now this file only
# creates the app and a health endpoint.
#
# Run locally: uv run uvicorn gateway.main:app --reload

from fastapi import FastAPI

app = FastAPI(title="LLM Gateway")


@app.get("/health")
def health() -> dict[str, str]:
    # Liveness only: "is this process up and answering?"
    # Deliberately no database call. If a DB blip failed this check, the
    # orchestrator would restart every gateway copy at once and turn a small
    # outage into a full one. DB readiness belongs in a separate check.
    return {"status": "ok"}
