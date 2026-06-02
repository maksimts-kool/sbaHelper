from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from umap.formatting import route_emoji
from umap.walk.constants import TRANSPORT_EMOJI_KEYS, TRANSPORT_HINT_RE


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
    if "trolleybus" in lower:
        return "🚎"
    if "train" in lower or "rail" in lower or re.search(r"\br\d+", lower):
        return "🚆"
    if "bus" in lower:
        return "🚌"
    if "tram" in lower:
        return "🚋"
    return ""


def format_leg_emoji(emoji: str) -> str:
    return route_emoji(TRANSPORT_EMOJI_KEYS.get(emoji, "transport"), emoji)


def escape_text(value: str) -> str:
    return html.escape(value, quote=False)
