from __future__ import annotations

from unittest import TestCase

from umap.app.service import (
    LEGACY_DEFAULT_LAYER_KEY,
    RouteFeature,
    UmapCheckResult,
    WatchedLayer,
    build_feature_url,
    feature_length_km,
    is_feature_state_key_for_layer,
    make_feature_state_key,
    normalize_feature_state_key,
    result_exit_code,
)


class RouteModelTests(TestCase):
    def test_feature_length_for_line_string(self) -> None:
        feature = RouteFeature(
            feature_id="route-1",
            name="Route 1",
            description="",
            month="",
            osmand_speed="",
            geometry_type="LineString",
            geometry={"type": "LineString", "coordinates": [[0, 0], [0, 1]]},
            properties={},
        )

        length = feature_length_km(feature)

        self.assertIsNotNone(length)
        self.assertAlmostEqual(length or 0, 111.2, places=1)


class FormatterTests(TestCase):
    def test_build_feature_url_adds_layer_and_feature(self) -> None:
        layer = WatchedLayer(
            key="2026",
            title="2026",
            layer_id="layer-id",
            route_label="route",
            new_notification_title="New route",
            change_notification_title="Changed route",
            new_notification_emojis=(),
            change_notification_emojis=(),
        )
        feature = RouteFeature(
            feature_id="route-1",
            name="Route A",
            description="",
            month="",
            osmand_speed="",
            geometry_type="LineString",
            geometry={},
            properties={},
        )

        url = build_feature_url("http://u.osmfr.org/m/1393155/?zoom=10", layer, feature)

        self.assertEqual(
            url,
            "http://u.osmfr.org/m/1393155/?zoom=10&datalayers=layer-id&feature=Route+A",
        )


class StateKeyTests(TestCase):
    def test_scoped_feature_keys_are_layer_aware(self) -> None:
        feature_key = make_feature_state_key("plans", "route/42")

        self.assertTrue(is_feature_state_key_for_layer(feature_key, "plans"))
        self.assertFalse(is_feature_state_key_for_layer(feature_key, "2026"))

    def test_legacy_feature_keys_normalize_to_default_layer(self) -> None:
        feature_key = normalize_feature_state_key("old-feature-id")

        self.assertTrue(is_feature_state_key_for_layer(feature_key, LEGACY_DEFAULT_LAYER_KEY))


class CheckResultTests(TestCase):
    def test_result_exit_code_distinguishes_config_and_runtime_failures(self) -> None:
        self.assertEqual(result_exit_code([UmapCheckResult("settings", False, "missing")]), 2)
        self.assertEqual(
            result_exit_code(
                [
                    UmapCheckResult("settings", True, "ok"),
                    UmapCheckResult("2026", False, "timeout"),
                ]
            ),
            1,
        )
        self.assertEqual(result_exit_code([UmapCheckResult("settings", True, "ok")]), 0)
