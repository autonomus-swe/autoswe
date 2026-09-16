"""Fetching a page. Needs the internet, which the sandbox does not have."""

from __future__ import annotations

from urllib.request import urlopen


def title_length(url: str) -> int:
    """Bytes in the response body. Requires network access."""
    with urlopen(url, timeout=5) as response:  # noqa: S310
        return len(response.read())
