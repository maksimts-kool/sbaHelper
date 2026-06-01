from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Protocol

from umap.models import RouteSnapshot, utc_now_iso
from umap.settings import BotSettings


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
