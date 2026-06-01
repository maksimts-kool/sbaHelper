from __future__ import annotations

import os
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
        if not layer.layer_id:
            return f"{base}/en/map/{layer.map_id}/geojson/"
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


def load_bot_settings() -> BotSettings:
    chat_id = env("DEFAULT_SUBSCRIBER_CHAT_ID")
    bike_map_id = env("UMAP_BIKE_MAP_ID") or "1393155"
    bike_map_url = f"http://u.osmfr.org/m/{bike_map_id}/"
    bike_layer_id = env("UMAP_BIKE_LAYER_ID") or "82e959ea-1b66-4088-b7f3-8fe6a06c3c4a"
    walk_map_id = env("UMAP_WALK_MAP_ID")
    walk_map_url = f"http://u.osmfr.org/m/{walk_map_id}/" if walk_map_id else ""
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
    if walk_map_id:
        watched_layers.append(
            WatchedLayer(
                key="walk",
                title="Пешие маршруты",
                map_id=walk_map_id,
                map_url=walk_map_url,
                layer_id="",
                formatter="walk",
                route_label="пеший маршрут",
                new_notification_title="Новый пеший маршрут добавлен!",
                change_notification_title="Изменен пеший маршрут!",
                new_notification_emojis=(("5397916757333654639", "➕"),),
                change_notification_emojis=(("5395444784611480792", "✏️"),),
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
