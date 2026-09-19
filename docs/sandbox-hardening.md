# Sandbox hardening

What the sandbox is by default, what gVisor adds, and what the collector reaps.

## The default boundary

Every sandbox container runs non-root under an explicit uid, with a read-only rootfs, a
`/tmp` tmpfs, every capability dropped, pid/CPU/memory limits, and no network outside the
install window. Those are namespace-and-cgroup boundaries around a **shared host kernel**:
a kernel privilege escalation in agent-generated code is a host compromise.

## Optional: gVisor (`runsc`)

gVisor replaces the shared kernel with a user-space one. The container's syscalls are
serviced by `runsc` rather than by Linux, so the host kernel's attack surface shrinks to
what gVisor forwards.

**The code side is already done.** `SANDBOX_RUNTIME=runsc` reaches `containers.run`
through `core/settings.py` → `orchestrator/deps.py` → `DockerSandbox`. There is nothing to
write; there is a host to prepare.

### First check whether your Docker is the snap

```bash
snap list docker
```

If that prints a row, **gVisor is not available to you** and no amount of configuration
will change it. The snap daemon is confined: it reads
`/var/snap/docker/current/config/daemon.json` rather than `/etc/docker/daemon.json`, it
ships only `runc` in a read-only `/snap/docker/current/bin/`, and a binary installed to
`/usr/local/bin` is outside its mount namespace. Install `docker-ce` first.

*(This is not hypothetical: the machine this was developed on runs snap Docker, which is
why the runtime could be verified as reaching the daemon but not as working.)*

### Installing it

```bash
# https://gvisor.dev/docs/user_guide/install/ — check the current instructions
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
sudo apt-get update && sudo apt-get install -y runsc
sudo runsc install          # writes the runtime into /etc/docker/daemon.json
sudo systemctl restart docker
```

Verify the daemon can see it, then turn it on:

```bash
docker info --format '{{json .Runtimes}}' | grep -q runsc && echo ok
echo 'SANDBOX_RUNTIME=runsc' >> .env
```

Requirements: x86-64 or ARM64, Linux 4.14.77+. KVM is **not** required — the default
`systrap` platform uses no virtualization.

### If the runtime is not registered

Docker answers `unknown or invalid runtime name`, which `DockerSandbox._start_sync` turns
into a `SandboxError` and the run fails in SETUP.

That is deliberate, and unlike the `no-new-privileges` fallback beside it. Silently falling
back to `runc` would mean an operator who asked for kernel isolation, and believes they
have it, quietly does not — which is worse than a run that fails loudly on the first
attempt after a misconfiguration.

## Reaping

`teardown` removes a worktree only when the run pushed: a failed run's tree is the evidence
somebody needs to read. Nothing removed it afterwards, so it accumulated. **Measured on a
development machine after Phase 5: 42 worktrees (332 MB), 55 bare clones, and 26 exited
containers still labelled `autoswe.run_id`.**

An arq cron job now sweeps every ten minutes.

| What | Kept for | Setting |
|---|---|---|
| Sandbox containers of finished runs | 1 h | `SANDBOX_TTL_S` |
| Worktrees of finished runs | 1 h | `WORKTREE_TTL_S` |
| Worktrees with no run at all | 24 h | `ORPHAN_GRACE_S` |
| Bare clones nothing has fetched into | 14 days | `BARE_CLONE_TTL_DAYS` |

`GC_ENABLED=false` turns it off without a redeploy.

### The safety condition

A collector that deletes a **running** run's worktree destroys live work, and would do it
rarely enough to be very hard to reproduce. So:

> A run is reapable only when its status is terminal **and** `finished_at` is set **and**
> `finished_at` is older than the TTL.

The middle clause is the one that matters. `set_run_phase` writes `status="done"` or
`"failed"` the moment the phase machine transitions, while the run is still inside its loop
holding that directory; only `finish_run` sets `finished_at`, and it is the last thing a
run does. A collector keyed on status alone deletes live work — there is a test that does
exactly that when the guard is removed.

Everything ambiguous is left alone: a directory whose run cannot be found gets the much
longer orphan grace, a probe that raises is a skip rather than a delete, and a container
that is still *running* is skipped whatever its row says — a row can be wrong about a
process, and a process cannot be wrong about itself.

Every skip is counted and logged with its reason, because a collector that quietly reclaims
nothing looks exactly like one that had nothing to reclaim.
