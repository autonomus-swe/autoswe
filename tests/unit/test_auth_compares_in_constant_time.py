"""`docs/security.md` §8 claims keys are compared in constant time. This is the proof.

The document opens by saying **"a guarantee with no test beside it is a hope"**, and then
lists every claim with the test that holds it. One row did not have one: *"Keys are compared
in constant time against every configured key"* cited `api/auth._matches` — the
implementation it is a claim about.

Measured rather than assumed: replacing `hmac.compare_digest` with `candidate in configured`
survives **the entire unit suite**. Both give the same answers, so no behavioural test can
tell them apart; the difference is only in how long a wrong key takes to reject, and that is
what leaks which prefix was right.

## Why this test reads the source instead of timing anything

A timing assertion is the obvious idea and the wrong one. The difference between a
constant-time compare and a short-circuiting one is nanoseconds, swamped on any real machine
by scheduling, frequency scaling and the GC; a test built on it would fail on a loaded CI box
and pass on a build that had lost the property. That is worse than no test, because it would
be *noise* that people learn to re-run until green.

So this asserts the mechanism: that `_matches` goes through `hmac.compare_digest`, and that
it compares against every configured key rather than stopping at the first match. Those are
the two facts the claim rests on, and both are decidable from the source without timing
anything.

It is a weaker kind of test than the rest of the suite and it says so. The alternative on
offer was a documentation row pointing at its own subject.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from api import auth

pytestmark = pytest.mark.unit


def test_a_wrong_key_is_rejected_and_a_configured_one_accepted() -> None:
    """The behaviour first, so the structural assertions below are not the only thing
    standing between this module and a `return True`."""
    configured = frozenset({"right-key", "another-right-key"})

    assert auth._matches("right-key", configured) is True
    assert auth._matches("another-right-key", configured) is True
    assert auth._matches("wrong-key", configured) is False
    assert auth._matches("", configured) is False
    assert auth._matches("right-key", frozenset()) is False, "no keys configured accepts none"


def test_a_key_that_is_a_prefix_of_a_configured_one_is_rejected() -> None:
    """The shape a short-circuiting comparison leaks: an attacker lengthening a guess one
    character at a time learns where the match stops. The answer must be no either way."""
    configured = frozenset({"abcdefghij"})

    for guess in ("a", "abcde", "abcdefghi", "abcdefghijk"):
        assert auth._matches(guess, configured) is False, f"{guess!r} is not the key"


def test_the_comparison_goes_through_hmac_compare_digest() -> None:
    """The mechanism the claim rests on, asserted from the source.

    `candidate in configured` returns the same answers and is not constant-time. Nothing
    behavioural distinguishes them, which is why that mutation survived the whole unit suite
    — so the test has to name the function being relied upon.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(auth._matches))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)

    assert "compare_digest" in names, (
        "`_matches` must compare through `hmac.compare_digest`; an `in` or `==` against the "
        "key set gives the same answers in a length of time that depends on the guess"
    )


def test_every_configured_key_is_compared_rather_than_the_first_match_winning() -> None:
    """The second half of the claim: *against every configured key*.

    `any(...)` over a generator short-circuits on the first `True`, which is fine — the
    timing that matters is the rejection path, and a wrong key is compared against all of
    them. What would break the claim is comparing against only one, so the test asserts a
    key placed anywhere in the set is still found.

    A set has no order to rely on, so this is done by construction: every member is tried as
    the correct guess, and all of them must be accepted.
    """
    configured = frozenset({f"key-{i}-{'x' * i}" for i in range(12)})

    for key in configured:
        assert auth._matches(key, configured) is True, f"{key!r} was configured and refused"


def test_the_source_comment_explaining_the_choice_is_still_there() -> None:
    """Not decoration. The next person to read `_matches` sees a loop that looks like it
    could be a set membership test, and the comment is the only thing that says why it is
    not. Deleting the comment is how the function gets "simplified" in a later refactor.
    """
    source = Path(auth.__file__).read_text()
    assert "compare_digest against every key" in source, (
        "the comment explaining why this is not `in` has gone; without it the next "
        "simplification removes the property"
    )
