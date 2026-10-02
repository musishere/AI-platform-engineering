# Container image for the LLM gateway.
#
# Kubernetes runs images, not Python folders, so this is how the gateway gets
# onto a cluster (minikube now, OKE later). CI builds, scans and pushes this
# exact file, so what runs in production is what the scanner checked.
#
# Two stages, like a workshop and a delivery box: stage 1 has uv and installs
# dependencies; stage 2 copies only the result, so the shipped image carries
# no build tools (smaller pull, fewer packages for the scanner to flag).

FROM python:3.12-slim AS build
# Pinned uv version so every build resolves the same way as my laptop.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv
# Compile .pyc now so the pod doesn't do it on every cold start.
# Copy (not hardlink) files into the venv, because the cache is a separate layer.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
# Lock files first, code later: Docker caches this layer, so editing gateway
# code doesn't reinstall every dependency. --frozen fails the build if
# uv.lock is out of date instead of silently picking new versions.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
# Install Debian's security fixes now, not when the python image is next
# rebuilt: on 2026-10-02 Trivy blocked the deploy on fixed OpenSSL/pcre2
# CVEs that the base image didn't have yet. Costs some repeatability (a
# rebuild can pull newer OS packages); worth it for same-day patches.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
# Non-root user: if someone breaks into the gateway process, they don't get
# root inside the container. A fixed UID lets Kubernetes enforce runAsNonRoot.
RUN useradd --system --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY gateway ./gateway
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8000
# No --reload (that's for local dev). Config (DATABASE_URL, REDIS_URL,
# UPSTREAM_*) comes from env vars that Kubernetes injects, never baked in.
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
