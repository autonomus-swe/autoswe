"""Prometheus scrape endpoint.

Unauthenticated, like `/healthz`, and for the same reason: a scraper is a piece of
infrastructure that cannot hold an API key, and every metric here is a count or a
duration. Nothing in it names a repository, a goal, or a customer.

This is the API process's registry. The worker keeps its own on `:9100` — they are
separate processes and one cannot see the other's counters, so a scrape config needs both.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from observability import metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def prometheus() -> Response:
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
