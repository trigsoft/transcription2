"""Real completion anchors and estimates for one recognition stage."""
from dataclasses import dataclass
import time


@dataclass
class StageProgress:
    started: float | None = None
    current: int = 0
    total: int = 0

    def update(self, current: int, total: int) -> None:
        if total <= 0 or not 0 <= current <= total:
            return
        if self.started is None or total != self.total or current < self.current:
            self.started = time.monotonic()
        self.current, self.total = current, total

    def payload(self) -> dict:
        elapsed = max(0, time.monotonic() - self.started) if self.started is not None else 0
        return {
            "current": self.current,
            "total": self.total,
            "elapsedSeconds": round(elapsed),
            "etaSeconds": round(elapsed / self.current * (self.total - self.current))
            if self.current > 0 else None,
        }
