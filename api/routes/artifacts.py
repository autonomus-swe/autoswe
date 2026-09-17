"""Read a run's artifacts.

Everything a run learned that is too large for an event: the diff it produced, the test
reports, the review with its rejected candidates and reasons, the security findings, and
the pull-request description. Seven kinds get written and until now none of them could be
read back — they were an audit trail nobody could open, which is not an audit trail.

**The listing carries sizes, not content.** A `diff` artifact can be megabytes, and a
caller deciding whether to fetch one should not have to fetch it to find out.

**Sits at the same trust level as `/runs/{id}/detail`**, which already returns every tool
call's input and an output preview. `require_api_key` via `read_rate_limit` is the gate; an
operator who can read a run's tool calls can read its artifacts. What does *not* pass
through here is anything a forge would see — that distinction lives in `repo/pr_body.py`,
which renders a published body from a narrower set of fields than these rows hold.
"""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from api.auth import read_rate_limit
from api.schemas import ArtifactView
from storage import repo as db
from storage.db import session

router = APIRouter(prefix="/runs", tags=["artifacts"])

# `diff` is a unified diff stored as `{"text": …}`, so it is served as text: a reviewer
# pipes it to `git apply` or reads it, and neither is helped by JSON escaping every line.
TEXT_KINDS = {"diff"}


@router.get("/{run_id}/artifacts", response_model=list[ArtifactView])
async def list_artifacts(
    run_id: UUID, request: Request, _key: str = Depends(read_rate_limit)
) -> list[ArtifactView]:
    """What this run wrote, in the order it wrote it, with sizes and without content."""
    async with session(request.app.state.engine) as s:
        if await db.get_run(s, run_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
        rows = await db.list_artifacts(s, run_id)
    return [
        ArtifactView(
            kind=r.kind,
            path=r.path,
            created_at=r.created_at,
            # Measured on the serialised form, because that is what a caller will receive.
            size=len(json.dumps(r.content, separators=(",", ":"))),
        )
        for r in rows
    ]


@router.get("/{run_id}/artifacts/{kind}")
async def get_artifact(
    run_id: UUID, kind: str, request: Request, _key: str = Depends(read_rate_limit)
) -> object:
    """The latest artifact of one kind.

    The latest rather than all of them: a run that went round the TEST loop four times has
    four `test_report` rows, and "what did it end up with" is the question this answers.
    The listing above is where the sequence is visible.
    """
    async with session(request.app.state.engine) as s:
        if await db.get_run(s, run_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
        row = await db.latest_artifact(s, run_id, kind)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"run has no {kind!r} artifact")
    if kind in TEXT_KINDS:
        text = row.content.get("text") if isinstance(row.content, dict) else None
        return PlainTextResponse(str(text if text is not None else row.content))
    return row.content
