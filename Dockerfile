# syntax=docker/dockerfile:1
# SafeSink ships as a CLI that starts other servers. The runtime image therefore
# carries Node as well: most MCP servers are launched with npx.
FROM python:3.12-slim AS build
WORKDIR /src
COPY . .
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ARG NODE_MAJOR=22
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
# Verify as an unprivileged user, and give MCP servers a writable sandbox that is
# not the current directory.
RUN useradd --create-home --uid 10001 safesink \
 && mkdir -p /work/sandbox /out \
 && chown -R safesink:safesink /work /out
USER safesink
WORKDIR /work
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
ENTRYPOINT ["safesink"]
CMD ["--help"]
