import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import bridge


class BridgeTests(unittest.TestCase):
    def test_profile_override_and_metadata(self):
        base = {
            "title_template": "{stem}",
            "description_template": "file={name}",
            "tags": ["default"],
            "profiles": [{"match": "*alice*", "tags": ["alice"], "source_url": "https://x"}],
        }
        video = Path("2026-alice-live.mp4")
        cfg = bridge.effective_config(base, video)
        title, description, tags = bridge.render_metadata(video, cfg)
        self.assertEqual(title, "2026-alice-live")
        self.assertEqual(description, "file=2026-alice-live.mp4")
        self.assertEqual(tags, ["alice"])
        self.assertEqual(cfg["source_url"], "https://x")

    def test_default_recording_title_uses_streamer_ai_topic_and_date(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "妮可罗宾_45ecd12026-07-23_09-45-06_中韩流行.flv"
            video.write_bytes(b"video")
            title, _, _ = bridge.render_metadata(
                video,
                {
                    "title_template": bridge.DEFAULT_TITLE_TEMPLATE,
                    "streamer_name": "妮可罗宾",
                },
                ai_topic="中韩流行歌单·点歌闲聊",
            )
        self.assertEqual(title, "妮可罗宾｜中韩流行歌单·点歌闲聊｜07-23 09:45｜【直播回放】")

    def test_default_recording_title_falls_back_to_live_title(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "主播_abcdef2026-07-23_09-45-06_深夜歌回.flv"
            video.write_bytes(b"video")
            title, _, _ = bridge.render_metadata(
                video,
                {"title_template": bridge.DEFAULT_TITLE_TEMPLATE},
            )
        self.assertEqual(title, "主播｜深夜歌回｜07-23 09:45｜【直播回放】")

    def test_current_filename_uses_recording_start_time_not_finalize_time(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "果小果是个弟弟_c3bc3d_备战宝可梦_2026-07-24_13-00.flv"
            video.write_bytes(b"video")
            finalized_at = datetime(2026, 7, 24, 14, 6).timestamp()
            os.utime(video, (finalized_at, finalized_at))
            title, _, _ = bridge.render_metadata(
                video,
                {
                    "title_template": bridge.DEFAULT_TITLE_TEMPLATE,
                    "streamer_name": "果小果是个弟弟",
                },
                ai_topic="凤凰翻盘",
            )

        self.assertEqual(title, "果小果｜凤凰翻盘｜07-24 13:00｜【直播回放】")
        self.assertEqual(bridge.recording_part_title(video, 1), "P1 13:00")

    def test_default_recording_title_uses_canonical_streamer_names(self):
        cases = (
            ("yyfyyf", "YYF"),
            ("YYFYYF", "YYF"),
            ("果小果是个弟弟", "果小果"),
            ("果小果", "果小果"),
        )
        for configured_name, expected_name in cases:
            with self.subTest(configured_name=configured_name):
                values = bridge.recording_metadata_values(
                    Path("recording.flv"),
                    {"streamer_name": configured_name},
                    ai_topic="直播主题",
                )
                self.assertEqual(values["streamer"], expected_name)

    def test_input_keeps_xml_and_pairs_by_stem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            xml = root / "clip.xml"
            video.write_bytes(b"video")
            xml.write_text("<i/>", encoding="utf-8")
            paths = bridge.input_paths([str(video), str(xml)], include_stdin=False)
            self.assertEqual(bridge.find_danmaku_xml(video, paths), xml.resolve())

    def test_state_deduplicates_completed_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            store = bridge.StateStore(Path(temp) / "state.sqlite3")
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"video")
            key = bridge.fingerprint(video)
            self.assertTrue(store.claim(key, video, "bilibili"))
            store.finish(key, "completed", {"ok": True})
            self.assertFalse(store.claim(key, video, "bilibili"))

    def test_finalize_session_ingests_final_video_before_closing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "final.flv"
            config = root / "bridge.config.json"
            state = root / "state.sqlite3"
            video.write_bytes(b"video")
            config.write_text(json.dumps({
                "state_db": str(state),
                "delete_recording_after_upload": False,
            }), encoding="utf-8")
            store = bridge.StateStore(state)
            store.save_multipart_session("room-1", {"bilibili": {"bvid": "BV1"}}, status="open")

            def fake_upload(path, _cfg, target_store, **kwargs):
                self.assertEqual(path, video.resolve())
                self.assertEqual(kwargs["session_key"], "room-1")
                key = bridge.fingerprint(path)
                target_store.claim(key, path, "bilibili")
                target_store.finish(key, "completed", {"ok": True})
                return True

            with patch.object(bridge, "upload_one", side_effect=fake_upload):
                result = bridge.main([
                    "--config", str(config),
                    "finalize-session", "--session-key", "room-1", str(video),
                ])

            self.assertEqual(result, 0)
            self.assertEqual(store.multipart_session("room-1"), {})

    def test_finalize_session_detaches_session_when_final_video_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "final.flv"
            config = root / "bridge.config.json"
            state = root / "state.sqlite3"
            video.write_bytes(b"video")
            config.write_text(json.dumps({"state_db": str(state)}), encoding="utf-8")
            store = bridge.StateStore(state)
            store.save_multipart_session("room-1", {"bilibili": {"bvid": "BV1"}}, status="open")

            with patch.object(bridge, "upload_one", return_value=False):
                result = bridge.main([
                    "--config", str(config),
                    "finalize-session", "--session-key", "room-1", str(video),
                ])

            self.assertEqual(result, 0)
            self.assertEqual(store.multipart_session("room-1"), {})
            self.assertEqual(store.multipart_session("room-1", include_closed=True), {})

    def test_state_persists_each_inspectable_pipeline_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            store = bridge.StateStore(Path(temp) / "state.sqlite3")
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"video")
            key = bridge.fingerprint(video)
            self.assertTrue(store.claim(key, video, "bilibili"))
            store.stage(key, "ass", "running", {"danmaku_xml": "clip.xml"})
            store.stage(key, "ass", "completed", {"ass_path": "clip.ass", "danmaku_count": 12})
            with store.connect() as db:
                rows = db.execute(
                    "SELECT stage, status, details_json FROM upload_stages WHERE fingerprint=? ORDER BY stage",
                    (key,),
                ).fetchall()
            stages = {row["stage"]: row for row in rows}
            self.assertEqual(set(stages), {"detect", "record", "ass", "ai", "cover", "upload"})
            self.assertEqual(stages["record"]["status"], "completed")
            self.assertEqual(stages["ass"]["status"], "completed")
            self.assertEqual(json.loads(stages["ass"]["details_json"])["danmaku_count"], 12)

    def test_retry_preserves_uploaded_bvid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            cover = root / "cover.jpg"
            cookie = root / "cookie.json"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            cookie.write_text("[]", encoding="utf-8")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "bilibili_cookies": str(cookie),
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.finish(key, "failed", {"bilibili": {"bvid": "BV1existing"}}, "dm failed")

            class MustNotUpload:
                def __init__(self, **_kwargs):
                    raise AssertionError("retry must not instantiate uploader")

            with patch.object(
                bridge,
                "enhance_recording_metadata",
                return_value=([], "171", {}),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                return_value=(None, {"ai_cover_enabled": False}),
            ), patch.object(bridge, "import_y2a", return_value=(MustNotUpload, None)):
                self.assertTrue(bridge.upload_one(video, cfg, store, retry=True))
            result = store.results(key)
            self.assertEqual(result["bilibili"]["bvid"], "BV1existing")
            self.assertFalse(video.exists())
            self.assertEqual(result["source_cleanup"]["deleted"], [str(video.resolve())])

    def test_retry_detaches_unsubmitted_first_part_from_stale_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            cover = root / "cover.jpg"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.finish(
                key,
                "failed",
                {"multipart_session": "room-1", "part_number": 1},
                "upload failed",
            )
            store.save_multipart_session(
                "room-1",
                {"pending_first_video": str(video), "title": ""},
            )

            self.assertTrue(bridge.upload_one(video, cfg, store, retry=True, dry_run=True))

            self.assertIsNone(store.results(key)["multipart_session"])

    def test_ingest_retry_only_processes_the_selected_failed_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected = root / "selected.mp4"
            other = root / "other.mp4"
            selected.write_bytes(b"selected")
            other.write_bytes(b"other")
            config = root / "bridge.config.json"
            state = root / "state.sqlite3"
            config.write_text(json.dumps({"state_db": str(state)}), encoding="utf-8")
            store = bridge.StateStore(state)
            for video in (selected, other):
                key = bridge.fingerprint(video)
                store.claim(key, video, "bilibili")
                store.finish(key, "failed", error="failed")

            with patch.object(bridge, "stdin_paths", return_value=[]), patch.object(
                bridge, "upload_one", return_value=True
            ) as upload:
                result = bridge.main([
                    "--config", str(config), "ingest", "--retry", str(selected),
                ])

            self.assertEqual(result, 0)
            upload.assert_called_once()
            self.assertEqual(upload.call_args.args[0], selected.resolve())

    def test_cleanup_after_upload_removes_video_xml_and_transcoded_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.flv"
            xml = root / "clip.xml"
            upload_video = root / "artifacts" / "clip.mp4"
            upload_video.parent.mkdir()
            for path in (video, xml, upload_video):
                path.write_bytes(b"data")

            result = bridge.cleanup_uploaded_recording(video, xml, upload_video)

            self.assertEqual(result["failed"], [])
            self.assertEqual(len(result["deleted"]), 3)
            self.assertTrue(all(not path.exists() for path in (video, xml, upload_video)))

    def test_live_segments_append_to_one_bilibili_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.jpg"
            cookie = root / "cookie.json"
            first = root / "主播_abcdef2026-07-23_09-00-00_直播.flv"
            second = root / "主播_abcdef2026-07-23_10-00-00_直播.flv"
            cover.write_bytes(b"cover")
            cookie.write_text("[]", encoding="utf-8")
            first.write_bytes(b"part-one")
            second.write_bytes(b"part-two")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "bilibili_cookies": str(cookie),
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "ai_danmaku_summary_enabled": False,
                "delete_recording_after_upload": False,
            }
            calls = []

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **kwargs):
                    calls.append(kwargs)
                    existing = kwargs.get("existing_submission")
                    part_count = int((existing or {}).get("part_count") or 0) + 1
                    parts = list((existing or {}).get("uploaded_parts") or [])
                    parts.append({"filename": f"part-{part_count}", "title": f"P{part_count}"})
                    return True, {
                        "bvid": "BV1multipart",
                        "aid": 123,
                        "url": "https://www.bilibili.com/video/BV1multipart",
                        "part_count": part_count,
                        "uploaded_parts": parts,
                        "cover_url": "https://example.com/cover.jpg",
                    }

            store = bridge.StateStore(root / "state.sqlite3")
            automation = {
                "tag_generation_enabled": True,
                "generated_tags": ["AI标签"],
                "partition_recommendation_enabled": True,
                "recommended_partition_id": "129",
                "selected_partition_id": "129",
                "cover_for_partition_ai": True,
            }
            with patch.object(
                bridge,
                "enhance_recording_metadata",
                return_value=(["主播", "AI标签"], "129", automation),
            ) as enhance_metadata, patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                return_value=(None, {"ai_cover_enabled": False}),
            ) as generate_cover, patch.object(
                bridge, "import_y2a", return_value=(FakeUploader, None)
            ):
                self.assertTrue(bridge.upload_one(first, cfg, store, session_key="room-1"))
                self.assertTrue(bridge.upload_one(second, cfg, store, session_key="room-1"))

            enhance_metadata.assert_called_once()
            generate_cover.assert_called_once()
            self.assertIsNone(calls[0]["existing_submission"])
            self.assertEqual(calls[0]["page_titles"], ["P1 09:00:00"])
            self.assertEqual(calls[0]["tags"], ["主播", "AI标签"])
            self.assertEqual(calls[0]["partition_id"], "129")
            self.assertEqual(calls[1]["existing_submission"]["bvid"], "BV1multipart")
            self.assertEqual(calls[1]["page_titles"], ["P2 10:00:00"])
            self.assertEqual(calls[1]["tags"], ["主播", "AI标签"])
            self.assertEqual(calls[1]["partition_id"], "129")
            session = store.multipart_session("room-1")
            self.assertEqual(session["bilibili"]["part_count"], 2)
            self.assertEqual(session["partition_id"], "129")
            self.assertTrue(session["metadata_automation"]["cover_for_partition_ai"])
            self.assertEqual(Path(session["cover_path"]), cover.resolve())
            self.assertTrue(store.close_multipart_session("room-1"))
            self.assertEqual(store.multipart_session("room-1"), {})

    def test_recording_metadata_uses_y2a_tags_partition_and_cover_setting(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.jpg"
            cover.write_bytes(b"cover")
            cfg = {"_config_dir": str(root), "y2a_root": str(y2a_root)}
            selection = {
                "id": "129",
                "source": "ai",
                "confidence": 0.92,
                "reason_summary": "封面与标题均为游戏内容",
                "alternatives": ["171"],
            }
            ai_module = types.ModuleType("modules.ai_enhancer")
            ai_module.generate_acfun_tags = Mock(
                return_value=["游戏", "直播回放", "", "游戏"]
            )
            recommend = Mock(return_value=selection)
            ai_module.recommend_bilibili_partition = recommend
            zones_module = types.ModuleType("modules.bilibili_zones")
            zones_module.get_zone_list_sub = Mock(
                return_value=[{"tid": 4, "name": "游戏", "sub": []}]
            )
            config_module = types.ModuleType("modules.config_manager")
            config_module.load_config = Mock(return_value={
                "GENERATE_TAGS": True,
                "RECOMMEND_PARTITION": True,
                "RECOMMEND_PARTITION_WITH_COVER": True,
                "OPENAI_API_KEY": "test-key",
                "OPENAI_MODEL_NAME": "vision-model",
            })
            with patch.dict(sys.modules, {
                "modules.ai_enhancer": ai_module,
                "modules.bilibili_zones": zones_module,
                "modules.config_manager": config_module,
            }):
                tags, partition_id, details = bridge.enhance_recording_metadata(
                    "直播标题",
                    "直播简介",
                    ["主播", "直播回放"],
                    cover,
                    "171",
                    cfg,
                )

        self.assertEqual(tags, ["主播", "直播回放", "游戏"])
        self.assertEqual(partition_id, "129")
        self.assertEqual(details["recommended_partition_id"], "129")
        self.assertTrue(details["cover_for_partition_ai"])
        self.assertEqual(recommend.call_args.kwargs["cover_path"], str(cover))
        self.assertTrue(recommend.call_args.kwargs["include_cover_for_ai"])
        self.assertEqual(recommend.call_args.kwargs["tags"], tags)

    def test_recording_cover_headline_removes_date_and_clock(self):
        headline = bridge.recording_cover_headline(
            "【直播回放】土豆｜深夜游戏挑战｜2026-07-23 21:30",
        )
        self.assertEqual(headline, "游戏挑战")
        self.assertNotRegex(headline, r"2026|21:30")

        chinese_date = bridge.recording_cover_headline(
            "【直播回放】土豆｜深夜游戏挑战｜07月23日 21:30",
        )
        self.assertEqual(chinese_date, "游戏挑战")
        self.assertNotRegex(chinese_date, r"07月23日|21:30")

    def test_ai_recording_cover_uses_ai_title_and_forbids_time(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            image_generate = Mock(return_value=response)
            client = types.SimpleNamespace(
                images=types.SimpleNamespace(generate=image_generate)
            )
            ai_module = types.ModuleType("modules.ai_enhancer")
            ai_module.get_openai_client = Mock(return_value=client)
            config_module = types.ModuleType("modules.config_manager")
            config_module.load_config = Mock(return_value={
                "AI_GENERATE_RECORDING_COVER": True,
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "https://example.com/v1",
                "OPENAI_IMAGE_MODEL_NAME": "gpt-image-2",
                "OPENAI_IMAGE_SIZE": "1536x1024",
            })

            def fake_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"jpeg")
                return types.SimpleNamespace(returncode=0, stderr="")

            with patch.dict(sys.modules, {
                "modules.ai_enhancer": ai_module,
                "modules.config_manager": config_module,
            }), patch.object(bridge.subprocess, "run", side_effect=fake_ffmpeg):
                cover, details = bridge.generate_recording_cover_with_ai(
                    title="【直播回放】土豆｜新地图极限挑战｜2026-07-23",
                    ai_topic="新地图极限挑战",
                    description="主播挑战新地图，弹幕反应热烈。",
                    streamer="土豆",
                    cfg={"_config_dir": str(root), "y2a_root": str(y2a_root), "ffmpeg": "ffmpeg"},
                    work_dir=work_dir,
                )

        self.assertEqual(cover.name, "ai_cover.jpg")
        self.assertTrue(details["ai_cover_generated"])
        self.assertEqual(details["ai_cover_headline"], "新地图极限挑战")
        prompt = image_generate.call_args.kwargs["prompt"]
        self.assertIn("AI 生成的核心标题：新地图极限挑战", prompt)
        self.assertIn("绝对禁止出现日期", prompt)
        self.assertNotIn("2026-07-23", prompt)

    def test_yyf_recording_cover_uses_identity_reference_image(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
        self.assertTrue(bridge.YYF_COVER_REFERENCE.is_file())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            image_edit = Mock(return_value=response)
            image_generate = Mock()
            client = types.SimpleNamespace(images=types.SimpleNamespace(
                edit=image_edit,
                generate=image_generate,
            ))
            ai_module = types.ModuleType("modules.ai_enhancer")
            ai_module.get_openai_client = Mock(return_value=client)
            config_module = types.ModuleType("modules.config_manager")
            config_module.load_config = Mock(return_value={
                "AI_GENERATE_RECORDING_COVER": True,
                "OPENAI_API_KEY": "test-key",
                "OPENAI_IMAGE_MODEL_NAME": "gpt-image-2",
                "OPENAI_IMAGE_SIZE": "1536x1024",
            })

            def fake_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"jpeg")
                return types.SimpleNamespace(returncode=0, stderr="")

            with patch.dict(sys.modules, {
                "modules.ai_enhancer": ai_module,
                "modules.config_manager": config_module,
            }), patch.object(bridge.subprocess, "run", side_effect=fake_ffmpeg), patch.object(
                bridge, "download_recording_avatar_reference"
            ) as avatar_download:
                cover, details = bridge.generate_recording_cover_with_ai(
                    title="【直播回放】YYF｜天梯翻盘局｜2026-07-24",
                    ai_topic="天梯翻盘局",
                    description="YYF进行天梯对局并完成翻盘。",
                    streamer="yyfyyf",
                    cfg={
                        "_config_dir": str(root),
                        "y2a_root": str(y2a_root),
                        "ffmpeg": "ffmpeg",
                        "streamer_avatar_url": "https://example.com/yyf-avatar.jpg",
                    },
                    work_dir=work_dir,
                )

        self.assertEqual(cover.name, "ai_cover.jpg")
        self.assertTrue(details["ai_cover_reference_used"])
        self.assertEqual(details["ai_cover_reference_name"], "YYF")
        self.assertEqual(
            details["ai_cover_reference_path"],
            str(bridge.YYF_COVER_REFERENCE),
        )
        image_generate.assert_not_called()
        image_edit.assert_called_once()
        edit_kwargs = image_edit.call_args.kwargs
        self.assertEqual(edit_kwargs["model"], "gpt-image-2")
        self.assertEqual(edit_kwargs["size"], "1536x1024")
        self.assertEqual(Path(edit_kwargs["image"].name), bridge.YYF_COVER_REFERENCE)
        self.assertIn("参考照片是主播 YYF 本人", edit_kwargs["prompt"])
        self.assertIn("保持其脸型、五官、发型和身份辨识度", edit_kwargs["prompt"])
        self.assertEqual(details["ai_cover_reference_kind"], "dedicated")
        avatar_download.assert_not_called()

    def test_unknown_streamer_cover_uses_room_avatar_as_reference(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            avatar = root / "avatar.jpg"
            avatar.write_bytes(b"avatar")
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            image_edit = Mock(return_value=response)
            image_generate = Mock()
            client = types.SimpleNamespace(images=types.SimpleNamespace(
                edit=image_edit,
                generate=image_generate,
            ))
            ai_module = types.ModuleType("modules.ai_enhancer")
            ai_module.get_openai_client = Mock(return_value=client)
            config_module = types.ModuleType("modules.config_manager")
            config_module.load_config = Mock(return_value={
                "AI_GENERATE_RECORDING_COVER": True,
                "OPENAI_API_KEY": "test-key",
                "OPENAI_IMAGE_MODEL_NAME": "gpt-image-2",
                "OPENAI_IMAGE_SIZE": "1536x1024",
            })

            def fake_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"jpeg")
                return types.SimpleNamespace(returncode=0, stderr="")

            with patch.dict(sys.modules, {
                "modules.ai_enhancer": ai_module,
                "modules.config_manager": config_module,
            }), patch.object(bridge.subprocess, "run", side_effect=fake_ffmpeg), patch.object(
                bridge, "download_recording_avatar_reference", return_value=avatar
            ) as avatar_download:
                cover, details = bridge.generate_recording_cover_with_ai(
                    title="【直播回放】新主播｜欢乐游戏｜07-24 11:20",
                    ai_topic="欢乐游戏",
                    description="直播间欢乐游戏。",
                    streamer="新主播",
                    cfg={
                        "_config_dir": str(root),
                        "y2a_root": str(y2a_root),
                        "ffmpeg": "ffmpeg",
                        "streamer_avatar_url": "https://example.com/avatar.jpg",
                    },
                    work_dir=work_dir,
                )

        self.assertEqual(cover.name, "ai_cover.jpg")
        self.assertEqual(details["ai_cover_reference_kind"], "avatar")
        self.assertEqual(details["ai_cover_reference_path"], str(avatar))
        avatar_download.assert_called_once()
        image_edit.assert_called_once()
        image_generate.assert_not_called()
        self.assertIn("直播间头像", image_edit.call_args.kwargs["prompt"])
        self.assertIn("作为封面主体底稿", image_edit.call_args.kwargs["prompt"])

    def test_yyf_reference_aliases_are_recognized(self):
        for alias in ("YYF", "yyfyyf", "月夜枫", "枫哥", "姜岑"):
            with self.subTest(alias=alias):
                reference = bridge.recording_cover_reference(alias)
                self.assertIsNotNone(reference)
                self.assertEqual(reference[0], "YYF")

    def test_guoxiaoguo_reference_aliases_are_recognized(self):
        self.assertTrue(bridge.GUOXIAOGUO_COVER_REFERENCE.is_file())
        for alias in ("果小果", "果小果是个弟弟", "果小果是个弟弟_直播间"):
            with self.subTest(alias=alias):
                reference = bridge.recording_cover_reference(alias)
                self.assertIsNotNone(reference)
                self.assertEqual(reference[0], "果小果")
                self.assertEqual(reference[1], bridge.GUOXIAOGUO_COVER_REFERENCE)
        instruction = bridge.recording_cover_reference_instruction("果小果")
        self.assertIn("头顶蛋壳", instruction)
        self.assertIn("禁止把蛋壳改成煎蛋", instruction)
        self.assertIn("禁止改成真人", instruction)

    def test_load_config_rejects_non_object(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaises(ValueError):
                bridge.load_config(path)

    def test_dry_run_validates_without_importing_y2a(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "alice.mp4"
            xml = root / "alice.xml"
            cover = root / "cover.jpg"
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            cover.write_bytes(b"cover")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            self.assertTrue(bridge.upload_one(video, cfg, store, dry_run=True, danmaku_xml=xml))
            row = store.recent(1)[0]
            self.assertEqual(row["status"], "dry_run")
            result = json.loads(row["result_json"])
            self.assertEqual(result["danmaku_count"], 1)
            self.assertTrue(Path(result["ass_path"]).is_file())

    def test_find_cover_retries_earlier_timestamps_for_truncated_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "truncated.flv"
            video.write_bytes(b"broken-video")
            work_dir = root / "artifacts"
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                if len(commands) == 2:
                    Path(command[-1]).write_bytes(b"recovered-cover")
                    return types.SimpleNamespace(returncode=0, stderr="")
                return types.SimpleNamespace(returncode=1, stderr="Invalid NAL unit size")

            with patch.object(bridge.subprocess, "run", side_effect=fake_run):
                cover = bridge.find_cover(
                    video,
                    {"_config_dir": str(root), "cover_seek_seconds": 10},
                    work_dir,
                )

            self.assertEqual(cover.read_bytes(), b"recovered-cover")
            self.assertEqual(commands[0][commands[0].index("-ss") + 1], "10")
            self.assertEqual(commands[1][commands[1].index("-ss") + 1], "3")

    def test_retry_prefers_saved_manual_review_over_generated_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            manual_cover = root / "manual-cover.jpg"
            video.write_bytes(b"video")
            manual_cover.write_bytes(b"manual-cover")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.finish(key, "failed", error="upload failed")
            override = {
                "title": "人工确认后的标题",
                "description": "人工补充简介",
                "tags": ["录播", "精彩"],
                "partition_id": "17",
                "cover_path": str(manual_cover),
                "updated_at": "2026-07-24T00:00:00+00:00",
            }
            with store.connect() as db:
                db.execute(
                    """INSERT INTO recording_review_overrides
                       (fingerprint, metadata_json, updated_at) VALUES (?, ?, ?)""",
                    (key, json.dumps(override, ensure_ascii=False), override["updated_at"]),
                )

            with patch.object(
                bridge,
                "find_cover",
                side_effect=AssertionError("manual review cover must bypass FFmpeg extraction"),
            ):
                self.assertTrue(bridge.upload_one(video, cfg, store, retry=True, dry_run=True))
            result = store.results(key)
            self.assertEqual(result["title"], override["title"])
            self.assertEqual(result["description"], override["description"])
            self.assertEqual(result["tags"], override["tags"])
            self.assertEqual(result["partition_id"], override["partition_id"])
            self.assertEqual(result["cover"], str(manual_cover))

    def test_cover_extraction_failure_is_reported_as_cover_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "broken.flv"
            video.write_bytes(b"broken-video")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)

            with patch.object(bridge, "find_cover", side_effect=RuntimeError("broken frames")):
                self.assertFalse(bridge.upload_one(video, cfg, store, dry_run=True))

            with store.connect() as db:
                stages = {
                    row["stage"]: dict(row)
                    for row in db.execute(
                        "SELECT stage, status, error FROM upload_stages WHERE fingerprint=?",
                        (key,),
                    )
                }
            self.assertEqual(stages["cover"]["status"], "failed")
            self.assertIn("broken frames", stages["cover"]["error"])
            self.assertEqual(stages["ass"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
