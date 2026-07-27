import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
Y2A_ROOT = ROOT / "y2a-auto"
if str(Y2A_ROOT) not in sys.path:
    sys.path.insert(0, str(Y2A_ROOT))

biliup_uploader = importlib.import_module("modules.biliup_uploader")
biliup_line_manager = importlib.import_module("modules.biliup_line_manager")
bilibili_uploader = importlib.import_module("modules.bilibili_uploader")


class BiliupUploaderAdapterTests(unittest.TestCase):
    def _fake_binary(self, directory: str, result: dict) -> str:
        path = Path(directory) / "fake-biliup"
        path.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'POTATOFLOW_PROGRESS={\"uploaded_bytes\":50,\"total_bytes\":100,\"percent\":50}'\n"
            f"printf '%s\\n' 'POTATOFLOW_RESULT={json.dumps(result, ensure_ascii=False)}'\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_new_submission_returns_bvid_and_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            cookie = Path(temp) / "cookie.json"
            cookie.write_text("{}", encoding="utf-8")
            binary = self._fake_binary(
                temp,
                {"code": 0, "data": {"aid": 123, "bvid": "BV1TEST"}},
            )
            details = []
            with (
                patch.object(biliup_uploader, "_biliup_binary", return_value=binary),
                patch.object(biliup_uploader, "_write_cookie_file", return_value=str(cookie)),
                patch.object(
                    biliup_uploader,
                    "load_config",
                    return_value={
                        "BILIBILI_UPLOAD_LINE": "tx",
                        "BILIBILI_UPLOAD_LIMIT": 3,
                    },
                ),
            ):
                ok, result = biliup_uploader.upload_with_biliup(
                    cookie_file="unused.json",
                    video_paths=["video.flv"],
                    cover_file="cover.jpg",
                    title="测试标题",
                    description="测试简介",
                    tags=["直播", "DOTA2"],
                    partition_id=171,
                    page_titles=["13点 大鱼人翻盘"],
                    progress_detail_callback=details.append,
                )
            self.assertTrue(ok)
            self.assertEqual(result["bvid"], "BV1TEST")
            self.assertEqual(result["upload_engine"], "biliup")
            self.assertEqual(result["upload_line"], "tx")
            self.assertEqual(details[-1]["percent"], 50.0)
            self.assertFalse(cookie.exists(), "临时 Biliup Cookie 应在进程结束后删除")

    def test_append_keeps_existing_bvid(self):
        with tempfile.TemporaryDirectory() as temp:
            cookie = Path(temp) / "cookie.json"
            cookie.write_text("{}", encoding="utf-8")
            binary = self._fake_binary(temp, {"code": 0, "data": {}})
            with (
                patch.object(biliup_uploader, "_biliup_binary", return_value=binary),
                patch.object(biliup_uploader, "_write_cookie_file", return_value=str(cookie)),
                patch.object(
                    biliup_uploader,
                    "load_config",
                    return_value={
                        "BILIBILI_UPLOAD_LINE": "bldsa",
                        "BILIBILI_UPLOAD_LIMIT": 3,
                    },
                ),
            ):
                ok, result = biliup_uploader.upload_with_biliup(
                    cookie_file="unused.json",
                    video_paths=["p2.flv"],
                    cover_file="cover.jpg",
                    title="不会覆盖原标题",
                    description="不会覆盖原简介",
                    tags=["直播"],
                    partition_id=171,
                    existing_submission={
                        "bvid": "BV1OLD",
                        "aid": 99,
                        "part_count": 1,
                    },
                )
            self.assertTrue(ok)
            self.assertEqual(result["bvid"], "BV1OLD")
            self.assertEqual(result["part_count"], 2)

    def test_rejects_unknown_cached_line_before_starting_process(self):
        with patch.object(
            biliup_uploader,
            "load_config",
            return_value={"BILIBILI_UPLOAD_LINE": "unknown"},
        ):
            ok, result = biliup_uploader.upload_with_biliup(
                cookie_file="unused.json",
                video_paths=["video.flv"],
                cover_file="cover.jpg",
                title="标题",
                description="简介",
                tags=[],
                partition_id=171,
            )
        self.assertFalse(ok)
        self.assertIn("不受支持", result)

    def test_main_uploader_routes_submission_to_biliup(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "video.flv"
            cover = Path(temp) / "cover.jpg"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            expected = {"bvid": "BV1FLOW", "aid": 1}
            with (
                patch.object(bilibili_uploader, "configure_bilibili_runtime"),
                patch.object(bilibili_uploader, "load_credential_from_file", return_value=object()),
                patch.object(
                    bilibili_uploader,
                    "validate_credential_remote",
                    return_value=(True, "ok"),
                ),
                patch.object(
                    bilibili_uploader,
                    "load_config",
                    return_value={
                        "BILIBILI_UPLOAD_ENGINE": "biliup",
                        "BILIBILI_UPLOAD_LINE": "tx",
                    },
                ),
                patch.object(
                    bilibili_uploader,
                    "upload_with_biliup",
                    return_value=(True, expected),
                ) as delegated,
            ):
                uploader = bilibili_uploader.BilibiliUploader("cookie.json")
                ok, result = uploader._upload_video_unlocked(
                    video_file_path=str(video),
                    cover_file_path=str(cover),
                    title="标题",
                    description="简介",
                    tags=["直播"],
                    partition_id=171,
                    page_titles=["13点 标题"],
                    is_original=True,
                )
            self.assertTrue(ok)
            self.assertEqual(result, expected)
            delegated.assert_called_once()
            self.assertEqual(delegated.call_args.kwargs["page_titles"], ["13点 标题"])


class BiliupLineManagerTests(unittest.TestCase):
    def test_manual_selection_is_global_and_persistent(self):
        config = {"BILIBILI_UPLOAD_LINE": "bldsa"}
        with (
            patch.object(biliup_line_manager, "load_config", return_value=config),
            patch.object(biliup_line_manager, "save_config", return_value=True) as save,
            patch.object(
                biliup_line_manager,
                "load_probe_state",
                return_value={"selected_line": "tx", "results": []},
            ),
        ):
            state = biliup_line_manager.select_upload_line("tx")
        self.assertEqual(config["BILIBILI_UPLOAD_LINE"], "tx")
        self.assertEqual(config["BILIBILI_UPLOAD_ENGINE"], "biliup")
        self.assertEqual(state["selected_line"], "tx")
        save.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
