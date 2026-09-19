# Pinned to the patch, not `golang:1.23-bookworm`. Same failure mode the python image
# documents — a floating tag moved that base from Debian 12 to 13 under an image we had
# already tested — except a compiler tag floats twice: `golang:1.23` also rolls the Go
# patch release, so codegen and vet behaviour can change under a green build.
# -bookworm, not -alpine: the agent builds repositories that link cgo against glibc.
FROM golang:1.23.12-bookworm

# git and ripgrep are what the agent's git and search tools shell out to; coreutils is
# here because the sandbox wraps every command in `timeout` (see DockerSandbox._exec_inner).
# The golang base already carries gcc/g++/make/pkg-config, so cgo builds work without
# build-essential.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*

# gotestsum produces the JUnit XML the run's TEST gate parses.
#
# GOBIN is forced onto the rootfs. Left alone, `go install` drops the binary in
# $GOPATH/bin, and GOPATH at *runtime* is /tmp/go on a fresh tmpfs — the tool would be
# built into the image and then be missing from every container that started from it.
#
# v1.12.3, not the newest: gotestsum v1.13.0 declares `go 1.24.0` in its go.mod, which
# GOTOOLCHAIN=local (below, and the base image's own default) correctly refuses to build
# with a 1.23 toolchain. Bumping Go and bumping gotestsum are the same decision.
#
# The -X is not cosmetic. gotestsum carries `var version = "dev"` and only its release
# pipeline stamps the real one, so a plain `go install ...@v1.12.3` produces a binary that
# answers `gotestsum --version` with "dev" — the pin becomes unverifiable from inside the
# sandbox, which is exactly where someone debugging a malformed JUnit report will look.
# (`go version -m $(command -v gotestsum)` knows the truth either way, but nobody reaches
# for that when --version already answered.)
#
# /root/go is the build's own GOPATH — module cache plus build cache, ~250MB of it — and
# nothing at runtime can read it anyway, since HOME is /tmp/home and the container runs as
# an arbitrary uid that cannot enter /root.
ARG GOTESTSUM_VERSION=v1.12.3
RUN GOBIN=/usr/local/bin go install \
      -ldflags "-X gotest.tools/gotestsum/cmd.version=${GOTESTSUM_VERSION}" \
      "gotest.tools/gotestsum@${GOTESTSUM_VERSION}" \
    && rm -rf /root/go /root/.cache

# Every path the go command writes to lives on the /tmp tmpfs, because the rootfs is
# mounted read-only and the container runs under an explicit --user that is not
# necessarily the uid baked in below. GOPATH alone is not enough: GOCACHE defaults to
# $HOME/.cache/go-build, not under GOPATH, so a read-only $HOME breaks `go build` with
# "failed to initialize build cache ... read-only file system" even though GOPATH is fine.
# GOMODCACHE is stated rather than inherited so that moving GOPATH later cannot silently
# strand a populated module cache.
#
# Consequence to know: /tmp is a fresh tmpfs per container, so the module cache starts
# empty and `go mod download` must happen while the install network is still connected.
# By the time an agent runs, there is no network.
#
# The other consequence is a cold GOCACHE per container, and since Go 1.20 the toolchain
# ships no prebuilt std — $GOROOT/pkg holds only `include` and `tool`. So the first
# compile in a container pays for building std from source: measured here under the
# worker's own limits (--cpus 2, read-only rootfs, tmpfs /tmp), 13.6s for the first
# `go build`, another 10.6s for the first `go test`, 30.4s for the first `go test -race`.
# Warm, the same test is 0.9s. This is a one-time cost per container, not per command.
#
# Deliberately NOT pre-seeded from a cache baked into the image. A seed is keyed by build
# flags, so the ~100MB that covers a plain build buys nothing for a `-race` run — which
# needs its own separately-instrumented std — while permanently spending that much of the
# 1g tmpfs, and tmpfs pages count against the container's 4g memory limit. Paying 14s once
# is the cheaper side of that trade.
ENV HOME=/tmp/home \
    GOPATH=/tmp/go \
    GOMODCACHE=/tmp/go/pkg/mod \
    GOCACHE=/tmp/go/cache \
    GOTMPDIR=/tmp

# `go install`ed helpers land in $GOPATH/bin, which is on the tmpfs and therefore not on
# the base image's PATH. Without this a tool the agent installs during the network phase
# is invisible to it afterwards.
ENV PATH=/tmp/go/bin:/usr/local/go/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ...and the ENV above is not sufficient on its own. The sandbox runs every command as
# `timeout -k 5 N bash -lc '<cmd>'`, and `bash -l` sources /etc/profile, which on Debian
# *unconditionally assigns* PATH rather than appending to it:
#     PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games"
# That discards /usr/local/go/bin, so `go version` inside the sandbox died with
# "bash: go: command not found" on an image where `docker run ... go version` was fine.
# The python image never met this because /usr/local/bin/python survives the reassignment;
# Go lives outside it and does not. /etc/profile re-sources /etc/profile.d/*.sh *after*
# clobbering PATH, so this is the one hook that wins.
#
# $PATH is written unescaped on purpose. Dockerfile variable substitution does not apply
# to the shell-form of RUN — the string goes to /bin/sh verbatim — and the single quotes
# stop the *build* shell expanding it too, so the literal three characters reach the file.
# Writing `\$PATH` here (the reflex, and what was tried first) leaves the backslash in the
# file; inside the double-quoted assignment that becomes a literal `$`, and every login
# shell ends up with a PATH whose last entry is the four characters "$PATH". The symptom
# is identical to having no hook at all, so verify by reading the file, not the build log.
RUN printf '%s\n' \
      '# The sandbox execs `bash -lc`; /etc/profile resets PATH. Put Go back.' \
      'PATH="/tmp/go/bin:/usr/local/go/bin:$PATH"' \
      'export PATH' \
    > /etc/profile.d/10-golang.sh && chmod 0644 /etc/profile.d/10-golang.sh

# Explicit, though the base image's go.env already defaults it: an environment variable
# outranks `go env -w`, so a repository's checked-in GOENV cannot flip this back. With no
# network, GOTOOLCHAIN=auto turns "go.mod asks for a newer Go" into a toolchain *download*
# that hangs and then fails opaquely; local turns it into an immediate, readable
# "go: go.mod requires go >= 1.25.0 (running go 1.23.12; GOTOOLCHAIN=local)".
ENV GOTOOLCHAIN=local

# The workspace is a bind mount owned by the host uid that created the worktree, which is
# not guaranteed to match the --user the container runs as. git refuses to operate in a
# repository it does not own ("detected dubious ownership"), and that surfaces in two
# places: the agent's git tool, and `go build`/`go test`, which shell out to git to stamp
# VCS metadata into the binary and hard-fail the *build* when that git call fails —
# "error obtaining VCS status: exit status 128", with the compiler blaming VCS for what is
# really a uid mismatch. Verified by running this image against a bind-mounted repo owned
# by uid 4242 as uid 1234: with the line, `go build` succeeds and stamps vcs.revision;
# with GIT_CONFIG_NOSYSTEM=1 to suppress it, both git and the build fail as above.
# A system-level safe.directory is the fix rather than -buildvcs=false, because the latter
# would only rescue the compiler and leave the git tool broken. Scoped to the sandbox:
# this container is single-tenant, networkless, and holds exactly one checkout.
RUN git config --system --add safe.directory '*'

RUN useradd -u 1000 -m agent
USER agent
WORKDIR /workspace
CMD ["sleep", "infinity"]
