import io
import json
import re
import subprocess
import tempfile
import threading
import time
import types
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from danmaku_pipeline import (
    DanmakuComment,
    build_ass,
    burn_ass,
    format_comments_for_ai,
    inspect_danmaku_xml,
    parse_danmaku_xml,
    select_summary_comments,
    danmaku_burn_slot,
    probe_encoding_capabilities,
)
import danmaku_pipeline


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<i>
  <d p="1.250,1,25,16711680,0,0,1,0">第一条弹幕</d>
  <d p="2.500,5,25,16777215,0,0,2,0">顶部弹幕</d>
  <d p="3.750,4,25,65280,0,0,3,0">底部弹幕</d>
</i>
"""


class DanmakuPipelineTests(unittest.TestCase):
    def test_cpu_query_reports_model_and_logical_cores(self):
        with patch("danmaku_pipeline.platform.system", return_value="Windows"), patch(
            "danmaku_pipeline.platform.processor", return_value="Intel Core i7-9700"
        ), patch(
            "danmaku_pipeline.os.cpu_count", return_value=8
        ):
            cpu = danmaku_pipeline._cpu_device()

        self.assertEqual(cpu, {"name": "Intel Core i7-9700", "logical_cores": 8})

    def test_windows_gpu_query_classifies_nvidia_amd_and_intel(self):
        output = json.dumps([
            {"Name": "NVIDIA GeForce RTX 2060", "DriverVersion": "32.0.15.7680", "PNPDeviceID": "PCI\\VEN_10DE"},
            {"Name": "AMD Radeon 780M", "DriverVersion": "31.0", "PNPDeviceID": "PCI\\VEN_1002"},
            {"Name": "Intel UHD Graphics 630", "DriverVersion": "30.0", "PNPDeviceID": "PCI\\VEN_8086"},
        ])
        result = types.SimpleNamespace(returncode=0, stdout=output, stderr="")
        with patch("danmaku_pipeline.os.name", "nt"), patch(
            "danmaku_pipeline.shutil.which", return_value="powershell.exe"
        ), patch("danmaku_pipeline.subprocess.run", return_value=result):
            devices = danmaku_pipeline._windows_graphics_devices()

        self.assertEqual([device["backend"] for device in devices], ["nvidia", "amd", "intel"])

    def test_nvidia_device_query_reports_model_and_driver(self):
        result = types.SimpleNamespace(
            returncode=0,
            stdout="NVIDIA GeForce RTX 2060, 576.80\n",
            stderr="",
        )
        with patch("danmaku_pipeline.shutil.which", return_value="nvidia-smi"), patch(
            "danmaku_pipeline.subprocess.run", return_value=result
        ) as run:
            devices = danmaku_pipeline._nvidia_devices()

        self.assertEqual(
            devices,
            [{"name": "NVIDIA GeForce RTX 2060", "driver": "576.80"}],
        )
        self.assertIn("--query-gpu=name,driver_version", run.call_args.args[0])

    def test_encoder_probe_uses_amf_compatible_frame_size(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("danmaku_pipeline._cpu_device", return_value={"name": "Test CPU", "logical_cores": 8}), patch(
            "danmaku_pipeline._graphics_devices", return_value=[]
        ), patch(
            "danmaku_pipeline.subprocess.run", side_effect=fake_run
        ):
            probe_encoding_capabilities(
                "test-ffmpeg-probe-size", force_refresh=True
            )

        self.assertTrue(commands)
        for command in commands:
            source = command[command.index("-i") + 1]
            self.assertNotIn("s=64x64", source)
        hardware_commands = [command for command in commands if "libx264" not in command]
        self.assertTrue(hardware_commands)
        for command in hardware_commands:
            source = command[command.index("-i") + 1]
            self.assertIn("s=640x360", source)
        for command in commands:
            if command not in hardware_commands:
                source = command[command.index("-i") + 1]
                self.assertIn("s=128x128", source)
        amf_command = next(command for command in commands if "h264_amf" in command)
        self.assertEqual(amf_command[amf_command.index("-rc") + 1], "qvbr")
        self.assertIn("-qvbr_quality_level", amf_command)
        self.assertNotIn("-qp_i", amf_command)

    def test_encoder_probe_prefers_configured_available_backend(self):
        def fake_run(command, **_kwargs):
            encoder = command[command.index("-c:v") + 1]
            available = encoder in {"libx264", "h264_qsv"}
            return types.SimpleNamespace(
                returncode=0 if available else 1,
                stdout="",
                stderr="" if available else "encoder unavailable",
            )

        with patch("danmaku_pipeline._cpu_device", return_value={"name": "Test CPU", "logical_cores": 8}), patch(
            "danmaku_pipeline._graphics_devices", return_value=[]
        ), patch(
            "danmaku_pipeline.subprocess.run", side_effect=fake_run
        ):
            result = probe_encoding_capabilities(
                "test-ffmpeg-qsv", preferred="intel", force_refresh=True
            )

        self.assertEqual(result["recommendation"]["id"], "intel")
        self.assertEqual(result["recommendation"]["quality_name"], "global_quality")

    def test_encoder_probe_reports_nvidia_model_and_uses_compatible_size(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            encoder = command[command.index("-c:v") + 1]
            return types.SimpleNamespace(
                returncode=0 if encoder in {"libx264", "h264_nvenc"} else 1,
                stdout="",
                stderr="" if encoder == "h264_nvenc" else "encoder unavailable",
            )

        devices = [{"name": "NVIDIA GeForce RTX 2060", "driver": "576.80"}]
        graphics = [{**device, "backend": "nvidia"} for device in devices]
        with patch("danmaku_pipeline._cpu_device", return_value={"name": "Test CPU", "logical_cores": 8}), patch(
            "danmaku_pipeline._graphics_devices", return_value=graphics
        ), patch(
            "danmaku_pipeline.subprocess.run", side_effect=fake_run
        ):
            result = probe_encoding_capabilities(
                "test-ffmpeg-rtx-2060", force_refresh=True
            )

        nvidia = next(item for item in result["capabilities"] if item["id"] == "nvidia")
        self.assertTrue(nvidia["available"])
        self.assertEqual(nvidia["devices"], graphics)
        self.assertEqual(nvidia["probe_size"], "640x360")
        self.assertIn("RTX 2060", nvidia["label"])
        self.assertIn("驱动 576.80", nvidia["label"])
        self.assertEqual(result["recommendation"]["id"], "nvidia")

    def test_hardware_burn_failure_falls_back_to_cpu_medium_crf20(self):
        commands = []

        class FakeProcess:
            def __init__(self, command, **_kwargs):
                commands.append(command)
                encoder = command[command.index("-c:v") + 1]
                self.returncode = 1 if encoder == "h264_nvenc" else 0
                if self.returncode == 0:
                    Path(command[-1]).write_bytes(b"cpu-fallback")
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("nvenc failed" if self.returncode else "")

            def wait(self):
                return self.returncode

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video, ass, output = root / "clip.flv", root / "clip.ass", root / "clip.mp4"
            video.write_bytes(b"video")
            ass.write_text("[Script Info]", encoding="utf-8")
            updates = []
            with patch(
                "danmaku_pipeline.subprocess.run",
                return_value=types.SimpleNamespace(returncode=0, stdout="0\n", stderr=""),
            ), patch("danmaku_pipeline.subprocess.Popen", FakeProcess):
                burn_ass(video, ass, output, encoder="nvidia", preset="p5", crf=18, progress_callback=updates.append)

        self.assertTrue(any(item.get("encoder_fallback") for item in updates))
        self.assertEqual(commands[-1][commands[-1].index("-c:v") + 1], "libx264")
        self.assertIn("medium", commands[-1])
        self.assertIn("20", commands[-1])

    def test_burn_reports_percent_speed_and_eta(self):
        popen_options = []

        class FakeProcess:
            def __init__(self, command, **_kwargs):
                popen_options.append(_kwargs)
                Path(command[-1]).write_bytes(b"burned-video")
                self.stdout = io.StringIO(
                    "[Parsed_subtitles] warning that must be drained\n"
                    "out_time_us=30000000\nspeed=2.0x\nprogress=continue\n"
                    "out_time_us=60000000\nspeed=2.0x\nprogress=end\n"
                )
                self.stderr = io.StringIO("")

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.flv"
            ass = root / "clip.ass"
            output = root / "clip.mp4"
            video.write_bytes(b"video")
            ass.write_text("[Script Info]", encoding="utf-8")
            updates = []
            with patch(
                "danmaku_pipeline.subprocess.run",
                return_value=types.SimpleNamespace(
                    returncode=0,
                    stdout="60.0\n",
                    stderr="",
                ),
            ), patch("danmaku_pipeline.subprocess.Popen", FakeProcess):
                result = burn_ass(
                    video,
                    ass,
                    output,
                    progress_callback=updates.append,
                )

        self.assertEqual(result, output)
        self.assertEqual(updates[0]["percent"], 50.0)
        self.assertEqual(updates[0]["encode_speed"], 2.0)
        self.assertEqual(updates[0]["eta_seconds"], 15.0)
        self.assertEqual(updates[-1]["percent"], 100.0)
        self.assertEqual(updates[-1]["eta_seconds"], 0.0)
        self.assertIs(popen_options[0]["stderr"], subprocess.STDOUT)

    def test_burn_queue_serializes_multiple_files(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def worker():
            nonlocal active, maximum
            with danmaku_burn_slot():
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(maximum, 1)

    def test_parse_and_build_ass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xml_path = root / "clip.xml"
            ass_path = root / "clip.ass"
            xml_path.write_text(SAMPLE_XML, encoding="utf-8")
            comments = parse_danmaku_xml(xml_path)
            self.assertEqual(len(comments), 3)
            self.assertEqual(comments[0].text, "第一条弹幕")
            build_ass(comments, ass_path, width=1280, height=720)
            self.assertTrue(ass_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            text = ass_path.read_text(encoding="utf-8-sig")
            self.assertIn("Title: 简体中文弹幕", text)
            self.assertIn("Original Script: PotatoFlow", text)
            self.assertIn("PlayResX: 1280", text)
            self.assertIn("\\move(", text)
            self.assertIn("\\an8", text)
            self.assertIn("底部弹幕", text)

    def test_build_ass_drops_comments_when_all_lanes_are_occupied(self):
        comments = [
            DanmakuComment(time=0.0, mode=5, text="顶部"),
            DanmakuComment(time=0.0, mode=1, text="滚动一"),
            DanmakuComment(time=0.0, mode=4, text="底部"),
            DanmakuComment(time=0.0, mode=1, text="不能覆盖"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dense.ass"
            # 240p yields three usable lanes with the default 42px font.
            build_ass(comments, path, width=320, height=240)
            text = path.read_text(encoding="utf-8-sig")

        dialogues = [line for line in text.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), 3)
        self.assertNotIn("不能覆盖", text)

    def test_build_ass_does_not_reuse_fixed_lane_until_it_expires(self):
        comments = [
            DanmakuComment(time=0.0, mode=5, text=f"固定{index}")
            for index in range(3)
        ]
        comments.extend(
            [
                DanmakuComment(time=1.0, mode=1, text="过早滚动"),
                DanmakuComment(time=5.0, mode=1, text="到期可用"),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mixed.ass"
            build_ass(comments, path, width=320, height=240)
            text = path.read_text(encoding="utf-8-sig")

        self.assertNotIn("过早滚动", text)
        self.assertIn("到期可用", text)

    def test_build_ass_accounts_for_full_width_cjk_glyphs(self):
        comments = [
            DanmakuComment(time=0.0, mode=1, text="中文宽度测试"),
            DanmakuComment(time=3.0, mode=1, text="中文宽度测试"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cjk-width.ass"
            build_ass(comments, path, width=320, height=240, font_size=42, duration=9)
            text = path.read_text(encoding="utf-8-sig")

        moves = re.findall(r"\\move\(320,(\d+),-256,(\d+)\)", text)
        self.assertEqual(len(moves), 2)
        self.assertNotEqual(moves[0][0], moves[1][0])

    def test_build_ass_blocks_configured_spam_phrase_without_changing_xml(self):
        comments = [
            DanmakuComment(time=0.0, mode=1, text="合成大西瓜"),
            DanmakuComment(time=1.0, mode=1, text=" 合成 大西瓜 "),
            DanmakuComment(time=2.0, mode=1, text="正常弹幕"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "blocked.ass"
            build_ass(comments, path)
            text = path.read_text(encoding="utf-8-sig")

        self.assertNotIn("合成大西瓜", text)
        self.assertNotIn("合成 大西瓜", text)
        self.assertIn("正常弹幕", text)

    def test_ai_sampling_deduplicates_spam(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clip.xml"
            repeated = "".join(
                f'<d p="{index},1,25,16777215,0,0,1,0">同一条</d>' for index in range(20)
            )
            path.write_text(f"<i>{repeated}</i>", encoding="utf-8")
            selected = select_summary_comments(parse_danmaku_xml(path), 20)
            self.assertEqual(len(selected), 2)
            self.assertNotIn("uid", format_comments_for_ai(selected).lower())

    def test_ai_sampling_preserves_rare_numeric_event_evidence(self):
        comments = [
            DanmakuComment(
                time=float(index),
                mode=1,
                color=0,
                text=f"普通反应{chr(0x4e00 + index)}",
            )
            for index in range(100)
        ]
        comments[93] = DanmakuComment(
            time=93.0,
            mode=1,
            color=0,
            text="20秒买活",
        )
        comments[99] = DanmakuComment(
            time=99.0,
            mode=1,
            color=0,
            text="q4q4q4q4q4q4q4q4q4q4q4q4q4q4",
        )

        selected = select_summary_comments(comments, 20)

        self.assertEqual(len(selected), 20)
        self.assertIn("20秒买活", [comment.text for comment in selected])
        self.assertEqual([comment.time for comment in selected], sorted(comment.time for comment in selected))

    def test_inspect_xml_reports_raw_valid_invalid_and_timeline_counts(self):
        path = self._write_xml(
            '<d p="10,1,25,16777215,0,0,1,0">第一条</d>'
            '<d p="invalid">坏节点</d>'
            '<d p="75.5,1,25,16777215,0,0,2,0">第二条</d>'
        )

        details = inspect_danmaku_xml(path)

        self.assertEqual(details["danmaku_xml_entries"], 3)
        self.assertEqual(details["danmaku_count"], 2)
        self.assertEqual(details["danmaku_invalid_count"], 1)
        self.assertEqual(details["danmaku_first_second"], 10.0)
        self.assertEqual(details["danmaku_last_second"], 75.5)
        self.assertEqual(details["danmaku_timeline_span_seconds"], 65.5)

    def test_ai_comment_timestamps_use_bilibili_chapter_format(self):
        comments = parse_danmaku_xml(
            self._write_xml(
                '<d p="65,1,25,16777215,0,0,1,0">一分五秒</d>'
                '<d p="3661,1,25,16777215,0,0,2,0">一小时一分一秒</d>'
            )
        )
        formatted = format_comments_for_ai(comments)
        self.assertIn("[01:05] 一分五秒", formatted)
        self.assertIn("[01:01:01] 一小时一分一秒", formatted)

    def _write_xml(self, body: str) -> Path:
        temp = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
        temp.close()
        path = Path(temp.name)
        path.write_text(f"<i>{body}</i>", encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        return path


if __name__ == "__main__":
    unittest.main()
