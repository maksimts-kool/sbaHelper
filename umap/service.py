"""
uMap route watcher service.

The project used to split the bot into many tiny modules. Keeping the related
pieces here makes the flow easier to follow: settings, route data, persisted
state, uMap API access, Telegram formatting, checks, and the bot runtime.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import math
import os
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeAlias
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import sentry_sdk
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

EmojiSpec: TypeAlias = tuple[str, str]


@dataclass(slots=True, frozen=True)
class WatchedLayer:
    key: str
    title: str
    layer_id: str
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
    umap_map_id: str
    umap_map_url: str
    watched_layers: tuple[WatchedLayer, ...]
    poll_interval_seconds: int
    change_poll_interval_seconds: int
    request_timeout_seconds: float
    bootstrap_notify_existing: bool
    log_level: str

    def build_datalayer_url(self, layer_id: str) -> str:
        base = self.umap_base_url.rstrip("/")
        return f"{base}/en/datalayer/{self.umap_map_id}/{layer_id}/"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def load_bot_settings() -> BotSettings:
    chat_id = _env("DEFAULT_SUBSCRIBER_CHAT_ID")
    route_layer_id = _env("UMAP_LAYER_ID", "82e959ea-1b66-4088-b7f3-8fe6a06c3c4a")
    plans_layer_id = _env("UMAP_PLANS_LAYER_ID")

    watched_layers = [
        WatchedLayer(
            key="2026",
            title="2026",
            layer_id=route_layer_id,
            route_label="маршрут",
            new_notification_title="Новый маршрут добавлен!",
            change_notification_title="Изменен маршрут!",
            new_notification_emojis=(("5397916757333654639", "➕"),),
            change_notification_emojis=(("5395444784611480792", "✏️"),),
        )
    ]
    if plans_layer_id:
        watched_layers.append(
            WatchedLayer(
                key="plans",
                title="В планах",
                layer_id=plans_layer_id,
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
        telegram_bot_token=_required_env("TELEGRAM_BOT_TOKEN"),
        default_subscriber_chat_id=int(chat_id) if chat_id else None,
        state_mongodb_uri=_required_env("UMAP_STATE_MONGODB_URI"),
        state_mongodb_database=_env("UMAP_STATE_MONGODB_DATABASE", "sbahelper"),
        state_mongodb_collection=_env("UMAP_STATE_MONGODB_COLLECTION", "umap_state"),
        umap_base_url=_env("UMAP_BASE_URL", "https://umap.openstreetmap.fr"),
        umap_map_id=_env("UMAP_MAP_ID", "1393155"),
        umap_map_url=_env("UMAP_MAP_URL", "http://u.osmfr.org/m/1393155/"),
        watched_layers=tuple(watched_layers),
        poll_interval_seconds=int(_env("POLL_INTERVAL_SECONDS", "300")),
        change_poll_interval_seconds=int(_env("CHANGE_POLL_INTERVAL_SECONDS", "3600")),
        request_timeout_seconds=float(_env("REQUEST_TIMEOUT_SECONDS", "30")),
        bootstrap_notify_existing=env_bool("BOOTSTRAP_NOTIFY_EXISTING", False),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
    )


# --------------------------------------------------------------------------- #
# Route data and geometry helpers
# --------------------------------------------------------------------------- #

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


@dataclass(slots=True)
class RouteSnapshot:
    feature_id: str
    name: str
    description: str
    month: str
    osmand_speed: str
    geometry_type: str
    geometry_hash: str
    length_km: float | None

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
            length_km=feature_length_km(feature),
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
            "length_km": self.length_km,
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
            length_km=float(data["length_km"]) if data.get("length_km") is not None else None,
        )


@dataclass(slots=True)
class RouteChange:
    feature: RouteFeature
    changes: list[str]


@dataclass(slots=True)
class CheckResult:
    checked_at: str
    current_feature_count: int
    previous_known_feature_count: int
    known_feature_count: int
    new_features: list[RouteFeature]
    bootstrap_completed: bool


@dataclass(slots=True)
class LayerStatus:
    title: str
    datalayer_url: str
    last_checked_at: str | None
    last_change_checked_at: str | None
    current_feature_count: int
    known_feature_count: int


def _pairwise_distance_km(points: list[list[float]]) -> float:
    if len(points) < 2:
        return 0.0

    total = 0.0
    for start, end in zip(points, points[1:]):
        lon1, lat1 = float(start[0]), float(start[1])
        lon2, lat2 = float(end[0]), float(end[1])
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


def feature_length_km(feature: RouteFeature) -> float | None:
    coordinates = feature.geometry.get("coordinates")
    if feature.geometry_type == "LineString" and isinstance(coordinates, list):
        return _pairwise_distance_km(coordinates)
    if feature.geometry_type == "MultiLineString" and isinstance(coordinates, list):
        return sum(_pairwise_distance_km(part) for part in coordinates if isinstance(part, list))
    return None


def feature_geometry_hash(feature: RouteFeature) -> str:
    payload = json.dumps(feature.geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Persistent bot state
# --------------------------------------------------------------------------- #

LEGACY_DEFAULT_LAYER_KEY = "2026"
SCOPED_FEATURE_KEY_PREFIX = "scoped"


def make_feature_state_key(layer_key: str, feature_id: str) -> str:
    encoded_feature_id = base64.urlsafe_b64encode(feature_id.encode("utf-8")).decode("ascii")
    return f"{SCOPED_FEATURE_KEY_PREFIX}:{layer_key}:{encoded_feature_id}"


def is_feature_state_key_for_layer(feature_key: str, layer_key: str) -> bool:
    return feature_key.startswith(f"{SCOPED_FEATURE_KEY_PREFIX}:{layer_key}:")


def normalize_feature_state_key(feature_key: str) -> str:
    if feature_key.startswith(f"{SCOPED_FEATURE_KEY_PREFIX}:"):
        return feature_key
    return make_feature_state_key(LEGACY_DEFAULT_LAYER_KEY, feature_key)


@dataclass(slots=True)
class AppState:
    known_feature_ids: set[str] = field(default_factory=set)
    subscriber_chat_ids: set[int] = field(default_factory=set)
    route_snapshots: dict[str, RouteSnapshot] = field(default_factory=dict)
    route_message_ids: dict[str, dict[int, int]] = field(default_factory=dict)
    bootstrap_completed_by_layer: dict[str, bool] = field(default_factory=dict)
    last_checked_at_by_layer: dict[str, str] = field(default_factory=dict)
    last_change_checked_at_by_layer: dict[str, str] = field(default_factory=dict)
    last_current_feature_count_by_layer: dict[str, int] = field(default_factory=dict)


class StateStore(Protocol):
    def load(self) -> AppState:
        ...

    def save(self, state: AppState) -> None:
        ...


class AppStateCodec:
    @staticmethod
    def _string_dict(raw_value: Any) -> dict[str, str]:
        if not isinstance(raw_value, dict):
            return {}
        return {str(key): str(value) for key, value in raw_value.items() if value}

    @staticmethod
    def _int_dict(raw_value: Any) -> dict[str, int]:
        if not isinstance(raw_value, dict):
            return {}
        return {str(key): int(value) for key, value in raw_value.items()}

    @classmethod
    def from_document(cls, data: dict[str, Any]) -> AppState:
        known_feature_ids = {
            normalize_feature_state_key(str(feature_id))
            for feature_id in data.get("known_feature_ids", [])
        }
        subscriber_chat_ids = {
            int(chat_id) for chat_id in data.get("subscriber_chat_ids", [])
        }
        route_snapshots = {
            normalize_feature_state_key(str(feature_id)): RouteSnapshot.from_dict(snapshot)
            for feature_id, snapshot in data.get("route_snapshots", {}).items()
            if isinstance(snapshot, dict)
        }

        route_message_ids: dict[str, dict[int, int]] = {}
        for feature_id, per_chat in data.get("route_message_ids", {}).items():
            if not isinstance(per_chat, dict):
                continue
            feature_key = normalize_feature_state_key(str(feature_id))
            route_message_ids[feature_key] = {
                int(chat_id): int(message_id) for chat_id, message_id in per_chat.items()
            }

        return AppState(
            known_feature_ids=known_feature_ids,
            subscriber_chat_ids=subscriber_chat_ids,
            route_snapshots=route_snapshots,
            route_message_ids=route_message_ids,
            bootstrap_completed_by_layer={
                str(key): bool(value)
                for key, value in data.get("bootstrap_completed_by_layer", {}).items()
            },
            last_checked_at_by_layer=cls._string_dict(data.get("last_checked_at_by_layer")),
            last_change_checked_at_by_layer=cls._string_dict(
                data.get("last_change_checked_at_by_layer")
            ),
            last_current_feature_count_by_layer=cls._int_dict(
                data.get("last_current_feature_count_by_layer")
            ),
        )

    @staticmethod
    def to_document(state: AppState) -> dict[str, Any]:
        return {
            "known_feature_ids": sorted(state.known_feature_ids),
            "subscriber_chat_ids": sorted(state.subscriber_chat_ids),
            "route_snapshots": {
                feature_id: snapshot.to_dict()
                for feature_id, snapshot in sorted(state.route_snapshots.items())
            },
            "route_message_ids": {
                feature_id: {
                    str(chat_id): message_id for chat_id, message_id in sorted(per_chat.items())
                }
                for feature_id, per_chat in sorted(state.route_message_ids.items())
            },
            "bootstrap_completed_by_layer": state.bootstrap_completed_by_layer,
            "last_checked_at_by_layer": state.last_checked_at_by_layer,
            "last_change_checked_at_by_layer": state.last_change_checked_at_by_layer,
            "last_current_feature_count_by_layer": state.last_current_feature_count_by_layer,
        }


class MongoStateStore:
    def __init__(
        self,
        *,
        uri: str,
        database: str,
        collection: str,
        document_id: str = "umap-route-bot",
    ) -> None:
        from pymongo import MongoClient

        self._client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self._collection = self._client[database][collection]
        self._document_id = document_id

    def load(self) -> AppState:
        data = self._collection.find_one({"_id": self._document_id})
        if data is not None:
            return AppStateCodec.from_document(data)

        return AppState()

    def save(self, state: AppState) -> None:
        data = AppStateCodec.to_document(state)
        data["_id"] = self._document_id
        data["updated_at"] = utc_now_iso()
        self._collection.replace_one({"_id": self._document_id}, data, upsert=True)


def build_state_store(settings: BotSettings) -> StateStore:
    return MongoStateStore(
        uri=settings.state_mongodb_uri,
        database=settings.state_mongodb_database,
        collection=settings.state_mongodb_collection,
    )


# --------------------------------------------------------------------------- #
# uMap API client
# --------------------------------------------------------------------------- #

class UmapClient:
    def __init__(self, datalayer_url: str, timeout_seconds: float) -> None:
        self._datalayer_url = datalayer_url
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": "umap-route-bot/1.0 (+https://umap.openstreetmap.fr)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_routes(self) -> list[RouteFeature]:
        response = await self._client.get(self._datalayer_url)
        response.raise_for_status()
        features = response.json().get("features", [])
        return [self._parse_feature(feature) for feature in features]

    def _parse_feature(self, feature: dict[str, Any]) -> RouteFeature:
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        feature_id = str(feature.get("id") or properties.get("name") or "")

        if not feature_id:
            coordinates = geometry.get("coordinates")
            digest = hashlib.sha1(str(coordinates).encode("utf-8")).hexdigest()
            feature_id = f"anonymous:{digest}"

        return RouteFeature(
            feature_id=feature_id,
            name=str(properties.get("name") or "Без названия"),
            description=str(properties.get("description") or "").strip(),
            month=str(properties.get("Месяц") or "").strip(),
            osmand_speed=str(properties.get("osmand_speed") or "").strip(),
            geometry_type=str(geometry.get("type") or "Unknown"),
            geometry=geometry,
            properties=properties,
        )


# --------------------------------------------------------------------------- #
# Telegram text formatting
# --------------------------------------------------------------------------- #

CHANGES_SECTION_EMOJI_ID = "5244758760429213978"
CHANGED_VALUE_EMOJI_ID = "5395748666432568078"
NAME_EMOJI_ID = "5929468240668397096"
LENGTH_EMOJI_ID = "5339573565102515237"
OPEN_ROUTE_BUTTON_TEXT = "📌 Открыть маршрут"


def _tg_emoji(emoji_id: str, fallback_emoji: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback_emoji}</tg-emoji>'


def _tg_emojis(emojis: tuple[EmojiSpec, ...]) -> str:
    return "".join(_tg_emoji(emoji_id, fallback_emoji) for emoji_id, fallback_emoji in emojis)


def format_route_notification(layer: WatchedLayer, feature: RouteFeature) -> str:
    lines = [
        f"{_tg_emojis(layer.new_notification_emojis)} <b>{html.escape(layer.new_notification_title)}</b>",
        f"{_tg_emoji(NAME_EMOJI_ID, '😀')} <b>{html.escape(feature.name)}</b>",
    ]

    length_km = feature_length_km(feature)
    if length_km is not None:
        lines.append(f"{_tg_emoji(LENGTH_EMOJI_ID, '🌊')} <b>Длина:</b> {length_km:.1f} км")

    return "\n".join(lines)


def format_route_change_notification(
    layer: WatchedLayer,
    feature: RouteFeature,
    changes_html: list[str],
) -> str:
    lines = [
        f"{_tg_emojis(layer.change_notification_emojis)} <b>{html.escape(layer.change_notification_title)}</b>",
        f"{_tg_emoji(NAME_EMOJI_ID, '😀')} <b>{html.escape(feature.name)}</b>",
    ]

    length_km = feature_length_km(feature)
    if length_km is not None:
        lines.append(f"{_tg_emoji(LENGTH_EMOJI_ID, '🌊')} <b>Длина:</b> {length_km:.1f} км")

    if changes_html:
        lines.append("")
        lines.append(f"{_tg_emoji(CHANGES_SECTION_EMOJI_ID, '🔄')} <b>Что изменилось:</b>")
        lines.extend(changes_html)

    return "\n".join(lines)


def build_feature_url(map_url: str, layer: WatchedLayer, feature: RouteFeature) -> str | None:
    slug = feature.name.strip()
    if not slug:
        return None

    parsed = urlsplit(map_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["datalayers"] = layer.layer_id
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


# --------------------------------------------------------------------------- #
# Error tracking
# --------------------------------------------------------------------------- #

_sentry_initialized = False


def init_error_tracking(service_name: str) -> bool:
    global _sentry_initialized

    if _sentry_initialized:
        sentry_sdk.set_tag("service", service_name)
        return True

    dsn = _env("SENTRY_DSN")
    if not dsn:
        logger.debug("Sentry is not configured.")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=_env("SENTRY_ENVIRONMENT") or None,
        release=_env("SENTRY_RELEASE") or None,
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("service", service_name)
    _sentry_initialized = True
    logger.info("Sentry error tracking enabled for %s.", service_name)
    return True


def capture_exception(error: BaseException) -> None:
    if _sentry_initialized:
        sentry_sdk.capture_exception(error)


def flush_error_tracking(timeout: float = 2.0) -> None:
    if _sentry_initialized:
        sentry_sdk.flush(timeout=timeout)


# --------------------------------------------------------------------------- #
# Startup checks
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class UmapCheckResult:
    name: str
    ok: bool
    message: str


async def _check_layer(settings: BotSettings, layer: WatchedLayer) -> UmapCheckResult:
    client = UmapClient(
        datalayer_url=settings.build_datalayer_url(layer.layer_id),
        timeout_seconds=settings.request_timeout_seconds,
    )
    try:
        routes = await client.fetch_routes()
    except Exception as error:
        capture_exception(error)
        logger.exception("uMap check failed for layer %s", layer.title)
        return UmapCheckResult(layer.key, False, str(error))
    finally:
        await client.close()

    return UmapCheckResult(
        layer.key,
        True,
        f"{layer.title}: fetched {len(routes)} routes",
    )


async def run_checks() -> list[UmapCheckResult]:
    try:
        settings = load_bot_settings()
    except RuntimeError as error:
        capture_exception(error)
        return [UmapCheckResult("settings", False, str(error))]

    results = [
        UmapCheckResult(
            "settings",
            True,
            f"configured {len(settings.watched_layers)} layer(s)",
        )
    ]

    try:
        await asyncio.to_thread(build_state_store(settings).load)
    except Exception as error:
        capture_exception(error)
        logger.exception("MongoDB state store check failed")
        results.append(UmapCheckResult("state-store", False, str(error)))
    else:
        results.append(
            UmapCheckResult(
                "state-store",
                True,
                (
                    "MongoDB "
                    f"{settings.state_mongodb_database}.{settings.state_mongodb_collection}"
                ),
            )
        )

    for layer in settings.watched_layers:
        results.append(await _check_layer(settings, layer))
    return results


def print_results(results: list[UmapCheckResult]) -> None:
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"{status} {result.name}: {result.message}")


def result_exit_code(results: list[UmapCheckResult]) -> int:
    if any(result.name == "settings" and not result.ok for result in results):
        return 2
    if any(not result.ok for result in results):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Telegram bot runtime
# --------------------------------------------------------------------------- #

class RouteWatcherService:
    def __init__(
        self,
        *,
        settings: BotSettings,
        state_store: StateStore,
        state: AppState,
        umap_clients: dict[str, UmapClient],
        bot: Bot,
    ) -> None:
        self._settings = settings
        self._state_store = state_store
        self._state = state
        self._umap_clients = umap_clients
        self._bot = bot
        self._lock = asyncio.Lock()

    @property
    def watched_layers(self) -> tuple[WatchedLayer, ...]:
        return self._settings.watched_layers

    async def save_state(self) -> None:
        await asyncio.to_thread(self._state_store.save, self._state)

    async def check_for_updates(self, *, notify: bool) -> dict[str, CheckResult]:
        async with self._lock:
            results: dict[str, CheckResult] = {}
            for layer in self._settings.watched_layers:
                routes = await self._umap_clients[layer.key].fetch_routes()
                results[layer.key] = await self._check_layer_for_updates(
                    layer=layer,
                    routes=routes,
                    notify=notify,
                )
            return results

    async def check_for_route_changes(self, *, notify: bool) -> dict[str, list[RouteChange]]:
        async with self._lock:
            changes_by_layer: dict[str, list[RouteChange]] = {}
            for layer in self._settings.watched_layers:
                routes = await self._umap_clients[layer.key].fetch_routes()
                changes_by_layer[layer.key] = await self._check_layer_for_route_changes(
                    layer=layer,
                    routes=routes,
                    notify=notify,
                )
            return changes_by_layer

    async def _check_layer_for_updates(
        self,
        *,
        layer: WatchedLayer,
        routes: list[RouteFeature],
        notify: bool,
    ) -> CheckResult:
        current_ids = {self._scoped_feature_id(layer, route.feature_id) for route in routes}
        previous_known_feature_count = self._known_feature_count(layer)
        new_routes = [
            route
            for route in routes
            if self._scoped_feature_id(layer, route.feature_id) not in self._state.known_feature_ids
        ]

        if not self._state.bootstrap_completed_by_layer.get(layer.key, False):
            should_notify = self._settings.bootstrap_notify_existing and notify
            self._state.bootstrap_completed_by_layer[layer.key] = True
            if not should_notify:
                new_routes = []

        self._cleanup_removed_routes(layer, current_ids)
        self._state.known_feature_ids.update(current_ids)
        self._ensure_missing_snapshots(layer, routes)
        self._state.last_checked_at_by_layer[layer.key] = utc_now_iso()
        self._state.last_current_feature_count_by_layer[layer.key] = len(routes)
        await self.save_state()

        result = CheckResult(
            checked_at=self._state.last_checked_at_by_layer[layer.key],
            current_feature_count=len(routes),
            previous_known_feature_count=previous_known_feature_count,
            known_feature_count=self._known_feature_count(layer),
            new_features=new_routes,
            bootstrap_completed=self._state.bootstrap_completed_by_layer[layer.key],
        )

        if notify and new_routes:
            await self._notify_about_new_routes(layer, new_routes)

        return result

    async def _check_layer_for_route_changes(
        self,
        *,
        layer: WatchedLayer,
        routes: list[RouteFeature],
        notify: bool,
    ) -> list[RouteChange]:
        current_ids = {self._scoped_feature_id(layer, route.feature_id) for route in routes}
        self._cleanup_removed_routes(layer, current_ids)

        changes: list[RouteChange] = []
        for route in routes:
            feature_key = self._scoped_feature_id(layer, route.feature_id)
            if feature_key not in self._state.known_feature_ids:
                continue

            previous_snapshot = self._state.route_snapshots.get(feature_key)
            if previous_snapshot is None:
                self._state.route_snapshots[feature_key] = RouteSnapshot.from_feature(route)
                continue

            current_snapshot = RouteSnapshot.from_feature(route)
            if current_snapshot == previous_snapshot:
                continue

            changes.append(
                RouteChange(
                    feature=route,
                    changes=await self._build_change_descriptions(
                        layer,
                        previous_snapshot,
                        current_snapshot,
                    ),
                )
            )
            self._state.route_snapshots[feature_key] = current_snapshot

        self._state.last_change_checked_at_by_layer[layer.key] = utc_now_iso()
        self._state.last_current_feature_count_by_layer[layer.key] = len(routes)
        await self.save_state()

        if notify and changes:
            await self._notify_about_route_changes(layer, changes)

        return changes

    async def _notify_about_new_routes(self, layer: WatchedLayer, routes: list[RouteFeature]) -> None:
        if not self._state.subscriber_chat_ids:
            logger.info("New routes detected in layer %s, but there are no subscribers yet.", layer.title)
            return

        state_changed = False
        for route in routes:
            for chat_id in sorted(self._state.subscriber_chat_ids):
                try:
                    message = await self.send_route_message(chat_id=chat_id, layer=layer, route=route)
                    self._remember_route_message(layer, route.feature_id, chat_id, message.message_id)
                    state_changed = True
                except Exception as error:
                    capture_exception(error)
                    logger.exception(
                        "Failed to send new route notification for layer %s to chat %s",
                        layer.title,
                        chat_id,
                    )

        if state_changed:
            await self.save_state()

    async def _notify_about_route_changes(
        self,
        layer: WatchedLayer,
        changes: list[RouteChange],
    ) -> None:
        if not self._state.subscriber_chat_ids:
            logger.info(
                "Route changes detected in layer %s, but there are no subscribers yet.",
                layer.title,
            )
            return

        for route_change in changes:
            for chat_id in sorted(self._state.subscriber_chat_ids):
                try:
                    await self.send_route_change_message(
                        chat_id=chat_id,
                        layer=layer,
                        route=route_change.feature,
                        changes_html=route_change.changes,
                        reply_to_message_id=self._state.route_message_ids.get(
                            self._scoped_feature_id(layer, route_change.feature.feature_id),
                            {},
                        ).get(chat_id),
                    )
                except Exception as error:
                    capture_exception(error)
                    logger.exception(
                        "Failed to send route change notification for layer %s to chat %s",
                        layer.title,
                        chat_id,
                    )

    def _build_route_markup(self, layer: WatchedLayer, route: RouteFeature) -> InlineKeyboardMarkup | None:
        feature_url = build_feature_url(
            self._settings.umap_map_url,
            layer,
            route,
        )
        if not feature_url:
            return None

        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=OPEN_ROUTE_BUTTON_TEXT, url=feature_url)]]
        )

    async def send_route_message(
        self,
        *,
        chat_id: int,
        layer: WatchedLayer,
        route: RouteFeature,
        prefix: str | None = None,
    ) -> Message:
        text = format_route_notification(layer, route)
        if prefix:
            text = f"{prefix}\n\n{text}"
        return await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=self._build_route_markup(layer, route),
        )

    async def send_route_change_message(
        self,
        *,
        chat_id: int,
        layer: WatchedLayer,
        route: RouteFeature,
        changes_html: list[str],
        reply_to_message_id: int | None,
    ) -> Message:
        text = format_route_change_notification(layer, route, changes_html)
        send_kwargs = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": self._build_route_markup(layer, route),
        }

        if reply_to_message_id is not None:
            try:
                return await self._bot.send_message(
                    **send_kwargs,
                    reply_parameters=ReplyParameters(message_id=reply_to_message_id),
                )
            except TelegramBadRequest:
                logger.warning(
                    "Failed to send change notification as reply for feature %s in layer %s in chat %s. "
                    "Sending standalone message instead.",
                    route.feature_id,
                    layer.title,
                    chat_id,
                )

        return await self._bot.send_message(**send_kwargs)

    def _cleanup_removed_routes(self, layer: WatchedLayer, current_ids: set[str]) -> None:
        known_ids = {
            feature_id
            for feature_id in self._state.known_feature_ids
            if is_feature_state_key_for_layer(feature_id, layer.key)
        }
        self._state.known_feature_ids.difference_update(known_ids - current_ids)

        snapshot_ids = {
            feature_id
            for feature_id in self._state.route_snapshots
            if is_feature_state_key_for_layer(feature_id, layer.key)
        }
        message_ids = {
            feature_id
            for feature_id in self._state.route_message_ids
            if is_feature_state_key_for_layer(feature_id, layer.key)
        }
        stale_ids = (snapshot_ids | message_ids) - current_ids
        for feature_id in stale_ids:
            self._state.route_snapshots.pop(feature_id, None)
            self._state.route_message_ids.pop(feature_id, None)

    def _ensure_missing_snapshots(self, layer: WatchedLayer, routes: list[RouteFeature]) -> None:
        for route in routes:
            self._state.route_snapshots.setdefault(
                self._scoped_feature_id(layer, route.feature_id),
                RouteSnapshot.from_feature(route),
            )

    def _remember_route_message(
        self,
        layer: WatchedLayer,
        feature_id: str,
        chat_id: int,
        message_id: int,
    ) -> None:
        self._state.route_message_ids.setdefault(
            self._scoped_feature_id(layer, feature_id),
            {},
        )[chat_id] = message_id

    async def _build_change_descriptions(
        self,
        layer: WatchedLayer,
        previous: RouteSnapshot,
        current: RouteSnapshot,
    ) -> list[str]:
        changes: list[str] = []

        if previous.name != current.name:
            changes.append(
                "• Название: "
                f"<s>{html.escape(self._display_value(previous.name, 'без названия'))}</s> "
                f"{_tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
                f"{html.escape(self._display_value(current.name, 'без названия'))}"
            )

        if previous.month != current.month:
            changes.append(
                "• Месяц: "
                f"<s>{html.escape(self._display_value(previous.month, 'не указан'))}</s> "
                f"{_tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
                f"{html.escape(self._display_value(current.month, 'не указан'))}"
            )

        if previous.osmand_speed != current.osmand_speed:
            changes.append(
                "• Скорость Osmand: "
                f"<s>{html.escape(self._display_value(previous.osmand_speed, 'не указана'))}</s> "
                f"{_tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
                f"{html.escape(self._display_value(current.osmand_speed, 'не указана'))}"
            )

        if previous.description != current.description:
            changes.append(
                "• Описание: "
                f"<s>{html.escape(self._display_value(previous.description, 'пусто'))}</s> "
                f"{_tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
                f"{html.escape(self._display_value(current.description, 'пусто'))}"
            )

        if previous.geometry_type != current.geometry_type:
            changes.append(
                "• Тип геометрии: "
                f"<s>{html.escape(previous.geometry_type)}</s> "
                f"{_tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
                f"{html.escape(current.geometry_type)}"
            )

        if self._length_changed(previous.length_km, current.length_km):
            changes.append(
                "• Длина: "
                f"<s>{html.escape(self._format_length(previous.length_km))}</s> "
                f"{_tg_emoji(CHANGED_VALUE_EMOJI_ID, '▫️')} "
                f"{html.escape(self._format_length(current.length_km))}"
            )

        if previous.geometry_hash != current.geometry_hash:
            changes.append("• Геометрия маршрута обновлена.")

        return changes or [f"• {layer.route_label.capitalize()} был обновлен."]

    @staticmethod
    def _display_value(value: str, fallback: str) -> str:
        normalized = value.strip()
        return normalized if normalized else fallback

    @staticmethod
    def _length_changed(previous: float | None, current: float | None) -> bool:
        if previous is None or current is None:
            return previous != current
        return abs(previous - current) >= 0.1

    @staticmethod
    def _format_length(value: float | None) -> str:
        if value is None:
            return "неизвестно"
        return f"{value:.1f} км"

    def _scoped_feature_id(self, layer: WatchedLayer, feature_id: str) -> str:
        return make_feature_state_key(layer.key, feature_id)

    def _known_feature_count(self, layer: WatchedLayer) -> int:
        return sum(
            1
            for feature_id in self._state.known_feature_ids
            if is_feature_state_key_for_layer(feature_id, layer.key)
        )

    def _layer_status(self, layer: WatchedLayer) -> LayerStatus:
        return LayerStatus(
            title=layer.title,
            datalayer_url=self._settings.build_datalayer_url(layer.layer_id),
            last_checked_at=self._state.last_checked_at_by_layer.get(layer.key),
            last_change_checked_at=self._state.last_change_checked_at_by_layer.get(layer.key),
            current_feature_count=self._state.last_current_feature_count_by_layer.get(layer.key, 0),
            known_feature_count=self._known_feature_count(layer),
        )

    async def subscribe(self, chat_id: int) -> bool:
        existed = chat_id in self._state.subscriber_chat_ids
        self._state.subscriber_chat_ids.add(chat_id)
        await self.save_state()
        return not existed

    async def unsubscribe(self, chat_id: int) -> bool:
        if chat_id not in self._state.subscriber_chat_ids:
            return False
        self._state.subscriber_chat_ids.remove(chat_id)
        await self.save_state()
        return True

    def status_message(self) -> str:
        return format_status_message(
            layer_statuses=[self._layer_status(layer) for layer in self._settings.watched_layers],
            subscriber_count=len(self._state.subscriber_chat_ids),
            poll_interval_seconds=self._settings.poll_interval_seconds,
            change_poll_interval_seconds=self._settings.change_poll_interval_seconds,
        )

    def start_message(self) -> str:
        layer_names = ", ".join(layer.title for layer in self._settings.watched_layers)
        return "\n".join(
            [
                f"Бот следит за маршрутами в слоях uMap: {layer_names}.",
                "Для каждого слоя отдельно отслеживаются новые маршруты и изменения.",
                "Команды:",
                "/subscribe - подписать этот чат",
                "/unsubscribe - отписать этот чат",
                "/status - показать статус",
                "/check - выполнить проверку сейчас",
                "/chatid - показать chat id",
                "/testnotify - отправить тестовое уведомление",
            ]
        )

    async def send_test_notification(self, chat_id: int) -> None:
        for layer in self._settings.watched_layers:
            routes = await self._umap_clients[layer.key].fetch_routes()
            route = routes[0] if routes else RouteFeature(
                feature_id=f"test-notification-{layer.key}",
                name="Тестовый маршрут" if layer.key != "plans" else "Тестовый план маршрута",
                description="Это тестовое уведомление бота.",
                month="",
                osmand_speed="",
                geometry_type="LineString",
                geometry={"type": "LineString", "coordinates": []},
                properties={},
            )
            await self.send_route_message(
                chat_id=chat_id,
                layer=layer,
                route=route,
                prefix=f"Тестовое уведомление для слоя {layer.title}.",
            )


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_dispatcher(service: RouteWatcherService) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def handle_start(message: Message) -> None:
        await message.answer(service.start_message())

    @dp.message(Command("subscribe"))
    async def handle_subscribe(message: Message) -> None:
        added = await service.subscribe(message.chat.id)
        text = "Чат подписан на уведомления." if added else "Этот чат уже подписан."
        await message.answer(text)

    @dp.message(Command("unsubscribe"))
    async def handle_unsubscribe(message: Message) -> None:
        removed = await service.unsubscribe(message.chat.id)
        text = "Чат отписан от уведомлений." if removed else "Этот чат не был подписан."
        await message.answer(text)

    @dp.message(Command("status"))
    async def handle_status(message: Message) -> None:
        await message.answer(service.status_message())

    @dp.message(Command("chatid"))
    async def handle_chat_id(message: Message) -> None:
        await message.answer(f"chat_id: <code>{message.chat.id}</code>")

    @dp.message(Command("check"))
    async def handle_check(message: Message) -> None:
        try:
            results = await service.check_for_updates(notify=True)
        except Exception as error:
            capture_exception(error)
            logger.exception("Manual check failed")
            await message.answer("Не удалось выполнить проверку. Подробности смотри в логах.")
            return

        total_new_routes = sum(len(result.new_features) for result in results.values())
        if total_new_routes == 0:
            await message.answer("Проверка завершена. Новых маршрутов не найдено ни в одном слое.")
            return

        lines = ["Проверка завершена."]
        for layer in service.watched_layers:
            result = results.get(layer.key)
            if result is not None:
                lines.append(f"{layer.title}: новых маршрутов {len(result.new_features)}.")
        await message.answer("\n".join(lines))

    @dp.message(Command("testnotify"))
    async def handle_test_notify(message: Message) -> None:
        try:
            await service.send_test_notification(message.chat.id)
        except Exception as error:
            capture_exception(error)
            logger.exception("Test notification failed")
            await message.answer("Тестовое уведомление не удалось отправить. Подробности смотри в логах.")

    @dp.message(F.text, F.chat.type == ChatType.PRIVATE)
    async def handle_fallback(message: Message) -> None:
        await message.answer("Используй /start, чтобы увидеть доступные команды.")

    @dp.message()
    async def handle_ignored_message(message: Message) -> None:
        logger.debug(
            "Ignored non-command message in chat %s (%s).",
            message.chat.id,
            message.chat.type,
        )

    return dp


async def watch_loop(service: RouteWatcherService, interval_seconds: int) -> None:
    while True:
        try:
            results = await service.check_for_updates(notify=True)
            for layer in service.watched_layers:
                result = results.get(layer.key)
                if result is None:
                    continue
                logger.info(
                    "Layer %s checked: current=%s known_before=%s known_after=%s new=%s",
                    layer.title,
                    result.current_feature_count,
                    result.previous_known_feature_count,
                    result.known_feature_count,
                    len(result.new_features),
                )
        except Exception as error:
            capture_exception(error)
            logger.exception("Scheduled layer check failed")

        await asyncio.sleep(interval_seconds)


async def watch_change_loop(service: RouteWatcherService, interval_seconds: int) -> None:
    while True:
        try:
            changes_by_layer = await service.check_for_route_changes(notify=True)
            for layer in service.watched_layers:
                layer_changes = changes_by_layer.get(layer.key, [])
                logger.info("Layer %s route changes checked: changed=%s", layer.title, len(layer_changes))
        except Exception as error:
            capture_exception(error)
            logger.exception("Scheduled route change check failed")

        await asyncio.sleep(interval_seconds)


async def run_bot() -> None:
    settings = load_bot_settings()
    configure_logging(settings.log_level)
    init_error_tracking("umap-route-bot")

    state_store = build_state_store(settings)
    state = await asyncio.to_thread(state_store.load)
    if (
        settings.default_subscriber_chat_id is not None
        and settings.default_subscriber_chat_id not in state.subscriber_chat_ids
    ):
        state.subscriber_chat_ids.add(settings.default_subscriber_chat_id)
        await asyncio.to_thread(state_store.save, state)

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    umap_clients = {
        layer.key: UmapClient(
            datalayer_url=settings.build_datalayer_url(layer.layer_id),
            timeout_seconds=settings.request_timeout_seconds,
        )
        for layer in settings.watched_layers
    }
    service = RouteWatcherService(
        settings=settings,
        state_store=state_store,
        state=state,
        umap_clients=umap_clients,
        bot=bot,
    )
    dp = build_dispatcher(service)

    watcher_task = asyncio.create_task(
        watch_loop(service, settings.poll_interval_seconds),
        name="umap-watch-loop",
    )
    change_watcher_task = asyncio.create_task(
        watch_change_loop(service, settings.change_poll_interval_seconds),
        name="umap-change-watch-loop",
    )

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        watcher_task.cancel()
        change_watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task
        with suppress(asyncio.CancelledError):
            await change_watcher_task
        for umap_client in umap_clients.values():
            await umap_client.close()
        await bot.session.close()


def run_startup_then_bot() -> int:
    configure_logging(_env("LOG_LEVEL", "INFO").upper())
    init_error_tracking("umap-startup")

    logger.info("Running startup uMap checks.")
    results = asyncio.run(run_checks())
    print_results(results)
    exit_code = result_exit_code(results)
    flush_error_tracking()

    if exit_code:
        if env_bool("UMAP_STARTUP_CHECKS_REQUIRED", True):
            logger.error("Startup uMap checks failed with exit code %s.", exit_code)
            return exit_code
        logger.warning(
            "Startup uMap checks failed with exit code %s. Continuing because "
            "UMAP_STARTUP_CHECKS_REQUIRED=0.",
            exit_code,
        )

    asyncio.run(run_bot())
    return 0


def main() -> int:
    return run_startup_then_bot()


if __name__ == "__main__":
    raise SystemExit(main())
