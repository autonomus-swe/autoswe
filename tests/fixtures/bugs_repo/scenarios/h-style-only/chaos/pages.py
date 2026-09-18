"""Pagination helpers.

Splitting a sequence into fixed-size pages, with a short final page when the length does
not divide evenly. An empty input has no pages at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

__all__ = ["paginate"]


def paginate(items: Sequence[T], size: int) -> list[list[T]]:
    """Split ``items`` into pages of at most ``size`` elements.

    Args:
        items: the sequence to split.
        size: the maximum number of elements per page; must be at least one.

    Returns:
        A list of pages, in order. The final page may be shorter than ``size``.

    Raises:
        ValueError: if ``size`` is less than one.
    """
    if size < 1:
        raise ValueError("size must be at least 1")
    starts = range(0, len(items), size)
    return [list(items[start : start + size]) for start in starts]
