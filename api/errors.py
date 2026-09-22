"""Control-plane complaints as HTTP status codes.

Its own module rather than a helper inside one router, because three of them map the same
errors and a route importing from a sibling route to get at it says the wrong thing about
where the shared piece lives.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from api.service import ControlError, NotFound


def http_error(error: ControlError) -> HTTPException:
    """404 for a run (or artifact) that is not there, 409 for one that is but is in the
    wrong state — the distinction a caller needs to decide whether retrying could ever
    help."""
    code = status.HTTP_404_NOT_FOUND if isinstance(error, NotFound) else status.HTTP_409_CONFLICT
    return HTTPException(code, detail=str(error))
