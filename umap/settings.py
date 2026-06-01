from __future__ import annotations

import os
import re
from dataclasses import dataclass


EmojiSpec = tuple[str, str]


@dataclass(slots=True, frozen=True)
class WatchedLayer:
    key: str
    title: str
    map_id: str
    map_url: str
    layer_id: str
    formatter: str
    route_label: str
    new_notification_title: str
    change_notification_title: str
    new_notification_emojis: tuple[EmojiSpec, ...]
    change_notification_emojis: tuple[EmojiSpec, ...]


@dataclass(slots=True)
class BotSettings:
    telegram_bot_token: str
    default_subscriber_chat_id: int | None
    state_mongodb_uri: str
    state_mongodb_database: str
    state_mongodb_collection: str
    umap_base_url: str
    watched_layers: tuple[WatchedLayer, ...]
    poll_interval_seconds: int
    change_poll_interval_seconds: int
    request_timeout_seconds: float
    request_retry_attempts: int
    request_retry_backoff_seconds: float
    bootstrap_notify_existing: bool
    log_level: str

    def build_datalayer_url(self, layer: WatchedLayer) -> str:
        base = self.umap_base_url.rstrip("/")
        return f"{base}/en/datalayer/{layer.map_id}/{layer.layer_id}/"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _split_env_list(raw_value: str) -> list[str]:
    if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {"'", '"'}:
        raw_value = raw_value[1:-1].strip()
    delimiter = ";" if ";" in raw_value else ","
    return [part.strip() for part in raw_value.split(delimiter) if part.strip()]


def _parse_named_layer_list(raw_value: str) -> list[tuple[str, str]]:
    layers: list[tuple[str, str]] = []
    for index, item in enumerate(_split_env_list(raw_value), start=1):
        if "=" in item:
            title, layer_id = item.split("=", 1)
            title = title.strip()
            layer_id = layer_id.strip()
        else:
            title = f"Пешие маршруты {index}"
            layer_id = item.strip()

        if layer_id:
            layers.append((title or f"Пешие маршруты {index}", layer_id))

    return layers


def _layer_key_suffix(layer_id: str, index: int) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", layer_id.lower()).strip("-")
    return normalized[:12] or str(index)


def _build_walk_layers(
    *,
    walk_map_id: str,
    walk_map_url: str,
    primary_walk_layer_id: str,
    configured_walk_layers: str,
) -> list[WatchedLayer]:
    if configured_walk_layers:
        layer_specs = _parse_named_layer_list(configured_walk_layers)
    elif primary_walk_layer_id:
        layer_specs = [("Пешие маршруты", primary_walk_layer_id)]
    else:
        layer_specs = []

    watched_layers: list[WatchedLayer] = []
    used_keys: set[str] = set()
    primary_key_assigned = False

    for index, (title, layer_id) in enumerate(layer_specs, start=1):
        if not walk_map_id:
            continue

        is_primary = (
            bool(primary_walk_layer_id)
            and layer_id == primary_walk_layer_id
            and not primary_key_assigned
        ) or (not primary_walk_layer_id and index == 1)
        base_key = "walk" if is_primary else f"walk-{_layer_key_suffix(layer_id, index)}"
        key = base_key
        duplicate_index = 2
        while key in used_keys:
            key = f"{base_key}-{duplicate_index}"
            duplicate_index += 1

        used_keys.add(key)
        if key == "walk":
            primary_key_assigned = True

        watched_layers.append(
            WatchedLayer(
                key=key,
                title=title,
                map_id=walk_map_id,
                map_url=walk_map_url,
                layer_id=layer_id,
                formatter="walk",
                route_label="пеший маршрут",
                new_notification_title="Новый пеший маршрут добавлен!",
                change_notification_title="Изменен пеший маршрут!",
                new_notification_emojis=(("5397916757333654639", "➕"),),
                change_notification_emojis=(("5395444784611480792", "✏️"),),
            )
        )

    return watched_layers


def load_bot_settings() -> BotSettings:
    chat_id = env("DEFAULT_SUBSCRIBER_CHAT_ID")
    bike_map_id = env("UMAP_BIKE_MAP_ID") or "1393155"
    bike_map_url = f"http://u.osmfr.org/m/{bike_map_id}/"
    bike_layer_id = env("UMAP_BIKE_LAYER_ID") or "82e959ea-1b66-4088-b7f3-8fe6a06c3c4a"
    walk_map_id = env("UMAP_WALK_MAP_ID")
    walk_map_url = f"http://u.osmfr.org/m/{walk_map_id}/" if walk_map_id else ""
    walk_layer_id = env("UMAP_WALK_LAYER_ID")
    walk_layers = env("UMAP_WALK_LAYERS")
    plans_layer_id = env("UMAP_BIKE_PLANS_LAYER_ID")

    watched_layers = [
        WatchedLayer(
            key="2026",
            title="2026",
            map_id=bike_map_id,
            map_url=bike_map_url,
            layer_id=bike_layer_id,
            formatter="bike",
            route_label="веломаршрут",
            new_notification_title="Новый веломаршрут добавлен!",
            change_notification_title="Изменен веломаршрут!",
            new_notification_emojis=(("5397916757333654639", "➕"),),
            change_notification_emojis=(("5395444784611480792", "✏️"),),
        )
    ]
    watched_layers.extend(
        _build_walk_layers(
            walk_map_id=walk_map_id,
            walk_map_url=walk_map_url,
            primary_walk_layer_id=walk_layer_id,
            configured_walk_layers=walk_layers,
        )
    )
    if plans_layer_id:
        watched_layers.append(
            WatchedLayer(
                key="plans",
                title="В планах",
                map_id=bike_map_id,
                map_url=bike_map_url,
                layer_id=plans_layer_id,
                formatter="bike",
                route_label="план маршрута",
                new_notification_title="Новый план маршрута добавлен",
                change_notification_title="Изменен план маршрута",
                new_notification_emojis=(("5958798052641738769", "📱"),),
                change_notification_emojis=(
                    ("5395444784611480792", "✏️"),
                    ("5958798052641738769", "📱"),
                ),
            )
        )

    return BotSettings(
        telegram_bot_token=required_env("TELEGRAM_BOT_TOKEN"),
        default_subscriber_chat_id=int(chat_id) if chat_id else None,
        state_mongodb_uri=required_env("UMAP_STATE_MONGODB_URI"),
        state_mongodb_database=env("UMAP_STATE_MONGODB_DATABASE", "sbahelper"),
        state_mongodb_collection=env("UMAP_STATE_MONGODB_COLLECTION", "umap_state"),
        umap_base_url=env("UMAP_BASE_URL", "https://umap.openstreetmap.fr"),
        watched_layers=tuple(watched_layers),
        poll_interval_seconds=int(env("POLL_INTERVAL_SECONDS", "300")),
        change_poll_interval_seconds=int(env("CHANGE_POLL_INTERVAL_SECONDS", "3600")),
        request_timeout_seconds=float(env("REQUEST_TIMEOUT_SECONDS", "30")),
        request_retry_attempts=max(1, int(env("REQUEST_RETRY_ATTEMPTS", "3"))),
        request_retry_backoff_seconds=max(0.0, float(env("REQUEST_RETRY_BACKOFF_SECONDS", "2"))),
        bootstrap_notify_existing=env_bool("BOOTSTRAP_NOTIFY_EXISTING", False),
        log_level=env("LOG_LEVEL", "INFO").upper(),
    )
