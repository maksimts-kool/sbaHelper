from __future__ import annotations

from umap.formatting import route_emoji
from umap.walk.models import RouteLeg
from umap.walk.values import escape_text, format_leg_emoji


def format_walk_details_block(legs: list[RouteLeg]) -> str:
    there = [leg for leg in legs if leg.section == "there"]
    back = [leg for leg in legs if leg.section == "back"]
    if not there and not back:
        return ""

    parts: list[str] = []
    if there:
        parts.append(f"{route_emoji('there', '🚶')} <b>Туда:</b>")
        parts.append("")
        parts.extend(format_walk_leg_lines(there))

    if there and back:
        parts.append("")
        parts.append("────────")
        parts.append("")

    if back:
        parts.append(f"{route_emoji('back', '🏠')} <b>Обратно:</b>")
        parts.append("")
        parts.extend(format_walk_leg_lines(back))

    return "\n".join(parts).strip()


def format_walk_leg_lines(legs: list[RouteLeg]) -> list[str]:
    lines: list[str] = []
    for index, leg in enumerate(legs):
        if index:
            lines.append("")
        emoji = format_leg_emoji(leg.emoji)
        title = f"<b>{escape_text(leg.route)}</b>"
        lines.append(f"{emoji} {title}" if emoji else title)
        if leg.path:
            lines.append(f"{route_emoji('path', '📍')} {escape_text(leg.path)}")
        if leg.duration:
            lines.append(f"{route_emoji('duration', '⏱')} {escape_text(leg.duration)}")
    return lines
