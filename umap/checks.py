from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from umap.client import UmapClient
from umap.errors import capture_exception
from umap.settings import BotSettings, WatchedLayer, load_bot_settings
from umap.state import build_state_store


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UmapCheckResult:
    name: str
    ok: bool
    message: str


async def _check_layer(settings: BotSettings, layer: WatchedLayer) -> UmapCheckResult:
    client = UmapClient(
        datalayer_url=settings.build_datalayer_url(layer),
        timeout_seconds=settings.request_timeout_seconds,
        retry_attempts=settings.request_retry_attempts,
        retry_backoff_seconds=settings.request_retry_backoff_seconds,
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
