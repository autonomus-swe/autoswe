"""Keeping a long agent loop inside its context window, host-side.

A forty-turn Coder loop re-sends its whole transcript on every turn, and most of that
transcript is tool *results* — file contents it has already edited, test output it has
already acted on, directory listings from twenty turns ago. The model needs to know those
calls happened; it very rarely needs to read them again.

## Why this is here rather than asked for

The phase document specifies Anthropic's server-side `clear_tool_uses_20250919` context
edit, which this build cannot use: it has no Anthropic provider, only an
OpenAI-compatible one. So the same idea is done here, on the transcript we assemble.

Host-side is not strictly worse. It is visible — `cleared_tool_results` says exactly what
went and when — and it works against every endpoint rather than one vendor's beta.

## Results, never inputs

The assistant's `tool_calls` stay untouched. The Debugger reads its own earlier commands to
know what it has already tried, and a loop that cannot remember what it ran repeats it. So
what is cleared is the *answer*, and it is replaced by a line saying so rather than
deleted: a tool result that silently vanishes reads as a call that never happened, and the
model makes it again.

## It costs a cache write, which is why it is rare

Editing an old message changes the prefix from that point, so the next request re-writes
the cache instead of reading it (see `gateway/caching`). That is worth paying once to make
every later turn smaller, and not worth paying every turn — so it fires only when the
transcript is genuinely large, and the most recent results are always kept, because those
are the ones the model is actually working from.
"""

from __future__ import annotations

from typing import Any

from observability.logging import get_logger

log = get_logger(__name__)

# The same rough divisor the repo map's budget uses, and the same caveat: being a third out
# costs a slightly early or slightly late trim, not a wrong one.
CHARS_PER_TOKEN = 4
# Below this the transcript is not the problem and clearing would spend a cache write for
# nothing.
TRIGGER_TOKENS = 40_000
# Results this recent are what the model is working from right now.
KEEP_RECENT = 6
# A result smaller than this is not worth the placeholder that would replace it.
MIN_CLEARABLE_CHARS = 400

CLEARED = (
    "[earlier result cleared to stay within the context window; call the tool again if you need it]"
)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Roughly how large this transcript is. Characters, divided."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(
                len(str(part.get("text", ""))) for part in content if isinstance(part, dict)
            )
        for call in message.get("tool_calls") or []:
            total += len(str(call.get("function", {}).get("arguments", "")))
    return total // CHARS_PER_TOKEN


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def clear_old_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = KEEP_RECENT,
    min_chars: int = MIN_CLEARABLE_CHARS,
) -> int:
    """Replace the bodies of old tool results in place. Returns how many were cleared.

    Only `role: "tool"` messages, only ones past `keep_recent`, and only ones big enough to
    be worth the trade. Anything already cleared is skipped, so calling this repeatedly is
    idempotent and does not keep spending cache writes on a transcript it has finished
    with.
    """
    indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(indices) <= keep_recent:
        return 0

    cleared = 0
    for index in indices[:-keep_recent] if keep_recent else indices:
        message = messages[index]
        body = _text_of(message)
        if body == CLEARED or len(body) < min_chars:
            continue
        message["content"] = CLEARED
        cleared += 1
    return cleared


def trim(messages: list[dict[str, Any]], *, trigger_tokens: int = TRIGGER_TOKENS) -> int:
    """Clear old tool results when the transcript has grown large enough to be worth it.

    Returns the number cleared, zero when nothing was done. The estimate is deliberately
    taken before and after so the log line says what it actually saved rather than what it
    hoped to.
    """
    before = estimate_tokens(messages)
    if before < trigger_tokens:
        return 0
    cleared = clear_old_tool_results(messages)
    if cleared:
        log.info(
            "cleared_tool_results",
            cleared=cleared,
            tokens_before=before,
            tokens_after=estimate_tokens(messages),
        )
    return cleared
