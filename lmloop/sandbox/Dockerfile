# Reference sandbox image for lmloop (see docs/GUIDE_DOCKER_AND_OPENROUTER.md).
# Pin the base digest in production; `lmloop sandbox build` will record @sha256:… in config.
FROM python:3.14-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git ripgrep curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
