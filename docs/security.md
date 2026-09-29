# Security: every claim, and the test that proves it

An agent that edits a repository and runs commands is a program executing text it did not
write, some of which was written to make it misbehave. This document lists what this
system claims about that, and points at the test for each claim.

**A guarantee with no test beside it is a hope.** The last section is the list of things
this build does *not* guarantee, which is the part worth reading twice.

---

## 1. The sandbox

| Claim | Proved by |
|---|---|
| Runs as a non-root user and cannot write its own filesystem | `tests/unit/test_sandbox_hardening.py::test_the_container_is_not_root_and_cannot_write_its_own_filesystem` |
| Every Linux capability dropped; process count capped | `…::test_every_capability_is_dropped_and_processes_are_capped` |
| `no-new-privileges` on by default | `…::test_new_privileges_are_refused_by_default` |
| The only writable place is a bounded tmpfs | `…::test_the_only_writable_place_is_a_bounded_tmpfs` |
| Memory and CPU ceilings are the ones configured | `…::test_the_memory_and_cpu_ceilings_are_the_ones_asked_for` |
| gVisor is used when configured | `…::test_the_gvisor_runtime_reaches_docker` |
| Containers are labelled so the collector can find them | `…::test_the_run_is_labelled_with_its_own_id_so_the_collector_can_find_it` |
| A real container behaves as the unit tests describe | `tests/integration/test_sandbox.py` |

**git is deliberately unavailable inside the container.** Repository operations are
host-side tools the agent has to call, which is what makes the push path auditable at all.

---

## 2. Network

| Claim | Proved by |
|---|---|
| An internal network has no route out | `tests/integration/test_egress.py::test_an_internal_network_has_no_way_out_at_all` |
| The egress proxy allows only allow-listed hosts | `…::test_an_allow_listed_host_is_reachable_and_others_are_not` |
| A pre-existing open network is refused rather than reused | `…::test_an_existing_open_network_is_refused_rather_than_reused` |

The container reaches the network only while dependencies install, and is disconnected
before the agent sees the repository.

---

## 3. Commands

| Claim | Proved by |
|---|---|
| Forbidden commands are refused, not merely gated | `tests/unit/test_policy.py::test_denied_commands`, `tests/integration/test_injection.py::test_the_forbidden_commands_really_are_forbidden` |
| Ordinary commands are not refused | `tests/unit/test_policy.py::test_allowed_commands` |
| Commands that change the environment stop for a human | `tests/integration/test_injection.py::test_the_gated_commands_are_gated_rather_than_forbidden` |
| `DENY` is checked before `ASK`, so a forbidden command is refused rather than offered for approval | `tests/integration/test_approvals.py::test_a_forbidden_command_is_not_offered_for_approval` |

---

## 4. Paths

| Claim | Proved by |
|---|---|
| `..`, absolute paths and symlinks out of the worktree are refused | `tests/unit/test_policy.py::test_confine_rejects_escapes_and_internal_dirs`, `::test_confine_rejects_symlink_out_of_root` |
| The MCP file tools inherit that containment rather than re-implementing it | `tests/unit/test_mcp_workspace.py::test_refuses_to_read_outside_the_checkout` |

`tools/policy.confine` is the single implementation. Both the agents' `read_file` and the
MCP server's go through it, which is the point of routing the latter through the former.

---

## 5. Prompt injection

| Claim | Proved by |
|---|---|
| A repository written to subvert the agent cannot make the harness run anything | `tests/integration/test_injection.py::test_a_poisoned_repository_cannot_make_the_harness_run_anything` |
| Repository content reaches the model inside an untrusted-content fence | `agents/prompts/_fences.md`, applied by `agents.base.fence` |

The model may be persuaded. The harness is what must not be — the gate is in the tool
layer, not in the prompt.

---

## 6. Approvals

| Claim | Proved by |
|---|---|
| An approval releases exactly the call it names | `tests/integration/test_approvals.py::test_approving_lets_the_call_through` |
| A decision for a different call is not consumed | `…::test_a_decision_for_another_call_is_not_consumed` |
| A rejection's reason reaches the model as the tool result | `…::test_rejecting_returns_the_reason_to_the_model` |
| An unattended run refuses rather than parking forever | `…::test_an_unattended_run_refuses_immediately_without_parking` |
| The same guard applies over MCP | `tests/integration/test_mcp_server.py::test_an_approval_for_a_different_call_is_refused` |
| A mounted external MCP tool marked mutating pauses the run | `tests/integration/test_mcp_client.py::test_a_mutating_mounted_tool_pauses_the_run_for_approval` |

`ControlPlane._pending` is why an approval cannot be replayed later against a call the
human never saw. It lives in the service rather than in a transport so that every route to
approving goes through it — which is the reason `api/service.py` exists.

---

## 7. Secrets

| Claim | Proved by |
|---|---|
| The sandbox never receives API keys or database credentials | `sandbox/docker.py` passes an explicit env; `tests/unit/test_sandbox_hardening.py` |
| A mounted MCP server gets only the variables named for it | `tests/integration/test_mcp_client.py::test_the_child_gets_only_the_environment_it_was_given` |
| Secrets are redacted from structured logs | `tests/unit/test_logging.py` |
| A committed secret stops the push before it happens | `tests/unit/test_pr_node.py::test_a_committed_secret_stops_the_push_before_it_happens` |
| Committed secrets are caught in CI | `gitleaks` in `.pre-commit-config.yaml` |
| An unset `${VAR}` in a config file is an error, not an empty credential | `tests/unit/test_mcp_config.py::test_an_unset_variable_is_an_error_not_an_empty_string` |

---

## 8. The API

| Claim | Proved by |
|---|---|
| Every route requires a key | `tests/integration/test_api.py::test_create_run_requires_a_valid_key` |
| Keys are compared in constant time against every configured key | `api/auth._matches` |
| The MCP transport is guarded too, at the ASGI layer | `tests/integration/test_mcp_server.py::test_the_http_transport_refuses_an_unknown_key` |
| Writes and reads have separate rate-limit buckets | `tests/integration/test_api.py::test_reads_do_not_spend_the_write_budget` |
| The write bucket actually limits | `…::test_rate_limit_after_the_burst` |
| Only `https://github.com/owner/name` repositories are accepted | `tests/integration/test_api.py::test_invalid_bodies_are_rejected` |

**The event stream accepts its key as a query parameter.** Browsers cannot set headers on
an `EventSource`, so there is no other way to authenticate one. Query strings end up in
access logs and browser history; this is deliberately limited to the read-only event
stream and every other route requires the header (`api/auth.require_api_key_or_query`).

---

## 9. Budgets

| Claim | Proved by |
|---|---|
| Spend is enforced from the usage ledger, not from an estimate | `tests/unit/test_budget.py` |
| A run out of budget lands its work rather than starting more | `tests/unit/test_budget_gate.py` |
| Waiting for a human does not consume the wall-clock budget | `tests/integration/test_approvals.py::test_waiting_time_is_reported_so_it_leaves_the_budget` |

---

## 10. What this build does **not** guarantee

- **The sandbox is Docker, not a VM.** With gVisor configured it is meaningfully stronger;
  without it, a container escape is a container escape. Do not run this against untrusted
  repositories on a host you care about.

  Production is settings rather than a compose profile — a profile can add services but
  cannot give the *same* ones stricter settings without a second copy of `api` and
  `worker`, and two copies is how the one nobody looks at drifts. So:

  | Setting | Why |
  |---|---|
  | `SANDBOX_RUNTIME=runsc` | gVisor; the single biggest difference between the development and production threat models |
  | `EGRESS_ENFORCED=true` | the sandbox joins an internal network and leaves only through the allow-listed proxy |
  | `KEEP_FAILED_SANDBOX_TTL` short | a kept container is a debugging aid in development and a running process holding somebody's repository in production |
  | TLS at a reverse proxy | not in compose on purpose: a self-signed certificate baked into a compose file is the kind of thing that ends up in production because it worked |

  **Rotate `API_KEYS` by adding the new key, deploying, then removing the old one.**
  Replacing it in one step is an outage for every caller that has not been told.
- **Prompt injection is mitigated, not solved.** The harness is what is defended. A model
  can still be persuaded to write a bad patch — that is what review, the security scan and
  the pull request are for, and none of them is a proof.
- **A mounted external MCP server is somebody else's code.** It gets only the environment
  you name, and the allow-list bounds which of its tools are reachable, but within that it
  does what it does. `mutating` is declared by you rather than by it for exactly this
  reason.
- **A scanner that cannot run does not stop the run.** `bandit`, `semgrep` and `gitleaks`
  each produce a `scan-failed` finding when they fail to execute, at `info` severity — and
  `info` cannot gate, so the run proceeds having checked less than it appears to have.

  That is deliberate and it is the right trade: refusing every push on a broken scanner
  makes a tooling outage into an outage of the whole system, and a scanner that found
  nothing because it never started has still found nothing. But it means **a clean security
  report is only as strong as the scanners that actually ran**, and the report says which
  those were rather than leaving you to assume.

  Not hypothetical. The `semgrep` vendored into this machine's Python sandbox image had
  corrupt bytecode — `ValueError: bad marshal data` on import, from a `pip install` that
  fetched a damaged wheel. Every SECURITY phase on that image ran `bandit` and `gitleaks`
  and silently contributed nothing from `semgrep`, which is the largest of the three rule
  sets. `tests/integration/test_scanners.py` is what caught it, by asserting the scanner
  runs rather than that the scan returned.

  **So read the `by_severity` block on a security report, and treat `info` findings named
  `scan-failed` as a failed build rather than as a clean one.** Pinned by
  `tests/unit/test_scanner_parsers.py` (the failure is a finding, not silence) and
  `tests/unit/test_pr_node.py` (it cannot gate, and it does not erase a secret already
  found).
- **The eval judge is not independent** when it runs on the worker's own model.
- **`API_KEYS` is a shared secret, not an identity.** There is no per-user authorisation
  and no audit of *which* caller did what — the ledger records what the run did, not who
  asked for it.
- **stdio MCP has no key check**, deliberately: the process reads `DATABASE_URL` from its
  own environment, so a key from that same environment guards nothing. `docs/mcp.md` §5.

---

## 11. If you find something

Open an issue with the run id if there is one. Everything a run did is in Postgres —
`/runs/{id}/detail` returns every tool call and model turn — which is usually faster than
reproducing it.
