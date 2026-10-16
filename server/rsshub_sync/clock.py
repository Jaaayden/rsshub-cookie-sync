"""clock (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import time


def now_seconds() -> int:
    return int(time.time())


class Clock:
    """Clock abstraction used by the state machine and tests."""

    def now(self) -> int:
        return now_seconds()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
