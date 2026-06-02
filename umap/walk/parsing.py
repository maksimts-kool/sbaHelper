from __future__ import annotations

import re
from typing import Any

from umap.walk.constants import PLACEHOLDER_RE
from umap.walk.models import RouteLeg
from umap.walk.values import (
    first_property_value,
    format_duration,
    strip_transport_hints,
    transport_emoji,
)


def replace_placeholders(instruction: str, routes: list[str], transports: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        field = match.group(1).lower()
        index = int(match.group(2)) - 1
        values = routes if field == "routes" else transports
        if 0 <= index < len(values):
            return values[index]
        return match.group(0)

    return PLACEHOLDER_RE.sub(replace, instruction)


def parse_structured_walk_legs(properties: dict[str, Any]) -> list[RouteLeg]:
    legs: list[RouteLeg] = []
    for section in ("there", "back"):
        for index in range(1, 21):
            prefix = f"{section}_{index}_"
            route = first_property_value(properties, f"{prefix}route", f"{prefix}routes")
            transport = first_property_value(properties, f"{prefix}transport", f"{prefix}type")
            origin = first_property_value(properties, f"{prefix}from", f"{prefix}origin")
            destination = first_property_value(properties, f"{prefix}to", f"{prefix}destination")
            duration = first_property_value(
                properties,
                f"{prefix}duration",
                f"{prefix}minutes",
                f"{prefix}time",
            )

            if not any((route, transport, origin, destination, duration)):
                continue

            route_text = strip_transport_hints(route or transport or "Переход")
            path = " - ".join(part for part in (origin, destination) if part)
            legs.append(
                RouteLeg(
                    section=section,
                    route=route_text,
                    emoji=transport_emoji(" ".join(part for part in (route, transport) if part)),
                    path=path,
                    duration=format_duration(duration),
                )
            )
    return legs


def parse_instruction_legs(instruction: str) -> list[RouteLeg]:
    legs: list[RouteLeg] = []
    section = ""
    current: RouteLeg | None = None

    for raw_line in instruction.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lower = line.rstrip(":").lower()
        if lower == "there":
            if current:
                legs.append(current)
                current = None
            section = "there"
            continue
        if lower == "back":
            if current:
                legs.append(current)
                current = None
            section = "back"
            continue

        if looks_like_transport_leg(line):
            if current:
                legs.append(current)
            current = RouteLeg(section=section, route=strip_transport_hints(line), emoji=transport_emoji(line))
            continue

        if looks_like_duration(line):
            if current:
                legs.append(
                    RouteLeg(
                        section=current.section,
                        route=current.route,
                        emoji=current.emoji,
                        path=current.path,
                        duration=line,
                    )
                )
                current = None
            continue

        if " - " in line and current:
            current = RouteLeg(
                section=current.section,
                route=current.route,
                emoji=current.emoji,
                path=line,
                duration=current.duration,
            )

    if current:
        legs.append(current)

    return legs


def infer_destination(legs: list[RouteLeg]) -> str:
    there_paths = [leg.path for leg in legs if leg.section == "there" and " - " in leg.path]
    if not there_paths:
        return ""
    return there_paths[-1].split(" - ", 1)[1].strip()


def looks_like_duration(line: str) -> bool:
    return bool(re.fullmatch(r"\d+\s*(min|mins|minutes|m|мин|минут[а-я]*)", line, re.IGNORECASE))


def looks_like_transport_leg(line: str) -> bool:
    lower = line.lower()
    if any(token in lower for token in ("bus", "train", "tram", "trolleybus")):
        return True
    return " - " in line and bool(re.search(r"\b(r\d+|\d+[a-z]?)\b\s+-", lower))
