# The sandbox's egress proxy (PHASE-5 step 5.9).
#
# Pinned to the patch release, not `alpine:3` or `tinyproxy` unversioned. This image is
# the enforcement point for what the agent can reach; an unpinned base is a config change
# nobody reviewed arriving by `docker build`.
FROM alpine:3.21.3

# tinyproxy rather than squid: squid's smallest useful image is ~60 MB against this one's
# ~10 MB, and its ACL language is large enough that "which hosts can this sandbox reach"
# stops being answerable by reading one file. Everything step 5.9 asks for — CONNECT,
# a host allow-list, a 403 on everything else — is four directives here.
RUN apk add --no-cache tinyproxy=1.11.2-r0

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY allowlist.txt /etc/tinyproxy/allowlist.txt

# World-readable, because the container is started with an explicit --user by the
# orchestrator (same convention as sandbox/images/python/Dockerfile) and that uid may not
# be the `tinyproxy` user baked in here. A config the process cannot read is a proxy that
# does not start; a config the process cannot *write* is the point.
RUN chmod 0444 /etc/tinyproxy/tinyproxy.conf /etc/tinyproxy/allowlist.txt

# Proves the *filter* is loaded, not merely that the port is open.
#
# The failure this is here to catch: if the allow-list file is missing or unreadable,
# tinyproxy logs and carries on with no filter at all — a wide-open proxy that passes any
# TCP or "is the port listening" check. So the probe asks for a host that MUST be denied
# and demands `403 Filtered` back. The two outcomes are distinguishable without network:
# filter loaded -> 403 Filtered; filter absent -> tinyproxy tries to resolve the name and
# answers 504, because `.invalid` is reserved by RFC 6761 and never resolves. A probe
# against a real host would instead make container health depend on that host being up.
#
# Env-var form rather than wget's `-e http_proxy=...`: `-e` is GNU wget. This image has
# busybox wget, which rejects it outright ("wget: unrecognized option: e") and therefore
# never contacts the proxy at all — the healthcheck fails for a reason that has nothing
# to do with the proxy's health.
HEALTHCHECK --interval=10s --timeout=5s --start-period=2s --retries=3 \
  CMD http_proxy=http://127.0.0.1:8888 \
      wget -q -S -T 3 -O /dev/null http://health-probe.invalid/ 2>&1 | grep -q '403 Filtered'

USER tinyproxy
EXPOSE 8888

# -d keeps it in the foreground as PID 1 so docker sees the real process state and stdout
# is the log. Without it tinyproxy daemonises, the container exits immediately, and the
# orchestrator's start check reports a proxy that "exited: exit code 0".
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
