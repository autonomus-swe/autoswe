"""HTTP handlers for the service."""

from app.auth import passwords, tokens
from app.auth.bucket import TokenBucket

_buckets: dict[str, TokenBucket] = {}


def login(username: str, password: str, stored_hash: str, client_ip: str) -> dict[str, object]:
    bucket = _buckets.setdefault(client_ip, TokenBucket())
    if not bucket.consume():
        return {"status": 429, "retry_after": round(bucket.retry_after(), 2)}
    if not passwords.matches(password, stored_hash):
        return {"status": 401}
    return {"status": 200, "token": tokens.issue(username)}


def whoami(token: str) -> dict[str, object]:
    user = tokens.verify(token)
    return {"status": 200, "user": user} if user else {"status": 401}
