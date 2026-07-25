import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
Y2A_ROOT = ROOT / "y2a-auto"
if str(Y2A_ROOT) not in sys.path:
    sys.path.insert(0, str(Y2A_ROOT))

try:
    bilibili_uploader = importlib.import_module("modules.bilibili_uploader")
except ModuleNotFoundError:
    bilibili_uploader = None


class _Credential:
    bili_jct = "csrf-token"


class _FakeApi:
    calls = []

    def __init__(self, **kwargs):
        self.url = kwargs["url"]
        self.params = {}
        self.data = {}

    def update_params(self, **kwargs):
        self.params.update(kwargs)
        return self

    def update_data(self, **kwargs):
        self.data.update(kwargs)
        return self

    @property
    def result(self):
        async def resolve():
            self.__class__.calls.append(self)
            if self.url.endswith("/archive/view"):
                return {
                    "archive": {
                        "aid": 123,
                        "bvid": "BV1test",
                        "title": "旧标题",
                        "desc": "旧简介",
                        "cover": "https://example.com/old.jpg",
                        "copyright": 1,
                        "tid": 171,
                        "tag": "DOTA2,直播录播",
                    },
                    "videos": [
                        {"title": "13点旧标题", "desc": "", "filename": "part-one", "cid": 1},
                        {"title": "14点旧标题", "desc": "", "filename": "part-two", "cid": 2},
                    ],
                }
            return {"aid": 123, "bvid": "BV1test"}

        return resolve()


@unittest.skipIf(
    bilibili_uploader is None,
    "本机未安装完整 Bilibili SDK 运行依赖",
)
class BilibiliUploadProgressTests(unittest.TestCase):
    def test_chunk_progress_reports_bytes_speed_and_eta(self):
        class Page:
            def get_size(self):
                return 10 * 1024 * 1024

        page = Page()
        with patch.object(bilibili_uploader.time, "monotonic", side_effect=[10.0, 12.0]):
            progress = bilibili_uploader._BilibiliChunkProgress([page])
            detail = progress.record({
                "page": page,
                "chunk_number": 0,
                "total_chunk_count": 2,
                "offset": 0,
                "chunk_size": 4 * 1024 * 1024,
                "page_size": 10 * 1024 * 1024,
            })

        self.assertEqual(detail["uploaded_bytes"], 4 * 1024 * 1024)
        self.assertEqual(detail["total_bytes"], 10 * 1024 * 1024)
        self.assertAlmostEqual(detail["speed_bytes_per_second"], 2 * 1024 * 1024)
        self.assertAlmostEqual(detail["eta_seconds"], 3.0)


@unittest.skipIf(
    bilibili_uploader is None,
    "本机未安装完整 Bilibili SDK 运行依赖",
)
class PublishedMetadataEditorTests(unittest.TestCase):
    def test_update_preserves_every_existing_page(self):
        _FakeApi.calls = []
        with TemporaryDirectory() as temp_dir:
            cover = Path(temp_dir) / "cover.jpg"
            cover.write_bytes(b"new-cover")
            uploader = bilibili_uploader.BilibiliUploader("cookies.json")
            with (
                patch.object(bilibili_uploader, "configure_bilibili_runtime"),
                patch.object(
                    bilibili_uploader,
                    "load_credential_from_file",
                    return_value=_Credential(),
                ),
                patch.object(
                    bilibili_uploader,
                    "validate_credential_remote",
                    return_value=(True, "ok"),
                ),
                patch.object(bilibili_uploader, "Api", _FakeApi),
                patch.object(
                    bilibili_uploader.video_uploader,
                    "upload_cover",
                    return_value=asyncio.sleep(
                        0,
                        result="https://example.com/new.jpg",
                    ),
                ),
            ):
                ok, result = uploader.update_uploaded_metadata(
                    result={"aid": 123, "bvid": "BV1test"},
                    title="新标题",
                    description="新简介",
                    cover_file_path=str(cover),
                )

        self.assertTrue(ok)
        self.assertEqual(result["bvid"], "BV1test")
        self.assertEqual(result["part_count"], 2)
        edit_call = next(call for call in _FakeApi.calls if call.url.endswith("/web/edit"))
        self.assertEqual(edit_call.data["title"], "新标题")
        self.assertEqual(edit_call.data["desc"], "新简介")
        self.assertEqual(edit_call.data["cover"], "https://example.com/new.jpg")
        self.assertEqual(
            [page["filename"] for page in edit_call.data["videos"]],
            ["part-one", "part-two"],
        )
        self.assertEqual(
            [page["title"] for page in edit_call.data["videos"]],
            ["13点旧标题", "14点旧标题"],
        )

    def test_update_aborts_when_bilibili_does_not_return_pages(self):
        class MissingPagesApi(_FakeApi):
            @property
            def result(self):
                async def resolve():
                    if self.url.endswith("/archive/view"):
                        return {"archive": {"aid": 123, "tid": 171}}
                    return {}

                return resolve()

        uploader = bilibili_uploader.BilibiliUploader("cookies.json")
        with (
            patch.object(bilibili_uploader, "configure_bilibili_runtime"),
            patch.object(
                bilibili_uploader,
                "load_credential_from_file",
                return_value=_Credential(),
            ),
            patch.object(
                bilibili_uploader,
                "validate_credential_remote",
                return_value=(True, "ok"),
            ),
            patch.object(bilibili_uploader, "Api", MissingPagesApi),
        ):
            ok, error = uploader.update_uploaded_metadata(
                result={"aid": 123, "bvid": "BV1test"},
                title="新标题",
                description="新简介",
            )

        self.assertFalse(ok)
        self.assertIn("保护现有视频", error)


if __name__ == "__main__":
    unittest.main()
