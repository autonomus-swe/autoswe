FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep build-essential curl ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv pytest pytest-json-report pytest-timeout
RUN useradd -u 1000 -m agent
# The container is started with an explicit --user (the worker's uid:gid); HOME lives on
# the /tmp tmpfs so any uid can write it.
ENV HOME=/tmp/home UV_CACHE_DIR=/tmp/uv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PIP_NO_CACHE_DIR=1
USER agent
WORKDIR /workspace
CMD ["sleep", "infinity"]
