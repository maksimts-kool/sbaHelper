from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteLeg:
    section: str
    route: str
    emoji: str
    path: str = ""
    duration: str = ""
