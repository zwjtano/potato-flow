import io
import os
import tempfile
import unittest
from unittest.mock import patch

from modules.submission_engine import upload_with_recorder


class _FakeProcess:
    def __init__(self, lines, return_code=0):
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.return_code = return_code

    def wait(self):
        return self.return_code


class SubmissionEngineStageTests(unittest.TestCase):
    def test_machine_cover_stage_events_are_forwarded(self):
        stages = []
        lines = [
            'POTATOFLOW_STAGE={"stage":"cover_upload","status":"running","message":"正在上传投稿封面"}',
            'POTATOFLOW_STAGE={"stage":"cover_upload","status":"completed","message":"投稿封面上传完成"}',
            'POTATOFLOW_RESULT={"code":0,"data":{"bvid":"BV1stage","aid":123}}',
        ]
        with tempfile.TemporaryDirectory() as directory:
            generated_cookie = os.path.join(directory, "cookie.json")
            with open(generated_cookie, "w", encoding="utf-8") as handle:
                handle.write("{}")
            with patch(
                "modules.submission_engine.load_config",
                return_value={
                    "BILIBILI_UPLOAD_LINE": "bldsa",
                    "BILIBILI_UPLOAD_LIMIT": 3,
                },
            ), patch(
                "modules.submission_engine._recorder_binary",
                return_value="/fake/recorder",
            ), patch(
                "modules.submission_engine._write_cookie_file",
                return_value=generated_cookie,
            ), patch(
                "modules.submission_engine.subprocess.Popen",
                return_value=_FakeProcess(lines),
            ):
                success, result = upload_with_recorder(
                    cookie_file="cookies.json",
                    video_paths=["video.mp4"],
                    cover_file="cover.jpg",
                    title="标题",
                    description="简介",
                    tags=["标签"],
                    partition_id=21,
                    stage_callback=lambda stage, status, message, details: stages.append(
                        (stage, status, message, details)
                    ),
                )

        self.assertTrue(success)
        self.assertEqual(result["bvid"], "BV1stage")
        self.assertEqual(
            [(stage, status) for stage, status, _message, _details in stages],
            [
                ("cover_upload", "running"),
                ("cover_upload", "completed"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
