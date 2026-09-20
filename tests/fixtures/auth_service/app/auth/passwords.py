"""Password hashing and comparison."""

import hashlib
import hmac
import os

ROUNDS = 120_000


def hash_password(plaintext: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), salt, ROUNDS)
    return f"{salt.hex()}${digest.hex()}"


def matches(plaintext: str, stored: str) -> bool:
    salt_hex, _, digest_hex = stored.partition("$")
    digest = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), bytes.fromhex(salt_hex), ROUNDS)
    return hmac.compare_digest(digest.hex(), digest_hex)
