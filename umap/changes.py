from __future__ import annotations

import html

from umap.formatting import CHANGED_VALUE_EMOJI_ID, tg_emoji
from umap.models import RouteSnapshot
from umap.settings import WatchedLayer


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

    if previous.details_hash and current.details_hash and previous.details_hash != current.details_hash:
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
