"""
uMap service layer: the HTTP client that fetches routes, the SQLite-backed
state store, and the RouteWatcherService that ties polling, diffing, and
Telegram notifications together.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from umap.config import (
    PLANNED_PROPERTY,
    SOURCE_LAYER_ID_PROPERTY,
    BotSettings,
    CheckResult,
    LayerStatus,
    RouteChange,
    RouteFeature,
    RouteSnapshot,
    WatchedLayer,
    capture_exception,
    collect_geojson_features,
    is_transient_network_error,
    property_bool,
    utc_now_iso,
)
from umap.formatting import (
    OPEN_ROUTE_BUTTON_TEXT,
    build_change_descriptions,
    build_feature_url,
    format_route_change_notification,
    format_route_notification,
    format_status_message,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# uMap HTTP client
# --------------------------------------------------------------------------- #


class UmapClient:
    def __init__(
        self,
        datalayer_url: str,
        timeout_seconds: float,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
    ) -> None:
        self._datalayer_url = datalayer_url
        self._retry_attempts = max(1, retry_attempts)
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": "umap-route-bot/1.0 (+https://umap.openstreetmap.fr)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_routes(self) -> list[RouteFeature]:
        data = await self._fetch_json(self._datalayer_url)
        datalayer_specs = map_datalayer_specs(data)
        if datalayer_specs:
            features: list[dict[str, Any]] = []
            for layer_id, _layer_title in datalayer_specs:
                layer_data = await self._fetch_json(self._build_datalayer_url(layer_id))
                features.extend(collect_geojson_features(layer_data))
            return [parse_umap_feature(feature, scope_source_layer=True) for feature in features]

        features = collect_geojson_features(data)
        return [parse_umap_feature(feature) for feature in features]

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        for attempt in range(1, self._retry_attempts + 1):
            try:
                response = await self._client.get(url)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    return data
                raise ValueError(f"Expected JSON object from {url}")
            except Exception as error:
                if not is_transient_network_error(error) or attempt == self._retry_attempts:
                    raise

                delay = self._retry_backoff_seconds * attempt + random.uniform(0.0, 0.5)
                logger.warning(
                    "Transient uMap fetch failure for %s, retrying in %.1fs (%s/%s): %s",
                    url,
                    delay,
                    attempt,
                    self._retry_attempts,
                    error,
                )
                await asyncio.sleep(delay)

        raise RuntimeError("unreachable")

    def _build_datalayer_url(self, layer_id: str) -> str:
        parsed = urlsplit(self._datalayer_url)
        match = re.search(r"/map/([^/]+)/geojson/?$", parsed.path)
        if not match:
            raise ValueError(f"Cannot build datalayer URL from {self._datalayer_url}")
        map_id = match.group(1)
        language_prefix = ""
        language_match = re.match(r"^/([a-z]{2})(?:/|$)", parsed.path)
        if language_match:
            language_prefix = f"/{language_match.group(1)}"
        path = f"{language_prefix}/datalayer/{map_id}/{layer_id}/"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def map_datalayer_specs(umap_data: dict[str, Any]) -> list[tuple[str, str]]:
    datalayers = (umap_data.get("properties") or {}).get("datalayers")
    if not isinstance(datalayers, list):
        return []

    specs: list[tuple[str, str]] = []
    for datalayer in datalayers:
        if not isinstance(datalayer, dict):
            continue
        layer_id = str(datalayer.get("id") or "").strip()
        layer_title = str((datalayer.get("properties") or {}).get("name") or "").strip()
        if layer_id:
            specs.append((layer_id, layer_title))
    return specs


def parse_umap_feature(
    feature: dict[str, Any], *, scope_source_layer: bool = False
) -> RouteFeature:
    geometry = feature.get("geometry") or {}
    properties = feature.get("properties") or {}
    feature_id = str(feature.get("id") or properties.get("name") or "")

    if not feature_id:
        coordinates = geometry.get("coordinates")
        digest = hashlib.sha1(str(coordinates).encode("utf-8")).hexdigest()
        feature_id = f"anonymous:{digest}"

    source_layer_id = str(properties.get(SOURCE_LAYER_ID_PROPERTY) or "").strip()
    if scope_source_layer and source_layer_id:
        feature_id = f"{source_layer_id}:{feature_id}"

    return RouteFeature(
        feature_id=feature_id,
        name=str(properties.get("name") or "Без названия"),
        description=str(properties.get("description") or "").strip(),
        month=str(properties.get("Месяц") or "").strip(),
        osmand_speed=str(properties.get("osmand_speed") or "").strip(),
        geometry_type=str(geometry.get("type") or "Unknown"),
        geometry=geometry,
        properties=properties,
        planned=property_bool(properties, PLANNED_PROPERTY),
    )


# --------------------------------------------------------------------------- #
# Application state + SQLite store
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
    def load(self) -> AppState: ...

    def save(self, state: AppState) -> None: ...


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
        subscriber_chat_ids = {int(chat_id) for chat_id in data.get("subscriber_chat_ids", [])}
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


class SqliteStateStore:
    """Stores the whole app state as one JSON document in a single-row SQLite table."""

    def __init__(self, path: str) -> None:
        self._path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS app_state ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "document TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def load(self) -> AppState:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute("SELECT document FROM app_state WHERE id = 1").fetchone()
        return AppStateCodec.from_document(json.loads(row[0])) if row else AppState()

    def save(self, state: AppState) -> None:
        document = json.dumps(AppStateCodec.to_document(state), ensure_ascii=False)
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT INTO app_state (id, document, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "document = excluded.document, updated_at = excluded.updated_at",
                (document, utc_now_iso()),
            )
            conn.commit()


def build_state_store(settings: BotSettings) -> StateStore:
    return SqliteStateStore(settings.state_db_path)


# --------------------------------------------------------------------------- #
# Route watcher service
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
        current_ids = {make_feature_state_key(layer.key, route.feature_id) for route in routes}
        previous_known_feature_count = self._known_feature_count(layer)
        new_routes = [
            route
            for route in routes
            if make_feature_state_key(layer.key, route.feature_id)
            not in self._state.known_feature_ids
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
            current_feature_count=len(routes),
            previous_known_feature_count=previous_known_feature_count,
            known_feature_count=self._known_feature_count(layer),
            new_features=new_routes,
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
        current_ids = {make_feature_state_key(layer.key, route.feature_id) for route in routes}
        self._cleanup_removed_routes(layer, current_ids)

        changes: list[RouteChange] = []
        for route in routes:
            feature_key = make_feature_state_key(layer.key, route.feature_id)
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
                    changes=build_change_descriptions(
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

    async def _notify_about_new_routes(
        self, layer: WatchedLayer, routes: list[RouteFeature]
    ) -> None:
        if not self._state.subscriber_chat_ids:
            logger.info(
                "New routes detected in layer %s, but there are no subscribers yet.", layer.title
            )
            return

        state_changed = False
        for route in routes:
            for chat_id in sorted(self._state.subscriber_chat_ids):
                try:
                    message = await self.send_route_message(
                        chat_id=chat_id, layer=layer, route=route
                    )
                    self._remember_route_message(
                        layer, route.feature_id, chat_id, message.message_id
                    )
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
                            make_feature_state_key(layer.key, route_change.feature.feature_id),
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

    def _build_route_markup(
        self, layer: WatchedLayer, route: RouteFeature
    ) -> InlineKeyboardMarkup | None:
        feature_url = build_feature_url(
            layer.map_url,
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
                    "Failed to send change notification as reply for feature %s "
                    "in layer %s in chat %s. Sending standalone message instead.",
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
                make_feature_state_key(layer.key, route.feature_id),
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
            make_feature_state_key(layer.key, feature_id),
            {},
        )[chat_id] = message_id

    def _known_feature_count(self, layer: WatchedLayer) -> int:
        return sum(
            1
            for feature_id in self._state.known_feature_ids
            if is_feature_state_key_for_layer(feature_id, layer.key)
        )

    def _layer_status(self, layer: WatchedLayer) -> LayerStatus:
        return LayerStatus(
            title=layer.title,
            datalayer_url=self._settings.build_datalayer_url(layer),
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
            route = (
                routes[0]
                if routes
                else RouteFeature(
                    feature_id=f"test-notification-{layer.key}",
                    name=f"Тестовый {layer.route_label}",
                    description="Это тестовое уведомление бота.",
                    month="",
                    osmand_speed="",
                    geometry_type="LineString",
                    geometry={"type": "LineString", "coordinates": []},
                    properties={},
                )
            )
            await self.send_route_message(
                chat_id=chat_id,
                layer=layer,
                route=route,
                prefix=f"Тестовое уведомление для слоя {layer.title}.",
            )
