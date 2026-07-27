import asyncio
import concurrent.futures
import importlib
import sys
import threading
import time
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

    def test_global_upload_queue_serializes_parallel_callers(self):
        active = 0
        maximum_active = 0
        guard = threading.Lock()
        statuses = {"one": [], "two": []}

        def fake_upload(**_kwargs):
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.08)
            with guard:
                active -= 1
            return True, {"bvid": "BV1queue"}

        with TemporaryDirectory() as temp_dir, patch.object(
            bilibili_uploader,
            "get_app_subdir",
            return_value=temp_dir,
        ), patch.object(
            bilibili_uploader.BilibiliUploader,
            "_upload_video_unlocked",
            side_effect=fake_upload,
        ):
            uploader = bilibili_uploader.BilibiliUploader("cookies.json")

            def run(name):
                return uploader.upload_video(
                    video_file_path="video.flv",
                    cover_file_path="cover.jpg",
                    title="title",
                    description="description",
                    tags=[],
                    partition_id=171,
                    queue_status_callback=statuses[name].append,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run, ("one", "two")))

        self.assertEqual(maximum_active, 1)
        self.assertEqual(statuses["one"], ["queued", "uploading"])
        self.assertEqual(statuses["two"], ["queued", "uploading"])
        self.assertTrue(all(result[0] for result in results))


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
                    return_value="https://example.com/new.jpg",
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
