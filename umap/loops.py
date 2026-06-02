from __future__ import annotations

import asyncio
import logging
from typing import Any

from umap.errors import capture_exception, is_transient_network_error


logger = logging.getLogger(__name__)


async def watch_loop(service: Any, interval_seconds: int) -> None:
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
            if is_transient_network_error(error):
                logger.warning("Scheduled layer check skipped after transient network failure: %s", error)
            else:
                capture_exception(error)
                logger.exception("Scheduled layer check failed")

        await asyncio.sleep(interval_seconds)


async def watch_change_loop(service: Any, interval_seconds: int) -> None:
    while True:
        try:
            changes_by_layer = await service.check_for_route_changes(notify=True)
            for layer in service.watched_layers:
                layer_changes = changes_by_layer.get(layer.key, [])
                logger.info("Layer %s route changes checked: changed=%s", layer.title, len(layer_changes))
        except Exception as error:
            if is_transient_network_error(error):
                logger.warning("Scheduled route change check skipped after transient network failure: %s", error)
            else:
                capture_exception(error)
                logger.exception("Scheduled route change check failed")

        await asyncio.sleep(interval_seconds)
