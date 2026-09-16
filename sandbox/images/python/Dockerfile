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

# The two scanners that run where the code is. They are here rather than installed per run
# because the sandbox has no network by the time any agent touches it.
RUN pip install --no-cache-dir bandit==1.8.6 semgrep==1.177.0

# semgrep's rules, vendored and pinned. `--config auto` and `p/python` fetch them over the
# network, which the sandbox does not have — so a run would silently scan against nothing.
#
# Pinned by commit, not by tag: semgrep-rules publishes no tags at all, so a `--branch`
# pin fails the build outright. Fetched by sha so a rule pack cannot change under an image
# that was already built and tested; refresh by bumping this and rebuilding.
# Narrowed to the security rules, because semgrep's cost here is almost entirely rule
# *parsing*, not scanning: measured inside this sandbox's own limits (2 CPUs, read-only
# rootfs), the full 593-file set took 96.6s on a two-file repository and 98.6s on an
# empty one. Rule count is the whole bill; 593 -> 337 files took a 181-file repository
# from 104.1s to 58.4s.
#
# Two categories go, for two different reasons:
#   - correctness / best-practice / maintainability / performance / compatibility /
#     caching: the Reviewer reads for these. A security scanner reporting
#     `useless-inner-function` spends a gate on lint.
#   - generic/secrets/gitleaks: semgrep's port of gitleaks' own rules, and we run the
#     real gitleaks on the worker over the run's commits with --redact.
# `generic/secrets/security` — semgrep's own secret rules — STAYS. gitleaks reads
# commits, so it structurally cannot see a secret in an untracked file, and
# `repo.diff.full_diff` deliberately includes untracked files. Dropping both sets left
# that case covered by nothing.
#
# A denylist rather than an allowlist of "*security*": paths like
# `generic/secrets/gitleaks` and `python/pyramid/audit` hold security rules without the
# word in them, so an allowlist silently drops real coverage, and a rule category added
# upstream later would be dropped sight-unseen. This way an unknown category is kept.
ARG SEMGREP_RULES_SHA=40b8c63f75dc7c22c8a77482d73bfb864b146f7e
RUN git init -q /opt/semgrep-rules \
    && git -C /opt/semgrep-rules remote add origin https://github.com/semgrep/semgrep-rules \
    && git -C /opt/semgrep-rules fetch -q --depth 1 origin "$SEMGREP_RULES_SHA" \
    && git -C /opt/semgrep-rules checkout -q FETCH_HEAD \
    && rm -rf /opt/semgrep-rules/.git \
    && find /opt/semgrep-rules -mindepth 1 -maxdepth 1 \
         ! -name python ! -name generic -exec rm -rf {} + \
    && find /opt/semgrep-rules -name '*.yaml' \
         \( -path '*/correctness/*' -o -path '*/best-practice/*' \
            -o -path '*/maintainability/*' -o -path '*/performance/*' \
            -o -path '*/compatibility/*' -o -path '*/caching/*' \) -delete \
    && rm -rf /opt/semgrep-rules/generic/secrets/gitleaks \
    && find /opt/semgrep-rules -type d -empty -delete \
    && chmod -R a+rX /opt/semgrep-rules
RUN useradd -u 1000 -m agent
# The container is started with an explicit --user (the worker's uid:gid); HOME lives on
# the /tmp tmpfs so any uid can write it.
ENV HOME=/tmp/home UV_CACHE_DIR=/tmp/uv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PIP_NO_CACHE_DIR=1
# semgrep phones semgrep.dev for a version check that `--metrics=off` does not cover, and
# by the time it runs the sandbox has no network — so it waits out the connect timeout on
# every scan. Measured in this image, networkless, on a two-file repository: 2m36s with the
# check, 1m01s without. That is ~95s of dead waiting inside a 300s scan timeout.
ENV SEMGREP_ENABLE_VERSION_CHECK=0
USER agent
WORKDIR /workspace
CMD ["sleep", "infinity"]
