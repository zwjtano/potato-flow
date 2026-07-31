import asyncio
import concurrent.futures
import importlib
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch


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
            if self.url.endswith("/x/v2/reply/main"):
                return {
                    "cursor": {"is_end": True},
                    "replies": [{
                        "rpid": 9001,
                        "ctime": 123456,
                        "like": 7,
                        "rcount": 1,
                        "member": {
                            "uname": "观众甲",
                            "avatar": "https://example.com/avatar.jpg",
                        },
                        "content": {"message": "主评论"},
                        "replies": [{
                            "rpid": 9002,
                            "ctime": 123457,
                            "like": 1,
                            "member": {"uname": "观众乙", "avatar": ""},
                            "content": {"message": "子回复"},
                        }],
                    }],
                }
            if self.url.endswith("/x/v2/reply/add"):
                return {"reply": {"rpid": 9003}}
            if self.url.endswith("/x/msgfeed/reply"):
                return {"items": [{
                    "user": {"nickname": "评论者", "avatar": "reply.jpg"},
                    "item": {"source_content": "新评论"},
                    "reply_time": 123,
                }], "unread": 2}
            if self.url.endswith("/session_svr/get_sessions"):
                return {"session_list": [{
                    "account_info": {"name": "私信用户", "pic_url": "dm.jpg"},
                    "last_msg": {"content": '{"content":"你好"}', "timestamp": 124},
                    "unread_count": 3,
                }]}
            if self.url.endswith("/x/msgfeed/like"):
                return {"items": [{
                    "user": {"nickname": "点赞用户"},
                    "item": {"title": "点赞了你的视频"},
                    "like_time": 125,
                }], "unread": 4}
            if self.url.endswith("/x/msgfeed/sys-msg"):
                return {"items": [{"title": "系统通知", "content": "稿件已通过", "ctime": 126}]}
            if self.url.endswith("/x/web/archives"):
                return {
                    "page": {"pn": 1, "ps": 20, "count": 1},
                    "arc_audits": [{
                        "Archive": {
                            "aid": 123,
                            "bvid": "BV1test0000",
                            "title": "历史稿件",
                            "cover": "https://example.com/old.jpg",
                            "state": 0,
                            "state_desc": "已通过",
                            "duration": 60,
                            "ptime": 123456,
                        },
                        "Videos": [{"cid": 1}],
                    }],
                }
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
    def test_archive_center_lists_all_authenticated_account_archives(self):
        _FakeApi.calls = []
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
        ):
            ok, result = uploader.list_archives(status="all")

        self.assertTrue(ok)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["archives"][0]["bvid"], "BV1test0000")
        archives_call = next(call for call in _FakeApi.calls if call.url.endswith("/x/web/archives"))
        self.assertEqual(archives_call.params["status"], "all")

    def test_archive_detail_exposes_editable_metadata_and_exact_pages(self):
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
        ):
            ok, result = uploader.archive_detail("BV1test0000")

        self.assertTrue(ok)
        self.assertEqual(result["description"], "旧简介")
        self.assertEqual(result["partition_id"], "171")
        self.assertEqual(result["tags"], ["DOTA2", "直播录播"])
        self.assertEqual(
            [(page["page_number"], page["cid"]) for page in result["pages"]],
            [(1, 1), (2, 2)],
        )

    def test_archive_comments_can_be_read_and_manually_replied_to(self):
        _FakeApi.calls = []
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
        ):
            comments_ok, comments = uploader.archive_comments(aid=123)
            reply_ok, reply = uploader.reply_to_archive_comment(
                aid=123,
                root_rpid="9001",
                parent_rpid="9002",
                message=" 谢谢支持 ",
            )

        self.assertTrue(comments_ok)
        self.assertEqual(
            [item["rpid"] for item in comments["comments"]],
            ["9001", "9002"],
        )
        self.assertEqual(comments["comments"][1]["root_rpid"], "9001")
        self.assertTrue(reply_ok)
        self.assertEqual(reply, {"rpid": "9003", "message": "谢谢支持"})
        reply_call = next(
            call for call in _FakeApi.calls if call.url.endswith("/x/v2/reply/add")
        )
        self.assertEqual(reply_call.data["root"], 9001)
        self.assertEqual(reply_call.data["parent"], 9002)

    def test_message_overview_covers_reply_private_like_and_system(self):
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
        ):
            ok, overview = uploader.message_overview()

        self.assertTrue(ok)
        categories = overview["categories"]
        self.assertEqual(set(categories), {"reply", "private", "like", "system"})
        self.assertEqual(categories["reply"]["unread_count"], 2)
        self.assertEqual(categories["private"]["unread_count"], 3)
        self.assertEqual(categories["private"]["items"][0]["text"], "你好")
        self.assertEqual(categories["like"]["unread_count"], 4)
        self.assertEqual(categories["system"]["items"][0]["text"], "稿件已通过")

    def test_replace_source_uploads_one_page_and_preserves_all_other_pages(self):
        class ReplacementUploader:
            def __init__(self, pages, **_kwargs):
                self.pages = pages

            def on(self, _event):
                return lambda callback: callback

            async def upload_pages(self):
                return [{
                    "title": self.pages[0].title,
                    "desc": self.pages[0].description,
                    "filename": "new-part-two",
                    "cid": 202,
                }]

        _FakeApi.calls = []
        with TemporaryDirectory() as temp_dir:
            replacement = Path(temp_dir) / "burned.mp4"
            replacement.write_bytes(b"burned-video")
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
                    "VideoUploader",
                    ReplacementUploader,
                ),
                patch.object(
                    bilibili_uploader,
                    "get_app_subdir",
                    return_value=temp_dir,
                ),
            ):
                ok, result = uploader.replace_archive_page_source(
                    bvid="BV1test0000",
                    page_number=2,
                    video_file_path=str(replacement),
                )

        self.assertTrue(ok)
        self.assertEqual(result["old_cid"], 2)
        self.assertEqual(result["new_cid"], 202)
        edit_call = next(call for call in _FakeApi.calls if call.url.endswith("/web/edit"))
        self.assertEqual(
            [page["filename"] for page in edit_call.data["videos"]],
            ["part-one", "new-part-two"],
        )
        self.assertEqual(
            [page["title"] for page in edit_call.data["videos"]],
            ["13点旧标题", "14点旧标题"],
        )

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
                    new=AsyncMock(return_value="https://example.com/new.jpg"),
                ),
            ):
                ok, result = uploader.update_uploaded_metadata(
                    result={"aid": 123, "bvid": "BV1test"},
                    title="新标题",
                    description="新简介",
                    tags=["DOTA2", "帕吉", "直播录播"],
                    partition_id="171",
                    cover_file_path=str(cover),
                )

        self.assertTrue(ok)
        self.assertEqual(result["bvid"], "BV1test")
        self.assertEqual(result["part_count"], 2)
        edit_call = next(call for call in _FakeApi.calls if call.url.endswith("/web/edit"))
        self.assertEqual(edit_call.data["title"], "新标题")
        self.assertEqual(edit_call.data["desc"], "新简介")
        self.assertEqual(edit_call.data["cover"], "https://example.com/new.jpg")
        self.assertEqual(edit_call.data["tag"], "DOTA2,帕吉,直播录播")
        self.assertEqual(edit_call.data["tid"], 171)
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
