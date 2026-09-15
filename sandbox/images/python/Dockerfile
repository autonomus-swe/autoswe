# Pinned, not `python:3.12-slim`. That tag followed Debian from 12 to 13, which moved
# glibc from 2.36 to 2.41 under an image we had already built and tested against.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep build-essential curl ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*

# uv comes from Astral's image, not `pip install uv`. The wheel ships a glibc-linked
# binary, and on the newer base every single invocation — `uv --version` included —
# died with SIGSEGV, so the agent could not install a repository's dependencies and
# every run failed in TEST with "no test report produced (exit 139)". The official
# binary is musl and statically linked, so the base image's glibc cannot break it again.
# Pinned deliberately: an unpinned uv is how this arrived.
COPY --from=ghcr.io/astral-sh/uv:0.12.14 /uv /uvx /usr/local/bin/

RUN pip install --no-cache-dir pytest pytest-json-report pytest-timeout
RUN useradd -u 1000 -m agent
# The container is started with an explicit --user (the worker's uid:gid); HOME lives on
# the /tmp tmpfs so any uid can write it.
ENV HOME=/tmp/home UV_CACHE_DIR=/tmp/uv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PIP_NO_CACHE_DIR=1
USER agent
WORKDIR /workspace
CMD ["sleep", "infinity"]
