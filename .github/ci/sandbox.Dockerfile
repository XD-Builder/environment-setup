# CI-only image. The reference sandbox stays lmloop/sandbox/Dockerfile
# (python, git, ripgrep, curl). This layer adds the REPL extras the agentic
# suite imports. It does not turn on `lmloop --docker`.
FROM lmloop-sandbox:ci

COPY lmloop/requirements.txt /tmp/lmloop-requirements.txt
RUN pip install --no-cache-dir -r /tmp/lmloop-requirements.txt
