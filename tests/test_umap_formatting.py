import unittest
from os import environ
from types import SimpleNamespace
from unittest.mock import patch

from umap.models import collect_geojson_features
from umap.settings import load_bot_settings
from umap.walk.formatter import format_route_notification, replace_placeholders


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
            "Новый пеший маршрут добавлен!" if formatter == "walk" else "Новый веломаршрут добавлен!"
        ),
        change_notification_title=(
            "Изменен пеший маршрут!" if formatter == "walk" else "Изменен веломаршрут!"
        ),
        new_notification_emojis=(("5397916757333654639", "➕"),),
        change_notification_emojis=(("5395444784611480792", "✏️"),),
    )


class UmapFormattingTest(unittest.TestCase):
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
        self.assertIn("🚩 <b>31.05.26 Kivimurru</b>", message)
        self.assertIn("🛣 <b>Длина:</b>", message)
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
        self.assertIn("🏠 <b>Обратно:</b>", message)
        self.assertIn("🚌 <b>12</b>", message)
        self.assertIn("📍 Mustamae tee - Kivimurru", message)
        self.assertIn("⏱ 16 min", message)
        self.assertIn("🚆 <b>R31</b>", message)

    def test_collect_geojson_features_from_umap_download(self) -> None:
        data = {
            "type": "umap",
            "layers": [
                {
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"Instruction": "There:"},
                            "geometry": {"type": "Point", "coordinates": [25, 59]},
                            "id": "one",
                        }
                    ]
                }
            ],
        }

        features = collect_geojson_features(data)

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["id"], "one")

    def test_settings_use_explicit_bike_and_walk_env_names(self) -> None:
        with patch.dict(
            environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "UMAP_STATE_MONGODB_URI": "mongodb://example",
                "UMAP_BIKE_MAP_ID": "bike-map",
                "UMAP_BIKE_LAYER_ID": "bike-layer",
                "UMAP_BIKE_PLANS_LAYER_ID": "plans-layer",
                "UMAP_WALK_MAP_ID": "walk-map",
                "UMAP_WALK_LAYER_ID": "walk-layer",
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
        self.assertEqual(layers["walk"].layer_id, "walk-layer")
        self.assertEqual(layers["plans"].map_id, "bike-map")
        self.assertEqual(layers["plans"].layer_id, "plans-layer")


if __name__ == "__main__":
    unittest.main()
