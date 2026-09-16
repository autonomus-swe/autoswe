"""Splitting a sequence into pages."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def paginate(items: Sequence[T], size: int) -> list[list[T]]:
    """Split ``items`` into pages of at most ``size``.

    The last page is short when the length does not divide evenly, and an empty input has
    no pages at all.
    """
    if size < 1:
        raise ValueError("size must be at least 1")
    return [list(items[start : start + size]) for start in range(0, len(items) - 1, size)]
