"""Request pacing for the login endpoint.

The words a keyword search would reach for are deliberately absent from this file; see the
fixture README for why.
"""

import time


class TokenBucket:
    """Lets a caller through while it has budget, and refills over time."""

    def __init__(self, capacity: int = 10, per_second: float = 0.5) -> None:
        self.capacity = capacity
        self.per_second = per_second
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.per_second)
        self.updated = now

    def consume(self, amount: int = 1) -> bool:
        """True when the caller may proceed, False when it must wait."""
        self.refill()
        if self.tokens < amount:
            return False
        self.tokens -= amount
        return True

    def retry_after(self) -> float:
        """Seconds until one more token is available."""
        self.refill()
        return max(0.0, (1 - self.tokens) / self.per_second)
