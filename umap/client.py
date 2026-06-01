from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from umap.errors import is_transient_network_error
from umap.models import SOURCE_LAYER_ID_PROPERTY, RouteFeature, collect_geojson_features


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


def parse_umap_feature(feature: dict[str, Any], *, scope_source_layer: bool = False) -> RouteFeature:
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
    )
