from __future__ import annotations

import html
from typing import Any

from umap.formatting import route_emoji, tg_emojis
from umap.models import (
    PLANNED_PROPERTY,
    SOURCE_LAYER_TITLE_PROPERTY,
    feature_length_km,
    property_bool,
)
from umap.walk.constants import (
    PLANNED_CHANGE_NOTIFICATION_EMOJIS,
    PLANNED_CHANGE_NOTIFICATION_TITLE,
    PLANNED_NEW_NOTIFICATION_EMOJIS,
    PLANNED_NEW_NOTIFICATION_TITLE,
)
from umap.walk.parsing import (
    infer_destination,
    parse_instruction_legs,
    parse_structured_walk_legs,
    replace_placeholders,
)
from umap.walk.rendering import format_walk_details_block
from umap.walk.values import escape_text, format_route_date, split_values, strip_transport_hints


def format_route_notification(layer: Any, feature: Any) -> str:
    return format_walk_route_message(
        layer=layer,
        feature=feature,
        title=walk_notification_title(layer, feature, change=False),
        emojis=walk_notification_emojis(layer, feature, change=False),
    )


def format_route_change_notification(layer: Any, feature: Any, changes_html: list[str]) -> str:
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


def walk_notification_emojis(layer: Any, feature: Any, *, change: bool) -> tuple[tuple[str, str], ...]:
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
        lines.append(f"{route_emoji('vald', '🏘')} <b>Область:</b> {escape_text(source_layer_title)}")

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
