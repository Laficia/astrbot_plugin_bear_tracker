"""Regression checks for correct map selection and FFXIV coordinate scales."""
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

MODULE = Path(__file__).resolve().parents[1] / "map_renderer.py"
spec = importlib.util.spec_from_file_location("bear_map_renderer", MODULE)
maps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maps)


class MapCoordinatesTests(unittest.TestCase):
    def test_normal_map_center_and_asymmetric_coordinates(self):
        self.assertEqual(maps.map_xy_to_pixel(21.5, 21.5, 100, 2048, 2048), (1024, 1024))
        # X:23.5 / Y:26.0 is in the southern part of Northern Thanalan.
        x, y = maps.map_xy_to_pixel(23.5, 26.0, 100, 2048, 2048)
        self.assertAlmostEqual(x, 1123.9024390243903)
        self.assertAlmostEqual(y, 1248.780487804878)

    def test_heavensward_uses_exact_95_scale(self):
        center = 1 + 41 / (2 * .95)
        self.assertEqual(maps.map_xy_to_pixel(center, center, 95, 2048, 2048), (1024, 1024))
        x, _ = maps.map_xy_to_pixel(40, 20, 95, 2048, 2048)
        self.assertAlmostEqual(x, 1850.6926829268292)
        self.assertGreater(abs(x - ((40 - 1) / 43 * 2048)), 6)

    def test_invalid_coordinates_are_not_clamped(self):
        for x, y in [(None, 20), ("unknown", 20), (float("nan"), 20), (20, float("inf")), (0, 20), (43, 20), (True, 20)]:
            with self.subTest(x=x, y=y), self.assertRaises(maps.MapRenderError):
                maps.map_xy_to_pixel(x, y, 100, 2048, 2048)

    def test_reject_wrong_texture_and_unknown_or_ambiguous_maps(self):
        payload = io.BytesIO()
        Image.new("RGB", (1024, 1024)).save(payload, format="PNG")
        payload.seek(0)
        with self.assertRaises(maps.MapRenderError):
            maps.BearMapRenderer._read_map(payload)
        with tempfile.TemporaryDirectory() as temporary:
            renderer = maps.BearMapRenderer(temporary)
            for definition in [{"Region": "Unverified zone"}, {"Regions": ["Northern Thanalan", "Central Shroud"]}]:
                with self.subTest(definition=definition), self.assertRaises(maps.MapRenderError):
                    renderer.render({"DatabaseHunt": {"S": definition}}, "S", 20, 20)

    def test_real_map_selection_cache_and_pixel_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            renderer = maps.BearMapRenderer(temporary, map_size=960)
            payload = io.BytesIO()
            Image.new("RGB", (2048, 2048), "#c8b86c").save(payload, format="PNG")
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = payload.getvalue()
            resources = {"DatabaseHunt": {"Minhocao": {"Region": "Northern Thanalan"}}}
            with mock.patch.object(maps.urllib.request, "urlopen", return_value=response) as request:
                first = renderer.render(resources, "Minhocao", 23.5, 26, region_name="北萨纳兰")
                renderer.render(resources, "Minhocao", 23.6, 26, region_name="北萨纳兰")
            self.assertEqual(request.call_count, 1)
            self.assertIn("Northern%20Thanalan.png", request.call_args.args[0].full_url)
            pixel_x, pixel_y = maps.map_xy_to_pixel(23.5, 26, 100, 960, 960)
            with Image.open(first) as image:
                # Square output preserves the full map; no header offset or labels.
                self.assertEqual(image.size, (960, 960))
                self.assertEqual(image.getpixel((20, 20)), (200, 184, 108))
                self.assertEqual(image.getpixel((round(pixel_x) + 5, round(pixel_y))), (0, 0, 255))


if __name__ == "__main__":
    unittest.main()
