import threading
import time
from collections import defaultdict, deque


class LimitExceeded(Exception):
    pass


class ReplyLimiter:
    """Caps replies per user per hour, one reply at a time per user, and replies running at once.

    State lives in this process, so run one instance of the app (see README).
    """

    def __init__(self, per_hour: int, max_concurrent: int, clock=time.monotonic) -> None:
        self.per_hour = per_hour
        self.max_concurrent = max_concurrent
        self._clock = clock
        self._lock = threading.Lock()
        self._history: dict[str, deque[float]] = defaultdict(deque)
        self._active: set[str] = set()

    def acquire(self, user: str) -> None:
        with self._lock:
            now = self._clock()
            history = self._history[user]
            while history and now - history[0] >= 3600:
                history.popleft()

            if user in self._active:
                raise LimitExceeded("Your previous question is still being answered. Wait for it to finish.")
            if len(history) >= self.per_hour:
                minutes = max(1, int((3600 - (now - history[0])) // 60) + 1)
                raise LimitExceeded(
                    f"You've reached the limit of {self.per_hour} questions an hour. Try again in about {minutes} min."
                )
            if len(self._active) >= self.max_concurrent:
                raise LimitExceeded("The assistant is busy with other people's questions. Try again in a minute.")

            history.append(now)
            self._active.add(user)

    def release(self, user: str) -> None:
        with self._lock:
            self._active.discard(user)
