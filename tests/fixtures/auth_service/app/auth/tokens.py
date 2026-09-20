"""Issue and verify signed session tokens."""

import base64
import hashlib
import hmac
import json
import time

SECRET = b"fixture-only-not-a-real-secret"
LIFETIME_S = 3600


def issue(user_id: str) -> str:
    payload = json.dumps({"sub": user_id, "exp": int(time.time()) + LIFETIME_S}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    mac = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def verify(token: str) -> str | None:
    """The user id, or None when the token is forged or stale."""
    body, _, mac = token.partition(".")
    expected = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, expected):
        return None
    padded = body + "=" * (-len(body) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    if claims.get("exp", 0) < time.time():
        return None
    return str(claims.get("sub", "")) or None
