from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from typing import Any

import httpx

from umap.errors import is_transient_network_error
from umap.models import RouteFeature, collect_geojson_features


logger = logging.getLogger(__name__)


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
        for attempt in range(1, self._retry_attempts + 1):
            try:
                response = await self._client.get(self._datalayer_url)
                response.raise_for_status()
                features = collect_geojson_features(response.json())
                return [parse_umap_feature(feature) for feature in features]
            except Exception as error:
                if not is_transient_network_error(error) or attempt == self._retry_attempts:
                    raise

                delay = self._retry_backoff_seconds * attempt + random.uniform(0.0, 0.5)
                logger.warning(
                    "Transient uMap fetch failure for %s, retrying in %.1fs (%s/%s): %s",
                    self._datalayer_url,
                    delay,
                    attempt,
                    self._retry_attempts,
                    error,
                )
                await asyncio.sleep(delay)

        raise RuntimeError("unreachable")


def parse_umap_feature(feature: dict[str, Any]) -> RouteFeature:
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
