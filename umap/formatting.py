"""
uMap Telegram formatting.

Covers the default (bike / plans) notification format, the change-description
builder, and the full walking-route formatter (structured leg fields plus the
legacy `Instruction` placeholder format).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from umap.config import (
    PLANNED_PROPERTY,
    SOURCE_LAYER_ID_PROPERTY,
    SOURCE_LAYER_TITLE_PROPERTY,
    EmojiSpec,
    LayerStatus,
    RouteFeature,
    RouteSnapshot,
    WatchedLayer,
    feature_length_km,
    property_bool,
)

# --------------------------------------------------------------------------- #
# Emoji helpers
# --------------------------------------------------------------------------- #

CHANGES_SECTION_EMOJI_ID = "5244758760429213978"
CHANGED_VALUE_EMOJI_ID = "5395748666432568078"
NAME_EMOJI_ID = "5929468240668397096"
LENGTH_EMOJI_ID = "5339573565102515237"
OPEN_ROUTE_BUTTON_TEXT = "📌 Открыть маршрут"
ROUTE_EMOJI_IDS = {
    "changes": CHANGES_SECTION_EMOJI_ID,
    "length": LENGTH_EMOJI_ID,
    "name": NAME_EMOJI_ID,
}


def tg_emoji(emoji_id: str, fallback_emoji: str) -> str:
    if not emoji_id:
        return fallback_emoji
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback_emoji}</tg-emoji>'


def tg_emojis(emojis: tuple[EmojiSpec, ...]) -> str:
    return "".join(tg_emoji(emoji_id, fallback_emoji) for emoji_id, fallback_emoji in emojis)


def route_emoji(name: str, fallback_emoji: str) -> str:
    return tg_emoji(ROUTE_EMOJI_IDS.get(name, ""), fallback_emoji)


# --------------------------------------------------------------------------- #
# Walk constants
# --------------------------------------------------------------------------- #

PLACEHOLDER_RE = re.compile(r"\((Routes|Transport)\s+(\d+)\)", re.IGNORECASE)
TRANSPORT_HINT_RE = re.compile(r"\b(bus|train|tram|trolleybus|rail)\b", re.IGNORECASE)
PLANNED_NEW_NOTIFICATION_TITLE = "Новый план маршрута добавлен"
PLANNED_CHANGE_NOTIFICATION_TITLE = "Изменен план маршрута"
PLANNED_NEW_NOTIFICATION_EMOJIS = (("5958798052641738769", "📱"),)
PLANNED_CHANGE_NOTIFICATION_EMOJIS = (
    ("5395444784611480792", "✏️"),
    ("5958798052641738769", "📱"),
)
TRANSPORT_EMOJI_KEYS = {
    "🚌": "bus",
    "🚆": "train",
    "🚋": "tram",
    "🚎": "trolleybus",
}


@dataclass(frozen=True)
class RouteLeg:
    section: str
    route: str
    emoji: str
    path: str = ""
    duration: str = ""


# --------------------------------------------------------------------------- #
# Walk value helpers
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Walk parsing
# --------------------------------------------------------------------------- #


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
            current = RouteLeg(
                section=section, route=strip_transport_hints(line), emoji=transport_emoji(line)
            )
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


# --------------------------------------------------------------------------- #
# Walk rendering
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Walk notifications
# --------------------------------------------------------------------------- #


def format_walk_route_notification(layer: Any, feature: Any) -> str:
    return format_walk_route_message(
        layer=layer,
        feature=feature,
        title=walk_notification_title(layer, feature, change=False),
        emojis=walk_notification_emojis(layer, feature, change=False),
    )


def format_walk_route_change_notification(layer: Any, feature: Any, changes_html: list[str]) -> str:
    return format_walk_route_message(
        layer=layer,
        feature=feature,
        title=walk_notification_title(layer, feature, change=True),
        emojis=walk_notification_emojis(layer, feature, change=True),
        changes_html=changes_html,
    )


def walk_notification_title(layer: Any, feature: Any, *, change: bool) -> str:
    if is_planned_walk_route(feature):
        return PLANNED_CHANGE_NOTIFICATION_TITLE if change else PLANNED_NEW_NOTIFICATION_TITLE
    return layer.change_notification_title if change else layer.new_notification_title


def walk_notification_emojis(
    layer: Any, feature: Any, *, change: bool
) -> tuple[tuple[str, str], ...]:
    if is_planned_walk_route(feature):
        return PLANNED_CHANGE_NOTIFICATION_EMOJIS if change else PLANNED_NEW_NOTIFICATION_EMOJIS
    return layer.change_notification_emojis if change else layer.new_notification_emojis


def is_planned_walk_route(feature: Any) -> bool:
    if getattr(feature, "planned", False):
        return True
    properties = getattr(feature, "properties", {})
    return isinstance(properties, dict) and property_bool(properties, PLANNED_PROPERTY)


def format_walk_route_message(
    *,
    layer: Any,
    feature: Any,
    title: str,
    emojis: tuple[tuple[str, str], ...],
    changes_html: list[str] | None = None,
) -> str:
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

    lines = [f"{tg_emojis(emojis)} <b>{html.escape(title)}</b>"]
    if title_details:
        lines.append(f"{route_emoji('name', '🚩')} <b>{escape_text(title_details)}</b>")
    elif feature.name:
        lines.append(f"{route_emoji('name', '🚩')} <b>{html.escape(feature.name)}</b>")

    source_layer_title = str(properties.get(SOURCE_LAYER_TITLE_PROPERTY) or "").strip()
    if source_layer_title:
        lines.append(
            f"{route_emoji('vald', '🏘')} <b>Область:</b> {escape_text(source_layer_title)}"
        )

    length_km = feature_length_km(feature)
    if length_km:
        lines.append(f"{route_emoji('length', '🛣')} <b>Длина:</b> {length_km:.2f} км")

    details = format_walk_details_block(legs)
    notes = str(properties.get("notes") or properties.get("Notes") or "").strip()
    if details:
        lines.extend(["", "<b>Детали:</b>", f"<blockquote expandable>{details}</blockquote>"])
    elif raw_routes:
        routes = [strip_transport_hints(route) for route in raw_routes]
        lines.extend(["", f"{route_emoji('routes', '🗺')} <b>{escape_text(', '.join(routes))}</b>"])

    if notes:
        lines.extend(["", f"{route_emoji('notes', '📝')} {escape_text(notes)}"])

    if changes_html:
        lines.extend(["", f"{route_emoji('changes', '🔄')} <b>Что изменилось:</b>"])
        lines.extend(changes_html)

    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# Default (bike / plans) notifications + dispatchers
# --------------------------------------------------------------------------- #


def format_route_notification(layer: WatchedLayer, feature: RouteFeature) -> str:
    if layer.formatter == "walk":
        return format_walk_route_notification(layer, feature)
    return format_default_route_notification(layer, feature)


def format_default_route_notification(layer: WatchedLayer, feature: RouteFeature) -> str:
    lines = [
        f"{tg_emojis(layer.new_notification_emojis)} "
        f"<b>{html.escape(layer.new_notification_title)}</b>",
        f"{route_emoji('name', '🚩')} <b>{html.escape(feature.name)}</b>",
    ]

    length_km = feature_length_km(feature)
    if length_km is not None:
        lines.append(f"{route_emoji('length', '🛣')} <b>Длина:</b> {length_km:.1f} км")

    return "\n".join(lines)


def format_route_change_notification(
    layer: WatchedLayer,
    feature: RouteFeature,
    changes_html: list[str],
) -> str:
    if layer.formatter == "walk":
        return format_walk_route_change_notification(layer, feature, changes_html)

    lines = [
        f"{tg_emojis(layer.change_notification_emojis)} "
        f"<b>{html.escape(layer.change_notification_title)}</b>",
        f"{route_emoji('name', '🚩')} <b>{html.escape(feature.name)}</b>",
    ]

    source_layer_title = str(feature.properties.get(SOURCE_LAYER_TITLE_PROPERTY) or "").strip()
    if source_layer_title:
        lines.append(
            f"{route_emoji('vald', '🏘')} <b>Область:</b> {html.escape(source_layer_title)}"
        )

    length_km = feature_length_km(feature)
    if length_km is not None:
        lines.append(f"{route_emoji('length', '🛣')} <b>Длина:</b> {length_km:.1f} км")

    if changes_html:
        lines.append("")
        lines.append(f"{route_emoji('changes', '🔄')} <b>Что изменилось:</b>")
        lines.extend(changes_html)

    return "\n".join(lines)


def build_feature_url(map_url: str, layer: WatchedLayer, feature: RouteFeature) -> str | None:
    slug = feature.name.strip()
    if not map_url or not slug:
        return None

    parsed = urlsplit(map_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    source_layer_id = str(feature.properties.get(SOURCE_LAYER_ID_PROPERTY) or "").strip()
    datalayer_id = layer.layer_id or source_layer_id
    if datalayer_id:
        query["datalayers"] = datalayer_id
    query["feature"] = slug
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query, doseq=True),
            parsed.fragment,
        )
    )


def format_status_message(
    *,
    layer_statuses: list[LayerStatus],
    subscriber_count: int,
    poll_interval_seconds: int,
    change_poll_interval_seconds: int,
) -> str:
    lines = [
        "<b>Статус бота</b>",
        f"<b>Подписчиков:</b> {subscriber_count}",
        f"<b>Интервал новых маршрутов:</b> {poll_interval_seconds} сек",
        f"<b>Интервал изменений:</b> {change_poll_interval_seconds} сек",
    ]

    for layer_status in layer_statuses:
        last_checked = (
            html.escape(layer_status.last_checked_at)
            if layer_status.last_checked_at
            else "еще не было"
        )
        last_change_checked = (
            html.escape(layer_status.last_change_checked_at)
            if layer_status.last_change_checked_at
            else "еще не было"
        )
        lines.extend(
            [
                "",
                f"<b>Слой {html.escape(layer_status.title)}</b>",
                f"<b>Последняя проверка новых маршрутов:</b> {last_checked}",
                f"<b>Последняя проверка изменений:</b> {last_change_checked}",
                f"<b>Маршрутов в текущем слое:</b> {layer_status.current_feature_count}",
                f"<b>Известных ID:</b> {layer_status.known_feature_count}",
                f'<b>JSON слоя:</b> <a href="'
                f'{html.escape(layer_status.datalayer_url, quote=True)}">'
                f"открыть JSON</a>",
            ]
        )

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Route change descriptions
# --------------------------------------------------------------------------- #


def build_change_descriptions(
    layer: WatchedLayer,
    previous: RouteSnapshot,
    current: RouteSnapshot,
) -> list[str]:
    changes: list[str] = []

    if previous.name != current.name:
        changes.append(
            "• Название: "
            f"<s>{html.escape(display_value(previous.name, 'без названия'))}</s> "
            f"{tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
            f"{html.escape(display_value(current.name, 'без названия'))}"
        )

    if previous.month != current.month:
        changes.append(
            "• Месяц: "
            f"<s>{html.escape(display_value(previous.month, 'не указан'))}</s> "
            f"{tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
            f"{html.escape(display_value(current.month, 'не указан'))}"
        )

    if previous.osmand_speed != current.osmand_speed:
        changes.append(
            "• Скорость Osmand: "
            f"<s>{html.escape(display_value(previous.osmand_speed, 'не указана'))}</s> "
            f"{tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
            f"{html.escape(display_value(current.osmand_speed, 'не указана'))}"
        )

    if previous.planned != current.planned:
        changes.append(
            "• В планах: "
            f"<s>{format_bool(previous.planned)}</s> "
            f"{tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
            f"{format_bool(current.planned)}"
        )

    if previous.description != current.description:
        changes.append(
            "• Описание: "
            f"<s>{html.escape(display_value(previous.description, 'пусто'))}</s> "
            f"{tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
            f"{html.escape(display_value(current.description, 'пусто'))}"
        )

    if (
        previous.details_hash
        and current.details_hash
        and previous.details_hash != current.details_hash
    ):
        changes.append("• Детали маршрута обновлены.")

    if previous.geometry_type != current.geometry_type:
        changes.append(
            "• Тип геометрии: "
            f"<s>{html.escape(previous.geometry_type)}</s> "
            f"{tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
            f"{html.escape(current.geometry_type)}"
        )

    if length_changed(previous.length_km, current.length_km):
        changes.append(
            "• Длина: "
            f"<s>{html.escape(format_length(previous.length_km))}</s> "
            f"{tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
            f"{html.escape(format_length(current.length_km))}"
        )

    if previous.geometry_hash != current.geometry_hash:
        changes.append("• Геометрия маршрута обновлена.")

    return changes or [f"• {layer.route_label.capitalize()} был обновлен."]


def display_value(value: str, fallback: str) -> str:
    normalized = value.strip()
    return normalized if normalized else fallback


def length_changed(previous: float | None, current: float | None) -> bool:
    if previous is None or current is None:
        return previous != current
    return abs(previous - current) >= 0.1


def format_length(value: float | None) -> str:
    if value is None:
        return "неизвестно"
    return f"{value:.1f} км"


def format_bool(value: bool) -> str:
    return "да" if value else "нет"
