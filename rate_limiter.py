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


class ConsecutiveNoneTracker:
    """Per-location tracker that pauses detection after a run of consecutive
    'None' results within a rolling time window."""

    def __init__(self, threshold, window_seconds):
        """
        Args:
            threshold: Number of consecutive None results that triggers a pause
            window_seconds: Only Nones within this rolling window count toward the streak
        """
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.none_times = []  # timestamps of the current consecutive-None streak
        self.lock = Lock()

    def should_skip(self):
        """
        Returns:
            True if the last `threshold` results were all None and all fall
            within the past `window_seconds` (i.e. skip the LLM call).
        """
        with self.lock:
            self._prune()
            return len(self.none_times) >= self.threshold

    def record_none(self):
        """Record a 'None' result, extending the consecutive streak."""
        with self.lock:
            self.none_times.append(datetime.now())
            self._prune()

    def record_detection(self):
        """Record a real (non-None) result, which breaks the streak."""
        with self.lock:
            self.none_times.clear()

    def _prune(self):
        """Drop streak timestamps older than the window. Caller holds the lock."""
        now = datetime.now()
        self.none_times = [t for t in self.none_times
                           if (now - t).total_seconds() < self.window_seconds]
