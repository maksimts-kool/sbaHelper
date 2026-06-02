from __future__ import annotations

import threading


class ProgressStage:
    def __init__(self, stage: str = "download") -> None:
        self._stage = stage
        self._version = 0
        self._lock = threading.Lock()

    def snapshot(self) -> tuple[str, int]:
        with self._lock:
            return self._stage, self._version

    def is_current(self, stage: str, version: int) -> bool:
        with self._lock:
            return self._stage == stage and self._version == version

    def advance(self, stage: str) -> None:
        with self._lock:
            self._stage = stage
            self._version += 1


def rounded_progress_percent(raw_percent: float) -> int:
    bounded = min(100, max(0, int(raw_percent)))
    return (bounded // 10) * 10
