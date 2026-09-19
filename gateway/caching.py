"""Where the cache breakpoints go, and what has to stay still for them to be worth having.

Prompt caching pays only for an *exact* prefix match. Every provider that caches —
Anthropic explicitly, OpenAI and DeepSeek and Gemini automatically — compares bytes from
the start of the request, and the first byte that differs ends the match. So the whole
subject of this module is one question: which parts of a request are allowed to change,
and where does the boundary between still and moving sit.

## The shape

    tools:    the role's registry order, fixed, never reordered between calls
    system:   [ role prompt  — static per role, no dates, no ids, no counters ]
              [ run block    — repo facts, conventions, map; fixed for a whole run ]
    messages: task, file contents, tool results — volatile, and that is fine

Two blocks rather than one because they go stale at different times. The role prompt is
the same for every run this build ever does; the run block is the same for every step of
one run. Splitting them means a new run still reads the role prompt from cache.

## The invalidators are the point

A timestamp, a run id, or a turn counter anywhere in the prefix costs the entire cache for
every call after it — silently, since a cache miss looks exactly like a cache hit except on
the bill. That is why `invalidators()` exists and why a test runs it over every role prompt
in the repository: the failure mode is invisible in output and only shows up in cost.

This is also why `{test_command}` left `coder.md`. It is a repository fact, so it belongs in
the run block with the other repository facts — in the role prompt it made the Coder's
system prefix differ per repository for no gain.

## Moving breakpoints

A Coder loop of forty turns grows a transcript far larger than the system prefix, and none
of it was cached by the two static blocks. So `moving_breakpoints` marks the most recent
tool result every few turns: that call writes the longer prefix, and the next call reads it.

The provider limit is four breakpoints. Two are spent on `system`, which leaves two to
move, and two is the right number rather than a compromise — the older mark is the prefix
the next call *reads*, the newer one is the prefix it *writes*. Keeping only the newest
would mean every call wrote a cache no call ever read.

## Explicit breakpoints only where they are understood

`cache_control` inside a content part is an Anthropic field that OpenRouter passes through.
Sending it to an endpoint that validates its input strictly gets the whole request
rejected, so it goes out only where it is known to be read. Everywhere else the request is
assembled the same way and sent as plain text — which still matters, because automatic
caching keys on exactly the prefix stability this module exists to protect.
"""

from __future__ import annotations

import json
import re
from typing import Any

from contracts import RepoFacts, RepoProfile

EPHEMERAL: dict[str, str] = {"type": "ephemeral"}

# The provider's ceiling. Two go to `system`; what is left moves through the transcript.
MAX_BREAKPOINTS = 4
SYSTEM_BREAKPOINTS = 2
MOVING_BREAKPOINTS = MAX_BREAKPOINTS - SYSTEM_BREAKPOINTS
# How often a moving breakpoint is placed. Every turn would write a cache entry per call
# and read almost none of them; the write costs more than the read saves.
TOOL_RESULT_EVERY = 8
# Below this a block is not worth a breakpoint — providers decline to cache short prefixes
# anyway, and the marker still costs a write attempt.
MIN_CACHEABLE_CHARS = 2_000

# Gateways known to read `cache_control`. Everything else gets the same bytes without it.
BREAKPOINT_HOSTS = ("openrouter.ai",)

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?\b")
COUNTER_RE = re.compile(r"\b(?:seq|turn|iteration|attempt|step)\s*[:=]\s*\d+", re.I)
EPOCH_RE = re.compile(r"\b1[6-9]\d{8}(?:\.\d+)?\b")


def supports_breakpoints(base_url: str | None) -> bool:
    """Whether this endpoint reads `cache_control`, or would reject it."""
    return bool(base_url) and any(host in (base_url or "") for host in BREAKPOINT_HOSTS)


def invalidators(text: str) -> list[str]:
    """Substrings that would make this text differ between two otherwise identical calls.

    Used as an audit over the role prompts rather than as a runtime guard: a timestamp in a
    prompt is a mistake to catch in a test, not something to strip in production, where
    stripping it would silently change what the model was told.
    """
    found: list[str] = []
    for pattern in (UUID_RE, ISO_RE, COUNTER_RE, EPOCH_RE):
        found.extend(match.group(0) for match in pattern.finditer(text))
    return found


def render_run_block(
    facts: RepoFacts | None,
    profile: RepoProfile | None,
    repo_map: str,
) -> str:
    """The part of the prefix that is fixed for a whole run.

    Deterministic by construction rather than by care: the profile is dumped with
    `sort_keys=True`, and `RepoFacts.render()` emits a fixed row order. Nothing here reads
    a clock, a run id, or a counter — see `invalidators`, and the test that runs it over
    this function's output.
    """
    parts: list[str] = []
    if facts is not None:
        parts.append("# Repository (detected)\n" + facts.render())
    if profile is not None:
        parts.append(
            "# Repository (confirmed by the Analyzer)\n"
            + json.dumps(profile.model_dump(), sort_keys=True, indent=2)
        )
    if repo_map.strip():
        parts.append("# Repository map\n" + repo_map.strip())
    return "\n\n".join(parts)


def build_system(role_prompt: str, run_block: str | None, *, breakpoints: bool) -> Any:
    """The system field: one string, or blocks with the static parts marked cacheable.

    Returns a plain string when there is nothing to cache and nothing to separate, because
    a one-element list of content parts is a needlessly exotic request to send to a gateway
    that gains nothing from it — and every gateway accepts the string form.
    """
    blocks: list[dict[str, Any]] = [_block(role_prompt, breakpoints)]
    if run_block:
        blocks.append(_block(run_block, breakpoints))
    if len(blocks) == 1 and "cache_control" not in blocks[0]:
        return role_prompt
    return blocks


def _block(text: str, breakpoints: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if breakpoints and len(text) >= MIN_CACHEABLE_CHARS:
        block["cache_control"] = EPHEMERAL
    return block


def moving_breakpoints(messages: list[dict[str, Any]], turns: int) -> None:
    """Mark the most recent tool result every `TOOL_RESULT_EVERY` turns, in place.

    Keeps the two most recent marks and drops the rest. Two is what the four-breakpoint
    limit leaves after `system`, and it is also the number the mechanism wants: the older
    mark names the prefix this call reads from cache, the newer one names the longer prefix
    it writes for the next call.
    """
    if turns <= 0 or turns % TOOL_RESULT_EVERY or not messages:
        return
    latest = _last_tool_index(messages)
    if latest is None:
        return
    _mark(messages[latest])
    marked = [i for i, m in enumerate(messages) if _is_marked(m)]
    for index in marked[:-MOVING_BREAKPOINTS]:
        _unmark(messages[index])


def _last_tool_index(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "tool":
            return index
    return None


def _mark(message: dict[str, Any]) -> None:
    """Turn a tool message's text into a content part carrying `cache_control`."""
    if _is_marked(message):
        return
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content, "cache_control": EPHEMERAL}]


def _unmark(message: dict[str, Any]) -> None:
    """Back to a plain string. An unmarked old breakpoint is bytes nobody reads."""
    content = message.get("content")
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        message["content"] = content[0].get("text", "")


def _is_marked(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and "cache_control" in content[0]
    )


def prefix_of(system: Any, tools: list[dict[str, Any]]) -> str:
    """The bytes a cache match is decided on, for tests and for logging.

    Tools come first because that is the order the request is assembled in: a reordered
    tool list invalidates the cache just as surely as a changed system prompt, and the
    registry order is fixed for exactly that reason.
    """
    return json.dumps({"tools": tools, "system": system}, sort_keys=True)
