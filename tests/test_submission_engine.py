import importlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

submission_engine = importlib.import_module("modules.submission_engine")
upload_line_manager = importlib.import_module("modules.upload_line_manager")
bilibili_uploader = importlib.import_module("modules.bilibili_uploader")


class SubmissionEngineAdapterTests(unittest.TestCase):
    def test_recorder_binary_detects_windows_release_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "recorder-core" / "target" / "release" / "biliup.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"windows executable")
            with patch.object(submission_engine, "get_resource_root_dir", return_value=temp):
                self.assertEqual(submission_engine._recorder_binary(), str(binary))

    def _fake_binary(self, directory: str, result: dict) -> str:
        path = Path(directory) / "fake-recorder"
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
                patch.object(submission_engine, "_recorder_binary", return_value=binary),
                patch.object(submission_engine, "_write_cookie_file", return_value=str(cookie)),
                patch.object(
                    submission_engine,
                    "load_config",
                    return_value={
                        "BILIBILI_UPLOAD_LINE": "tx",
                        "BILIBILI_UPLOAD_LIMIT": 3,
                    },
                ),
            ):
                ok, result = submission_engine.upload_with_recorder(
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
            self.assertEqual(result["upload_engine"], "recorder")
            self.assertEqual(result["upload_line"], "tx")
            self.assertEqual(details[-1]["percent"], 50.0)
            self.assertIn("speed_bytes_per_second", details[-1])
            self.assertNotIn("speed_bytes_per_sec", details[-1])
            self.assertFalse(cookie.exists(), "临时投稿 Cookie 应在进程结束后删除")

    def test_append_keeps_existing_bvid(self):
        with tempfile.TemporaryDirectory() as temp:
            cookie = Path(temp) / "cookie.json"
            cookie.write_text("{}", encoding="utf-8")
            binary = self._fake_binary(temp, {"code": 0, "data": {}})
            with (
                patch.object(submission_engine, "_recorder_binary", return_value=binary),
                patch.object(submission_engine, "_write_cookie_file", return_value=str(cookie)),
                patch.object(
                    submission_engine,
                    "load_config",
                    return_value={
                        "BILIBILI_UPLOAD_LINE": "bldsa",
                        "BILIBILI_UPLOAD_LIMIT": 3,
                    },
                ),
            ):
                ok, result = submission_engine.upload_with_recorder(
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
            submission_engine,
            "load_config",
            return_value={"BILIBILI_UPLOAD_LINE": "unknown"},
        ):
            ok, result = submission_engine.upload_with_recorder(
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

    def test_main_uploader_routes_submission_to_recorder(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "video.flv"
            cover = Path(temp) / "cover.jpg"
            video.write_bytes(b"video")
            Image.new("RGB", (640, 400), (20, 40, 60)).save(cover)
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
                    "upload_with_recorder",
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


class UploadLineManagerTests(unittest.TestCase):
    def test_legacy_probe_cache_is_migrated_to_canonical_name(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy = Path(temp) / "biliup_line_probe.json"
            canonical = Path(temp) / "upload_line_probe.json"
            legacy.write_text('{"selected_line":"tx"}', encoding="utf-8")

            with patch.object(upload_line_manager, "get_app_subdir", return_value=temp):
                resolved = Path(upload_line_manager._cache_path())

            self.assertEqual(resolved, canonical)
            self.assertTrue(canonical.is_file())
            self.assertFalse(legacy.exists())
            self.assertEqual(
                json.loads(canonical.read_text(encoding="utf-8")),
                {"selected_line": "tx"},
            )

    def test_new_bilibili_probe_lines_are_supported(self):
        self.assertIn("akbd", upload_line_manager.SUPPORTED_LINES)
        self.assertIn("estx", upload_line_manager.SUPPORTED_LINES)
        self.assertEqual(upload_line_manager.LINE_LABELS["akbd"], "百度云（新线路）")
        self.assertEqual(upload_line_manager.LINE_LABELS["estx"], "腾讯云（新线路）")

    def test_probe_measures_upload_lines_strictly_one_at_a_time(self):
        active = 0
        maximum_active = 0
        lock = threading.Lock()
        measured = []

        class ProbeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "probe": {"post": 1},
                    "lines": [
                        {"os": "tx", "probe_url": "https://tx.example/probe"},
                        {"os": "alia", "probe_url": "https://alia.example/probe"},
                    ],
                }

        def measure(item, method, payload):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            measured.append(item["os"])
            with lock:
                active -= 1
            return {
                "line": item["os"],
                "ok": True,
                "supported": True,
                "elapsed_ms": 20 if item["os"] == "tx" else 30,
            }

        config = {}
        with (
            patch.object(upload_line_manager.requests, "get", return_value=ProbeResponse()),
            patch.object(upload_line_manager, "_measure_line", side_effect=measure),
            patch.object(upload_line_manager, "load_config", return_value=config),
            patch.object(upload_line_manager, "save_config", return_value=True),
            patch.object(upload_line_manager, "_write_json_atomic"),
            patch.object(
                upload_line_manager,
                "load_probe_state",
                return_value={"selected_line": "tx", "results": []},
            ),
        ):
            state = upload_line_manager.probe_and_select()

        self.assertEqual(measured, ["tx", "alia"])
        self.assertEqual(maximum_active, 1)
        self.assertEqual(config["BILIBILI_UPLOAD_LINE"], "tx")
        self.assertEqual(state["selected_line"], "tx")

    def test_manual_selection_is_global_and_persistent(self):
        config = {"BILIBILI_UPLOAD_LINE": "bldsa"}
        with (
            patch.object(upload_line_manager, "load_config", return_value=config),
            patch.object(upload_line_manager, "save_config", return_value=True) as save,
            patch.object(
                upload_line_manager,
                "load_probe_state",
                return_value={"selected_line": "tx", "results": []},
            ),
        ):
            state = upload_line_manager.select_upload_line("tx")
        self.assertEqual(config["BILIBILI_UPLOAD_LINE"], "tx")
        self.assertEqual(config["BILIBILI_UPLOAD_ENGINE"], "recorder")
        self.assertEqual(state["selected_line"], "tx")
        save.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
