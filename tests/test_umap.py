import sys
import types
import unittest
from os import environ
from types import SimpleNamespace
from unittest.mock import patch


def _install_runtime_stubs() -> None:
    sentry_sdk = types.ModuleType("sentry_sdk")
    sentry_sdk.init = lambda **kwargs: None
    sentry_sdk.set_tag = lambda *args, **kwargs: None
    sentry_sdk.capture_exception = lambda *args, **kwargs: None
    sentry_sdk.flush = lambda *args, **kwargs: None
    sys.modules.setdefault("sentry_sdk", sentry_sdk)

    aiogram = types.ModuleType("aiogram")
    aiogram_exceptions = types.ModuleType("aiogram.exceptions")

    class TelegramNetworkError(Exception):
        pass

    aiogram_exceptions.TelegramNetworkError = TelegramNetworkError
    aiogram.exceptions = aiogram_exceptions
    sys.modules.setdefault("aiogram", aiogram)
    sys.modules.setdefault("aiogram.exceptions", aiogram_exceptions)


_install_runtime_stubs()

from umap.config import (
    SOURCE_LAYER_ID_PROPERTY,
    SOURCE_LAYER_TITLE_PROPERTY,
    RouteSnapshot,
    collect_geojson_features,
    load_bot_settings,
    property_bool,
)
from umap.formatting import (
    build_change_descriptions,
    build_feature_url,
    format_route_change_notification,
    format_route_notification,
    replace_placeholders,
    route_emoji,
    transport_emoji,
)


def make_layer(formatter: str = "walk") -> SimpleNamespace:
    return SimpleNamespace(
        key=formatter,
        title=formatter,
        map_id="1415270",
        map_url="https://umap.openstreetmap.fr/ru/map/map_1415270",
        layer_id="",
        formatter=formatter,
        route_label="пеший маршрут" if formatter == "walk" else "веломаршрут",
        new_notification_title=(
            "Новый пеший маршрут добавлен!"
            if formatter == "walk"
            else "Новый веломаршрут добавлен!"
        ),
        change_notification_title=(
            "Изменен пеший маршрут!" if formatter == "walk" else "Изменен веломаршрут!"
        ),
        new_notification_emojis=(("5397916757333654639", "➕"),),
        change_notification_emojis=(("5395444784611480792", "✏️"),),
    )


class UmapFormattingTest(unittest.TestCase):
    def test_route_emoji_uses_custom_id_or_basic_fallback(self) -> None:
        self.assertEqual(
            route_emoji("name", "🚩"),
            '<tg-emoji emoji-id="5929468240668397096">🚩</tg-emoji>',
        )
        self.assertEqual(route_emoji("vald", "🏘"), "🏘")

    def test_walk_transport_emoji_supports_known_transport_types(self) -> None:
        self.assertEqual(transport_emoji("12 Bus"), "🚌")
        self.assertEqual(transport_emoji("3 Tram"), "🚋")
        self.assertEqual(transport_emoji("R31 Train"), "🚆")
        self.assertEqual(transport_emoji("1 Trolleybus"), "🚎")
        self.assertEqual(transport_emoji("Metro"), "")

    def test_replace_placeholders_from_routes_and_transport(self) -> None:
        instruction = "(Routes 1) - (Transport 1)\n(Routes 2) - (Transport 2)"

        self.assertEqual(
            replace_placeholders(instruction, ["12", "R31"], ["Bus", "Train"]),
            "12 - Bus\nR31 - Train",
        )

    def test_walk_formatter_keeps_legacy_instruction_format(self) -> None:
        feature = SimpleNamespace(
            feature_id="abc",
            name="Kivimurru",
            description="",
            month="",
            osmand_speed="",
            geometry_type="LineString",
            geometry={"type": "LineString", "coordinates": [[25.0, 59.0], [25.1, 59.1]]},
            properties={
                "Routes": "12 Bus, R31 Train",
                "date": "2026-05-31",
                "name": "Kivimurru",
                "Instruction": "There:\n(Routes 1)\nMustamae tee - Kivimurru\n16 min\n(Routes 2)",
            },
        )

        message = format_route_notification(make_layer("walk"), feature)

        self.assertIn("Новый пеший маршрут добавлен!", message)
        self.assertIn(f"{route_emoji('name', '🚩')} <b>31.05.26 Kivimurru</b>", message)
        self.assertIn(f"{route_emoji('length', '🛣')} <b>Длина:</b>", message)
        self.assertIn("<blockquote expandable>", message)
        self.assertIn("🚌 <b>12</b>", message)
        self.assertIn("🚆 <b>R31</b>", message)
        self.assertNotIn("12 Bus", message)

    def test_walk_formatter_prefers_structured_leg_fields(self) -> None:
        feature = SimpleNamespace(
            feature_id="abc",
            name="Kivimurru",
            description="",
            month="",
            osmand_speed="",
            geometry_type="LineString",
            geometry={"type": "LineString", "coordinates": []},
            properties={
                SOURCE_LAYER_TITLE_PROPERTY: "Raasiku vald",
                "destination_name": "Kivimurru",
                "date": "2026-05-31",
                "there_1_route": "12 Bus",
                "there_1_from": "Mustamae tee",
                "there_1_to": "Kivimurru",
                "there_1_minutes": "16",
                "back_1_route": "R31 Train",
                "back_1_from": "Kivimurru",
                "back_1_to": "Balti jaam",
                "back_1_duration": "22 min",
            },
        )

        message = format_route_notification(make_layer("walk"), feature)

        self.assertIn("🚶 <b>Туда:</b>", message)
        self.assertIn("🏘 <b>", message)
        self.assertIn("Raasiku vald", message)
        self.assertIn("🏠 <b>Обратно:</b>", message)
        self.assertIn("🚌 <b>12</b>", message)
        self.assertIn("📍 Mustamae tee - Kivimurru", message)
        self.assertIn("⏱ 16 min", message)
        self.assertIn("🚆 <b>R31</b>", message)

    def test_walk_change_formatter_includes_details_and_changes(self) -> None:
        feature = SimpleNamespace(
            feature_id="abc",
            name="Kivimurru",
            description="",
            month="",
            osmand_speed="",
            geometry_type="LineString",
            geometry={"type": "LineString", "coordinates": []},
            properties={
                SOURCE_LAYER_TITLE_PROPERTY: "Raasiku vald",
                "destination_name": "Kivimurru",
                "date": "2026-05-31",
                "there_1_route": "12 Bus",
                "there_1_from": "Mustamae tee",
                "there_1_to": "Kivimurru",
                "there_1_minutes": "16",
            },
        )

        message = format_route_change_notification(
            make_layer("walk"),
            feature,
            ["• Детали маршрута обновлены."],
        )

        self.assertIn("Изменен пеший маршрут!", message)
        self.assertIn("🚶 <b>Туда:</b>", message)
        self.assertIn("🚌 <b>12</b>", message)
        self.assertIn(f"{route_emoji('changes', '🔄')} <b>Что изменилось:</b>", message)
        self.assertIn("• Детали маршрута обновлены.", message)

    def test_walk_formatter_uses_planned_title_for_planned_checkbox(self) -> None:
        feature = SimpleNamespace(
            feature_id="abc",
            name="Kivimurru",
            description="",
            month="",
            osmand_speed="",
            geometry_type="LineString",
            geometry={"type": "LineString", "coordinates": []},
            properties={
                "planned": True,
                "date": "2026-05-31",
                "name": "Kivimurru",
            },
        )

        message = format_route_notification(make_layer("walk"), feature)

        self.assertIn("Новый план маршрута добавлен", message)
        self.assertNotIn("Новый пеший маршрут добавлен!", message)

    def test_planned_checkbox_parser_accepts_umap_truthy_values(self) -> None:
        self.assertTrue(property_bool({"planned": "true"}, "planned"))
        self.assertTrue(property_bool({"Planned": "on"}, "planned"))
        self.assertFalse(property_bool({"planned": "false"}, "planned"))

    def test_collect_geojson_features_from_umap_download(self) -> None:
        data = {
            "type": "umap",
            "layers": [
                {
                    "id": "layer-one",
                    "properties": {"name": "Raasiku vald"},
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"Instruction": "There:"},
                            "geometry": {"type": "Point", "coordinates": [25, 59]},
                            "id": "one",
                        }
                    ],
                }
            ],
        }

        features = collect_geojson_features(data)

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["id"], "one")
        self.assertEqual(features[0]["properties"][SOURCE_LAYER_ID_PROPERTY], "layer-one")
        self.assertEqual(features[0]["properties"][SOURCE_LAYER_TITLE_PROPERTY], "Raasiku vald")

    def test_collect_geojson_features_ignores_umap_map_metadata_feature(self) -> None:
        data = {
            "type": "Feature",
            "properties": {
                "name": "Walking map",
                "datalayers": [
                    {
                        "id": "layer-one",
                        "properties": {"name": "Raasiku vald"},
                    }
                ],
            },
            "geometry": {"type": "Point", "coordinates": [25, 59]},
        }

        self.assertEqual(collect_geojson_features(data), [])

    def test_settings_use_explicit_bike_and_walk_env_names(self) -> None:
        with patch.dict(
            environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "UMAP_BIKE_MAP_ID": "bike-map",
                "UMAP_BIKE_LAYER_ID": "bike-layer",
                "UMAP_BIKE_PLANS_LAYER_ID": "plans-layer",
                "UMAP_WALK_MAP_ID": "walk-map",
            },
            clear=True,
        ):
            settings = load_bot_settings()

        layers = {layer.key: layer for layer in settings.watched_layers}

        self.assertEqual(layers["2026"].map_id, "bike-map")
        self.assertEqual(layers["2026"].map_url, "http://u.osmfr.org/m/bike-map/")
        self.assertEqual(layers["2026"].layer_id, "bike-layer")
        self.assertEqual(layers["walk"].map_id, "walk-map")
        self.assertEqual(layers["walk"].map_url, "http://u.osmfr.org/m/walk-map/")
        self.assertEqual(layers["walk"].layer_id, "")
        self.assertEqual(layers["plans"].map_id, "bike-map")
        self.assertEqual(layers["plans"].layer_id, "plans-layer")

    def test_settings_fetch_walk_routes_from_all_map_layers(self) -> None:
        with patch.dict(
            environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "UMAP_BIKE_MAP_ID": "bike-map",
                "UMAP_BIKE_LAYER_ID": "bike-layer",
                "UMAP_WALK_MAP_ID": "walk-map",
            },
            clear=True,
        ):
            settings = load_bot_settings()

        layers = {layer.key: layer for layer in settings.watched_layers}

        self.assertEqual(layers["walk"].title, "Пешие маршруты")
        self.assertEqual(layers["walk"].layer_id, "")
        self.assertEqual(
            settings.build_datalayer_url(layers["walk"]),
            "https://umap.openstreetmap.fr/en/map/walk-map/geojson/",
        )

    def test_walk_feature_url_uses_source_datalayer_id(self) -> None:
        layer = make_layer("walk")
        feature = SimpleNamespace(
            name="Aruküla",
            properties={SOURCE_LAYER_ID_PROPERTY: "raasiku-layer"},
        )

        url = build_feature_url(layer.map_url, layer, feature)

        self.assertEqual(
            url,
            "https://umap.openstreetmap.fr/ru/map/map_1415270?datalayers=raasiku-layer&feature=Aruk%C3%BCla",
        )


class RouteChangeDescriptionsTest(unittest.TestCase):
    def test_change_descriptions_are_pure_and_specific(self) -> None:
        layer = SimpleNamespace(route_label="веломаршрут")
        previous = RouteSnapshot(
            feature_id="abc",
            name="Old",
            description="",
            month="May",
            osmand_speed="",
            geometry_type="LineString",
            geometry_hash="one",
            details_hash="details-one",
            length_km=10.0,
            planned=False,
        )
        current = RouteSnapshot(
            feature_id="abc",
            name="New",
            description="",
            month="May",
            osmand_speed="",
            geometry_type="LineString",
            geometry_hash="two",
            details_hash="details-two",
            length_km=10.2,
            planned=True,
        )

        changes = build_change_descriptions(layer, previous, current)

        self.assertTrue(any("Название" in change for change in changes))
        self.assertTrue(any("В планах" in change for change in changes))
        self.assertIn("• Детали маршрута обновлены.", changes)
        self.assertIn("• Геометрия маршрута обновлена.", changes)


if __name__ == "__main__":
    unittest.main()
