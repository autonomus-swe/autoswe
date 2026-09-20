"""A terminated end-to-end run still unwinds, so its measurements survive.

The e2e suite is the only place several Phase 5 numbers can be obtained, and a run against
a local model takes hours — so it is far likelier to be *stopped* than to finish: a CI step
timeout, `timeout(1)`, an operator with a deadline.

Python's default SIGTERM handling ends the process without unwinding, skipping every
`finally`. `test_m5_cache` computes its cache report in a `finally` exactly so a failed run
still records its numbers, and that report reads the `llm_calls` ledger from a
testcontainer Postgres that dies with the process. So the default behaviour does not cost
the last measurement — it costs all of them, and the evidence has to be gathered again from
a run that takes hours.

These are unit tests of the handler itself rather than of a real run, because the thing
worth pinning is small and exact: the signal becomes an exception, `finally` sees it, and
the previous handler is put back.
"""

from __future__ import annotations

import signal
from typing import Any

import pytest

from tests.e2e.conftest import _raise_on_terminate, unwinding_on_termination

pytestmark = pytest.mark.unit


def test_a_termination_signal_becomes_an_exception() -> None:
    """Without this, the process simply stops and the stack is never unwound."""
    with pytest.raises(KeyboardInterrupt, match="signal 15"):
        _raise_on_terminate(signal.SIGTERM, None)


def test_finally_blocks_run_when_the_signal_arrives() -> None:
    """The property the whole thing exists for, exercised end to end with a real signal.

    `signal.raise_signal` delivers SIGTERM to this process for real, so this fails if the
    handler is not installed, if it returns instead of raising, or if it raises something
    that does not propagate out of the `try`.
    """
    recorded: list[str] = []
    previous = signal.signal(signal.SIGTERM, _raise_on_terminate)
    try:
        with pytest.raises(KeyboardInterrupt):
            try:
                signal.raise_signal(signal.SIGTERM)
            finally:
                recorded.append("report written")
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert recorded == ["report written"], "the finally block never ran"


def test_the_fixture_restores_whatever_handler_was_there() -> None:
    """Leaving it installed would change how the whole pytest session dies, including for
    tests that never asked for this."""

    def sentinel(signum: int, frame: Any) -> None:
        return None

    original = signal.signal(signal.SIGTERM, sentinel)
    try:
        with unwinding_on_termination():
            assert signal.getsignal(signal.SIGTERM) is _raise_on_terminate, "not installed"
        assert signal.getsignal(signal.SIGTERM) is sentinel, "did not restore"
    finally:
        signal.signal(signal.SIGTERM, original)


def test_the_handler_is_installed_only_for_the_duration() -> None:
    """Before and after, SIGTERM belongs to whoever owned it."""
    original = signal.getsignal(signal.SIGTERM)

    with unwinding_on_termination():
        inside = signal.getsignal(signal.SIGTERM)

    assert inside is _raise_on_terminate
    assert signal.getsignal(signal.SIGTERM) is original
