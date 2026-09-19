# Pinned to the patch and to the Debian codename, not `node:20-slim`. That is the same trap
# the python image documents: `python:3.12-slim` followed Debian 12 -> 13 and moved glibc
# under an image we had already tested. `node:20-slim` floats both halves — the Debian
# release and the Node minor, and a Node minor is a V8 bump.
FROM node:20.20.2-bookworm-slim

# git and ripgrep are what the agent's git and search tools shell out to; coreutils is here
# because the sandbox wraps every command in `timeout` (see DockerSandbox._exec_inner).
#
# build-essential and python3 are node-gyp's toolchain. The node base carries neither, so
# without them any dependency with a native addon (bcrypt, sqlite3, sharp's fallback build,
# anything that misses a prebuilt for this platform) fails during the one phase that has a
# network, and the failure text is a node-gyp stack trace about a missing `python3` rather
# than anything a reader would connect to the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep ca-certificates coreutils build-essential python3 \
    && rm -rf /var/lib/apt/lists/*

# pnpm is a pinned global install, not `corepack enable`. Corepack's shims resolve the
# version named in the repository's `packageManager` field *at invoke time* and download it;
# by the time an agent runs a command there is no network, so every shimmed `pnpm` would be
# a download attempt against nothing. A real binary on PATH cannot fail that way.
#
# 10.34.5 rather than the newest pnpm: pnpm 11 raised `engines.node` to >=22.13, which is
# past this image's Node. 12.4.2 declares >=18.* and does run here, but that range reads as
# stale metadata next to the 11.x it supersedes, and betting the image on it buys nothing —
# 10.34.5 and 12.4.2 both write `lockfileVersion: '9.0'`, verified side by side, so the pin
# costs no lockfile compatibility with repositories resolved by a newer pnpm.
#
# --prefix is stated because npm_config_prefix below moves global installs to the tmpfs;
# this one has to land on the rootfs or it would vanish with the build container.
RUN npm install -g --prefix /usr/local pnpm@10.34.5 \
    && npm cache clean --force \
    && rm -rf /root/.npm

# The workspace is a bind mount owned by whichever host uid created the worktree, which is
# not guaranteed to match the --user the container runs as. git refuses to operate in a
# repository it does not own ("detected dubious ownership"), which would break the agent's
# git tool outright. Scoped to the sandbox: this container is single-tenant and holds
# exactly one checkout.
RUN git config --system --add safe.directory '*'

# Debian's /etc/profile *overwrites* PATH unconditionally, and the sandbox runs every
# command through `bash -lc` (a login shell). So `ENV PATH=...` below is wiped before the
# agent's command ever runs — confirmed by passing a PATH that came back as the stock
# Debian one. /etc/profile sources /etc/profile.d/*.sh after that assignment, so this is the
# only place a PATH addition survives a login shell. Without it, anything the agent installs
# globally during the network phase is on disk and invisible.
RUN printf '%s\n' \
      'PATH="/tmp/npm-global/bin:/tmp/pnpm-home:$PATH"' \
      'export PATH' \
      > /etc/profile.d/10-agent-tmpfs-path.sh \
    && chmod 0644 /etc/profile.d/10-agent-tmpfs-path.sh

# Every path a node toolchain writes to has to be under /tmp: the rootfs is read-only and
# the container runs under an explicit --user that is not the uid baked in below, so
# "writable by root" and "writable by uid 1000" are both the wrong answer — only the tmpfs
# is writable by an arbitrary uid.
#
#   npm_config_cache   npm's package cache *and* its crash logs (`_logs/`) hang off this one
#                      key. It defaults to $HOME/.npm, which is only safe by accident —
#                      because the worker happens to export HOME=/tmp/home. Without HOME it
#                      resolves to /.npm on the read-only rootfs, and the first command that
#                      actually fetches a package dies with
#                      `ENOENT ... mkdir '/.npm'` — note ENOENT, not EROFS, so the message
#                      does not say "read-only" anywhere — followed by "Log files were not
#                      written due to an error writing to the directory: /.npm/_logs". The
#                      diagnostic for the failure is destroyed by the same cause as the
#                      failure. Stating the key removes the dependency on HOME.
#   npm_config_prefix  `npm install -g` targets /usr/local by default, which is read-only at
#                      runtime, so a global install fails even during the network phase.
#                      Paired with the profile.d drop-in above, which is what puts the
#                      resulting bin dir on PATH.
#   store_dir          pnpm's store defaults under $HOME, so like the npm cache it is only
#                      writable here by way of the worker's HOME. Stated for the same
#                      reason: a command that loses HOME should still land on the tmpfs.
#   package_import_method=copy
#                      the store is on the tmpfs and the project is on the bind mount, so
#                      pnpm's default `auto` — clone, then hardlink, then copy — can only
#                      ever end at copy, and would end somewhere else if the store ever
#                      moved onto the same filesystem as the project. This is for
#                      determinism, not speed: measured on a two-package install, `copy`
#                      2.34s vs `auto` 2.08s, and neither emitted a fallback warning. Same
#                      reason the python image sets UV_LINK_MODE=copy.
#   manage_package_manager_versions=false
#                      pnpm >= 9.7 reads `packageManager` from package.json and fetches that
#                      exact pnpm before doing anything else. Offline it does not fall back
#                      to the pnpm that is installed — it fails, on every command including
#                      `pnpm test`, with "Command failed with exit code 1: pnpm add
#                      pnpm@<pinned>". Verified: same repository, same container, the only
#                      difference being this key.
#   audit/fund/update_notifier
#                      registry round-trips npm makes around an install that have nothing to
#                      do with resolving the dependency tree. Two costs, both measured in
#                      this image rather than assumed: once the container is disconnected,
#                      `npm install` on a two-package project goes from ~0.95s to ~1.3s
#                      (three runs each) — small, because a container with no interfaces
#                      fails DNS immediately rather than waiting out a connect; and while
#                      the network is still up every install ends with a five-line
#                      `npm notice New major version of npm available! 10.8.2 -> 12.0.2`
#                      block, which is advice an agent cannot act on — a global npm upgrade
#                      lands on the tmpfs prefix below and is gone with the container — in
#                      output an agent nevertheless has to read and reason about.
#   COREPACK_HOME      corepack is still in the base image and a repository script may call
#                      it; its default home is under $HOME/.cache and its writes are the
#                      only part of it we can make work here. It remains network-bound, so
#                      it is only usable while the install network is up.
#
# Consequence to know: /tmp is a fresh tmpfs per container, so the npm cache and the pnpm
# store start empty every run and share the 1g tmpfs (which also counts against the
# container's memory limit) with node_modules itself. Dependencies must be installed while
# the install network is still connected.
ENV HOME=/tmp/home \
    npm_config_cache=/tmp/npm-cache \
    npm_config_prefix=/tmp/npm-global \
    npm_config_store_dir=/tmp/pnpm-store \
    npm_config_package_import_method=copy \
    npm_config_manage_package_manager_versions=false \
    npm_config_audit=false \
    npm_config_fund=false \
    npm_config_update_notifier=false \
    COREPACK_HOME=/tmp/corepack \
    PNPM_HOME=/tmp/pnpm-home \
    PATH=/tmp/npm-global/bin:/tmp/pnpm-home:$PATH

# The base image already ships `node` as uid 1000, so there is no useradd to do. This line
# only decides what happens if someone runs the image without --user, and the answer should
# not be root.
USER node
WORKDIR /workspace
CMD ["sleep", "infinity"]
