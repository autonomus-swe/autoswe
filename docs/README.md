# Documentation

## Start here

| | |
|---|---|
| [`../README.md`](../README.md) | what this is and how to run it |
| [`../TESTING.md`](../TESTING.md) | the whole product running in five minutes, with no API key |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | the design, and why each piece is where it is |

## Using it

| | |
|---|---|
| [`cli.md`](cli.md) | the twelve commands, `--json`, `--follow`, configuration |
| [`mcp.md`](mcp.md) | driving autoswe from an editor, and mounting other MCP servers |
| [`open-source-model.md`](open-source-model.md) | Ollama, vLLM, free endpoints, per-run providers |
| [`evals.md`](evals.md) | task suites, what "resolved" means, SWE-bench |
| [`observability.md`](observability.md) | traces, metrics, Langfuse |

## Operating it

| | |
|---|---|
| [`security.md`](security.md) | every guarantee, the test that proves it, and the limits |
| [`sandbox-hardening.md`](sandbox-hardening.md) | what the container can and cannot do |
| [`egress.md`](egress.md) | the proxy and the allow-list |

## The record

| | |
|---|---|
| [`numbers.md`](numbers.md) | every measured figure, with the run that produced it |
| [`test-gaps.md`](test-gaps.md) | test gaps proved by mutation, with the ones still open |
| [`design-notes.md`](design-notes.md) | decisions taken along the way |
| [`review-rubric.md`](review-rubric.md) | what the Reviewer is asked to look for |
| [`../CHANGELOG.md`](../CHANGELOG.md) | what each phase added |

## The build plan

[`PHASES.md`](PHASES.md) is the map. Each phase document carries its own exit criteria,
ticked only where something was run — and marked `[~]` with the reason where it was not.

| | |
|---|---|
| [`PHASE-0-foundations.md`](PHASE-0-foundations.md) | contracts, settings, the skeleton |
| [`PHASE-1-single-agent-loop.md`](PHASE-1-single-agent-loop.md) | one agent, one sandbox, one PR |
| [`PHASE-2-plan-and-state.md`](PHASE-2-plan-and-state.md) | planning, the task graph, checkpoints |
| [`PHASE-3-verification-loop.md`](PHASE-3-verification-loop.md) | tests, the Debugger, escalation |
| [`PHASE-4-review-security-pr.md`](PHASE-4-review-security-pr.md) | review, scanning, the PR body |
| [`PHASE-5-scale-and-cost.md`](PHASE-5-scale-and-cost.md) | the repo map, caching, budgets, tracing |
| [`PHASE-6-interop-and-evals.md`](PHASE-6-interop-and-evals.md) | MCP both ways, providers, evals |
