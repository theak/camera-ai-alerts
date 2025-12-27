"""Simple thread-safe rate limiter."""

from threading import Lock
from datetime import datetime


class RateLimiter:
    """Simple thread-safe rate limiter."""

    def __init__(self, cooldown_seconds):
        """
        Initialize rate limiter.

        Args:
            cooldown_seconds: Minimum seconds between allowed actions
        """
        self.cooldown_seconds = cooldown_seconds
        self.last_used = None
        self.lock = Lock()

    def check_and_update(self):
        """
        Check if action is allowed and mark as used if so.

        Returns:
            True if action is allowed (not in cooldown), False otherwise
        """
        with self.lock:
            now = datetime.now()
            if self.last_used is None or \
               (now - self.last_used).total_seconds() >= self.cooldown_seconds:
                self.last_used = now
                return True
            return False
