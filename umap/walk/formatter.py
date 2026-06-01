from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from umap.formatting import tg_emojis
from umap.models import feature_length_km


PLACEHOLDER_RE = re.compile(r"\((Routes|Transport)\s+(\d+)\)", re.IGNORECASE)
TRANSPORT_HINT_RE = re.compile(r"\b(bus|train|tram|metro|rail)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RouteLeg:
    section: str
    route: str
    emoji: str
    path: str = ""
    duration: str = ""


def format_route_notification(layer: Any, feature: Any) -> str:
    properties = feature.properties
    raw_routes = split_values(properties.get("Routes"))
    transports = split_values(properties.get("Transport"))
    instruction = str(properties.get("Instruction") or "").strip()
    instruction = replace_placeholders(instruction, routes=raw_routes, transports=transports)
    legs = parse_structured_walk_legs(properties)
    if not legs:
        legs = parse_instruction_legs(instruction)

    route_date = format_route_date(properties.get("date") or properties.get("time"))
    destination = (
        str(properties.get("destination_name") or "").strip()
        or str(properties.get("name") or "").strip()
        or infer_destination(legs)
    )
    title_details = " ".join(part for part in (route_date, destination) if part)

    lines = [f"{tg_emojis(layer.new_notification_emojis)} <b>{html.escape(layer.new_notification_title)}</b>"]
    if title_details:
        lines.append(f"🚩 <b>{escape_text(title_details)}</b>")
    elif feature.name:
        lines.append(f"🚩 <b>{html.escape(feature.name)}</b>")

    length_km = feature_length_km(feature)
    if length_km:
        lines.append(f"🛣 <b>Длина:</b> {length_km:.2f} км")

    details = format_walk_details_block(legs)
    notes = str(properties.get("notes") or properties.get("Notes") or "").strip()
    if details:
        lines.extend(["", "<b>Детали:</b>", f"<blockquote expandable>{details}</blockquote>"])
    elif raw_routes:
        routes = [strip_transport_hints(route) for route in raw_routes]
        lines.extend(["", f"🗺 <b>{escape_text(', '.join(routes))}</b>"])

    if notes:
        lines.extend(["", f"📝 {escape_text(notes)}"])

    return "\n".join(lines).strip()


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

        if _looks_like_transport_leg(line):
            if current:
                legs.append(current)
            current = RouteLeg(section=section, route=strip_transport_hints(line), emoji=transport_emoji(line))
            continue

        if _looks_like_duration(line):
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


def format_walk_details_block(legs: list[RouteLeg]) -> str:
    there = [leg for leg in legs if leg.section == "there"]
    back = [leg for leg in legs if leg.section == "back"]
    if not there and not back:
        return ""

    parts: list[str] = []
    if there:
        parts.append("🚶 <b>Туда:</b>")
        parts.append("")
        parts.extend(format_walk_leg_lines(there))

    if there and back:
        parts.append("")
        parts.append("────────")
        parts.append("")

    if back:
        parts.append("🏠 <b>Обратно:</b>")
        parts.append("")
        parts.extend(format_walk_leg_lines(back))

    return "\n".join(parts).strip()


def format_walk_leg_lines(legs: list[RouteLeg]) -> list[str]:
    lines: list[str] = []
    for index, leg in enumerate(legs):
        if index:
            lines.append("")
        lines.append(f"{leg.emoji} <b>{escape_text(leg.route)}</b>")
        if leg.path:
            lines.append(f"📍 {escape_text(leg.path)}")
        if leg.duration:
            lines.append(f"⏱ {escape_text(leg.duration)}")
    return lines


def infer_destination(legs: list[RouteLeg]) -> str:
    there_paths = [leg.path for leg in legs if leg.section == "there" and " - " in leg.path]
    if not there_paths:
        return ""
    return there_paths[-1].split(" - ", 1)[1].strip()


def format_route_date(raw_date: Any) -> str:
    if not raw_date:
        return ""
    try:
        parsed = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")).date()
    except ValueError:
        return str(raw_date)
    return parsed.strftime("%d.%m.%y")


def split_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def first_property_value(properties: dict[str, Any], *names: str) -> str:
    lower_properties = {str(key).lower(): value for key, value in properties.items()}
    for name in names:
        value = lower_properties.get(name.lower())
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def format_duration(value: str) -> str:
    if not value:
        return ""
    if re.fullmatch(r"\d+", value):
        return f"{value} min"
    return value


def strip_transport_hints(value: str) -> str:
    without_hints = TRANSPORT_HINT_RE.sub("", value)
    without_dangling_separators = re.sub(r"\s+-\s*$", "", without_hints)
    without_dangling_separators = re.sub(r"^\s*-\s+", "", without_dangling_separators)
    return re.sub(r"\s+", " ", without_dangling_separators).strip()


def transport_emoji(line: str) -> str:
    lower = line.lower()
    if "train" in lower or "rail" in lower or re.search(r"\br\d+", lower):
        return "🚆"
    if "bus" in lower:
        return "🚌"
    if "tram" in lower:
        return "🚋"
    if "metro" in lower:
        return "🚇"
    return "➡️"


def escape_text(value: str) -> str:
    return html.escape(value, quote=False)


def _looks_like_duration(line: str) -> bool:
    return bool(re.fullmatch(r"\d+\s*(min|mins|minutes|m|мин|минут[а-я]*)", line, re.IGNORECASE))


def _looks_like_transport_leg(line: str) -> bool:
    lower = line.lower()
    if any(token in lower for token in ("bus", "train", "tram", "metro")):
        return True
    return " - " in line and bool(re.search(r"\b(r\d+|\d+[a-z]?)\b\s+-", lower))
