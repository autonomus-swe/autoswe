# The command line

```bash
uv run autoswe --help
```

Twelve commands. Everything the control plane does, minus the console.

---

## 1. Starting and following a run

```bash
autoswe run --repo https://github.com/you/project --goal "Add a --json flag to status."
autoswe run --repo … --goal … --follow          # stream it and exit with its outcome
```

| Option | |
|---|---|
| `--base` | branch to start from (default `main`) |
| `--budget` | dollar ceiling for this run |
| `--unattended` | nobody is watching: refuse approvals rather than park |
| `--provider` | LLM provider for this run — see `docs/open-source-model.md` |
| `--upstream` | `owner/repo` to open the PR on, when `--repo` is your fork |
| `--follow` | stream events and exit with the run's status |

**`--follow` exits 0 done, 1 failed, 2 awaiting input.** The third is not a failure — it
means the run wants an answer from you — and a script that treated it as one would give up
on a question it could have answered.

---

## 2. Watching and reading

```bash
autoswe list                        # recent runs, newest first
autoswe status <run-id>             # phase, status, cost, PR URL
autoswe status <run-id> --json      # the server's JSON, verbatim
autoswe watch <run-id>              # stream events until the run ends
autoswe artifacts <run-id>          # what the run wrote, with sizes
autoswe artifacts <run-id> diff     # one of them
```

`--json` works on **every** read command. A flag that worked on four out of six would be
worse than none: a script cannot tell which without trying, and the one it tries is the one
in production.

`watch` differs from `run --follow` in one thing only: it keeps following through a
question. It is meant for a second terminal, where answering happens elsewhere, and
stopping there would end the stream at the moment its reader most wants to keep looking.

---

## 3. Answering

```bash
autoswe answer <run-id> "Postgres, not MySQL"
autoswe approve <run-id> <tool-call-id>
autoswe reject  <run-id> <tool-call-id> "not on production"
autoswe cancel  <run-id>
```

The tool call id comes from the `awaiting_input` event, which `watch` prints.

---

## 4. Evaluating

```bash
autoswe eval --suite private
autoswe eval --suite private --tags bugfix --concurrency 3
```

Exits non-zero when a task went unresolved, so it is usable in CI without a wrapper that
greps the table. A task with no verify command is *unverifiable* rather than unresolved and
does not fail the command. `docs/evals.md`.

---

## 5. Serving MCP

```bash
autoswe mcp            # same as the `autoswe-mcp` console script
```

`autoswe-mcp` exists separately because an editor's MCP configuration runs a single
command, and typer prints its own diagnostics to stdout — which on that transport is the
protocol. `docs/mcp.md`.

---

## 6. Configuration

Flags, then environment, then a file — in order of how specific the intent is. A flag was
typed for this invocation; an environment variable was exported for this shell; a file was
written once and forgotten, which is why it loses to both.

```toml
# ~/.config/autoswe/config.toml
api = "https://autoswe.internal"
key = "..."
```

| | |
|---|---|
| `--api` / `AUTOSWE_API` / `api` | control-plane base URL |
| `--key` / `AUTOSWE_API_KEY` / `key` | API key |

A flag that happens to equal the default is still a flag and still wins — which is how you
get back to `http://127.0.0.1:8000` when a config file points at staging.

**A `config.toml` that cannot be parsed is an error**, not a fall-through to the defaults.
Falling through would connect to localhost with no key and report "unauthorized", which
sends you to look at your key rather than at the file you just edited.

---

## 7. Shell completion

```bash
autoswe --install-completion
```

Typer's, for bash, zsh, fish and PowerShell.
