# Egress control for dependency installs

A sandbox has no network at all except during one bounded window in SETUP, when its
dependencies install. Until this, that window had the whole internet. This confines it to
an allow-list — and the interesting part is *what does the confining*, because it is not
the allow-list.

## The network is the boundary; the proxy is the policy

On a normal bridge network a container has a default route and reaches anything it likes,
ignoring `HTTP_PROXY` entirely. **Measured**: a sandbox on `agent-install` today runs
`curl https://example.com` and gets a 200 without the proxy being involved.

So the enforcement is that `agent-egress` is an **internal** network. It has no default
route and no DNS, which closes the DNS-tunnel escape as well — `getent hosts pypi.org`
exits 2. The only thing reachable is the proxy, which is attached to both `agent-egress`
and the default network. The allow-list then decides where the proxy will go on the
sandbox's behalf.

Ship the proxy without the internal network and the allow-list is decorative.

## Turning it on

```bash
make egress-up                      # builds the image, creates the network, starts the proxy
echo 'EGRESS_PROXY_URL=http://agent-egress-proxy:8888' >> .env
```

Unset `EGRESS_PROXY_URL` and nothing changes: the sandbox joins `agent-install` as before,
with unrestricted egress during the install. That is the default because it is what every
existing machine already does, and because a half-configured egress setup degrades runs
silently — an install failure is only a warning.

## Why a second network rather than making `agent-install` internal

Because flipping it is a silent no-op, which is the worst available outcome:

- `_ensure_network` reuses a network that already exists without inspecting it, so an
  existing `agent-install` stays open.
- `networks.create` on a live name is a 409.
- The old network cannot be removed while any run holds an endpoint on it.

Every machine that had ever run autoswe would keep unrestricted egress **while the deny
tests passed** — the proxy really does return 403; it just would not be the only way out.
Two names make "is this host enforcing egress?" answerable by reading one.

For the same reason, a `agent-egress` that exists and is *not* internal is a hard failure
rather than a warning. The error says how to fix it.

## The allow-list

`proxy/allowlist.txt`, one anchored regex per line:

| Host | For |
|---|---|
| `pypi.org`, `files.pythonhosted.org` | pip, uv |
| `registry.npmjs.org` | npm, pnpm |
| `proxy.golang.org`, `sum.golang.org` | Go modules and checksum verification |
| `github.com` | Go modules resolved by VCS path |

Two fences, not one. `FilterDefaultDeny Yes` denies any host not listed, and
`ConnectPort 443` denies any port not listed — **without the second, `github.com` on the
allow-list is an SSH tunnel out of the sandbox.** Measured: `CONNECT github.com:22` through
a config with that line removed returns `200 Connection established` and the peer sends a
real SSH banner. With it, `403 Access violation`.

The two denials have different reason strings, which is worth knowing when reading a log:

- a disallowed host: `403 Filtered`, log line `Proxying refused on filtered domain "..."`
- a disallowed port on an allowed host: `403 Access violation`, no `filtered domain` line

### What is deliberately not on it

`raw.githubusercontent.com` and `codeload.github.com`. A release-asset or raw fetch during
an install will be refused, and the refusal will look like a proxy bug. Add them
deliberately if a repository needs them rather than discovering the gap in a run log.

## The healthcheck probes the filter, not the port

A tinyproxy whose allow-list fails to load **runs wide open** and answers a TCP check
perfectly happily — measured, with the `Filter*` directives stripped. So the healthcheck
asks for a host that must be refused and expects the refusal, and it was verified to fail
against exactly that defect.

## Known limitations

**Concurrent runs share the network.** Two sandboxes on one `agent-egress` can reach each
other — measured: container B reads a file served by container A's workspace. Disabling
inter-container communication is not a fix, because it also cuts the path to the proxy; the
fix is a network per run, which is not done here.

**`no-new-privileges` is absent on the proxy.** On hosts whose AppArmor profile needs the
setuid transition it stops tinyproxy from execing at all — the same quirk the sandbox
documents and falls back from. Non-root, all capabilities dropped and a read-only rootfs
are all present.
