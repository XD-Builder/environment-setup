# Reference sandbox image for lmloop (see docs/GUIDE_DOCKER_AND_OPENROUTER.md).
# `lmloop sandbox build` records image@sha256:… in sandbox_image. A tag is never persisted.
FROM python:3.14-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git ripgrep curl python3-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
