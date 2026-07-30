import os
import tempfile
import unittest

from PIL import Image

from modules.cover_preflight import (
    BILIBILI_COVER43_SIZE,
    BILIBILI_COVER_SIZE,
    CoverPreflightError,
    prepare_bilibili_cover,
)


class CoverPreflightTests(unittest.TestCase):
    def _source(self, directory, name, mode, size, color):
        path = os.path.join(directory, name)
        Image.new(mode, size, color).save(path)
        return path

    def test_png_is_converted_to_real_jpeg_with_target_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory, "cover.png", "RGBA", (900, 900), (255, 0, 0, 96))

            result = prepare_bilibili_cover(source)

            self.assertEqual((result["width"], result["height"]), BILIBILI_COVER_SIZE)
            self.assertEqual(result["format"], "JPEG")
            self.assertEqual(result["source_format"], "PNG")
            with open(result["path"], "rb") as handle:
                self.assertEqual(handle.read(3), b"\xff\xd8\xff")

    def test_webp_is_converted_to_jpeg_under_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory, "cover.webp", "RGB", (2048, 1152), (20, 40, 60))

            result = prepare_bilibili_cover(source)

            self.assertLessEqual(result["size_bytes"], 5 * 1024 * 1024)
            self.assertTrue(result["path"].endswith("upload_cover.jpg"))

    def test_cover43_uses_independent_four_by_three_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory, "cover43.png", "RGB", (800, 600), (10, 100, 200))

            result = prepare_bilibili_cover(source, target_size=BILIBILI_COVER43_SIZE)

            self.assertEqual((result["width"], result["height"]), BILIBILI_COVER43_SIZE)
            self.assertTrue(result["path"].endswith("upload_cover43.jpg"))

    def test_invalid_image_reports_clear_preflight_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "broken.webp")
            with open(source, "wb") as handle:
                handle.write(b"not-an-image")

            with self.assertRaisesRegex(CoverPreflightError, "封面预检失败"):
                prepare_bilibili_cover(source)


if __name__ == "__main__":
    unittest.main()
