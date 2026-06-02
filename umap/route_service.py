from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from umap.changes import build_change_descriptions
from umap.client import UmapClient
from umap.errors import capture_exception
from umap.formatting import (
    OPEN_ROUTE_BUTTON_TEXT,
    build_feature_url,
    format_route_change_notification,
    format_route_notification,
    format_status_message,
)
from umap.models import CheckResult, LayerStatus, RouteChange, RouteFeature, RouteSnapshot, utc_now_iso
from umap.settings import BotSettings, WatchedLayer
from umap.state import (
    AppState,
    StateStore,
    is_feature_state_key_for_layer,
    make_feature_state_key,
)


logger = logging.getLogger(__name__)


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
            if make_feature_state_key(layer.key, route.feature_id) not in self._state.known_feature_ids
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

    def _build_route_markup(self, layer: WatchedLayer, route: RouteFeature) -> InlineKeyboardMarkup | None:
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
            route = routes[0] if routes else RouteFeature(
                feature_id=f"test-notification-{layer.key}",
                name=f"Тестовый {layer.route_label}",
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
