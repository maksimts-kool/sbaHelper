import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from downloader.formatting import build_download_progress_line, build_video_caption, format_count
from downloader.metadata import normalize_video_info, parse_compact_count
from sbahelper.startup import print_results
from umap.changes import build_change_descriptions
from umap.models import RouteSnapshot


class RefactoredHelpersTest(unittest.TestCase):
    def test_downloader_count_and_progress_formatting(self) -> None:
        self.assertEqual(format_count(999), "999")
        self.assertEqual(format_count(1_200), "1.2K")
        self.assertEqual(format_count(2_000_000), "2M")
        self.assertIn("100%", build_download_progress_line(125))

    def test_downloader_caption_prevents_auto_links(self) -> None:
        info = SimpleNamespace(
            title="Watch https://example.com #tag",
            uploader="@creator",
            duration=65,
            view_count=1_500,
            like_count=None,
        )

        caption = build_video_caption(info)

        self.assertIn("1:⁠05", caption)
        self.assertIn("h⁠ttps://example.com", caption)
        self.assertIn("#⁠tag", caption)
        self.assertIn("@⁠creator", caption)
        self.assertIn("1.5K просмотров", caption)

    def test_metadata_normalizes_facebook_title_stats(self) -> None:
        info = normalize_video_info(
            "https://www.facebook.com/reel/123",
            {
                "title": "1.2K views 45 reactions | Real title | Author Name",
                "uploader": "",
                "duration": 10,
            },
            10,
        )

        self.assertEqual(info.title, "Real title")
        self.assertEqual(info.uploader, "Author Name")
        self.assertEqual(info.view_count, 1200)
        self.assertEqual(info.like_count, 45)

    def test_metadata_parses_compact_counts(self) -> None:
        self.assertEqual(parse_compact_count("1.5K"), 1500)
        self.assertEqual(parse_compact_count("2 M"), 2_000_000)
        self.assertIsNone(parse_compact_count("many"))

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

    def test_shared_startup_printer_supports_warnings(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            print_results(
                [
                    SimpleNamespace(service="ok", ok=True, message="fine", blocks_startup=False),
                    SimpleNamespace(service="warn", ok=False, message="soft", blocks_startup=False),
                    SimpleNamespace(name="fail", ok=False, message="hard", blocks_startup=True),
                ]
            )

        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "OK ok: fine",
                "WARN warn: soft",
                "FAIL fail: hard",
            ],
        )


if __name__ == "__main__":
    unittest.main()
