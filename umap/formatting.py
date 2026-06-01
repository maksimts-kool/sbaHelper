from __future__ import annotations

import html
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from umap.models import (
    SOURCE_LAYER_ID_PROPERTY,
    SOURCE_LAYER_TITLE_PROPERTY,
    LayerStatus,
    RouteFeature,
    feature_length_km,
)
from umap.settings import EmojiSpec, WatchedLayer


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


def format_route_notification(layer: WatchedLayer, feature: RouteFeature) -> str:
    if layer.formatter == "walk":
        from umap.walk.formatter import format_route_notification as format_walk_route_notification

        return format_walk_route_notification(layer, feature)
    return format_default_route_notification(layer, feature)


def format_default_route_notification(layer: WatchedLayer, feature: RouteFeature) -> str:
    lines = [
        f"{tg_emojis(layer.new_notification_emojis)} <b>{html.escape(layer.new_notification_title)}</b>",
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
        from umap.walk.formatter import format_route_change_notification as format_walk_route_change_notification

        return format_walk_route_change_notification(layer, feature, changes_html)

    lines = [
        f"{tg_emojis(layer.change_notification_emojis)} <b>{html.escape(layer.change_notification_title)}</b>",
        f"{route_emoji('name', '🚩')} <b>{html.escape(feature.name)}</b>",
    ]

    source_layer_title = str(feature.properties.get(SOURCE_LAYER_TITLE_PROPERTY) or "").strip()
    if source_layer_title:
        lines.append(f"{route_emoji('vald', '🏘')} <b>Область:</b> {html.escape(source_layer_title)}")

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
                f'<b>JSON слоя:</b> <a href="{html.escape(layer_status.datalayer_url, quote=True)}">открыть JSON</a>',
            ]
        )

    return "\n".join(lines)
