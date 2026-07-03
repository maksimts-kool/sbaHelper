"""
uMap bot configuration: environment settings, watched-layer definitions,
route data models / GeoJSON helpers, and Sentry / transient-error handling.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from aiogram.exceptions import TelegramNetworkError

from shared import DEFAULT_TRANSIENT_ERROR_TEXT, SentryTracker, is_transient_error

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Route data models + GeoJSON helpers
# --------------------------------------------------------------------------- #

SOURCE_LAYER_ID_PROPERTY = "_umap_layer_id"
SOURCE_LAYER_TITLE_PROPERTY = "_umap_layer_title"
PLANNED_PROPERTY = "planned"
TRUTHY_PROPERTY_VALUES = {"1", "true", "yes", "y", "on", "checked"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect_geojson_features(umap_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract features from uMap datalayers, full downloads, or single GeoJSON features."""
    raw_features: list[dict[str, Any]] = []

    if umap_data.get("type") == "FeatureCollection":
        raw_features.extend(
            _tag_features_with_layer(
                umap_data.get("features", []),
                layer_id=umap_data.get("id"),
                layer_title=(umap_data.get("properties") or {}).get("name"),
            )
        )
    elif umap_data.get("type") == "Feature":
        if not isinstance((umap_data.get("properties") or {}).get("datalayers"), list):
            raw_features.append(umap_data)
    elif "layers" in umap_data:
        for layer in umap_data.get("layers", []):
            if isinstance(layer, dict):
                layer_properties = layer.get("properties") or {}
                raw_features.extend(
                    _tag_features_with_layer(
                        layer.get("features", []),
                        layer_id=layer.get("id"),
                        layer_title=layer.get("name") or layer_properties.get("name"),
                    )
                )

    return [feature for feature in raw_features if isinstance(feature, dict)]


def _tag_features_with_layer(
    features: Any,
    *,
    layer_id: Any,
    layer_title: Any,
) -> list[dict[str, Any]]:
    tagged_features: list[dict[str, Any]] = []
    source_layer_id = str(layer_id or "").strip()
    source_layer_title = str(layer_title or "").strip()

    for feature in features if isinstance(features, list) else []:
        if not isinstance(feature, dict):
            continue
        tagged_feature = dict(feature)
        properties = dict(tagged_feature.get("properties") or {})
        if source_layer_id:
            properties.setdefault(SOURCE_LAYER_ID_PROPERTY, source_layer_id)
        if source_layer_title:
            properties.setdefault(SOURCE_LAYER_TITLE_PROPERTY, source_layer_title)
        tagged_feature["properties"] = properties
        tagged_features.append(tagged_feature)

    return tagged_features


@dataclass(slots=True)
class RouteFeature:
    feature_id: str
    name: str
    description: str
    month: str
    osmand_speed: str
    geometry_type: str
    geometry: dict[str, Any]
    properties: dict[str, Any]
    planned: bool = False


@dataclass(slots=True)
class RouteSnapshot:
    feature_id: str
    name: str
    description: str
    month: str
    osmand_speed: str
    geometry_type: str
    geometry_hash: str
    details_hash: str
    length_km: float | None
    planned: bool = False

    @classmethod
    def from_feature(cls, feature: RouteFeature) -> RouteSnapshot:
        return cls(
            feature_id=feature.feature_id,
            name=feature.name,
            description=feature.description,
            month=feature.month,
            osmand_speed=feature.osmand_speed,
            geometry_type=feature.geometry_type,
            geometry_hash=feature_geometry_hash(feature),
            details_hash=feature_details_hash(feature),
            length_km=feature_length_km(feature),
            planned=feature.planned,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "name": self.name,
            "description": self.description,
            "month": self.month,
            "osmand_speed": self.osmand_speed,
            "geometry_type": self.geometry_type,
            "geometry_hash": self.geometry_hash,
            "details_hash": self.details_hash,
            "length_km": self.length_km,
            "planned": self.planned,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RouteSnapshot:
        return cls(
            feature_id=str(data["feature_id"]),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            month=str(data.get("month") or ""),
            osmand_speed=str(data.get("osmand_speed") or ""),
            geometry_type=str(data.get("geometry_type") or "Unknown"),
            geometry_hash=str(data.get("geometry_hash") or ""),
            details_hash=str(data.get("details_hash") or ""),
            length_km=float(data["length_km"]) if data.get("length_km") is not None else None,
            planned=property_bool(data, "planned"),
        )


@dataclass(slots=True)
class RouteChange:
    feature: RouteFeature
    changes: list[str]


@dataclass(slots=True)
class CheckResult:
    current_feature_count: int
    previous_known_feature_count: int
    known_feature_count: int
    new_features: list[RouteFeature]


@dataclass(slots=True)
class LayerStatus:
    title: str
    datalayer_url: str
    last_checked_at: str | None
    last_change_checked_at: str | None
    current_feature_count: int
    known_feature_count: int


def feature_length_km(feature: Any) -> float | None:
    coordinates = feature.geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return None

    if feature.geometry_type == "LineString":
        points = _coordinate_sequence(coordinates)
        return _pairwise_distance_km(points)

    if feature.geometry_type == "MultiLineString":
        return sum(
            _pairwise_distance_km(points)
            for part in coordinates
            if len(points := _coordinate_sequence(part)) >= 2
        )

    points = _flatten_coordinates(coordinates)
    if len(points) >= 2:
        return _pairwise_distance_km(points)
    return None


def feature_geometry_hash(feature: RouteFeature) -> str:
    payload = json.dumps(
        feature.geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def feature_details_hash(feature: RouteFeature) -> str:
    payload = json.dumps(
        feature.properties, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def property_bool(properties: dict[str, Any], *names: str) -> bool:
    lower_properties = {str(key).lower(): value for key, value in properties.items()}
    for name in names:
        value = lower_properties.get(name.lower())
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if value is not None and str(value).strip().lower() in TRUTHY_PROPERTY_VALUES:
            return True
    return False


def _coordinate_pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None


def _coordinate_sequence(values: Any) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    return [point for item in values if (point := _coordinate_pair(item)) is not None]


def _flatten_coordinates(values: Any) -> list[tuple[float, float]]:
    point = _coordinate_pair(values)
    if point is not None:
        return [point]
    if not isinstance(values, list):
        return []

    points: list[tuple[float, float]] = []
    for item in values:
        points.extend(_flatten_coordinates(item))
    return points


def _pairwise_distance_km(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0

    total = 0.0
    for start, end in zip(points, points[1:], strict=False):
        lon1, lat1 = start
        lon2, lat2 = end
        total += _haversine_km(lat1, lon1, lat2, lon2)
    return total


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0088
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius_km * math.asin(math.sqrt(value))


# --------------------------------------------------------------------------- #
# Watched layers + bot settings
# --------------------------------------------------------------------------- #

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
    state_db_path: str
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
        state_db_path=env("UMAP_STATE_DB", "/data/umap_state.db"),
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


# --------------------------------------------------------------------------- #
# Error tracking
# --------------------------------------------------------------------------- #

_TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_ERROR_TEXT = ("bad gateway",) + DEFAULT_TRANSIENT_ERROR_TEXT


def _transient_status_code(error: BaseException) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    return None


def is_transient_network_error(error: BaseException) -> bool:
    return is_transient_error(
        error,
        exception_types=(httpx.TimeoutException, httpx.TransportError, TelegramNetworkError),
        text_markers=_TRANSIENT_ERROR_TEXT,
        status_code_getter=_transient_status_code,
        transient_status_codes=_TRANSIENT_HTTP_STATUS_CODES,
    )


_sentry_tracker = SentryTracker(
    dsn_getter=lambda: env("SENTRY_DSN"),
    environment_getter=lambda: env("SENTRY_ENVIRONMENT"),
    release_getter=lambda: env("SENTRY_RELEASE"),
    is_transient=is_transient_network_error,
    text_markers=_TRANSIENT_ERROR_TEXT,
)


def init_error_tracking(service_name: str) -> bool:
    return _sentry_tracker.init(service_name)


def capture_exception(error: BaseException) -> None:
    _sentry_tracker.capture_exception(error)


def flush_error_tracking(timeout: float = 2.0) -> None:
    _sentry_tracker.flush(timeout=timeout)
