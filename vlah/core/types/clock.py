"""Injected time source; components never read the wall clock directly."""

import time
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def now_seconds(self) -> float:
        """Current time as epoch seconds."""
        ...


@dataclass(kw_only=True)
class SystemClock:
    def now_seconds(self) -> float:
        return time.time()


@dataclass(kw_only=True)
class FakeClock:
    """Clock for tests that only moves when advance is called."""

    current_seconds: float = 0.0

    def now_seconds(self) -> float:
        return self.current_seconds

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"a clock cannot move backwards (seconds={seconds})")
        else:
            self.current_seconds += seconds
