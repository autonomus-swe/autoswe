"""Formatting timestamps."""

from __future__ import annotations

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def seconds_since_epoch(moment: datetime) -> int:
    """Whole seconds between ``moment`` and the epoch."""
    return int((moment - EPOCH).total_seconds())
