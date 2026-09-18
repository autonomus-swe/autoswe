"""Session tokens for the pages API."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

TTL_S = 900


def issue_token(sub: str, ttl_s: int = TTL_S) -> str:
    """Issue a token for ``sub`` that stops being valid after ``ttl_s`` seconds."""
    payload = {"sub": sub, "exp": int(time.time()) + ttl_s}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_token(token: str) -> dict[str, Any]:
    """Unpack a token into its payload."""
    raw = base64.urlsafe_b64decode(token.encode())
    return dict(json.loads(raw.decode()))


def current_user(token: str) -> str:
    """Return the user a token belongs to, for an authenticated request."""
    payload = decode_token(token)
    return str(payload["sub"])
