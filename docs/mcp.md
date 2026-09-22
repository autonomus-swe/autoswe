# autoswe and MCP

Two directions, and they are independent. **Part A** is autoswe *as* an MCP server, so an
editor can drive a run. **Part B** is autoswe as an MCP *client*, mounting somebody else's
server so the agents can call its tools.

---

# Part A — driving autoswe over MCP

The control plane is an MCP server. An editor — Claude Code, or anything else that speaks
the protocol — can start a run, wait for it, answer its questions, approve the tool calls
it asks about, and read the diff and the pull-request URL at the end.

Two transports serve the same thirteen tools:

- **stdio** (`autoswe-mcp`), a child process the client launches. No HTTP hop: the tools
  call the control plane directly.
- **streamable HTTP** at `/mcp` on the API, for a client that cannot launch a process.
  Same API key as every other route.

---

## 1. One implementation, two transports

The tools do no work of their own. Each parses its arguments, calls `api/service.py`, and
renders the result — the same thing `api/routes/` does for HTTP. That is deliberate and it
is the whole design:

```
 editor ──MCP──┐
               ├──► api/service.py (ControlPlane) ──► Postgres · Redis · arq
 curl ──HTTP───┘
```

The alternative — an MCP tool that opens its own session, checks its own preconditions and
pushes its own inbox message — is a second implementation of a path that already exists.
Phase 5 produced four defects in one twenty-line block written exactly that way, each of
them a case the original handled and the copy forgot.

The approval path makes the cost concrete. `ControlPlane._pending` refuses a decision that
names a tool call the run is not currently parked on, because otherwise an approval could
be replayed later against a different call — a human authorising something they never saw.
A copy of the approve path that omitted that check would be a security hole reachable from
an editor. `tests/integration/test_mcp_server.py::test_an_approval_for_a_different_call_is_refused`
asserts the guard applies over MCP, and the MCP layer contains no line about it.

---

## 2. Install

### Claude Code, stdio

```bash
claude mcp add autoswe \
  -e DATABASE_URL="$DATABASE_URL" \
  -e REDIS_URL="$REDIS_URL" \
  -e API_KEYS="$API_KEYS" \
  -- uv run autoswe-mcp
```

Then, in a session:

> Use autoswe to add a `slugify()` helper with tests to
> `https://github.com/<you>/autoswe-fixture-python`, and tell me the PR link when it is done.

Claude Code calls `create_run`, then `wait_for_run`, and replies with the pull-request URL.

### Claude Code, HTTP

```bash
claude mcp add --transport http autoswe http://127.0.0.1:8000/mcp \
  --header "X-API-Key: $AUTOSWE_API_KEY"
```

### The MCP Inspector

```bash
npx @modelcontextprotocol/inspector uv run autoswe-mcp                  # stdio
npx @modelcontextprotocol/inspector http://127.0.0.1:8000/mcp           # HTTP
```

---

## 3. Tools

| Tool | Arguments | Returns |
|---|---|---|
| `create_run` | `repo_url`, `goal`, `base_branch="main"`, `budget_usd=10`, `unattended=false` | `{run_id}` |
| `get_run` | `run_id` | status, phase, cost, `pr_url`, and `pending` when parked |
| `list_runs` | `limit=20` | recent runs, newest first |
| `wait_for_run` | `run_id`, `timeout_s=1800` | the run once it finishes or needs an answer |
| `answer_run` | `run_id`, `text`, `tool_call_id=""` | `{ok}` |
| `approve_tool` / `reject_tool` | `run_id`, `tool_call_id` (+ `reason`) | `{ok}` |
| `cancel_run` | `run_id` | `{ok}` |
| `list_events` | `run_id`, `after_id=0`, `limit=200` | a page, plus `next_after_id` |
| `list_artifacts` | `run_id` | kinds, paths and sizes, without content |
| `get_artifact` | `run_id`, `kind` | the latest of that kind, as text |
| `search_code` / `read_file` | `run_id`, … | the run's checkout, read-only |

Resource: `run://{run_id}/artifacts/{kind}`, for clients that prefer resources to tools.

### The shape of a conversation

`wait_for_run` returns when the run *finishes* **or** when it needs something. A wait that
only stopped on a terminal status would deadlock against a run parked on a question the
caller is the one expected to answer, so `awaiting_input` ends the wait too, and the
`pending` field says what is being asked:

```json
{"status": "awaiting_input",
 "pending": {"kind": "approval", "tool_call_id": "toolu_7",
             "tool_name": "bash", "input": {"command": "pip install httpx"}}}
```

Answer it (`approve_tool`, `reject_tool`, or `answer_run` for the planner's open
questions) and wait again. If `status` comes back `running` or `queued`, the timeout
elapsed rather than anything going wrong; call `wait_for_run` again.

### `search_code` and `read_file`

These read the run's checkout at `<WORKTREES_DIR>/<run_id>` — the worker's disk. They are
registered only when a worktrees directory is configured, because a tool a client can see
is a tool it will try, and two that fail every time are worse than two that are absent.

When the API and the worker are separate hosts, or the run has been collected, they report
that there is no worktree rather than returning an empty result: "no matches" and "nothing
to search" are different answers and only one of them is worth acting on. The `diff`
artifact is the durable version.

Both go through `tools/fs.py` and `tools/search.py` — the same implementations the agents
use, so path containment comes with them. `read_file("../../.ssh/id_rsa")` is refused by
`tools/policy.confine`, and there is no second copy of that check to drift.

---

## 4. Errors

An MCP tool reports failure by raising, and the SDK keeps a *crashing* tool's message on
the server — the client sees only `Error executing tool <name>`. That is right for a crash
and wrong for "run not found", which is the answer. So the server translates every
`ControlError` into a `ToolError` whose text survives:

```
Error executing tool approve_tool: the run is waiting on toolu_9, not toolu_1
```

`api/errors.py` is the same translation for the other transport (404 / 409). Anything
unexpected still reaches the client as a bare tool name, which is where a connection
string would otherwise end up.

---

## 5. Configuration

| Variable | Used by | Meaning |
|---|---|---|
| `DATABASE_URL`, `REDIS_URL` | both | as everywhere else |
| `API_KEYS` | both | required to load settings at all; checked against callers only on HTTP |
| `WORKTREES_DIR` | both | enables `search_code` and `read_file` |
| `MCP_ALLOWED_HOSTS` | HTTP | `Host` headers to accept; empty means no check |

### Authorisation is per transport

**stdio has no key check.** There is nothing for one to protect: the process reads
`DATABASE_URL` and `REDIS_URL` from its own environment and connects straight to both, so
whoever can start it with those variables can already read and write everything a key
would have guarded. A check against a key supplied by the same environment is a lock whose
key is taped to it. (The Phase 6 plan sketched one; this is a deliberate departure.)

**HTTP requires `X-API-Key`**, the same header as every other route. The check is ASGI-level
rather than a FastAPI dependency, because the MCP transport is a separate ASGI application
and FastAPI's dependency tree stops at that boundary — a `Depends` beside it would never
run, and every tool including `cancel_run` would be open while looking guarded.

### `MCP_ALLOWED_HOSTS`

The MCP SDK enables DNS-rebinding protection by default with a localhost-only allow-list.
Left alone, the first deployment behind a real hostname answers every MCP request with a
bare `421 Invalid Host header` and nothing in it points at the cause.

That protection exists for a localhost server a browser can reach using the user's ambient
authority. This transport has none to borrow: every request needs `X-API-Key`, a custom
header a cross-origin page cannot set without a preflight, and the app installs no CORS
middleware to answer one. So the check is **off unless you turn it on**:

```bash
MCP_ALLOWED_HOSTS=autoswe.internal,autoswe.internal:8443
```

A `host:*` pattern matches only when the request carries a port — `example.com:*` does not
cover a request to `example.com` on port 443. List both spellings.

---

## 6. Deliberate departures from the Phase 6 plan

- **`FastMCP` is `MCPServer`.** The `mcp` SDK renamed it in 2.0; same object.
- **No `provider` argument on `create_run`.** The worker builds its provider from process
  settings, so a run that asked for a different one would be recorded as something it is
  not. Per-run provider selection is step 6.3, which is where the worker learns to honour
  it — `runs.provider` is written from the deployment's setting until then.
- **No `AUTOSWE_API_KEY` on stdio**, for the reason in §5.
- **Routes rather than a mount.** `app.mount("/mcp", …)` only matches `/mcp/…`; a request
  to `/mcp` itself gets a 307. `http://host/mcp` is what goes in an editor's config, so
  both spellings are exact routes.

---

## 7. Testing

```bash
uv run pytest tests/unit/test_mcp_server.py tests/unit/test_mcp_workspace.py    # no I/O
uv run pytest tests/integration/test_mcp_server.py                              # containers
```

The integration file drives the server three ways — in-process over the real control plane,
over the mounted HTTP transport, and as a spawned `autoswe-mcp` subprocess. The last one
exists because nothing in-process can catch a console script that does not resolve or a log
line written to stdout; an editor would report "server failed to start" and nothing else.

---

# Part B — mounting external MCP servers as agent tools

A GitHub server that can read issues, a Postgres server that can run a read-only query
against the application's own database: tools the agents did not have to be written to
know about. Copy `mcp_servers.yaml.example` to `mcp_servers.yaml` to enable; no file means
none, which is the default.

## 8. Everything is a policy the harness enforces

```yaml
servers:
  github:
    transport: stdio
    command: ["npx", "-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
    roles: [coder, debugger]
    allow: [get_issue, list_issues, get_pull_request, add_issue_comment]
    mutating: [add_issue_comment]
```

| Field | What it decides |
|---|---|
| `allow` | the only tools wrapped at all. Nothing else is reachable, whatever the server offers. |
| `roles` | which agents get them |
| `mutating` | which of them pause the run for a human's approval |
| `env` | everything the child process gets beyond the SDK's safe-list |
| `timeout_s` | per-call ceiling, default 60 |

**`mutating` is read from this file and never from the server's own `ToolAnnotations`.** A
server that declared its write tool read-only — by mistake or otherwise — would be
disarming its own approval gate, and the point of mounting through the harness is that the
harness decides. The *schema*, by contrast, does come from the server: it describes what
the tool accepts, which is not a claim about what it may do, and a local copy would be
wrong the first time the server changed.

Tools are renamed `mcp_<server>_<tool>`. A server exposing `read_file` would otherwise
shadow ours, and a model calling it would be reading the wrong machine's disk with nothing
in the transcript to say so.

## 9. The gates apply because the tools are registered

A mounted tool is put into `tools/registry.py` like any other. That is not bookkeeping:
`orchestrator/hooks.py` resolves a tool call's name in that registry and reads
`requires_approval` off what it finds. A mounted tool reaching the provider by some other
route would be a `None` in that lookup, and a `None` has no flag to read — the mutating
GitHub tool would run and nobody would be asked.

So a mounted tool gets the same treatment as `bash`: the policy check, the budget gate, the
approval gate, a row in `tool_calls`, and a line in the event stream.

**A mutating tool may not go to a read-only role.** `analyzer`, `planner`, `review`,
`review_pre`, `security` and `pr_writer` are read-only, and the worker refuses to start if
a configuration routes a mutating tool to one. Checked twice on purpose: `mcp_bridge/config.py`
says so early and legibly, and `tools/registry.py` asserts it again where nothing can get
past it.

> The Phase 6 plan's own example sets `roles: [planner, analyzer, pr_writer]` beside
> `mutating: [add_issue_comment]`. All three are read-only, so that configuration is
> rejected — by the rule the same plan asks for two paragraphs later. The example here
> gives the GitHub server to `coder` and `debugger`, which are the roles that may act.

## 10. Secrets

`env` is everything the child gets beyond the MCP SDK's own safe-list (`HOME`, `LOGNAME`,
`PATH`, `SHELL`, `TERM`, `USER`). An MCP stdio server is a subprocess, and one started with
`os.environ` would hold `ANTHROPIC_API_KEY`, `DATABASE_URL` and the GitHub token whether it
had any business with them or not — while being somebody else's code, chosen from a
registry, reading a repository the run does not control.

`${VAR}` is resolved from the worker's environment at mount time. A missing one is an error
rather than an empty string: a server that starts unauthenticated comes up, answers
`list_tools`, and fails on its first real call somewhere that mentions neither this file nor
the variable.

**Not in `command`.** The plan put a Postgres DSN in argv; an argument is visible in `ps` to
every user on the box. `${VAR}` expands in `env` values only, and the example uses a server
that reads its connection string from the environment.

## 11. Lifecycle

Servers are mounted once per worker process, at startup, and the sessions are shared by
every run. Starting `npx @modelcontextprotocol/server-github` per run would add seconds to
each and leave a process behind whenever one was cancelled.

- **A bad configuration stops the worker.** Somebody wrote that file; a worker that started
  anyway would be silently running without the tools it asked for, or worse, with a
  mutating tool routed somewhere it should not be.
- **A server that will not connect does not.** That is the network. Its tools are absent
  and the failure is logged twice — once by the supervisor, once naming the server the
  worker is running *without*, which is what an operator scanning a startup log is after.
- **A session that dies is replaced on the next call.** A worker lives for days and a child
  process does not have to.

## 12. Testing

```bash
uv run pytest tests/unit/test_mcp_config.py tests/unit/test_mcp_registry.py   # no I/O
uv run pytest tests/integration/test_mcp_client.py                            # containers
```

`tests/fixtures/stub_mcp_server.py` is a real subprocess speaking MCP over its pipes,
because that is what a mounted server is. An in-process stub would skip the half of
`mcp_bridge/client.py` that is about processes: the environment the child receives, a
session outliving the task that made a call, a server that will not start.
