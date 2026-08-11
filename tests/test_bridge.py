import io
import json
import multiprocessing
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, Mock, patch

APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import bridge


def _process_multipart_session_slot(state_db, session_key, result_queue, hold_seconds):
    cfg = {"state_db": state_db}
    with bridge.multipart_session_queue(cfg, session_key):
        started = time.monotonic()
        time.sleep(hold_seconds)
        result_queue.put((started, time.monotonic()))


class BridgeTests(unittest.TestCase):
    def test_real_world_claim_from_danmaku_keeps_source_attribution(self):
        self.assertEqual(
            bridge.qualify_danmaku_only_real_world_claim(
                "国民大舅哥被动宣布新人归熊掌，退货没成功"
            ),
            "直播间调侃国民大舅哥被动宣布新人归熊掌，退货没成功",
        )
        self.assertEqual(
            bridge.qualify_danmaku_only_real_world_claim("弹幕称新人已经转入熊掌"),
            "观众讨论新人已经转入熊掌",
        )
        self.assertEqual(
            bridge.qualify_danmaku_only_real_world_claim("观众讨论某选手涉嫌假赛"),
            "",
        )
        self.assertEqual(
            bridge.qualify_danmaku_only_real_world_claim("弹幕称某选手已经结婚"),
            "观众讨论某选手已经结婚",
        )
        self.assertEqual(
            bridge.qualify_danmaku_only_real_world_claim("走到河边，抽烟2.0刷屏"),
            "走到河边，抽烟2.0刷屏",
        )
        self.assertEqual(
            bridge.qualify_danmaku_only_real_world_claim("弹幕称影魔六神装完成翻盘"),
            "影魔六神装完成翻盘",
        )

    def test_real_world_claim_over_limit_is_rejected_instead_of_truncated(self):
        claim = (
            "某选手已经正式签约一家新的职业俱乐部并将在下周参加首场比赛，"
            "随后还会前往海外长期集训并担任队伍的核心位置"
        )

        self.assertGreater(len(f"观众讨论{claim}"), bridge.RECORDING_TITLE_TOPIC_LIMIT)
        self.assertEqual(bridge.qualify_danmaku_only_real_world_claim(claim), "")

    def test_negative_danmaku_rumors_are_removed_from_title_and_description(self):
        self.assertEqual(
            bridge.qualify_danmaku_only_real_world_claim("弹幕称某选手因假赛被封禁"),
            "",
        )
        self.assertEqual(
            bridge.remove_negative_rumor_text(
                "观众讨论新队伍阵容。弹幕称某选手因假赛被封禁。随后进入下一局。"
            ),
            "观众讨论新队伍阵容。随后进入下一局。",
        )

    def test_danmaku_only_real_world_harm_claims_are_removed(self):
        self.assertEqual(
            bridge.qualify_danmaku_only_real_world_claim(
                "弹幕称暖妹出拳后苏西嘴唇出血"
            ),
            "",
        )
        self.assertEqual(
            bridge.remove_negative_rumor_text(
                "暖妹放话1V10招募对手。拳击挑战中苏西被打哭。直播间齐喊暂停。"
            ),
            "暖妹放话1V10招募对手。直播间齐喊暂停。",
        )

    def test_app_root_accepts_legacy_config_key_and_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canonical = root / "potatoflow-app"
            (canonical / "modules").mkdir(parents=True)
            cfg = {"_config_dir": str(root), "y2a_root": "y2a-auto"}

            self.assertEqual(bridge.resolve_app_root(cfg), canonical.resolve())

    def test_image_generation_queue_serializes_threads(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = {"state_db": str(Path(temp) / "state.sqlite3")}
            first_entered = threading.Event()
            second_entered = threading.Event()
            release_first = threading.Event()

            def first_worker():
                with bridge.image_generation_queue(cfg):
                    first_entered.set()
                    release_first.wait(2)

            def second_worker():
                first_entered.wait(2)
                with bridge.image_generation_queue(cfg):
                    second_entered.set()

            first = threading.Thread(target=first_worker)
            second = threading.Thread(target=second_worker)
            first.start()
            self.assertTrue(first_entered.wait(1))
            second.start()
            self.assertFalse(second_entered.wait(0.1))
            release_first.set()
            first.join(2)
            second.join(2)
            self.assertTrue(second_entered.is_set())

    def test_reusable_burned_video_requires_video_stream_and_matching_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "recording.flv"
            burned = root / "recording.danmaku.mp4"
            source.write_bytes(b"source")
            burned.write_bytes(b"burned")
            probe = types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                    "format": {"duration": "600.5"},
                }),
            )

            with patch.object(bridge.subprocess, "run", return_value=probe), patch.object(
                bridge,
                "video_duration_seconds",
                return_value=600.0,
            ):
                valid, details = bridge.reusable_burned_video(burned, source)

            self.assertTrue(valid)
            self.assertTrue(details["burned_video_reuse_validated"])
            self.assertEqual(details["burned_video_duration_seconds"], 600.5)

    def test_retry_moves_legacy_burn_beside_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "recording.flv"
            video.write_bytes(b"source")
            legacy = root / "state" / "artifacts" / "old.danmaku.mp4"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"burned")
            prior_stage = {
                "status": "completed",
                "details": {"burned_video_path": str(legacy)},
            }

            def validate(candidate, *_args):
                exists = candidate.is_file()
                return exists, {"burned_video_reuse_validated": exists}

            with patch.object(bridge, "reusable_burned_video", side_effect=validate):
                reused, details = bridge.reusable_burned_video_for_retry(
                    video,
                    prior_stage,
                )

            expected = root / "recording.danmaku.mp4"
            self.assertEqual(reused, expected.resolve())
            self.assertTrue(expected.is_file())
            self.assertFalse(legacy.exists())
            self.assertTrue(details["reused_on_retry"])
            self.assertEqual(details["burned_video_location"], "recording_directory")

    def test_ai_metadata_queue_finishes_one_task_before_the_next(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = {"state_db": str(Path(temp) / "state.sqlite3")}
            entered = [threading.Event() for _ in range(2)]
            release = threading.Event()

            def worker(index):
                with bridge.ai_metadata_queue(cfg):
                    entered[index].set()
                    release.wait(2)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
            threads[0].start()
            self.assertTrue(entered[0].wait(1))
            threads[1].start()
            self.assertFalse(entered[1].wait(0.15))
            release.set()
            for thread in threads:
                thread.join(2)
            self.assertTrue(entered[1].is_set())

    def test_ai_queue_retries_only_real_lock_contention(self):
        self.assertTrue(bridge._queue_lock_is_busy(OSError(11, "busy")))
        self.assertTrue(bridge._queue_lock_is_busy(OSError(13, "locked")))
        self.assertFalse(bridge._queue_lock_is_busy(OSError(5, "io failure")))

    def test_task_error_detail_preserves_reason_and_redacts_credentials(self):
        detail = bridge.safe_task_error_detail(
            "HTTP 401 Invalid token; Authorization: Bearer secret-value; "
            "api_key=sk-example-secret"
        )

        self.assertIn("HTTP 401 Invalid token", detail)
        self.assertNotIn("secret-value", detail)
        self.assertNotIn("sk-example-secret", detail)
        self.assertIn("[redacted]", detail)

    def test_ai_batch_error_summary_includes_each_failed_batch(self):
        summary = bridge.ai_batch_error_summary([
            {"index": 1, "error": "Your request was blocked."},
            {"index": 2, "error": "502 Bad Gateway"},
        ])

        self.assertIn("批次 1: Your request was blocked.", summary)
        self.assertIn("批次 2: 502 Bad Gateway", summary)

    def test_windows_bridge_media_helpers_never_open_a_console(self):
        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with patch.object(bridge.os, "name", "nt"):
            self.assertEqual(
                bridge._hidden_subprocess_kwargs(),
                {"creationflags": expected},
            )
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("subprocess.run("), 6)
        self.assertEqual(source.count("**_hidden_subprocess_kwargs(),"), 6)

    def test_ai_metadata_request_slots_allow_three_and_queue_the_fourth(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = {"state_db": str(Path(temp) / "state.sqlite3")}
            entered = [threading.Event() for _ in range(4)]
            release = threading.Event()

            def worker(index):
                with bridge.ai_metadata_request_slot(cfg):
                    entered[index].set()
                    release.wait(2)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
            for thread in threads[:3]:
                thread.start()
            self.assertTrue(all(event.wait(1) for event in entered[:3]))
            threads[3].start()
            self.assertFalse(entered[3].wait(0.15))
            release.set()
            for thread in threads:
                thread.join(2)
            self.assertTrue(entered[3].is_set())

    def test_multipart_session_queue_serializes_the_entire_part_flow(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = {"state_db": str(Path(temp) / "state.sqlite3")}
            active = 0
            maximum = 0
            guard = threading.Lock()

            def worker():
                nonlocal active, maximum
                with bridge.multipart_session_queue(cfg, "room-1:2026-08-09"):
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
                thread.join(timeout=2)

        self.assertEqual(maximum, 1)

    def test_multipart_session_queue_serializes_processes(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temp:
            state_db = str(Path(temp) / "state.sqlite3")
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_process_multipart_session_slot,
                    args=(state_db, "room-1:2026-08-09", result_queue, 0.2),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            intervals = sorted(
                (result_queue.get(timeout=10), result_queue.get(timeout=10)),
                key=lambda interval: interval[0],
            )
            for process in processes:
                process.join(timeout=10)

        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertLessEqual(intervals[0][1], intervals[1][0])

    def test_ai_metadata_generation_records_queue_wait_and_callback(self):
        diagnostics = {}
        entered_waits = []
        comments = [types.SimpleNamespace(time=1.0, text="测试弹幕")]
        cfg = {
            "state_db": "/tmp/test-ai-metadata-queue/state.sqlite3",
            "ai_danmaku_summary_enabled": True,
        }

        with patch.object(
            bridge,
            "ai_metadata_queue",
            return_value=bridge.contextmanager(lambda: iter([1.23456]))(),
        ), patch.object(
            bridge,
            "_generate_danmaku_metadata_with_ai",
            return_value=("生成简介", "生成标题"),
        ) as generate:
            result = bridge.generate_danmaku_metadata_with_ai(
                comments,
                "原简介",
                cfg,
                timeline_diagnostics=diagnostics,
                queue_entered_callback=entered_waits.append,
            )

        self.assertEqual(result, ("生成简介", "生成标题"))
        self.assertEqual(diagnostics["ai_metadata_queue_wait_seconds"], 1.235)
        self.assertEqual(entered_waits, [1.23456])
        generate.assert_called_once()

    def test_title_prompt_rejects_vague_marketing_conclusions(self):
        prompt = bridge.DEFAULT_RECORDING_TITLE_AI_PROMPT

        self.assertIn("出装引争议", prompt)
        self.assertIn("被指", prompt)
        self.assertIn("必须直接写清具体动作", prompt)

    def test_title_rejects_opaque_attribution(self):
        self.assertTrue(
            bridge.recording_title_uses_opaque_attribution(
                "风暴之灵复盘被指忘记双倍符"
            )
        )
        self.assertFalse(
            bridge.recording_title_uses_opaque_attribution(
                "川神风暴之灵复盘双倍符决策"
            )
        )

    def test_default_title_prompt_integrates_subject_without_label_prefix(self):
        prompt = bridge.DEFAULT_RECORDING_TITLE_AI_PROMPT

        self.assertIn("不能为统一格式强塞主播名", prompt)
        self.assertIn("主播名｜事件", prompt)
        self.assertIn("必须同时进入重要时间点", prompt)
        self.assertIn("绝不能直接截断半句话", prompt)
        self.assertIn("选题优先级依次为", prompt)
        self.assertIn("读者脱离上下文也能理解的一项", prompt)
        self.assertIn("中文分号", prompt)
        self.assertIn("不得加入第三个事件", prompt)

    def test_default_description_prompt_requests_detail_without_padding(self):
        prompt = bridge.DEFAULT_RECORDING_DESCRIPTION_AI_PROMPT

        self.assertIn("尽可能完整", prompt)
        self.assertIn("独立信息增量", prompt)
        self.assertIn("不是为了达到数量", prompt)
        self.assertIn("Role 编号", prompt)
        self.assertIn("不要一律退化成", prompt)

    def test_stage_prompts_share_one_dota2_evidence_policy(self):
        bridge_source = Path(bridge.__file__).read_text(encoding="utf-8")
        manager_source = (
            Path(bridge.__file__).parent / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")

        self.assertIn("多条原始 XML\n直接人物—英雄绑定形成保守共识", bridge_source)
        self.assertIn("最终持有的装备、KDA\n只能使用其中已经确认的数据", bridge_source)
        self.assertIn("已通过完整 XML 核验的人物—英雄直接关系可以原样用于标题", bridge_source)
        self.assertIn("已通过完整 XML 核验的人物—英雄直接关系可以原样用于标题", manager_source)
        self.assertNotIn("英雄、装备和 KDA\n只能使用其中已经确认的数据", bridge_source)
        self.assertNotIn("不得猜测主播、英雄、装备", bridge_source)
        self.assertNotIn("mode=unknown 时不得声称主播参赛或观战", bridge_source)
        self.assertNotIn("unknown 不得声称当前主播参赛或观战", manager_source)

    def test_cover_copy_only_requires_owner_name_when_verified_headline_has_it(self):
        with_owner = bridge.recording_cover_subject_copy_instruction(
            "谢彬DD",
            "奶哥NEC中路压制",
            "奶哥",
        )
        without_owner = bridge.recording_cover_subject_copy_instruction(
            "谢彬DD",
            "顶上战争决赛进入决胜局",
            "奶哥",
        )

        self.assertIn("必须清晰保留", with_owner)
        self.assertIn("奶哥", with_owner)
        self.assertIn("不得为了房间归属强塞主播名", without_owner)
        self.assertNotIn("必须清晰保留", without_owner)

    def test_upload_pipeline_persists_duration_before_optional_ass_stage(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        self.assertIn('"video_duration_seconds": recording_duration_seconds', source)
        self.assertIn(
            'recording_duration_seconds = recording_effective_duration_seconds(',
            source,
        )

    @unittest.skip("removed cover behavior")
    def test_default_cover_prompt_requires_official_dota2_item_references(self):
        prompt = bridge.DEFAULT_RECORDING_COVER_AI_PROMPT

        self.assertIn("Valve 官方装备图标参考", prompt)
        self.assertIn("缺少官方参考时不得表现具体装备", prompt)
        self.assertIn("独立官方图标展示", prompt)
        self.assertIn("不得新增名单外装备", prompt)
        self.assertIn("可沿人物和英雄外围错落环绕", prompt)
        self.assertIn("画面最下方安全区整齐排成一排", prompt)
        self.assertIn("不得穿到主播或英雄身上", prompt)
        self.assertIn("不带商店黑底、名称或物品栏边框", prompt)
        self.assertIn("主播人物与 Valve 官方英雄必须作为两个清楚分开的视觉主体", prompt)
        self.assertIn("禁止把主播画成英雄 Cos", prompt)
        self.assertIn("标题已经包含当前主播时", prompt)
        self.assertIn("标题没有当前主播时不得强塞名字", prompt)
        self.assertIn("排序第一", prompt)
        self.assertIn("不得画入封面", prompt)
        self.assertIn("16:9 与 4:3 必须独立构图", prompt)
        self.assertIn("封面人物底稿", prompt)
        self.assertIn("不得替换主角或混合人脸", prompt)
        self.assertIn("只有最终投稿标题明确出现", prompt)
        self.assertIn("简介、时间线或弹幕中顺带提及的人物一律不得出镜", prompt)
        self.assertIn("优先16至24字、最多28字", prompt)
        self.assertIn("至少两类有区分度的信息", prompt)
        self.assertIn("Dota 2 事件优先两至三行", prompt)
        self.assertIn("负面未经证实的现实传言不得进入封面", prompt)
        self.assertNotIn("只有在弹幕可靠提及其他主播", prompt)

    def test_recording_tags_dedupe_repeated_streamer_aliases(self):
        self.assertEqual(
            bridge.dedupe_recording_tags(["yyfyyf", "YYF", "直播回放"]),
            ["yyfyyf", "直播回放"],
        )

    def test_unverified_dota_hero_discussion_is_kept_but_tag_is_removed(self):
        topic, description, tags, details = bridge.filter_unverified_dota2_metadata(
            "死灵法师翻盘",
            "弹幕热议输赢。影魔最终六神装。\n观众讨论赛后复盘。",
            ["YYF", "影魔", "DOTA2"],
        )

        self.assertEqual(topic, "死灵法师翻盘")
        self.assertEqual(
            description,
            "弹幕热议输赢。影魔最终六神装。\n观众讨论赛后复盘。",
        )
        self.assertEqual(tags, ["YYF", "DOTA2"])
        self.assertEqual(details["unverified_hero_tags_removed"], ["影魔"])

    def test_unverified_owner_hero_claim_is_removed_without_blank_placeholder(self):
        topic, description, tags, details = bridge.filter_unverified_dota2_metadata(
            "YYF影魔最终六神装",
            "00:10 开场互动\n05:03 YYF影魔最终六神装。\n19:34 观众复盘",
            ["YYF", "影魔"],
            streamer="YYF",
        )

        self.assertEqual(topic, "")
        self.assertEqual(description, "00:10 开场互动\n19:34 观众复盘")
        self.assertNotIn("\n\n", description)
        self.assertTrue(details["unverified_hero_description_removed"])

    def test_ordinary_phrase_and_other_hero_discussion_are_not_deleted(self):
        description = (
            "00:00 开场互动\n"
            "05:03 原班人马重组，队伍被吐槽全员说谎\n"
            "55:17 霸气提问大鱼加速能否被末日驱散\n"
            "57:28 团战出现虚空大幻象的低级失误"
        )

        _topic, filtered, _tags, details = bridge.filter_unverified_dota2_metadata(
            "赛后趣味问答",
            description,
            ["谢彬DD", "DOTA2"],
            streamer="谢彬DD",
            verified_timeline=description,
        )

        self.assertEqual(filtered, description)
        self.assertFalse(details["unverified_hero_description_removed"])
        self.assertEqual(bridge._dota2_hero_identity_keys("原班人马重组"), set())

    def test_model_timeline_cannot_verify_owner_hero_without_gsi(self):
        timeline = (
            "43:17 弹幕嘲讽YYF虚空假面出装刮痧，与鲷哥的虚空形成对比"
        )
        topic, description, tags, details = bridge.filter_unverified_dota2_metadata(
            "YYF虚空假面冰眼出装刮痧",
            timeline,
            ["YYF", "虚空假面", "DOTA2"],
            streamer="YYF",
            verified_timeline=timeline,
        )

        self.assertEqual(topic, "")
        self.assertEqual(description, "")
        self.assertEqual(tags, ["YYF", "DOTA2"])
        self.assertEqual(details["hero_evidence_source"], "none")
        self.assertEqual(details["verified_timeline_hero_evidence"], [])

    def test_repeated_raw_danmaku_owner_alias_validates_hero(self):
        comments = [
            types.SimpleNamespace(time=1655.5, text="奶哥这么肥的NEC"),
            types.SimpleNamespace(time=1972.7, text="奶哥开局说他的NEC不一样"),
            types.SimpleNamespace(time=2421.8, text="奶哥NEC中路压制"),
        ]
        topic, description, tags, details = bridge.filter_unverified_dota2_metadata(
            "奶哥的NEC中路压制",
            "27:35 奶哥这么肥的NEC开始发力。",
            ["谢彬DD", "NEC", "DOTA2"],
            streamer="谢彬DD",
            raw_comments=comments,
        )

        self.assertEqual(topic, "奶哥的NEC中路压制")
        self.assertEqual(description, "27:35 奶哥这么肥的NEC开始发力。")
        self.assertEqual(tags, ["谢彬DD", "NEC", "DOTA2"])
        self.assertEqual(details["hero_evidence_source"], "danmaku_owner_hero_consensus")
        self.assertEqual(details["verified_timeline_hero_evidence"], ["瘟疫法师（Necrophos）"])

    def test_raw_danmaku_owner_hero_consensus_is_generic(self):
        comments = [
            types.SimpleNamespace(time=100.0, text="枫哥虚空这波空大"),
            types.SimpleNamespace(time=460.0, text="YYF的虚空假面开始出装"),
        ]
        topic, _description, tags, details = bridge.filter_unverified_dota2_metadata(
            "枫哥虚空假面出装复盘",
            "",
            ["YYF", "虚空假面", "DOTA2"],
            streamer="yyfyyf",
            raw_comments=comments,
        )

        self.assertEqual(topic, "枫哥虚空假面出装复盘")
        self.assertEqual(tags, ["YYF", "虚空假面", "DOTA2"])
        self.assertEqual(details["hero_evidence_source"], "danmaku_owner_hero_consensus")

    def test_raw_danmaku_consensus_keeps_hero_that_only_appears_in_tags(self):
        comments = [
            types.SimpleNamespace(time=100.0, text="枫哥虚空这波空大"),
            types.SimpleNamespace(time=460.0, text="YYF的虚空假面开始出装"),
        ]
        _topic, _description, tags, details = bridge.filter_unverified_dota2_metadata(
            "赛后出装复盘",
            "05:00 赛后开始复盘。",
            ["YYF", "虚空假面", "DOTA2"],
            streamer="yyfyyf",
            raw_comments=comments,
        )

        self.assertEqual(tags, ["YYF", "虚空假面", "DOTA2"])
        self.assertEqual(details["verified_timeline_hero_evidence"], ["虚空假面（Faceless Void）"])

    def test_danmaku_owner_hero_consensus_reaches_cover_without_items(self):
        context = bridge.recording_cover_danmaku_game_context(
            {
                "hero_evidence_source": "danmaku_owner_hero_consensus",
                "verified_timeline_hero_evidence": ["瘟疫法师（Necrophos）"],
            },
            "奶哥NEC中路压制",
            "27:35 奶哥的NEC开始发力。",
        )

        self.assertEqual(context["hero"], "瘟疫法师")
        self.assertEqual(context["items"], [])
        self.assertEqual(
            context["identity_source"],
            "xml_repeated_owner_hero_relation",
        )
        self.assertIsNone(bridge.recording_cover_danmaku_game_context(
            {
                "hero_evidence_source": "danmaku_owner_hero_consensus",
                "verified_timeline_hero_evidence": [
                    "瘟疫法师（Necrophos）",
                    "死亡先知（Death Prophet）",
                ],
            },
            "多英雄对局复盘",
        ))

    def test_complete_dota2_hero_roster_and_one_character_names_are_safe(self):
        self.assertEqual(len(bridge._DOTA2_HERO_ALIAS_GROUPS), 127)
        for hero in ("百戏大王", "朗戈", "森海飞霞", "祸乱之源", "凯"):
            with self.subTest(hero=hero):
                self.assertTrue(bridge._dota2_hero_identity_keys(hero))
        self.assertEqual(bridge._dota2_hero_identity_keys("陈述比赛过程"), set())
        self.assertEqual(bridge._dota2_hero_identity_keys("慷慨发言"), set())

    def test_two_letter_dota2_hero_aliases_are_token_matched(self):
        for alias in ("DP", "SF", "PA", "AM", "TB", "WK", "LC"):
            with self.subTest(alias=alias):
                self.assertTrue(bridge._dota2_hero_identity_keys(alias))
                self.assertTrue(bridge._dota2_hero_identity_keys(f"奶哥{alias}这局"))
        self.assertEqual(bridge._dota2_hero_identity_keys("DPS统计"), set())
        self.assertEqual(bridge._dota2_hero_identity_keys("template"), set())

    def test_common_chinese_hero_aliases_cover_the_complete_roster(self):
        for alias in (
            "剧毒", "毒龙", "黑贤", "全能", "小鹿", "夜魔", "蜘蛛",
            "双头龙", "冰魂", "毒狗", "熊德", "大树", "尸王", "小强",
            "小精灵", "死灵龙", "海民", "天怒", "死骑", "冰龙", "墨客",
        ):
            with self.subTest(alias=alias):
                self.assertTrue(bridge._dota2_hero_identity_keys(alias))

    def test_repeated_two_letter_owner_hero_alias_validates_metadata(self):
        comments = [
            types.SimpleNamespace(time=100.0, text="奶哥DP中路压制"),
            types.SimpleNamespace(time=420.0, text="谢彬的DP准备推高地"),
        ]
        topic, description, tags, details = bridge.filter_unverified_dota2_metadata(
            "奶哥DP中路压制后推上高地",
            "07:00 谢彬的DP准备推上高地。",
            ["谢彬DD", "DP", "DOTA2"],
            streamer="谢彬DD",
            raw_comments=comments,
        )

        self.assertEqual(topic, "奶哥DP中路压制后推上高地")
        self.assertEqual(description, "07:00 谢彬的DP准备推上高地。")
        self.assertEqual(tags, ["谢彬DD", "DP", "DOTA2"])
        self.assertEqual(details["hero_evidence_source"], "danmaku_owner_hero_consensus")
        self.assertEqual(details["verified_timeline_hero_evidence"], ["死亡先知（Death Prophet）"])

    def test_reversed_owner_hero_word_order_can_form_consensus(self):
        comments = [
            types.SimpleNamespace(time=100.0, text="DP才是谢彬这局玩的"),
            types.SimpleNamespace(time=420.0, text="这DP奶哥玩得真肥"),
        ]
        topic, _description, tags, details = bridge.filter_unverified_dota2_metadata(
            "谢彬的DP中路压制",
            "",
            ["谢彬DD", "DP", "DOTA2"],
            streamer="谢彬DD",
            raw_comments=comments,
        )

        self.assertEqual(topic, "谢彬的DP中路压制")
        self.assertEqual(tags, ["谢彬DD", "DP", "DOTA2"])
        self.assertEqual(details["hero_evidence_source"], "danmaku_owner_hero_consensus")

    def test_combat_relation_does_not_become_owner_hero_consensus(self):
        comments = [
            types.SimpleNamespace(time=100.0, text="DP击杀了谢彬"),
            types.SimpleNamespace(time=420.0, text="奶哥被DP追杀"),
        ]
        topic, _description, tags, details = bridge.filter_unverified_dota2_metadata(
            "谢彬的DP中路压制",
            "",
            ["谢彬DD", "DP", "DOTA2"],
            streamer="谢彬DD",
            raw_comments=comments,
        )

        self.assertEqual(topic, "")
        self.assertEqual(tags, ["谢彬DD", "DP", "DOTA2"])
        self.assertEqual(details["hero_evidence_source"], "none")

    def test_latest_xiebin_nec_title_keeps_opponent_hero_separate(self):
        comments = [
            types.SimpleNamespace(time=1655.5, text="奶哥这么肥的NEC"),
            types.SimpleNamespace(time=1972.7, text="奶哥开局说他的NEC不一样"),
            types.SimpleNamespace(time=2250.0, text="对面剧毒军团，你的NEC怎么玩"),
            types.SimpleNamespace(time=2420.0, text="杀个剧毒把自己高地杀没了"),
        ]
        expected = "奶哥的NEC以3-0开局，四打一剧毒反被击杀丢高地"
        topic, _description, tags, details = bridge.filter_unverified_dota2_metadata(
            expected,
            "",
            ["谢彬DD", "NEC", "剧毒", "DOTA2"],
            streamer="谢彬DD",
            raw_comments=comments,
        )

        self.assertEqual(topic, expected)
        self.assertEqual(tags, ["谢彬DD", "NEC", "剧毒", "DOTA2"])
        self.assertEqual(details["verified_timeline_hero_evidence"], ["瘟疫法师（Necrophos）"])
        self.assertIn("剧毒术士（Venomancer）", details["danmaku_hero_presence_evidence"])

    def test_raw_hero_chatter_without_repeated_owner_binding_is_not_evidence(self):
        comments = [
            types.SimpleNamespace(time=10.0, text="NEC太肥了"),
            types.SimpleNamespace(time=20.0, text="NEC出了辉耀"),
            types.SimpleNamespace(time=30.0, text="奶哥在看比赛"),
        ]
        topic, description, tags, details = bridge.filter_unverified_dota2_metadata(
            "奶哥NEC辉耀成型",
            "00:20 奶哥NEC辉耀成型。",
            ["谢彬DD", "NEC", "DOTA2"],
            streamer="谢彬DD",
            raw_comments=comments,
        )

        self.assertEqual(topic, "")
        self.assertEqual(description, "")
        self.assertEqual(tags, ["谢彬DD", "NEC", "DOTA2"])
        self.assertEqual(details["hero_evidence_source"], "none")

    def test_other_streamer_timeline_does_not_validate_owner_hero_title(self):
        timeline = "43:17 查理斯虚空假面出装成型，YYF在旁观赛"
        topic, _description, tags, details = bridge.filter_unverified_dota2_metadata(
            "YYF虚空假面冰眼出装刮痧",
            timeline,
            ["YYF", "虚空假面"],
            streamer="YYF",
            verified_timeline=timeline,
        )

        self.assertEqual(topic, "")
        self.assertEqual(tags, ["YYF"])
        self.assertEqual(details["hero_evidence_source"], "none")

    def test_participation_mode_requires_owner_specific_spectating_evidence(self):
        self.assertEqual(
            bridge.infer_streamer_participation_mode(
                "39:48 YYF观赛主舞台决赛\n56:41 YYF全程陪伴吃瓜",
                "YYF",
            ),
            "spectating",
        )
        self.assertEqual(
            bridge.infer_streamer_participation_mode(
                "39:48 谢彬观赛主舞台决赛\n56:41 蓝猫对阵DP",
                "YYF",
            ),
            "unknown",
        )
        self.assertEqual(
            bridge.infer_streamer_participation_mode(
                "21:11 蓝猫对阵DP",
                "YYF",
                gameplay_verified=True,
            ),
            "playing",
        )

    def test_title_requires_event_matched_gsi_hero_without_streamer_prefix(self):
        timeline = [
            "13:21 本局高地推进并最终出现基地爆炸",
            "48:17 玛西连续空掉两个大招后攻击刃甲被反伤",
        ]
        segments = [
            {"start_seconds": 0, "end_seconds": 1149, "hero": "风暴之灵"},
            {"start_seconds": 1407, "end_seconds": 3501, "hero": "玛西"},
        ]

        self.assertEqual(
            bridge.recording_title_missing_selected_gsi_heroes(
                "高地推进后基地爆炸；玛西空大后被刃甲反伤",
                [0, 1],
                timeline,
                segments,
            ),
            ["风暴之灵"],
        )
        self.assertEqual(
            bridge.recording_title_missing_selected_gsi_heroes(
                "风暴之灵高地推进至基地爆炸；玛西空大后被刃甲反伤",
                [0, 1],
                timeline,
                segments,
            ),
            [],
        )
        self.assertTrue(
            bridge.recording_title_missing_selected_gsi_streamer(
                "风暴之灵高地推进至基地爆炸；玛西空大后被刃甲反伤",
                [0, 1],
                timeline,
                segments,
                "川神",
            )
        )
        self.assertFalse(
            bridge.recording_title_missing_selected_gsi_streamer(
                "川神风暴之灵推进至基地爆炸；换玛西后被刃甲反伤",
                [0, 1],
                timeline,
                segments,
                "川神",
            )
        )

    def test_unknown_gsi_hero_does_not_force_placeholder_into_title(self):
        timeline = ["42:18 本局高地推进后基地爆炸"]
        segments = [{
            "start_seconds": 0,
            "end_seconds": 3501,
            "hero": "未知(255)",
            "identity_source": "gsi_streamer_anchor",
        }]

        self.assertFalse(bridge.dota2_gsi_hero_is_usable("未知(255)"))
        self.assertFalse(bridge.dota2_gsi_hero_is_usable("unknown(255)"))
        self.assertFalse(bridge.dota2_gsi_hero_is_usable("npc_dota_hero_126"))
        self.assertFalse(bridge.streamer_gameplay_is_verified(segments[0]))
        self.assertEqual(
            bridge.recording_title_missing_selected_gsi_heroes(
                "高地推进后基地爆炸",
                [0],
                timeline,
                [segment for segment in segments if bridge.streamer_gameplay_is_verified(segment)],
            ),
            [],
        )

    @unittest.skip("removed cover behavior")
    def test_gsi_equipment_prompt_separates_six_slots_neutral_and_upgrades(self):
        prompt = bridge.dota2_gsi_equipment_prompt_instruction(
            ["紫怨", "魔瓶", "灵魂之戒", "巫师之刃", "散慧对剑", "动力鞋"],
            "锯齿短刀",
            ["A杖", "魔晶"],
        )

        self.assertIn("最终主装备栏快照（最多六格）", prompt)
        self.assertIn("中立物品：锯齿短刀；中立物品不占主装备六格", prompt)
        self.assertIn("额外升级状态：A杖, 魔晶", prompt)
        self.assertIn("不得重复算作第七件主装备", prompt)
        self.assertIn("Valve 官方装备图标参考表现全部已确认装备", prompt)
        self.assertIn("可沿主播人物和官方英雄外围错落环绕", prompt)
        self.assertIn("画面最下方安全区整齐排成一排", prompt)
        self.assertIn("禁止把装备穿戴、手持、背负或嵌入主播与英雄身体", prompt)
        self.assertIn("经典双主体切片构图", prompt)
        self.assertIn("主播头像人物独立位于前景", prompt)
        self.assertIn("Valve 官方英雄独立位于侧后方", prompt)
        self.assertNotIn("不得额外添加第七件装备", prompt)

    @unittest.skip("removed cover behavior")
    def test_both_cover_aspects_allow_surrounding_or_bottom_row_item_icons(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")

        self.assertIn("适合个人空间大图展示。装备图标可沿外围安全区域错落环绕", source)
        self.assertIn("统一放在最下方安全区一排", source)
        self.assertIn("画幅变窄时仍使用经典双主体构图", source)
        self.assertIn("装备以清晰独立的官方图标沿安全区域外围错落环绕", source)
        self.assertIn("最下方安全区一排", source)

    def test_known_dota2_items_do_not_silently_continue_without_references(self):
        with self.assertRaisesRegex(RuntimeError, "已停止生成"):
            bridge.require_dota2_item_reference(
                None,
                ["紫怨: download failed", "魔瓶: invalid image"],
            )

        reference = Path("/tmp/dota2-item-reference.png")
        self.assertEqual(
            bridge.require_dota2_item_reference(reference, []),
            reference,
        )

    def test_real_multigame_title_requires_streamer_heroes_even_with_opponent_mention(self):
        timeline = [
            "06:15 第一局末段基地被摧毁，弹幕随后出现下一把，并继续讨论帕克买活后阵亡",
            "41:34 玛西追击火猫和小小时，观众多次提醒不要继续追击",
        ]
        segments = [
            {"start_seconds": 0, "end_seconds": 1149, "hero": "风暴之灵"},
            {"start_seconds": 1407, "end_seconds": 3501, "hero": "玛西"},
        ]

        self.assertEqual(
            bridge.recording_title_missing_selected_gsi_heroes(
                "第一局基地被摧毁；追击火猫后被拉扯",
                [0, 1],
                timeline,
                segments,
            ),
            ["风暴之灵", "玛西"],
        )
        self.assertEqual(
            bridge.recording_title_missing_selected_gsi_heroes(
                "川神风暴之灵基地告破；换玛西追击火猫后被拉扯",
                [0, 1],
                timeline,
                segments,
            ),
            [],
        )

    def test_long_recording_coverage_uses_actual_timeline_span(self):
        timeline = [
            "00:30 事件一",
            "08:00 事件二",
            "19:15 事件三",
            "37:22 事件四",
            "48:17 事件五",
            "56:24 事件六",
            "58:08 事件七",
            "59:35 事件八",
        ]

        self.assertFalse(
            bridge.recording_title_timeline_coverage_is_sufficient(
                [3, 4, 5], 3600, len(timeline), timeline
            )
        )
        self.assertTrue(
            bridge.recording_title_timeline_coverage_is_sufficient(
                [2, 4, 6], 3600, len(timeline), timeline
            )
        )

    def test_live_stats_are_placed_after_archive_description(self):
        description = bridge.append_live_stats_to_description(
            "直播录播：YYF《休赛期改名狂欢》。",
            "【直播信息】\n峰值人气：123 万",
        )

        self.assertEqual(
            description,
            "【直播信息】\n峰值人气：123 万",
        )

    def test_game_stats_are_first_and_audience_stats_are_last(self):
        stats = (
            "——— 直播数据 ———\n"
            "🎁 飞机×1(单价100元/总价100元) | 礼物价值合计 100元\n"
            "👥 在线 1000~1500\n"
            "🎮 噬魂鬼｜六格：力量手套、相位鞋 K/D/A 8/2/10 KDA 9"
        )

        description = bridge.append_live_stats_to_description(
            "正文\n\n重要时间点\n00:21 YYF小狗四拳套出门",
            stats,
        )

        self.assertTrue(description.startswith(
            "——— 对局数据 ———\n🎮 噬魂鬼｜六格：力量手套、相位鞋"
        ))
        self.assertIn("\n正文\n\n重要时间点\n00:21 YYF小狗四拳套出门\n", description)
        self.assertTrue(description.endswith(
            "——— 直播数据 ———\n"
            "🎁 飞机×1(单价100元/总价100元) | 礼物价值合计 100元\n"
            "👥 在线 1000~1500"
        ))
        self.assertEqual(bridge.strip_live_stats_from_description(description, stats),
                         "正文\n\n重要时间点\n00:21 YYF小狗四拳套出门")

    def test_live_stats_are_last_when_description_reaches_limit(self):
        stats = "【直播信息】\n" + "统计" * 20
        description = bridge.append_live_stats_to_description("正文" * 2000, stats)

        self.assertTrue(description.endswith(stats))
        self.assertLessEqual(len(description), 1900)

    def test_description_limit_keeps_complete_timeline_before_long_prose(self):
        points = "\n".join(f"{index:02d}:00 看点{index}" for index in range(10))
        description = f"{'正文' * 1000}\n\n重要时间点\n{points}"

        fitted = bridge.fit_description_preserving_timeline(description, 1800)

        self.assertLessEqual(len(fitted), 1800)
        self.assertEqual(len(bridge.timeline_lines(fitted)), 10)
        self.assertEqual(bridge.timeline_lines(fitted)[-1], "09:00 看点9")

    def test_live_stats_limit_preserves_verified_timeline(self):
        stats = "——— 直播数据 ———\n" + "统计" * 190
        points = "\n".join(f"{index:02d}:00 看点{index}" for index in range(10))
        body = f"{'正文' * 750}\n\n重要时间点\n{points}"

        description = bridge.append_live_stats_to_description(body, stats)

        self.assertTrue(description.endswith(stats))
        self.assertLessEqual(len(description), 1900)
        self.assertEqual(len(bridge.timeline_lines(description)), 10)

    def test_oversized_gift_stats_keep_timeline_and_complete_gift_entries(self):
        gifts = " ".join(
            f"礼物{index}×1(单价5元/总价5元)"
            for index in range(160)
        )
        stats = (
            f"——— 直播数据 ———\n🎁 {gifts} | 礼物价值合计 800元\n"
            "👥 在线 15000~23000"
        )
        points = "\n".join(f"{index:02d}:00 看点{index}" for index in range(10))
        body = f"{'正文' * 1000}\n\n重要时间点\n{points}"

        description = bridge.append_live_stats_to_description(body, stats)

        self.assertLessEqual(len(description), 1900)
        self.assertEqual(len(bridge.timeline_lines(description)), 10)
        gift_line = next(
            line for line in description.splitlines() if line.startswith("🎁 ")
        )
        self.assertIn("礼物价值合计 800元", gift_line)
        self.assertIn("另", gift_line)
        self.assertNotRegex(gift_line, r"单价[^()]*$")

    def test_live_stats_limit_preserves_every_multipart_section(self):
        parts = []
        for part_number in (1, 2):
            points = "\n".join(
                f"{index:02d}:00 P{part_number}看点{index}"
                for index in range(10)
            )
            parts.append({
                "part_number": part_number,
                "title_topic": f"第{part_number}段",
                "recorded_at": "07-31",
                "description": f"{'正文' * 500}\n\n重要时间点\n{points}",
            })
        body = bridge.render_multipart_description(parts, "总简介")
        stats = "——— 直播数据 ———\n" + "统计" * 90

        description = bridge.append_live_stats_to_description(body, stats)

        self.assertLessEqual(len(description), 1900)
        self.assertIn("【P1｜第1段｜07-31】", description)
        self.assertIn("【P2｜第2段｜07-31】", description)
        self.assertEqual(description.count("重要时间点"), 2)
        self.assertEqual(len(bridge.timeline_lines(description)), 20)

    def test_live_stats_are_moved_to_end_when_ai_repeats_them(self):
        stats = "——— 直播数据 ———\n🎁 狂欢飞机×2(200元)｜合计 200元\n👥 在线 8257~10000"
        ai_description = f"{stats}\n\n直播录播正文"

        description = bridge.append_live_stats_to_description(ai_description, stats)

        self.assertEqual(description, f"直播录播正文\n{stats}")
        self.assertEqual(description.count("——— 直播数据 ———"), 1)

    def test_existing_duplicate_live_stats_are_collapsed_on_retry(self):
        stats = "——— 直播数据 ———\n🎁 狂欢飞机×2(200元)｜合计 200元\n👥 在线 8257~10000"
        duplicated = f"{stats}\n\n{stats}\n\n直播录播正文"

        description = bridge.append_live_stats_to_description(duplicated, stats)

        self.assertEqual(description, f"直播录播正文\n{stats}")
        self.assertEqual(description.count("——— 直播数据 ———"), 1)

    def test_live_stats_can_be_removed_from_persisted_submission_description(self):
        stats = "——— 直播数据 ———\n🎁 飞机×1(100元) | 合计 100元"
        persisted = f"{stats}\n\n{stats}\n\nAI 正文"

        self.assertEqual(
            bridge.strip_live_stats_from_description(persisted, stats),
            "AI 正文",
        )

        appended = f"AI 正文\n\n{stats}"
        self.assertEqual(
            bridge.strip_live_stats_from_description(appended, stats),
            "AI 正文",
        )

    def test_ai_live_stats_context_is_grounding_only(self):
        stats = "——— 直播数据 ———\n👥 在线 8257~10000"
        package = types.ModuleType("modules")
        package.__path__ = []
        enhancer = types.ModuleType("modules.ai_enhancer")
        config_manager = types.ModuleType("modules.config_manager")
        enhancer.get_openai_client = lambda _cfg: object()
        enhancer._request_json_object = lambda **_kwargs: {
            "title_topic": "测试主题",
            "description": f"{stats}\n\nAI 正文",
        }
        config_manager.load_config = lambda: {"OPENAI_API_KEY": "test"}

        with patch.dict(sys.modules, {
            "modules": package,
            "modules.ai_enhancer": enhancer,
            "modules.config_manager": config_manager,
        }):
            description, topic = bridge.generate_danmaku_metadata_with_ai(
                [types.SimpleNamespace(time=1.0, text="测试弹幕")],
                "录播前缀\n\n",
                {
                    "_config_dir": str(Path(bridge.__file__).resolve().parent),
                    "ai_danmaku_summary_enabled": True,
                },
                {"live_stats": stats},
            )

        self.assertEqual(topic, "测试主题")
        self.assertEqual(description, "录播前缀\n\nAI 正文")
        self.assertNotIn("直播数据", description)

    def test_ai_timeline_is_reanchored_to_xml_seconds_and_formatted_by_code(self):
        package = types.ModuleType("modules")
        package.__path__ = []
        enhancer = types.ModuleType("modules.ai_enhancer")
        config_manager = types.ModuleType("modules.config_manager")
        enhancer.get_openai_client = lambda _cfg: object()
        enhancer._request_json_object = lambda **_kwargs: {
            "title_topic": "BP 争议",
            "description": "正文\n\n重要时间点\n00:21 AI 手写的错误时间",
            "timeline": [{
                "event": "弹幕质疑 BP 顺位",
                "evidence_text": "BP顺位受到质疑",
                "evidence_keywords": ["BP", "顺位"],
            }],
        }
        config_manager.load_config = lambda: {"OPENAI_API_KEY": "test"}

        with patch.dict(sys.modules, {
            "modules": package,
            "modules.ai_enhancer": enhancer,
            "modules.config_manager": config_manager,
        }):
            description, topic = bridge.generate_danmaku_metadata_with_ai(
                [types.SimpleNamespace(time=1268.0, text="BP顺位受到质疑")],
                "录播前缀\n\n",
                {
                    "_config_dir": str(Path(bridge.__file__).resolve().parent),
                    "ai_danmaku_summary_enabled": True,
                    "ai_danmaku_reaction_delay_seconds": 8,
                },
                timeline_duration_seconds=3600,
            )

        self.assertEqual(topic, "BP 争议")
        self.assertEqual(
            description,
            "21:00 弹幕质疑 BP 顺位",
        )
        self.assertNotIn("00:21", description)

    def test_headingless_timeline_is_kept_and_fitted_as_description_body(self):
        body = "\n".join(f"0{index}:00 看点{index}" for index in range(1, 10))

        self.assertEqual(len(bridge.timeline_lines(body)), 9)
        fitted = bridge.fit_description_preserving_timeline(body, 42)
        self.assertNotIn("重要时间点", fitted)
        self.assertTrue(all(
            bridge._TIMELINE_LINE_RE.match(line)
            for line in fitted.splitlines()
        ))

    def test_ai_timeline_retries_once_when_verified_points_are_below_target(self):
        package = types.ModuleType("modules")
        package.__path__ = []
        enhancer = types.ModuleType("modules.ai_enhancer")
        config_manager = types.ModuleType("modules.config_manager")
        enhancer.get_openai_client = lambda _cfg: object()
        comments = [
            types.SimpleNamespace(time=float(index * 300), text=f"事件证据{index}")
            for index in range(1, 9)
        ]
        responses = iter([
            {
                "title_topic": "完整看点",
                "description": "正文",
                "timeline": [{
                    "event": "事件1",
                    "evidence_texts": ["事件证据1"],
                    "evidence_keywords": ["证据1"],
                }],
            },
            {
                "description": "重生成的完整正文",
                "timeline": [{
                    "event": f"事件{index}",
                    "evidence_texts": [f"事件证据{index}"],
                    "evidence_keywords": [f"证据{index}"],
                } for index in range(1, 9)],
            },
        ])
        enhancer._request_json_object = lambda **_kwargs: next(responses)
        config_manager.load_config = lambda: {"OPENAI_API_KEY": "test"}
        diagnostics = {}

        with patch.dict(sys.modules, {
            "modules": package,
            "modules.ai_enhancer": enhancer,
            "modules.config_manager": config_manager,
        }):
            description, _topic = bridge.generate_danmaku_metadata_with_ai(
                comments,
                "",
                {
                    "_config_dir": str(Path(bridge.__file__).resolve().parent),
                    "ai_danmaku_summary_enabled": True,
                    "ai_danmaku_reaction_delay_seconds": 8,
                },
                timeline_duration_seconds=3600,
                timeline_diagnostics=diagnostics,
            )

        self.assertEqual(len(bridge.timeline_lines(description)), 8)
        self.assertTrue(diagnostics["timeline_retry_attempted"])
        self.assertTrue(diagnostics["description_regeneration_attempted"])
        self.assertTrue(diagnostics["description_regeneration_used"])
        self.assertTrue(diagnostics["timeline_target_met"])
        self.assertEqual(diagnostics["timeline_verified_count"], 8)
        self.assertEqual(diagnostics["timeline_shortfall"], 0)

    def test_ai_timeline_records_insufficient_evidence_without_forcing_retry(self):
        package = types.ModuleType("modules")
        package.__path__ = []
        enhancer = types.ModuleType("modules.ai_enhancer")
        config_manager = types.ModuleType("modules.config_manager")
        enhancer.get_openai_client = lambda _cfg: object()
        enhancer._request_json_object = lambda **_kwargs: {
            "title_topic": "单一看点",
            "description": "正文",
            "timeline": [{
                "event": "唯一事件",
                "evidence_texts": ["唯一证据"],
                "evidence_keywords": ["唯一证据"],
            }],
        }
        config_manager.load_config = lambda: {"OPENAI_API_KEY": "test"}
        diagnostics = {}

        with patch.dict(sys.modules, {
            "modules": package,
            "modules.ai_enhancer": enhancer,
            "modules.config_manager": config_manager,
        }):
            bridge.generate_danmaku_metadata_with_ai(
                [types.SimpleNamespace(time=120.0, text="唯一证据")],
                "",
                {
                    "_config_dir": str(Path(bridge.__file__).resolve().parent),
                    "ai_danmaku_summary_enabled": True,
                },
                timeline_duration_seconds=3600,
                timeline_diagnostics=diagnostics,
            )

        self.assertFalse(diagnostics["timeline_retry_attempted"])
        self.assertFalse(diagnostics["timeline_target_met"])
        self.assertEqual(diagnostics["timeline_evidence_status"], "insufficient")
        self.assertEqual(diagnostics["timeline_shortfall"], 5)
        self.assertEqual(
            diagnostics["timeline_anchor_policy"],
            "same_time_screen_spam_or_exact_xml",
        )
        self.assertEqual(diagnostics["timeline_cluster_window_seconds"], 60)

    def test_description_is_regenerated_before_title_uses_final_description(self):
        package = types.ModuleType("modules")
        package.__path__ = []
        enhancer = types.ModuleType("modules.ai_enhancer")
        config_manager = types.ModuleType("modules.config_manager")
        enhancer.get_openai_client = lambda _cfg: object()
        comments = [
            types.SimpleNamespace(time=float(index * 300), text=f"证据{index}")
            for index in range(1, 9)
        ]
        calls = []
        title_payloads = []

        def request(**kwargs):
            calls.append(kwargs)
            scene = kwargs["scene_name"]
            if scene == "recording_danmaku_summary":
                return {
                    "description": "时间点不完整的首稿",
                    "timeline": [{
                        "event": "首稿事件",
                        "evidence_texts": ["证据1"],
                        "evidence_keywords": ["证据1"],
                    }],
                }
            if scene == "recording_danmaku_description_regenerate":
                return {
                    "description": "重生成的完整简介",
                    "timeline": [{
                        "event": f"完整事件{index}",
                        "evidence_texts": [f"证据{index}"],
                        "evidence_keywords": [f"证据{index}"],
                    } for index in range(1, 9)],
                }
            self.assertEqual(scene, "recording_danmaku_title_from_description")
            title_payloads.append(kwargs["payload"])
            return {
                "title_topic": "完整事件1推进后在完整事件8收尾，弹幕见证整段变化",
                "coverage_mode": "main_arc",
                "selected_timeline_indexes": [0, 7],
            }

        enhancer._request_json_object = request
        config_manager.load_config = lambda: {"OPENAI_API_KEY": "test"}

        with patch.dict(sys.modules, {
            "modules": package,
            "modules.ai_enhancer": enhancer,
            "modules.config_manager": config_manager,
        }):
            description, topic = bridge.generate_danmaku_metadata_with_ai(
                comments,
                "",
                {
                    "_config_dir": str(Path(bridge.__file__).resolve().parent),
                    "ai_danmaku_summary_enabled": True,
                    "ai_danmaku_reaction_delay_seconds": 8,
                },
                timeline_duration_seconds=3600,
            )

        self.assertEqual(
            topic,
            "完整事件1推进后在完整事件8收尾，弹幕见证整段变化",
        )
        self.assertEqual(len(bridge.timeline_lines(description)), 8)
        self.assertEqual(title_payloads[0]["final_description"], description)
        self.assertEqual(title_payloads[0]["verified_timeline"], bridge.timeline_lines(description))
        self.assertEqual(
            title_payloads[0]["streamer_participation"]["mode"],
            "unknown",
        )
        self.assertFalse(
            title_payloads[0]["streamer_participation"]["gameplay_verified"]
        )
        self.assertEqual(
            [call["scene_name"] for call in calls],
            [
                "recording_danmaku_summary",
                "recording_danmaku_description_regenerate",
                "recording_danmaku_title_from_description",
            ],
        )

    def test_long_recording_never_falls_back_to_single_timeline_point_after_retries(self):
        package = types.ModuleType("modules")
        package.__path__ = []
        enhancer = types.ModuleType("modules.ai_enhancer")
        config_manager = types.ModuleType("modules.config_manager")
        enhancer.get_openai_client = lambda _cfg: object()
        comments = [
            types.SimpleNamespace(time=float(index * 300), text=f"证据{index}")
            for index in range(8)
        ]
        title_calls = []

        def request(**kwargs):
            if kwargs["scene_name"] == "recording_danmaku_summary":
                return {
                    "description": "完整简介",
                    "timeline": [{
                        "event": f"完整事件{index}",
                        "evidence_texts": [f"证据{index}"],
                        "evidence_keywords": [f"证据{index}"],
                    } for index in range(8)],
                }
            self.assertEqual(
                kwargs["scene_name"],
                "recording_danmaku_title_from_description",
            )
            title_calls.append(kwargs["payload"])
            return {
                "title_topic": "单个短节点",
                "coverage_mode": "sparse",
                "selected_timeline_indexes": [4],
            }

        enhancer._request_json_object = request
        config_manager.load_config = lambda: {"OPENAI_API_KEY": "test"}
        diagnostics = {}

        with patch.dict(sys.modules, {
            "modules": package,
            "modules.ai_enhancer": enhancer,
            "modules.config_manager": config_manager,
        }):
            description, topic = bridge.generate_danmaku_metadata_with_ai(
                comments,
                "",
                {
                    "_config_dir": str(Path(bridge.__file__).resolve().parent),
                    "ai_danmaku_summary_enabled": True,
                },
                timeline_duration_seconds=3600,
                timeline_diagnostics=diagnostics,
            )

        self.assertEqual(len(bridge.timeline_lines(description)), 8)
        self.assertEqual(topic, "")
        self.assertEqual(len(title_calls), 3)
        self.assertTrue(diagnostics["title_topic_manual_review_required"])
        self.assertTrue(diagnostics["title_topic_long_video_fallback_rejected"])

    def test_generic_recording_intro_is_removed_from_final_body(self):
        stats = "——— 直播数据 ———\n👥 在线 8257~10000"
        description = bridge.append_live_stats_to_description(
            "直播录播：YYF。正文从这里开始。",
            stats,
        )

        self.assertEqual(description, f"正文从这里开始。\n{stats}")
        self.assertNotIn("直播录播：YYF。", description)

    def test_full_batch_summary_sends_every_effective_comment(self):
        comments = [
            Mock(time=float(index), text=f"完整弹幕{index}")
            for index in range(405)
        ]
        package = types.ModuleType("modules")
        package.__path__ = []
        enhancer = types.ModuleType("modules.ai_enhancer")
        config_manager = types.ModuleType("modules.config_manager")
        enhancer.get_openai_client = lambda _cfg: object()
        batch_payloads = []
        batch_prompts = []
        selection_prompts = []
        concurrency_lock = threading.Lock()
        release_batches = threading.Event()
        active_batches = 0
        maximum_active_batches = 0

        def request(**kwargs):
            nonlocal active_batches, maximum_active_batches
            if kwargs["scene_name"] == "recording_danmaku_summary_batch":
                batch_prompts.append(kwargs["system_prompt"])
                with concurrency_lock:
                    active_batches += 1
                    maximum_active_batches = max(maximum_active_batches, active_batches)
                    if maximum_active_batches >= 3:
                        release_batches.set()
                release_batches.wait(1)
                payload = kwargs["payload"]
                batch_payloads.append(payload)
                evidence = payload["sampled_comment_evidence"]
                result = {
                    "description": "批次简介",
                    "timeline": [
                        {
                            "event": f"批次事件{item['text']}",
                            "evidence_texts": [item["text"]],
                            "evidence_keywords": [item["text"]],
                        }
                        for item in evidence[:3]
                    ],
                }
                with concurrency_lock:
                    active_batches -= 1
                return result
            if kwargs["scene_name"] == "recording_danmaku_timeline_select":
                selection_prompts.append(kwargs["system_prompt"])
                self.assertEqual(
                    len(kwargs["payload"]["verified_candidates"]),
                    15,
                )
                return {"selected_indexes": list(range(10))}
            self.assertEqual(
                kwargs["scene_name"],
                "recording_danmaku_title_from_description",
            )
            return {"title_topic": "全量分批标题"}

        enhancer._request_json_object = request
        config_manager.load_config = lambda: {"OPENAI_API_KEY": "test"}

        with patch.dict(sys.modules, {
            "modules": package,
            "modules.ai_enhancer": enhancer,
            "modules.config_manager": config_manager,
        }):
            bridge.generate_danmaku_metadata_with_ai(
                comments,
                "",
                {
                    "_config_dir": str(Path(bridge.__file__).resolve().parent),
                    "ai_danmaku_summary_enabled": True,
                    "ai_danmaku_full_batch_enabled": True,
                    "ai_danmaku_batch_comments": 100,
                    "ai_danmaku_reaction_delay_seconds": 0,
                },
            )

        self.assertEqual(len(batch_payloads), 5)
        self.assertEqual(maximum_active_batches, 3)
        all_evidence = [
            item["text"]
            for payload in sorted(
                batch_payloads,
                key=lambda value: value["batch_context"]["index"],
            )
            for item in payload["sampled_comment_evidence"]
        ]
        self.assertEqual(all_evidence, [comment.text for comment in comments])
        self.assertEqual(
            [
                payload["batch_context"]["comment_count"]
                for payload in sorted(
                    batch_payloads,
                    key=lambda value: value["batch_context"]["index"],
                )
            ],
            [100, 100, 100, 100, 5],
        )
        self.assertTrue(all(not payload["sampled_comments"] for payload in batch_payloads))
        self.assertTrue(batch_prompts)
        self.assertTrue(all("谁对阵谁" in prompt for prompt in batch_prompts))
        self.assertTrue(all("谢彬一方/眼子一方" in prompt for prompt in batch_prompts))
        self.assertTrue(all("人物+英雄+具体事件" in prompt for prompt in batch_prompts))
        self.assertTrue(all("应当完成人物—英雄归因" in prompt for prompt in batch_prompts))
        self.assertTrue(all("只适用于能确认为比赛或游戏对局" in prompt for prompt in batch_prompts))
        self.assertTrue(all("禁止强行补英雄、对阵或胜负" in prompt for prompt in batch_prompts))
        self.assertTrue(all("不要为了规避风险而回避明显结论" in prompt for prompt in batch_prompts))
        self.assertTrue(all("应当直接下结论" in prompt for prompt in batch_prompts))
        self.assertEqual(len(selection_prompts), 1)
        self.assertIn("谁和谁在比赛", selection_prompts[0])
        self.assertIn("英雄归属和最终胜负", selection_prompts[0])
        self.assertIn("不得为了“安全”只选更空泛的讨论句", selection_prompts[0])
        self.assertIn("人物+英雄+事件", selection_prompts[0])
        self.assertIn("沉默没开大", selection_prompts[0])

    def test_recording_intro_with_internal_exclamation_is_removed_as_one_unit(self):
        self.assertEqual(
            bridge.strip_recording_intro(
                "直播录播：川神《来这里开心就好！ 74960》。本场直播开始。"
            ),
            "本场直播开始。",
        )

    def test_generic_recording_intro_is_removed_from_multipart_description(self):
        description = bridge.render_multipart_description(
            [{
                "part_number": 1,
                "title_topic": "第一局",
                "description": "直播录播：YYF。第一局正文",
            }],
            "直播录播：YYF。",
        )

        self.assertEqual(description, "【P1｜第一局】\n第一局正文")

    def test_grounded_timeline_finds_earliest_xml_evidence_then_compensates(self):
        sampled = [types.SimpleNamespace(time=729.0, text="39个人等你大西瓜")]
        comments = [
            types.SimpleNamespace(time=681.9, text="39个人等你大西瓜"),
            *sampled,
        ]

        self.assertEqual(
            bridge.render_grounded_danmaku_timeline(
                [{
                    "event": "三十多人苦等开局",
                    "evidence_text": "39个人等你大西瓜",
                    "evidence_keywords": ["39个人", "等", "大西瓜"],
                }],
                sampled,
                comments,
                delay_seconds=8,
                duration_seconds=3600,
            ),
            "重要时间点\n11:13 三十多人苦等开局",
        )

    def test_grounded_timeline_formats_minutes_and_hours_from_seconds(self):
        sampled = [
            types.SimpleNamespace(time=1268.0, text="BP顺位受到质疑"),
            types.SimpleNamespace(time=3608.0, text="第二小时开始"),
        ]
        timeline = [
            {"event": "进入第二小时", "evidence_text": "第二小时开始", "evidence_keywords": ["第二小时"]},
            {"event": "弹幕质疑 BP 顺位", "evidence_text": "BP顺位受到质疑", "evidence_keywords": ["BP", "顺位"]},
        ]

        self.assertEqual(
            bridge.render_grounded_danmaku_timeline(
                timeline,
                sampled,
                sampled,
                delay_seconds=8,
                duration_seconds=3700,
            ),
            "重要时间点\n21:00 弹幕质疑 BP 顺位\n01:00:00 进入第二小时",
        )

    def test_grounded_timeline_enforces_configured_maximum(self):
        comments = [
            types.SimpleNamespace(time=float(index * 100), text=f"证据{index}")
            for index in range(1, 21)
        ]
        timeline = [{
            "event": f"事件{index}",
            "evidence_texts": [f"证据{index}"],
            "evidence_keywords": [f"证据{index}"],
        } for index in range(1, 21)]

        rendered = bridge.render_grounded_danmaku_timeline(
            timeline,
            comments,
            comments,
            duration_seconds=3600,
            maximum_points=12,
        )

        self.assertEqual(len(bridge.timeline_lines(rendered)), 12)

    def test_grounded_timeline_rejects_exact_evidence_spread_across_distant_events(self):
        sampled = [
            types.SimpleNamespace(time=1572.0, text="紧急情况 大狗不在"),
            types.SimpleNamespace(time=1848.0, text="大狗撤销回溯"),
        ]
        comments = [
            types.SimpleNamespace(time=1571.0, text="大狗不在选人要回溯了"),
            *sampled,
        ]

        self.assertEqual(bridge.render_grounded_danmaku_timeline(
            [{
                "event": "大狗缺席导致选人回溯",
                "evidence_texts": ["紧急情况 大狗不在", "大狗撤销回溯"],
                "evidence_keywords": ["大狗", "不在", "回溯"],
            }],
            sampled,
            comments,
            delay_seconds=8,
            duration_seconds=3600,
        ), "")

    def test_grounded_timeline_does_not_shift_to_earlier_keyword_only_comment(self):
        exact = types.SimpleNamespace(time=729.0, text="BP顺位受到质疑")
        comments = [
            types.SimpleNamespace(time=300.0, text="BP和顺位要怎么选"),
            exact,
        ]

        diagnostics = {}
        rendered = bridge.render_grounded_danmaku_timeline(
            [{
                "event": "弹幕质疑 BP 顺位",
                "evidence_texts": ["BP顺位受到质疑"],
                "evidence_keywords": ["BP", "顺位", "质疑"],
            }],
            [exact],
            comments,
            delay_seconds=8,
            duration_seconds=3600,
            anchor_diagnostics=diagnostics,
        )

        self.assertEqual(rendered, "重要时间点\n12:01 弹幕质疑 BP 顺位")
        self.assertEqual(diagnostics["timeline_anchor_details"], [{
            "event": "弹幕质疑 BP 顺位",
            "xml_anchor": "12:09",
            "final_timestamp": "12:01",
            "reaction_delay_seconds": 8,
            "evidence_count": 1,
        }])

    def test_grounded_timeline_merges_duplicate_event_candidates(self):
        comments = [
            types.SimpleNamespace(time=100.0, text="盾被抢了"),
            types.SimpleNamespace(time=112.0, text="对面抢到肉山盾"),
        ]
        diagnostics = {}
        rendered = bridge.render_grounded_danmaku_timeline(
            [
                {
                    "event": "对手抢下肉山盾",
                    "evidence_texts": ["盾被抢了"],
                    "evidence_keywords": ["盾被", "被抢"],
                },
                {
                    "event": "对手抢下肉山盾",
                    "evidence_texts": ["对面抢到肉山盾"],
                    "evidence_keywords": ["对面", "肉山盾"],
                },
            ],
            comments,
            comments,
            delay_seconds=8,
            duration_seconds=300,
            anchor_diagnostics=diagnostics,
        )

        self.assertEqual(rendered, "重要时间点\n01:32 对手抢下肉山盾")
        self.assertEqual(diagnostics["timeline_rejection_reasons"]["duplicate_event"], 1)

    def test_grounded_timeline_accepts_adjacent_exact_evidence_cluster(self):
        comments = [
            types.SimpleNamespace(time=100.0, text="肉山已经开了"),
            types.SimpleNamespace(time=112.0, text="盾被对面抢了"),
        ]

        self.assertEqual(
            bridge.render_grounded_danmaku_timeline(
                [{
                    "event": "肉山盾遭到对手抢夺",
                    "evidence_texts": ["肉山已经开了", "盾被对面抢了"],
                    "evidence_keywords": ["肉山", "盾", "抢"],
                }],
                comments,
                comments,
                delay_seconds=8,
                duration_seconds=300,
            ),
            "重要时间点\n01:32 肉山盾遭到对手抢夺",
        )

    def test_grounded_timeline_accepts_same_time_keyword_screen_spam(self):
        sampled = [types.SimpleNamespace(time=112.0, text="马上要打了")]
        comments = [
            types.SimpleNamespace(time=100.0, text="开团了开团了"),
            types.SimpleNamespace(time=108.0, text="真的开团了"),
            types.SimpleNamespace(time=112.0, text="马上要打了"),
            types.SimpleNamespace(time=118.0, text="这波开团可以"),
        ]
        diagnostics = {}

        rendered = bridge.render_grounded_danmaku_timeline(
            [{
                "event": "双方突然开团",
                "evidence_texts": ["模型没有逐字复制"],
                "evidence_keywords": ["开团"],
            }],
            sampled,
            comments,
            delay_seconds=8,
            duration_seconds=300,
            anchor_diagnostics=diagnostics,
        )

        self.assertEqual(rendered, "重要时间点\n01:32 双方突然开团")
        self.assertEqual(diagnostics["timeline_relaxed_screen_spam_count"], 1)

    def test_timeline_target_scales_with_recording_duration(self):
        self.assertEqual(bridge.timeline_target_range(None), (4, 10))
        self.assertEqual(bridge.timeline_target_range(1800), (4, 8))
        self.assertEqual(bridge.timeline_target_range(3600), (6, 12))
        self.assertEqual(bridge.timeline_target_range(7200), (8, 16))

    def test_grounded_timeline_rejects_unverifiable_evidence(self):
        sampled = [types.SimpleNamespace(time=1268.0, text="BP顺位受到质疑")]

        self.assertEqual(
            bridge.render_grounded_danmaku_timeline(
                [{"event": "锁定屠夫", "evidence_text": "上屠夫了", "evidence_keywords": ["屠夫"]}],
                sampled,
                sampled,
            ),
            "",
        )

    def test_ai_written_timeline_is_discarded(self):
        self.assertEqual(
            bridge.strip_ai_timeline_lines("正文\n\n重要时间点\n00:21 BP\n00:01 团战"),
            "正文",
        )

    def test_ai_written_important_event_list_is_discarded(self):
        self.assertEqual(
            bridge.strip_ai_timeline_lines("正文\n\n重要事件：\n事件一\n事件二"),
            "正文",
        )

    def test_live_stats_stage_details_persist_visible_summary(self):
        stats = "——— 直播数据 ———\n👥 在线 547~957"

        self.assertEqual(
            bridge.live_stats_stage_details(stats),
            {
                "stats_collected": True,
                "stats_summary": stats,
                "stats_length": len(stats),
                "outcome": "matched",
            },
        )

    def test_long_sparse_xml_is_marked_suspected_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.flv"
            xml = root / "clip.xml"
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1,1,25,16777215,0,0,1,0">仅一条</d></i>',
                encoding="utf-8",
            )
            comments = bridge.parse_danmaku_xml(xml)

            with patch.object(bridge, "video_duration_seconds", return_value=3601):
                details = bridge.danmaku_stage_details(video, xml, comments, {})

            self.assertEqual(details["danmaku_integrity"], "suspected_incomplete")
            self.assertEqual(details["video_duration_seconds"], 3601.0)
            self.assertIn("已保留源 XML", details["danmaku_integrity_reason"])

    def test_avatar_reference_cache_defaults_to_writable_bridge_state_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            response = Mock()
            response.read.return_value = b"avatar-bytes"
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            with patch.object(bridge.urllib.request, "urlopen", return_value=response):
                avatar = bridge.download_recording_avatar_reference(
                    "https://example.com/avatar.jpg",
                    {
                        "_config_dir": str(root),
                        "state_db": ".bridge/state.sqlite3",
                    },
                )

            self.assertEqual(
                avatar.parent,
                (root / ".bridge" / "avatar-cache").resolve(),
            )
            self.assertEqual(avatar.read_bytes(), b"avatar-bytes")

    def test_default_recording_description_requests_clickable_timeline(self):
        prompt = bridge.DEFAULT_RECORDING_DESCRIPTION_AI_PROMPT
        self.assertIn("时间点式中文简介", prompt)
        self.assertIn("完整 XML", prompt)
        self.assertIn("不要在简介正文中手写时间点", prompt)
        self.assertIn("不得编造时间或事件", prompt)
        self.assertIn("候选事件必须按直播时间向前推进", prompt)
        self.assertIn("不要为了突出标题打乱顺序", prompt)
        self.assertIn("赛后复盘", prompt)
        self.assertIn("重要时间点必须覆盖简介中的关键事件", prompt)
        self.assertIn("事件文案只做证据的最小忠实改写", prompt)

    def test_timeline_prompt_is_generic_and_only_adds_game_events_conditionally(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")

        self.assertIn("适用于所有直播类型", source)
        self.assertIn("只有输入明确属于", source)
        self.assertIn("不得把聊天、访谈", source)
        self.assertIn("streamer_identity", source)
        self.assertIn("其他主播、选手或嘉宾", source)
        self.assertIn("谁做了什么", source)
        self.assertIn("同一 60 秒窗口内至少 3 条弹幕", source)
        self.assertIn("现在只能根据 final_description", source)
        self.assertIn("重新生成完整 description 和完整 timeline", source)
        self.assertIn("简介包含两个先后发生的独立转折", source)
        self.assertIn("默认选择一个最强事件", source)
        self.assertIn("只有两个事件都足够重要且在48字内仍能分别完整表达时", source)
        self.assertIn("使用 main_arc", source)
        self.assertIn("使用 two_highlights", source)
        self.assertIn("标题不需要每条都出现", source)
        self.assertIn("删除观众反应后仍不影响", source)
        self.assertIn("同一标题最多保留一处观众反应", source)
        self.assertIn("优先写清“谁和谁做了什么”", source)
        self.assertIn("不得猜测为一起玩", source)
        self.assertIn("按5W1H检查", source)
        self.assertIn("原因也不得从常识", source)
        self.assertIn("非 DOTA2 的游戏", source)
        self.assertIn("普通催促、单条“你”", source)
        self.assertIn("selected_timeline_indexes", source)
        self.assertIn("禁止截断半句话", source)
        self.assertIn("每条 event 必须是 evidence_texts 的最小忠实改写", source)
        self.assertIn("一条像总结稿的超长弹幕不能独自支撑", source)
        self.assertIn("开场承接", source)
        self.assertIn("分析批次的第一条不等于录播开场", source)
        self.assertIn("后续批次严禁使用“开场”措辞", source)
        self.assertIn("事件正文默认不写“前段/中段/后段”", source)
        self.assertIn("不能把04分钟写成中段", source)

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

    def test_profile_avatar_uses_exact_recording_room_match(self):
        base = {
            "profiles": [
                {
                    "match": "*叫我老陈就好了_*",
                    "streamer_name": "叫我老陈就好了",
                    "streamer_avatar_url": "https://example.com/laochen.jpg",
                },
                {
                    "match": "*其他主播_*",
                    "streamer_name": "其他主播",
                    "streamer_avatar_url": "https://example.com/other.jpg",
                },
            ],
        }

        cfg = bridge.effective_config(
            base,
            Path("叫我老陈就好了_冲击第三冠！_2026-08-01_05-26.flv"),
        )

        self.assertEqual(cfg["streamer_name"], "叫我老陈就好了")
        self.assertEqual(
            cfg["streamer_avatar_url"],
            "https://example.com/laochen.jpg",
        )

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
        self.assertEqual(title, "中韩流行歌单·点歌闲聊｜07-23 09:45")

    def test_verified_ai_title_ignores_legacy_streamer_template(self):
        video = Path("叫我老陈就好了_来这里开心就好_2026-08-07_02-05.flv")

        title, _, _ = bridge.render_metadata(
            video,
            {
                "title_template": "川神｜{ai_topic}｜{date}｜【直播回放】",
                "streamer_name": "叫我老陈就好了",
            },
            ai_topic="玛西追击三人未能完成击杀",
        )

        self.assertEqual(title, "玛西追击三人未能完成击杀｜08-07 02:05")

    def test_danmaku_edition_marker_is_inserted_before_title_time(self):
        title = bridge.recording_danmaku_edition_title(
            "玛西追击三人未能完成击杀｜08-07 02:05"
        )

        self.assertEqual(title, "玛西追击三人未能完成击杀｜弹幕版 08-07 02:05")
        self.assertEqual(bridge.recording_danmaku_edition_title(title), title)

    def test_danmaku_edition_marker_is_removed_from_cover_text(self):
        self.assertEqual(
            bridge.strip_danmaku_edition_marker(
                "玛西追击三人未能完成击杀｜弹幕版 08-07 02:05"
            ),
            "玛西追击三人未能完成击杀｜08-07 02:05",
        )
        self.assertEqual(
            bridge.recording_cover_display_text(
                "玛西追击三人未能完成击杀",
                "弹幕版 玛西追击三人未能完成击杀",
            ),
            "玛西追击三人未能完成击杀",
        )

    def test_third_party_observer_topic_naturally_attributes_streamer(self):
        self.assertEqual(
            bridge.contextualize_streamer_title_topic(
                "老蔡三角区跳吼，马甲首夺高导冠军", "YYF", "spectating"
            ),
            "YYF观战老蔡三角区跳吼，马甲首夺高导冠军",
        )

    def test_unknown_third_party_topic_stays_event_led(self):
        self.assertEqual(
            bridge.contextualize_streamer_title_topic(
                "老蔡三角区跳吼，马甲首夺高导冠军", "YYF", "unknown"
            ),
            "老蔡三角区跳吼，马甲首夺高导冠军",
        )

    def test_room_discussion_fillers_are_removed_from_event_title(self):
        self.assertEqual(
            bridge.contextualize_streamer_title_topic(
                "国民大舅哥直播间热议直播间讨论小冉加速与小哈尼互超",
                "国民大舅哥",
                "unknown",
            ),
            "小冉加速与小哈尼互超",
        )
        self.assertEqual(
            bridge.contextualize_streamer_title_topic(
                "YYF直播中谢彬三星角色打出高伤害",
                "YYF",
                "playing",
            ),
            "谢彬三星角色打出高伤害",
        )
        self.assertEqual(
            bridge.contextualize_streamer_title_topic(
                "DD｜进入抽卡环节，弹幕刷屏“保底”“歪了”",
                "奶哥",
                "playing",
            ),
            "进入抽卡环节，弹幕刷屏“保底”“歪了”",
        )

    def test_yyf_title_name_uses_fengge_by_default_and_pangtou_for_verified_poor_play(self):
        self.assertEqual(bridge.preferred_recording_title_name("yyfyyf"), "枫哥")
        self.assertEqual(
            bridge.preferred_recording_title_name(
                "YYF",
                "35:17 YYF面对BOSS时千亿战力仍在刮痧，随后卡关。",
            ),
            "胖头",
        )
        self.assertEqual(
            bridge.preferred_recording_title_name(
                "YYF",
                "35:17 YYF观战老蔡暴毙，随后点评团战处理。",
            ),
            "枫哥",
        )

    def test_xiebin_title_name_prefers_naige(self):
        self.assertEqual(bridge.preferred_recording_title_name("谢彬DD"), "奶哥")
        self.assertEqual(bridge.preferred_recording_title_name("谢彬"), "奶哥")

    def test_non_dota_verified_gameplay_can_identify_the_room_owner(self):
        description = "35:17 YYF面对BOSS时千亿战力仍在刮痧，随后卡关。"
        self.assertEqual(
            bridge.infer_streamer_participation_mode(description, "yyfyyf"),
            "playing",
        )
        self.assertEqual(
            bridge.contextualize_streamer_title_topic(
                "胖头战力冲上千亿，打BOSS仍刮痧卡关", "胖头", "playing"
            ),
            "胖头战力冲上千亿，打BOSS仍刮痧卡关",
        )
        self.assertEqual(
            bridge.infer_streamer_participation_mode(
                "12:30 YYF选择核心装备后完成翻盘。", "YYF"
            ),
            "playing",
        )

    def test_existing_streamer_attribution_is_not_duplicated(self):
        self.assertEqual(
            bridge.contextualize_streamer_title_topic(
                "YYF观战老蔡三角区跳吼", "YYF", "spectating"
            ),
            "YYF观战老蔡三角区跳吼",
        )

    def test_mechanical_streamer_prefix_is_rewritten_with_relation(self):
        self.assertEqual(
            bridge.contextualize_streamer_title_topic(
                "YYF｜老蔡三角区跳吼", "YYF", "spectating"
            ),
            "YYF观战老蔡三角区跳吼",
        )

    def test_observer_cover_keeps_streamer_as_viewpoint_not_event_actor(self):
        role, instruction = bridge.recording_cover_streamer_role_instruction(
            "YYF",
            "YYF观战老蔡三角区跳吼，马甲首夺高导冠军",
        )
        self.assertEqual(role, "spectating")
        self.assertIn("当前主播头像为主视觉入口", instruction)
        self.assertIn("禁止把第三方选手的操作", instruction)

    def test_neutral_room_cover_does_not_claim_streamer_participation(self):
        role, instruction = bridge.recording_cover_streamer_role_instruction(
            "YYF",
            "YYF直播间热议老蔡三角区跳吼",
        )
        self.assertEqual(role, "room_discussion")
        self.assertIn("没有证明其参赛或观战", instruction)

    def test_danmaku_dominant_hero_does_not_prove_streamer_gameplay(self):
        self.assertFalse(bridge.streamer_gameplay_is_verified({
            "hero": "食人魔魔法师",
            "identity_source": "xml_dominant_hero_only",
        }))
        self.assertTrue(bridge.streamer_gameplay_is_verified({
            "hero": "食人魔魔法师",
            "identity_source": "douyu_gsi_streamer_anchor",
        }))

    def test_repeated_explicit_guest_hero_relation_is_kept(self):
        comments = [
            Mock(time=10.0, text="南枫这末日"),
            Mock(time=11.0, text="这末日"),
            Mock(time=12.0, text="南枫"),
        ]
        timeline = bridge.render_grounded_danmaku_timeline(
            [{
                "event": "南枫的末日使者成为场上焦点",
                "evidence_texts": [comment.text for comment in comments],
                "evidence_keywords": ["南枫", "末日"],
            }],
            comments,
            comments,
            delay_seconds=0,
        )
        self.assertIn("00:10 南枫的末日使者成为场上焦点", timeline)

    def test_conflicting_nearby_hero_does_not_bind_to_guest(self):
        comments = [
            Mock(time=10.0, text="谢彬才是DP"),
            Mock(time=11.0, text="蓝猫"),
            Mock(time=12.0, text="蓝猫"),
        ]
        diagnostics = {}
        timeline = bridge.render_grounded_danmaku_timeline(
            [{
                "event": "谢彬的蓝猫成为场上焦点",
                "evidence_texts": [comment.text for comment in comments],
                "evidence_keywords": ["谢彬", "蓝猫"],
            }],
            comments,
            comments,
            delay_seconds=0,
            anchor_diagnostics=diagnostics,
        )
        self.assertEqual(timeline, "")
        self.assertEqual(
            diagnostics["timeline_rejection_reasons"]["person_hero_relation_not_supported"],
            1,
        )

    def test_title_guest_hero_relation_must_exist_in_verified_description(self):
        self.assertTrue(bridge.title_person_hero_relations_supported(
            "YYF观战南枫的末日使者",
            "03:20 南枫的末日使者成为团战焦点",
        ))
        self.assertFalse(bridge.title_person_hero_relations_supported(
            "YYF观战谢彬的蓝猫",
            "03:20 谢彬的死亡先知成为团战焦点",
        ))

    def test_segmented_gsi_supports_streamer_hero_relations_across_games(self):
        segments = [
            {
                "hero": "风暴之灵",
                "identity_source": "gsi_explicit_hero_segment:http",
            },
            {
                "hero": "玛西",
                "identity_source": "gsi_explicit_hero_segment:http",
            },
        ]

        self.assertTrue(bridge.title_person_hero_relations_supported_with_gsi(
            "川神用风暴之灵守高；换玛西追击后空大",
            "16:28 风暴之灵守高\n48:08 玛西追击后空大",
            "叫我老陈就好了",
            segments,
        ))
        self.assertFalse(bridge.title_person_hero_relations_supported_with_gsi(
            "川神用风暴之灵守高；南枫用末日使者追击",
            "16:28 风暴之灵守高\n48:08 末日使者追击",
            "叫我老陈就好了",
            segments,
        ))

    def test_verified_gameplay_rejects_audience_label_prefix(self):
        timeline = ["02:13 川神的风暴之灵被锤中"]
        segments = [{
            "start_seconds": 0,
            "end_seconds": 1200,
            "hero": "风暴之灵",
            "identity_source": "gsi_explicit_hero_segment:http",
        }]

        self.assertTrue(
            bridge.recording_title_audience_prefix_obscures_selected_gsi_gameplay(
                "观众讨论川神使用风暴之灵被锤中",
                [0],
                timeline,
                segments,
            )
        )
        self.assertFalse(
            bridge.recording_title_audience_prefix_obscures_selected_gsi_gameplay(
                "川神的风暴之灵被锤中后残血脱身",
                [0],
                timeline,
                segments,
            )
        )

    def test_competitive_result_rejects_inverted_winner(self):
        comments = [
            types.SimpleNamespace(time=60.0, text="南枫输了"),
            types.SimpleNamespace(time=61.0, text="南枫输给对面"),
        ]
        rendered = bridge.render_grounded_danmaku_timeline(
            [{
                "event": "南枫赢下比赛",
                "evidence_texts": ["南枫输了", "南枫输给对面"],
                "evidence_keywords": ["南枫", "输了"],
            }],
            comments,
            comments,
        )
        self.assertEqual(rendered, "")

    def test_competitive_result_accepts_explicit_person_binding(self):
        comments = [
            types.SimpleNamespace(time=60.0, text="南枫赢了"),
            types.SimpleNamespace(time=61.0, text="南枫赢下这局"),
        ]
        rendered = bridge.render_grounded_danmaku_timeline(
            [{
                "event": "南枫赢下比赛",
                "evidence_texts": ["南枫赢了", "南枫赢下这局"],
                "evidence_keywords": ["南枫", "赢了"],
            }],
            comments,
            comments,
        )
        self.assertIn("南枫赢下比赛", rendered)

    def test_title_competitive_result_must_match_verified_person_and_direction(self):
        self.assertTrue(bridge.title_competitive_results_supported(
            "YYF观战南枫夺冠",
            "01:00 YYF观战南枫夺冠",
        ))
        self.assertFalse(bridge.title_competitive_results_supported(
            "YYF观战南枫夺冠",
            "01:00 YYF观战南枫被淘汰",
        ))

    def test_head_to_head_result_assigns_opposite_directions(self):
        self.assertEqual(
            {
                (names[0], polarity)
                for names, polarity in bridge._person_result_relations(
                    "眼子淘汰谢彬，随后眼子夺冠"
                )
            },
            {("Sylar", "win"), ("DD", "loss")},
        )
        self.assertEqual(
            {
                (names[0], polarity)
                for names, polarity in bridge._person_result_relations(
                    "谢彬输给眼子"
                )
            },
            {("DD", "loss"), ("Sylar", "win")},
        )

    def test_competitive_result_recognizes_numbered_championship_phrasing(self):
        self.assertEqual(
            bridge._competitive_result_polarities("川神夺五冠"),
            {"win"},
        )
        self.assertEqual(
            bridge._competitive_result_polarities("恭喜老蔡5冠王诞生"),
            {"win"},
        )
        self.assertTrue(bridge.title_competitive_results_supported(
            "川神斧王追回经济夺五冠",
            "42:56 比赛收尾后，川神拿到第五冠",
        ))

    def test_early_championship_prediction_is_not_treated_as_final_result(self):
        self.assertEqual(
            bridge._competitive_result_polarities(
                "优势扩大后，弹幕提前预祝川神成为五冠王"
            ),
            set(),
        )

    def test_default_recording_title_falls_back_to_live_title(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "主播_abcdef2026-07-23_09-45-06_深夜歌回.flv"
            video.write_bytes(b"video")
            title, _, _ = bridge.render_metadata(
                video,
                {"title_template": bridge.DEFAULT_TITLE_TEMPLATE},
            )
        self.assertEqual(title, "主播｜深夜歌回｜07-23 09:45")

    def test_current_recorder_filename_falls_back_to_embedded_live_title(self):
        with tempfile.TemporaryDirectory() as temp:
            video = (
                Path(temp)
                / "果小果是个弟弟_果小果：8月你好！_2026-08-01_17-25.flv"
            )
            video.write_bytes(b"video")
            title, description, _ = bridge.render_metadata(
                video,
                {
                    "title_template": bridge.DEFAULT_TITLE_TEMPLATE,
                    "streamer_name": "果小果是个弟弟",
                },
            )

        self.assertEqual(title, "果小果：8月你好！｜08-01 17:25")
        self.assertIn("《果小果：8月你好！》", description)

    def test_yyf_alias_at_start_of_topic_avoids_repeated_streamer_prefix(self):
        aliases = ("FG", "胖头", "胖头鱼")
        for alias in aliases:
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as temp:
                video = Path(temp) / "yyfyyf_陪伴每一天_2026-07-31_12-00.flv"
                video.write_bytes(b"video")
                title, _, _ = bridge.render_metadata(
                    video,
                    {
                        "title_template": bridge.DEFAULT_TITLE_TEMPLATE,
                        "streamer_name": "yyfyyf",
                    },
                    ai_topic=f"{alias}天梯翻盘",
                )
            self.assertEqual(title, f"{alias}天梯翻盘｜07-31 12:00")

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

        self.assertEqual(title, "凤凰翻盘｜07-24 13:00")
        self.assertEqual(
            bridge.recording_part_title(video, 1, "凤凰翻盘"),
            "13:00 凤凰翻盘",
        )

    def test_default_title_does_not_force_room_owner_onto_verified_third_party_event(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "yyfyyf_陪伴每一天_2026-08-06_15-25.flv"
            video.write_bytes(b"video")
            title, _, _ = bridge.render_metadata(
                video,
                {
                    "title_template": bridge.DEFAULT_TITLE_TEMPLATE,
                    "streamer_name": "yyfyyf",
                },
                ai_topic="老蔡三角区跳吼，马甲随后夺冠",
            )

        self.assertEqual(title, "老蔡三角区跳吼，马甲随后夺冠｜08-06 15:25")

    def test_default_description_hides_internal_room_marker_and_recording_time(self):
        video = Path(
            "yyfyyf_50e0b32026-07-23_20-24-08_陪伴每一天.flv"
        )
        _, description, _ = bridge.render_metadata(video, {})

        self.assertEqual(description, "直播录播：YYF《陪伴每一天》。")
        self.assertNotIn("50e0b3", description)
        self.assertNotIn("2026-07-23", description)

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

    def test_stdin_paths_decode_rust_hook_input_as_utf8_on_windows(self):
        expected = Path(r"C:\录播\陪伴每一天.flv")

        class HookInput:
            def __init__(self, value: str):
                self.buffer = io.BytesIO((value + "\n").encode("utf-8"))

            @staticmethod
            def isatty():
                return False

        with patch.object(sys, "stdin", HookInput(str(expected))):
            received = bridge.stdin_paths()

        self.assertEqual(received, [expected])

    def test_videos_shorter_than_five_minutes_never_create_a_task(self):
        for command in ("ingest", "record-only"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                video = root / "short.flv"
                state = root / "state.sqlite3"
                config = root / "bridge.config.json"
                video.write_bytes(b"video")
                config.write_text(
                    json.dumps({
                        "state_db": str(state),
                        "MIN_RECORDING_UPLOAD_DURATION_SECONDS": 60,
                    }),
                    encoding="utf-8",
                )
                arguments = ["--config", str(config), command]
                if command == "record-only":
                    arguments.extend(["--room-id", "room-1"])
                arguments.append(str(video))

                with patch.object(
                    bridge,
                    "video_duration_seconds",
                    return_value=299.9,
                ), patch.object(bridge, "upload_one") as upload:
                    result = bridge.main(arguments)

                self.assertEqual(result, 0)
                upload.assert_not_called()
                with closing(sqlite3.connect(state)) as db, db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0],
                        0,
                    )

    def test_recorder_wall_clock_rejects_restart_fragment_with_false_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "主播_标题_2026-08-01_17-25.flv"
            state = root / "state.sqlite3"
            config = root / "bridge.config.json"
            video.write_bytes(b"video")
            finished_at = datetime(2026, 8, 1, 17, 25, 23).timestamp()
            os.utime(video, (finished_at, finished_at))
            config.write_text(
                json.dumps({"state_db": str(state)}),
                encoding="utf-8",
            )

            with patch.object(
                bridge,
                "video_duration_seconds",
                return_value=3600,
            ), patch.object(bridge, "upload_one") as upload:
                result = bridge.main([
                    "--config", str(config), "ingest", str(video),
                ])

            self.assertEqual(result, 0)
            upload.assert_not_called()
            with closing(sqlite3.connect(state)) as db, db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0],
                    0,
                )

    def test_effective_recording_duration_caps_false_segment_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "主播_标题_2026-08-10_09-45.flv"
            video.write_bytes(b"video")
            finished_at = datetime(2026, 8, 10, 9, 53, 45).timestamp()
            os.utime(video, (finished_at, finished_at))

            with patch.object(
                bridge,
                "video_duration_seconds",
                return_value=3600.0,
            ):
                duration = bridge.recording_effective_duration_seconds(video)

            self.assertEqual(duration, 525.0)

    def test_burn_reuse_uses_effective_recording_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "主播_标题_2026-08-10_09-45.flv"
            burned = root / "主播_标题_2026-08-10_09-45.danmaku.mp4"
            source.write_bytes(b"source")
            burned.write_bytes(b"burned")
            finished_at = datetime(2026, 8, 10, 9, 53, 45).timestamp()
            os.utime(source, (finished_at, finished_at))
            probe = types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{"codec_type": "video"}],
                    "format": {"duration": "525.1"},
                }),
            )

            with patch.object(
                bridge.subprocess,
                "run",
                return_value=probe,
            ), patch.object(
                bridge,
                "video_duration_seconds",
                return_value=3600.0,
            ):
                valid, details = bridge.reusable_burned_video(burned, source)

            self.assertTrue(valid)
            self.assertTrue(details["burned_video_reuse_validated"])
            self.assertEqual(details["source_video_duration_seconds"], 525.0)

    def test_danmaku_xml_falls_back_to_same_session_stop_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "主播_标题_2026-07-26_10-13.flv"
            xml = root / "主播_标题_2026-07-26_10-41.xml"
            video.write_bytes(b"video")
            xml.write_text("<i/>", encoding="utf-8")
            stamp = 1_700_000_000
            os.utime(video, (stamp, stamp))
            os.utime(xml, (stamp + 1, stamp + 1))

            self.assertEqual(bridge.find_danmaku_xml(video), xml.resolve())

    def test_wait_for_danmaku_xml_returns_stable_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.flv"
            xml = root / "clip.xml"
            video.write_bytes(b"video")
            xml.write_text("<i/>", encoding="utf-8")

            with patch.object(bridge, "wait_until_stable") as wait:
                result = bridge.wait_for_danmaku_xml(
                    video,
                    [xml],
                    timeout=0,
                    interval=0.01,
                )

            self.assertEqual(result, xml.resolve())
            wait.assert_called_once_with(xml.resolve(), checks=2, interval=0.01)

    def test_state_deduplicates_completed_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            store = bridge.StateStore(Path(temp) / "state.sqlite3")
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"video")
            key = bridge.fingerprint(video)
            self.assertTrue(store.claim(key, video, "bilibili"))
            store.finish(key, "completed", {"ok": True})
            self.assertFalse(store.claim(key, video, "bilibili"))

    def test_record_only_command_permanently_excludes_video_from_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "record-only.flv"
            xml = root / "record-only.xml"
            state = root / "state.sqlite3"
            config = root / "bridge.config.json"
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.2,1,25,16777215,0,0,0,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            config.write_text(
                json.dumps({"state_db": str(state)}),
                encoding="utf-8",
            )

            cover = video.with_suffix(".jpg")
            cover.write_bytes(b"cover")

            def finish_local_pipeline(_video, _cover, _cfg, progress_callback=None, **_kwargs):
                output = video.with_suffix(".mp4")
                for event, details in (
                    ("remux_completed", {"output_path": str(output), "copy_mode": "-c copy"}),
                    ("verify_running", {"output_path": str(output)}),
                    ("verify_completed", {"output_path": str(output), "attached_pic": 1}),
                    ("cleanup_running", {"original_flv": str(video)}),
                    ("cleanup_completed", {
                        "final_video_path": str(output),
                        "original_flv_deleted": True,
                    }),
                ):
                    progress_callback(event, details)
                return output

            with patch.object(
                bridge,
                "probe_video_size",
                return_value=(1280, 720),
            ), patch.object(
                bridge,
                "generate_record_only_cover",
                return_value=cover,
            ) as generate_cover, patch.object(
                bridge,
                "remux_record_only_flv_with_cover",
                side_effect=finish_local_pipeline,
            ) as remux:
                result = bridge.main([
                    "--config", str(config),
                    "record-only", "--room-id", "room-1", str(video), str(xml),
                ])

            self.assertEqual(result, 0)
            self.assertEqual(generate_cover.call_count, 1)
            self.assertEqual(generate_cover.call_args.args[0], video.resolve())
            self.assertEqual(remux.call_count, 1)
            self.assertEqual(remux.call_args.args[0], video.resolve())
            self.assertEqual(remux.call_args.args[1], cover)
            self.assertEqual(
                remux.call_args.kwargs["output_path"],
                video.resolve().with_suffix(".mp4"),
            )
            self.assertEqual(remux.call_args.kwargs["original_flv"], video.resolve())
            with closing(sqlite3.connect(state)) as db, db:
                rows = db.execute(
                    "SELECT video_path, room_id, reason FROM recording_exclusions"
                ).fetchall()
                task = db.execute(
                    "SELECT platform, status, result_json FROM uploads"
                ).fetchone()
                stages = db.execute(
                    "SELECT stage, status FROM upload_stages ORDER BY rowid"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    (str(video.resolve()), "room-1", "record_only"),
                    (str(video.with_suffix(".mp4").resolve()), "room-1", "record_only"),
                ],
            )
            self.assertEqual(task[0:2], ("record_only", "completed"))
            self.assertEqual(
                json.loads(task[2])["final_video_path"],
                str(video.with_suffix(".mp4")),
            )
            self.assertEqual(
                stages,
                [
                    ("record", "completed"),
                    ("ass", "completed"),
                    ("burn", "skipped"),
                    ("cover", "completed"),
                    ("remux", "completed"),
                    ("verify", "completed"),
                    ("cleanup", "completed"),
                ],
            )
            ass = video.parent / "ass" / f"{video.stem}.zh-CN.ass"
            self.assertTrue(ass.is_file())
            self.assertIn("测试弹幕", ass.read_text(encoding="utf-8-sig"))

    def test_record_only_burns_ass_before_attaching_cover_when_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "record-only.flv"
            xml = root / "record-only.xml"
            state = root / "state.sqlite3"
            config = root / "bridge.config.json"
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.2,1,25,16777215,0,0,0,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            config.write_text(json.dumps({
                "state_db": str(state),
                "danmaku_burn_in": True,
            }), encoding="utf-8")
            cover = video.with_suffix(".jpg")
            cover.write_bytes(b"cover")
            burned = video.with_suffix(".mp4")
            final_video = video.with_suffix(".mp4")

            with patch.object(bridge, "probe_video_size", return_value=(1280, 720)), \
                    patch.object(bridge, "generate_record_only_cover", return_value=cover), \
                    patch.object(bridge, "burn_ass", return_value=burned) as burn, \
                    patch.object(
                        bridge,
                        "remux_record_only_flv_with_cover",
                        return_value=final_video,
                    ) as remux:
                result = bridge.main([
                    "--config", str(config),
                    "record-only", "--room-id", "room-1", str(video), str(xml),
                ])

            self.assertEqual(result, 0)
            self.assertEqual(burn.call_args.args[:3], (video.resolve(), ANY, burned.resolve()))
            self.assertEqual(remux.call_count, 1)
            self.assertEqual(remux.call_args.args[0], burned)
            self.assertEqual(remux.call_args.args[1], cover)
            self.assertEqual(remux.call_args.kwargs["output_path"], final_video.resolve())
            self.assertEqual(remux.call_args.kwargs["original_flv"], video.resolve())
            with closing(sqlite3.connect(state)) as db, db:
                burn_stage = db.execute(
                    "SELECT status, details_json FROM upload_stages WHERE stage='burn'"
                ).fetchone()
                task = db.execute("SELECT result_json FROM uploads").fetchone()
            self.assertEqual(burn_stage[0], "completed")
            self.assertTrue(json.loads(burn_stage[1])["burn_in"])
            self.assertTrue(json.loads(task[0])["danmaku_burn_in"])

    def test_record_only_cover_matches_video_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "record-only.flv"
            video.write_bytes(b"video")
            expected_cover = video.with_suffix(".jpg")
            expected_cover.write_bytes(b"ai-native-resolution-cover")

            with patch.object(
                bridge,
                "probe_video_size",
                return_value=(1920, 1080),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                return_value=(
                    expected_cover,
                    {"ai_cover_generated": True},
                ),
            ) as generate_ai:
                cover = bridge.generate_record_only_cover(video, {})

            self.assertEqual(cover, expected_cover)
            self.assertEqual(cover.read_bytes(), b"ai-native-resolution-cover")
            self.assertEqual(generate_ai.call_args.kwargs["target_size"], (1920, 1080))
            self.assertEqual(
                generate_ai.call_args.kwargs["output_path"],
                expected_cover,
            )

    def test_record_only_missing_cover_is_skipped_and_pipeline_continues(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "failed-cover.flv"
            xml = root / "failed-cover.xml"
            state = root / "state.sqlite3"
            config = root / "bridge.config.json"
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.2,1,25,16777215,0,0,0,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            config.write_text(json.dumps({"state_db": str(state)}), encoding="utf-8")

            with patch.object(
                bridge,
                "probe_video_size",
                return_value=(1280, 720),
            ), patch.object(
                bridge,
                "generate_record_only_cover",
                side_effect=RuntimeError("图片模型不可用"),
            ), patch.object(
                bridge,
                "remux_record_only_flv_with_cover",
            ) as remux:
                result = bridge.main([
                    "--config", str(config),
                    "record-only", "--room-id", "room-1", str(video), str(xml),
                ])

            self.assertEqual(result, 0)
            remux.assert_not_called()
            with closing(sqlite3.connect(state)) as db, db:
                task = db.execute("SELECT platform, status, result_json FROM uploads").fetchone()
                cover_stage = db.execute(
                    "SELECT status, details_json FROM upload_stages WHERE stage='cover'"
                ).fetchone()
            self.assertEqual(task[0:2], ("record_only", "completed"))
            self.assertIsNone(json.loads(task[2])["cover_path"])
            self.assertEqual(cover_stage[0], "skipped")
            self.assertIn("图片模型不可用", json.loads(cover_stage[1])["reason"])
            with closing(sqlite3.connect(state)) as db, db:
                remux_stage = db.execute(
                    "SELECT status FROM upload_stages WHERE stage='remux'"
                ).fetchone()
            self.assertEqual(remux_stage[0], "skipped")

    def test_record_only_missing_cover_keeps_completed_burn_without_remux(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "failed-cover.flv"
            xml = root / "failed-cover.xml"
            state = root / "state.sqlite3"
            config = root / "bridge.config.json"
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.2,1,25,16777215,0,0,0,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            config.write_text(json.dumps({
                "state_db": str(state),
                "danmaku_burn_in": True,
            }), encoding="utf-8")
            burned = video.with_suffix(".mp4")
            def finish_burn(*_args, **_kwargs):
                burned.write_bytes(b"already-burned-video")
                return burned

            with patch.object(bridge, "probe_video_size", return_value=(1280, 720)), \
                    patch.object(bridge, "burn_ass", side_effect=finish_burn) as burn, \
                    patch.object(
                        bridge,
                        "generate_record_only_cover",
                        side_effect=RuntimeError("图片模型不可用"),
                    ) as generate_cover, patch.object(
                        bridge,
                        "remux_record_only_flv_with_cover",
                    ) as remux:
                first = bridge.main([
                    "--config", str(config),
                    "record-only", "--room-id", "room-1", str(video), str(xml),
                ])
                self.assertEqual(first, 0)
                self.assertTrue(burned.is_file())
                with closing(sqlite3.connect(state)) as db, db:
                    exclusions = db.execute(
                        "SELECT video_path FROM recording_exclusions ORDER BY video_path"
                    ).fetchall()
                self.assertIn((str(burned.resolve()),), exclusions)

            self.assertEqual(burn.call_count, 1)
            self.assertEqual(generate_cover.call_count, 1)
            remux.assert_not_called()
            with closing(sqlite3.connect(state)) as db, db:
                task = db.execute(
                    "SELECT status, attempts FROM uploads"
                ).fetchone()
                burn_details = json.loads(db.execute(
                    "SELECT details_json FROM upload_stages WHERE stage='burn'"
                ).fetchone()[0])
            self.assertEqual(task, ("completed", 1))
            self.assertTrue(burn_details["burn_in"])

    def test_record_only_flv_is_remuxed_to_mp4_with_attached_cover(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "record-only.flv"
            cover = root / "record-only.jpg"
            video.write_bytes(b"flv")
            cover.write_bytes(b"jpg")

            def fake_run(command, **_kwargs):
                if command[0] == "ffmpeg":
                    Path(command[-1]).write_bytes(b"mp4-with-cover")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"streams": [
                        {"disposition": {"attached_pic": 0}},
                        {"disposition": {"attached_pic": 1}},
                    ]}),
                    "",
                )

            with patch.object(bridge.subprocess, "run", side_effect=fake_run) as run:
                output = bridge.remux_record_only_flv_with_cover(video, cover, {})

            self.assertEqual(output, root / "record-only.mp4")
            self.assertEqual(output.read_bytes(), b"mp4-with-cover")
            self.assertFalse(video.exists())
            ffmpeg_command = run.call_args_list[0].args[0]
            self.assertIn("copy", ffmpeg_command)
            self.assertIn("attached_pic", ffmpeg_command)

    def test_failed_record_only_remux_keeps_original_flv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "record-only.flv"
            cover = root / "record-only.jpg"
            video.write_bytes(b"flv")
            cover.write_bytes(b"jpg")
            failed = subprocess.CompletedProcess(
                ["ffmpeg"],
                1,
                "",
                "unsupported codec",
            )

            with patch.object(bridge.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "录播封装 MP4 失败"):
                    bridge.remux_record_only_flv_with_cover(video, cover, {})

            self.assertTrue(video.is_file())
            self.assertFalse(video.with_suffix(".mp4").exists())

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
            self.assertEqual(
                set(stages),
                {
                    "detect", "record", "ass", "burn", "ai", "xml_identity", "live_stats",
                    "cover_16x9", "cover_4x3", "upload", "collection", "comment", "cleanup",
                },
            )
            self.assertEqual(stages["record"]["status"], "completed")
            self.assertEqual(stages["ass"]["status"], "completed")
            self.assertEqual(json.loads(stages["ass"]["details_json"])["danmaku_count"], 12)

    def test_recording_upload_exposes_independent_stats_and_cover_stages(self):
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
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "douyu_stats_enabled": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")

            with patch.object(bridge, "find_cover", return_value=cover):
                self.assertTrue(bridge.upload_one(video, cfg, store, dry_run=True))

            key = bridge.fingerprint(video)
            self.assertEqual(store.stage_state(key, "xml_identity")["status"], "skipped")
            self.assertEqual(store.stage_state(key, "live_stats")["status"], "skipped")
            self.assertEqual(store.stage_state(key, "cover_16x9")["status"], "skipped")
            self.assertEqual(store.stage_state(key, "cover_4x3")["status"], "skipped")

    def test_armed_ai_review_stops_before_cover_generation_and_upload(self):
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
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "douyu_stats_enabled": False,
                "delete_recording_after_upload": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.save_review_override(key, {
                "hold_before_cover": True,
                "pre_upload_review_requested_at": "2026-08-02T12:00:00+00:00",
                "updated_at": "2026-08-02T12:00:00+00:00",
            })

            with patch.object(
                bridge,
                "find_cover",
                return_value=cover,
            ), patch.object(
                bridge,
                "enhance_recording_metadata",
                return_value=(["录播"], "171", {}),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
            ) as generate_cover, patch.object(
                bridge,
                "import_app",
            ) as import_app:
                self.assertTrue(bridge.upload_one(video, cfg, store))

            with store.connect() as db:
                status = db.execute(
                    "SELECT status FROM uploads WHERE fingerprint=?",
                    (key,),
                ).fetchone()["status"]
            self.assertEqual(status, "paused")
            self.assertEqual(store.stage_state(key, "ai")["status"], "skipped")
            self.assertEqual(store.stage_state(key, "cover_16x9")["status"], "pending")
            self.assertTrue(store.results(key)["pre_upload_review"])
            generate_cover.assert_not_called()
            import_app.assert_not_called()

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
            store.finish(key, "failed", {
                "bilibili": {"bvid": "BV1existing"},
                "description_comment": {
                    "enabled": True,
                    "posted": False,
                    "error": "temporary comment failure",
                },
            }, "dm failed")

            comment_calls = []

            class MustNotUpload:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **_kwargs):
                    raise AssertionError("retry must not upload video again")

                def publish_description_comment(self, **kwargs):
                    comment_calls.append(kwargs)
                    return {"enabled": True, "posted": True, "pinned": True}

            cleanup_observations = []
            real_cleanup = bridge.cleanup_uploaded_recording

            def observed_cleanup(*args, **kwargs):
                with store.connect() as db:
                    status = db.execute(
                        "SELECT status FROM uploads WHERE fingerprint=?",
                        (key,),
                    ).fetchone()["status"]
                cleanup_observations.append((status, video.is_file()))
                return real_cleanup(*args, **kwargs)

            with patch.object(
                bridge,
                "enhance_recording_metadata",
                return_value=([], "171", {}),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                return_value=(None, {"ai_cover_enabled": False}),
            ), patch.object(bridge, "import_app", return_value=(MustNotUpload, None)):
                with patch.object(
                    bridge,
                    "cleanup_uploaded_recording",
                    side_effect=observed_cleanup,
                ):
                    self.assertTrue(bridge.upload_one(video, cfg, store, retry=True))
            result = store.results(key)
            with store.connect() as db:
                status = db.execute(
                    "SELECT status FROM uploads WHERE fingerprint=?",
                    (key,),
                ).fetchone()["status"]
            self.assertEqual(cleanup_observations, [("video_uploaded", True)])
            self.assertEqual(status, "completed")
            self.assertEqual(store.stage_state(key, "cleanup")["status"], "completed")
            self.assertEqual(result["bilibili"]["bvid"], "BV1existing")
            self.assertEqual(len(comment_calls), 1)
            self.assertEqual(store.stage_state(key, "comment")["status"], "completed")
            self.assertFalse(video.exists())
            self.assertEqual(result["source_cleanup"]["deleted"], [str(video.resolve())])

    def test_retry_reuses_completed_ai_metadata_and_cover(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            cookie = root / "cookie.json"
            persisted_cover = root / "artifacts" / "task-covers" / "saved.jpg"
            persisted_cover.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            cookie.write_text("[]", encoding="utf-8")
            persisted_cover.write_bytes(b"saved-cover")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "bilibili_cookies": str(cookie),
                "cover_path": str(persisted_cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "delete_recording_after_upload": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.stage(key, "ai", "completed", {
                "title": "已生成标题",
                "description": "——— 直播数据 ———\n\n已生成正文",
                "description_body": "已生成正文",
                "title_topic": "已生成主题",
                "final_tags": ["直播", "DOTA2"],
                "selected_partition_id": "129",
            })
            store.stage(key, "cover_16x9", "completed", {
                "ai_cover_generated": True,
                "ai_cover_path": str(persisted_cover),
                "cover_used_for_upload": str(persisted_cover),
            })
            store.finish(
                key,
                "failed",
                {
                    "cover_path": str(persisted_cover),
                    "metadata_automation": {"selected_partition_id": "129"},
                },
                "upload failed",
            )
            uploads = []

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **kwargs):
                    uploads.append(kwargs)
                    return True, {
                        "bvid": "BV1retry",
                        "url": "https://www.bilibili.com/video/BV1retry",
                    }

            with patch.object(
                bridge,
                "generate_danmaku_metadata_with_ai",
                side_effect=AssertionError("retry must reuse AI summary"),
            ), patch.object(
                bridge,
                "enhance_recording_metadata",
                side_effect=AssertionError("retry must reuse AI metadata"),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                side_effect=AssertionError("retry must reuse AI cover"),
            ), patch.object(
                bridge,
                "import_app",
                return_value=(FakeUploader, None),
            ):
                self.assertTrue(bridge.upload_one(video, cfg, store, retry=True))

            self.assertEqual(uploads[0]["title"], "已生成标题")
            self.assertEqual(uploads[0]["description"], "已生成正文")
            self.assertEqual(uploads[0]["tags"], ["直播", "DOTA2"])
            self.assertEqual(uploads[0]["partition_id"], "129")
            self.assertEqual(uploads[0]["task_id"], key[:12])
            self.assertEqual(Path(uploads[0]["cover_file_path"]).read_bytes(), b"saved-cover")
            self.assertTrue(store.stage_state(key, "ai")["details"]["reused_on_retry"])
            self.assertTrue(store.stage_state(key, "cover_16x9")["details"]["reused_on_retry"])
            self.assertEqual(store.stage_state(key, "cover_4x3")["status"], "warning")

    def test_failed_upload_retry_reuses_burn_and_uploads_recording_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.flv"
            xml = root / "clip.xml"
            cookie = root / "cookie.json"
            cover = root / "cover.jpg"
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            cookie.write_text("[]", encoding="utf-8")
            cover.write_bytes(b"cover")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "bilibili_cookies": str(cookie),
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": True,
                "danmaku_burn_in": True,
                "delete_recording_after_upload": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video, xml)
            legacy_burn = root / "artifacts" / key[:16] / "clip.danmaku.mp4"
            legacy_burn.parent.mkdir(parents=True)
            legacy_burn.write_bytes(b"completed burn")
            store.claim(key, video, "bilibili")
            store.stage(key, "burn", "completed", {
                "burned_video_path": str(legacy_burn),
                "burn_in": True,
            })
            store.stage(key, "ai", "completed", {
                "title": "主播完成实际挑战｜08-07 02:05",
                "description": "已生成正文",
                "description_body": "已生成正文",
                "title_topic": "主播完成实际挑战",
                "final_tags": ["直播"],
                "selected_partition_id": "171",
            })
            store.stage(key, "cover_16x9", "completed", {
                "ai_cover_generated": True,
                "ai_cover_path": str(cover),
                "cover_used_for_upload": str(cover),
            })
            store.finish(key, "failed", {"cover_path": str(cover)}, "upload failed")
            uploads = []

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **kwargs):
                    uploads.append(kwargs)
                    return True, {"bvid": "BV1burn", "url": "https://example.com/BV1burn"}

            def validate(candidate, *_args):
                exists = candidate.is_file()
                return exists, {"burned_video_reuse_validated": exists}

            with patch.object(
                bridge,
                "reusable_burned_video",
                side_effect=validate,
            ), patch.object(
                bridge,
                "burn_ass",
                side_effect=AssertionError("valid completed burn must be reused"),
            ), patch.object(
                bridge,
                "generate_danmaku_metadata_with_ai",
                side_effect=AssertionError("retry must reuse AI summary"),
            ), patch.object(
                bridge,
                "enhance_recording_metadata",
                side_effect=AssertionError("retry must reuse AI metadata"),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                side_effect=AssertionError("retry must reuse AI cover"),
            ), patch.object(
                bridge,
                "probe_video_size",
                return_value=(1920, 1080),
            ), patch.object(
                bridge,
                "import_app",
                return_value=(FakeUploader, None),
            ):
                self.assertTrue(
                    bridge.upload_one(video, cfg, store, retry=True, danmaku_xml=xml)
                )

            expected = root / "clip.danmaku.mp4"
            self.assertEqual(Path(uploads[0]["video_file_path"]), expected.resolve())
            self.assertEqual(
                uploads[0]["title"],
                "主播完成实际挑战｜弹幕版 08-07 02:05",
            )
            self.assertTrue(expected.is_file())
            self.assertFalse(legacy_burn.exists())
            burn_details = store.stage_state(key, "burn")["details"]
            self.assertTrue(burn_details["reused_on_retry"])
            self.assertEqual(burn_details["burned_video_location"], "recording_directory")
            with store.connect() as db:
                exclusion = db.execute(
                    "SELECT reason FROM recording_exclusions WHERE video_path=?",
                    (str(expected.resolve()),),
                ).fetchone()
            self.assertEqual(exclusion["reason"], "generated_burn")

    def test_retry_repairs_unsubmitted_first_part_in_original_session(self):
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

            self.assertEqual(store.results(key)["multipart_session"], "room-1")
            self.assertEqual(
                store.multipart_session("room-1")["pending_first_video"],
                str(video),
            )

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
            ass = root / "artifacts" / "clip.ass"
            ai_cover = root / "artifacts" / "ai_cover.jpg"
            upload_video.parent.mkdir()
            for path in (video, xml, upload_video, ass, ai_cover):
                path.write_bytes(b"data")

            result = bridge.cleanup_uploaded_recording(
                video,
                xml,
                upload_video,
                artifact_dir=upload_video.parent,
            )

            self.assertEqual(result["failed"], [])
            self.assertTrue(all(
                not path.exists()
                for path in (video, xml, upload_video, ass, ai_cover)
            ))
            self.assertFalse(upload_video.parent.exists())
            self.assertEqual(result["retained"], [])

    def test_cleanup_reports_only_removed_paths_and_retained_final_covers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing_video = root / "missing.flv"
            cover = root / "final-cover.jpg"
            cover.write_bytes(b"cover")

            result = bridge.cleanup_uploaded_recording(
                missing_video,
                None,
                missing_video,
                retained_paths=(cover,),
            )

            self.assertEqual(result["deleted"], [])
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["retained"], [str(cover.resolve())])

    def test_cleanup_retains_xml_for_requested_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.flv"
            xml = root / "clip.xml"
            video.write_bytes(b"video")
            xml.write_text("<i></i>", encoding="utf-8")

            result = bridge.cleanup_uploaded_recording(
                video,
                xml,
                video,
                retained_paths=(xml,),
                xml_retention_hours=24,
            )

            self.assertFalse(video.exists())
            self.assertTrue(xml.exists())
            self.assertEqual(result["retained_xml_path"], str(xml.resolve()))
            expires = datetime.fromisoformat(result["retained_xml_until"])
            self.assertGreater(expires, datetime.now(timezone.utc) + timedelta(hours=23))

    def test_state_cleanup_deletes_only_expired_retained_xml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = bridge.StateStore(root / "state.sqlite3")
            video = root / "clip.flv"
            xml = root / "clip.xml"
            video.write_bytes(b"video")
            xml.write_text("<i></i>", encoding="utf-8")
            key = bridge.fingerprint(video)
            self.assertTrue(store.claim(key, video, "bilibili"))
            store.stage(key, "cleanup", "completed", {
                "retained": [str(xml)],
                "retained_xml_path": str(xml),
                "retained_xml_until": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            })

            self.assertEqual(store.cleanup_expired_retained_xml(), [str(xml)])
            self.assertFalse(xml.exists())
            details = store.stage_state(key, "cleanup")["details"]
            self.assertIn("retained_xml_deleted_at", details)
            self.assertEqual(details["retained"], [])

    def test_collection_failure_retries_without_reuploading_video(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "主播_完成三次反杀_2026-08-06_20-00.flv"
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
                "bilibili_collection_id": "8761711",
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "ai_danmaku_summary_enabled": False,
                "post_description_comment": False,
                "delete_recording_after_upload": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.stage(
                key,
                "ai",
                "failed",
                {"manual_review_required": True},
                error="AI unavailable",
            )
            store.finish(key, "failed", error="ready for review")
            store.save_review_override(key, {
                "title": "主播高地三杀带队逆转",
                "description": "00:10 主播高地三杀带队逆转",
                "tags": ["录播"],
                "partition_id": "171",
                "cover_path": str(cover),
                "hold_before_cover": False,
            })
            upload_calls = []
            collection_calls = []

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **kwargs):
                    upload_calls.append(kwargs)
                    return True, {"aid": 123, "bvid": "BV1collection"}

                def add_to_collection(self, result, collection_id, title=""):
                    collection_calls.append((dict(result), collection_id, title))
                    if len(collection_calls) == 1:
                        return {
                            "enabled": True,
                            "added": False,
                            "season_id": 8761711,
                            "aid": 123,
                            "error": "temporary failure",
                        }
                    return {
                        "enabled": True,
                        "added": True,
                        "season_id": 8761711,
                        "section_id": 99,
                        "aid": 123,
                    }

            patches = (
                patch.object(bridge, "video_duration_seconds", return_value=600.0),
                patch.object(
                    bridge,
                    "generate_recording_cover_with_ai",
                    return_value=(None, {"ai_cover_generated": False}),
                ),
                patch.object(bridge, "import_app", return_value=(FakeUploader, None)),
            )
            with patches[0], patches[1], patches[2]:
                self.assertFalse(bridge.upload_one(video, cfg, store, retry=True))
                self.assertEqual(store.stage_state(key, "upload")["status"], "completed")
                self.assertEqual(store.stage_state(key, "collection")["status"], "failed")
                self.assertEqual(store.results(key)["bilibili"]["bvid"], "BV1collection")
                self.assertTrue(
                    store.stage_state(key, "ai")["details"][
                        "manual_review_bypassed_failed_ai"
                    ]
                )

                self.assertTrue(bridge.upload_one(video, cfg, store, retry=True))

            self.assertEqual(len(upload_calls), 1)
            self.assertEqual(len(collection_calls), 2)
            self.assertEqual(store.stage_state(key, "collection")["status"], "completed")
            self.assertTrue(store.results(key)["bilibili"]["collection"]["added"])

    def test_description_comment_failure_retries_without_reuploading_video(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "主播_录播_2026-08-09_12-00.flv"
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
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "ai_danmaku_summary_enabled": False,
                "post_description_comment": True,
                "delete_recording_after_upload": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.stage(key, "ai", "failed", {}, error="AI unavailable")
            store.finish(key, "failed", error="ready for review")
            store.save_review_override(key, {
                "title": "人工标题",
                "description": "人工简介",
                "tags": ["录播"],
                "partition_id": "171",
                "cover_path": str(cover),
                "hold_before_cover": False,
            })
            upload_calls = []
            comment_calls = []

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **kwargs):
                    upload_calls.append(kwargs)
                    return True, {"aid": 456, "bvid": "BV1comment"}

                def publish_description_comment(self, **kwargs):
                    comment_calls.append(kwargs)
                    if len(comment_calls) == 1:
                        return {
                            "enabled": True,
                            "posted": False,
                            "pinned": False,
                            "error": "temporary comment failure",
                        }
                    return {"enabled": True, "posted": True, "pinned": True}

            with patch.object(
                bridge, "video_duration_seconds", return_value=600.0
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                return_value=(None, {"ai_cover_generated": False}),
            ), patch.object(bridge, "import_app", return_value=(FakeUploader, None)):
                self.assertFalse(bridge.upload_one(video, cfg, store, retry=True))
                self.assertEqual(store.stage_state(key, "comment")["status"], "failed")
                self.assertEqual(store.results(key)["bilibili"]["bvid"], "BV1comment")

                self.assertTrue(bridge.upload_one(video, cfg, store, retry=True))

            self.assertEqual(len(upload_calls), 1)
            self.assertEqual(len(comment_calls), 2)
            self.assertEqual(store.stage_state(key, "comment")["status"], "completed")
            self.assertEqual(store.results(key)["bilibili"]["bvid"], "BV1comment")

    def test_description_comment_pin_failure_retries_without_duplicate_comment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "主播_录播_2026-08-09_12-30.flv"
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
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "ai_danmaku_summary_enabled": False,
                "post_description_comment": True,
                "pin_description_comment": True,
                "delete_recording_after_upload": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.stage(key, "ai", "failed", {}, error="AI unavailable")
            store.finish(key, "failed", error="ready for review")
            store.save_review_override(key, {
                "title": "人工标题",
                "description": "人工简介",
                "tags": ["录播"],
                "partition_id": "171",
                "cover_path": str(cover),
                "hold_before_cover": False,
            })
            upload_calls = []
            publish_calls = []
            pin_calls = []

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **kwargs):
                    upload_calls.append(kwargs)
                    return True, {"aid": 789, "bvid": "BV1pinretry"}

                def publish_description_comment(self, **kwargs):
                    publish_calls.append(kwargs)
                    return {
                        "enabled": True,
                        "posted": True,
                        "pinned": False,
                        "aid": 789,
                        "bvid": "BV1pinretry",
                        "rpid": "9001",
                        "pin_error": "temporary pin failure",
                    }

                def retry_description_comment_pin(self, **kwargs):
                    pin_calls.append(kwargs)
                    return {**kwargs["comment"], "pinned": True, "pin_error": ""}

            with patch.object(
                bridge, "video_duration_seconds", return_value=600.0
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                return_value=(None, {"ai_cover_generated": False}),
            ), patch.object(bridge, "import_app", return_value=(FakeUploader, None)):
                self.assertFalse(bridge.upload_one(video, cfg, store, retry=True))
                self.assertEqual(store.stage_state(key, "comment")["status"], "failed")
                self.assertTrue(bridge.upload_one(video, cfg, store, retry=True))

            self.assertEqual(len(upload_calls), 1)
            self.assertEqual(len(publish_calls), 1)
            self.assertEqual(len(pin_calls), 1)
            self.assertEqual(pin_calls[0]["comment"]["rpid"], "9001")
            self.assertEqual(store.stage_state(key, "comment")["status"], "completed")

    def test_cleanup_failure_keeps_published_task_failed_and_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "主播_录播_2026-08-09_13-00.flv"
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
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "ai_danmaku_summary_enabled": False,
                "post_description_comment": False,
                "delete_recording_after_upload": True,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.stage(key, "ai", "failed", {}, error="AI unavailable")
            store.finish(key, "failed", error="ready for review")
            store.save_review_override(key, {
                "title": "人工标题",
                "description": "人工简介",
                "tags": ["录播"],
                "partition_id": "171",
                "cover_path": str(cover),
                "hold_before_cover": False,
            })

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **_kwargs):
                    return True, {"aid": 789, "bvid": "BV1cleanup"}

            cleanup_result = {
                "deleted": [],
                "retained": [],
                "failed": [{
                    "kind": "video",
                    "path": str(video),
                    "error": "permission denied",
                }],
            }
            with patch.object(
                bridge, "video_duration_seconds", return_value=600.0
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                return_value=(None, {"ai_cover_generated": False}),
            ), patch.object(
                bridge, "cleanup_uploaded_recording", return_value=cleanup_result
            ), patch.object(bridge, "import_app", return_value=(FakeUploader, None)):
                self.assertFalse(bridge.upload_one(video, cfg, store, retry=True))

            self.assertEqual(store.stage_state(key, "cleanup")["status"], "failed")
            with store.connect() as db:
                status = db.execute(
                    "SELECT status FROM uploads WHERE fingerprint=?", (key,)
                ).fetchone()["status"]
            self.assertEqual(status, "failed")
            self.assertEqual(store.results(key)["bilibili"]["bvid"], "BV1cleanup")

    def test_persist_pipeline_cover_survives_disposable_artifact_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = bridge.StateStore(root / "state.sqlite3")
            artifact_dir = root / "artifacts" / "temporary-run"
            artifact_dir.mkdir(parents=True)
            cover = artifact_dir / "ai-cover.jpg"
            cover.write_bytes(b"cover")

            persistent = bridge.persist_pipeline_cover(store, "a" * 64, cover)
            bridge.cleanup_uploaded_recording(
                root / "missing.flv",
                None,
                root / "missing.flv",
                artifact_dir=artifact_dir,
            )

            self.assertTrue(persistent.is_file())
            self.assertEqual(persistent.read_bytes(), b"cover")

    def test_live_segments_append_to_one_bilibili_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.jpg"
            cookie = root / "cookie.json"
            first = root / "主播_abcdef2026-07-23_09-00-00_第一局.flv"
            second = root / "主播_abcdef2026-07-23_10-00-00_第二局.flv"
            first_xml = root / "第一局.xml"
            second_xml = root / "第二局.xml"
            cover.write_bytes(b"cover")
            cookie.write_text("[]", encoding="utf-8")
            first.write_bytes(b"part-one")
            second.write_bytes(b"part-two")
            first_xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">第一段弹幕</d></i>',
                encoding="utf-8",
            )
            second_xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">第二段弹幕</d></i>',
                encoding="utf-8",
            )
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "bilibili_cookies": str(cookie),
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": True,
                "ai_danmaku_summary_enabled": True,
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
                bridge, "import_app", return_value=(FakeUploader, None)
            ), patch.object(
                bridge,
                "generate_danmaku_metadata_with_ai",
                side_effect=[
                    ("直播录播：主播《第一局》。第一段 AI 总结", "第一段 AI 主题"),
                    ("直播录播：主播《第二局》。第二段 AI 总结", "第二段 AI 主题"),
                ],
            ) as generate_summary, patch.object(
                bridge, "probe_video_size", return_value=(1920, 1080)
            ):
                self.assertTrue(bridge.upload_one(
                    first, cfg, store, danmaku_xml=first_xml, session_key="room-1"
                ))
                self.assertTrue(bridge.upload_one(
                    second, cfg, store, danmaku_xml=second_xml, session_key="room-1"
                ))

            enhance_metadata.assert_called_once()
            self.assertEqual(generate_cover.call_count, 2)
            self.assertEqual(
                [call.kwargs["target_size"] for call in generate_cover.call_args_list],
                [(1920, 1080), (1600, 1200)],
            )
            self.assertEqual(generate_summary.call_count, 2)
            self.assertIsNone(calls[0]["existing_submission"])
            self.assertEqual(calls[0]["page_titles"], ["09:00 第一段 AI 主题"])
            self.assertIn("【P1｜第一段 AI 主题｜07-23 09:00】", calls[0]["description"])
            self.assertIn("第一段 AI 总结", calls[0]["description"])
            self.assertNotIn("第二段 AI 总结", calls[0]["description"])
            self.assertEqual(calls[0]["tags"], ["主播", "AI标签"])
            self.assertEqual(calls[0]["partition_id"], "129")
            self.assertTrue(calls[0]["is_original"])
            self.assertEqual(calls[1]["existing_submission"]["bvid"], "BV1multipart")
            self.assertEqual(calls[1]["page_titles"], ["10:00 第二段 AI 主题"])
            self.assertIn("【P1｜第一段 AI 主题｜07-23 09:00】", calls[1]["description"])
            self.assertIn("【P2｜第二段 AI 主题｜07-23 10:00】", calls[1]["description"])
            self.assertIn("第一段 AI 总结", calls[1]["description"])
            self.assertIn("第二段 AI 总结", calls[1]["description"])
            self.assertEqual(calls[1]["tags"], ["主播", "AI标签"])
            self.assertEqual(calls[1]["partition_id"], "129")
            self.assertTrue(calls[1]["is_original"])
            session = store.multipart_session("room-1")
            self.assertEqual(session["bilibili"]["part_count"], 2)
            self.assertEqual(session["partition_id"], "129")
            self.assertTrue(session["metadata_automation"]["cover_for_partition_ai"])
            session_cover = Path(session["cover_path"])
            self.assertEqual(session_cover.parent, root.resolve())
            self.assertEqual(session_cover.read_bytes(), cover.read_bytes())
            self.assertEqual(
                [part["title_topic"] for part in session["parts"]],
                ["第一段 AI 主题", "第二段 AI 主题"],
            )
            self.assertTrue(store.close_multipart_session("room-1"))
            self.assertEqual(store.multipart_session("room-1"), {})

    def test_recording_metadata_uses_app_tags_partition_and_cover_setting(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.jpg"
            cover.write_bytes(b"cover")
            cfg = {"_config_dir": str(root), "app_root": str(app_root)}
            selection = {
                "id": "129",
                "source": "ai",
                "confidence": 0.92,
                "reason_summary": "封面与标题均为游戏内容",
                "alternatives": ["171"],
            }
            ai_module = types.ModuleType("modules.ai_enhancer")
            ai_module.generate_video_tags = Mock(
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

    def test_dota2_recording_defaults_to_esports_partition(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.jpg"
            cover.write_bytes(b"cover")
            cfg = {"_config_dir": str(root), "app_root": str(app_root)}
            ai_module = types.ModuleType("modules.ai_enhancer")
            ai_module.generate_video_tags = Mock(return_value=["DOTA2", "直播回放"])
            recommend = Mock(return_value={"id": "65", "source": "ai"})
            ai_module.recommend_bilibili_partition = recommend
            zones_module = types.ModuleType("modules.bilibili_zones")
            zones_module.get_zone_list_sub = Mock(return_value=[])
            config_module = types.ModuleType("modules.config_manager")
            config_module.load_config = Mock(return_value={
                "GENERATE_TAGS": True,
                "RECOMMEND_PARTITION": True,
                "RECOMMEND_PARTITION_WITH_COVER": True,
                "OPENAI_API_KEY": "test-key",
                "OPENAI_MODEL_NAME": "vision-model",
                "FIXED_PARTITION_ID_BILIBILI": "",
            })
            with patch.dict(sys.modules, {
                "modules.ai_enhancer": ai_module,
                "modules.bilibili_zones": zones_module,
                "modules.config_manager": config_module,
            }):
                tags, partition_id, details = bridge.enhance_recording_metadata(
                    "YYF Dota 2 对局复盘",
                    "弹幕讨论本场刀塔比赛",
                    ["YYF"],
                    cover,
                    "65",
                    cfg,
                )

        self.assertEqual(tags, ["YYF", "DOTA2", "直播回放"])
        self.assertEqual(partition_id, "171")
        self.assertEqual(details["recommended_partition_id"], "171")
        self.assertEqual(details["selected_partition_id"], "171")
        self.assertEqual(details["partition_source"], "dota2_default")
        self.assertEqual(details["partition_confidence"], 1.0)
        recommend.assert_not_called()

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

    def test_recording_cover_headline_rejects_generic_topic_and_uses_reviewed_title(self):
        headline = bridge.recording_cover_headline(
            "YYF蓝猫残局送人头转小黑复健失败｜08-01 14:39",
            "直播精彩内容",
            "yyfyyf",
        )

        self.assertEqual(headline, "YYF蓝猫残局送人头转小黑复健失败")
        self.assertNotIn("直播精彩内容", headline)
        self.assertNotIn("08-01", headline)

    def test_vague_title_topic_uses_closest_verified_timeline_event(self):
        topic = bridge.recording_title_topic_from_timeline(
            "YYF绝活小狗出装引争议",
            (
                "重要时间点\n"
                "00:21 YYF操刀小狗带4个拳套出门，弹幕讨论其出装与对线压力\n"
                "31:08 YYF赛后复盘上一局失误"
            ),
        )

        self.assertEqual(
            topic,
            "YYF操刀小狗带4个拳套出门，弹幕讨论其出装与对线压力",
        )
        self.assertFalse(bridge.recording_title_topic_is_vague(topic))

    def test_timeline_fallback_skips_over_limit_event_without_slicing(self):
        diagnostics = {}
        long_event = (
            "枫哥在高地连续完成三次关键反杀并最终带领队伍守住基地逆转比赛，"
            "弹幕从担心基地失守到刷屏庆祝整场翻盘"
        )
        topic = bridge.recording_title_topic_from_timeline(
            "",
            f"00:10 {long_event}\n31:08 枫哥高地三杀带队逆转",
            diagnostics=diagnostics,
        )

        self.assertEqual(topic, "枫哥高地三杀带队逆转")
        self.assertEqual(diagnostics["title_topic_timeline_over_limit_count"], 1)
        self.assertEqual(
            bridge.recording_title_topic_from_timeline("", f"00:10 {long_event}"),
            "枫哥在高地连续完成三次关键反杀并最终带领队伍守住基地逆转比赛",
        )

    def test_passive_reaction_titles_are_vague_but_concrete_followups_are_kept(self):
        for topic in (
            "YYF宝可梦BP与虚空出装被吐槽",
            "川神主锤骷髅王被赞完美适配",
            "果小果巫妖操作被狂喷",
            "风暴之灵所在一方的翻盘可能遭到弹幕反复质疑",
        ):
            with self.subTest(topic=topic):
                self.assertTrue(bridge.recording_title_topic_is_vague(topic))

        for topic in (
            "YYF宝可梦一轮游被狂喷发红包求饶",
            "果小果巫妖公式化被喷后躺赢",
        ):
            with self.subTest(topic=topic):
                self.assertFalse(bridge.recording_title_topic_is_vague(topic))

    def test_progress_only_titles_are_weak(self):
        for topic in (
            "小哈尼长跑挑战进入后段",
            "半马接力：小哈尼第三圈左右",
            "进入抽卡环节，弹幕刷屏保底",
            "实战队伍频繁踩中范围伤害，弹幕调侃",
            "牛姐对视挑战失败，弹幕围绕“牛蛙”",
            "本局结束后转入玛西对局；后段继续守高",
        ):
            with self.subTest(topic=topic):
                self.assertTrue(bridge.recording_title_topic_is_vague(topic))

    def test_timeline_fallback_keeps_complete_detail_instead_of_first_clause(self):
        topic = bridge.recording_title_topic_from_timeline(
            "挑战进入后段",
            "00:10 小冉突然加速，小哈尼追上后两人多次互超",
        )

        self.assertEqual(topic, "小冉突然加速，小哈尼追上后两人多次互超")

    def test_only_rich_long_recordings_reject_tiny_single_moment_titles(self):
        self.assertTrue(
            bridge.recording_title_topic_is_underfilled(
                "负六百魔伤仍选魔法技能", 3600, 10
            )
        )
        self.assertFalse(
            bridge.recording_title_topic_is_underfilled(
                "负六百魔伤仍选魔法技能", 1200, 10
            )
        )
        self.assertFalse(
            bridge.recording_title_topic_is_underfilled(
                "飞升后卡等级；负六百魔伤仍选魔法技能", 3600, 10
            )
        )

    def test_long_recording_title_requires_cross_stage_timeline_evidence(self):
        self.assertFalse(
            bridge.recording_title_timeline_coverage_is_sufficient([4], 3600, 10)
        )
        self.assertFalse(
            bridge.recording_title_timeline_coverage_is_sufficient([4, 5], 3600, 10)
        )
        self.assertTrue(
            bridge.recording_title_timeline_coverage_is_sufficient([1, 7], 3600, 10)
        )
        self.assertTrue(
            bridge.recording_title_timeline_coverage_is_sufficient([], 1200, 10)
        )

    def test_recent_server_long_recording_title_examples_are_complete(self):
        topics = (
            "副本输出从刮痧到突然领先，后段一波“222”操作收尾",
            "机械派对四人挑战20秒团灭，果小果从1105分垫底到手速局第一",
            "小哈尼单挑十人接力，遭小AA反超后再追，42圈仍领先",
            "满级专武让战力快速起飞，神性路线争到最后才发现Tab切换",
            "从林老师背人深蹲到末世派对，记忆小游戏连错拿下0分",
            "谢彬三星弓箭手从落后冲到领先；三炮全中炸掉基地",
            "小哈尼穿小僵尸服1V10挑战半马，开局拉开后17圈暂时领先",
            "RPG商城掀起“军备竞赛”，160抽连吃保底后芯片又装不上",
            "难一推进受阻后商店掀起“军备竞赛”，通行证与抽卡保底接连上阵",
            "骚子打招呼接连被无视，牛姐与理理对视后“东坡肉”互动收尾",
            "鲷哥战力反超冲上千亿；合成EX后数值进兆仍打不动BOSS",
            "胖头160亿战力被反超；挑战压到0.5%仍未限时击杀",
            "宝石镶嵌和套装机制一路没理清，RPG天团鏖战一下午仍卡难一",
            "更新崩溃后重回战斗，三炮全中炸掉基地；谢彬战力升至第一",
            "千亿战力仍打不动BOSS，服务器崩后围绕YYF、谢彬和阿龙争输出位",
            "首个十连几乎全是紫色晶石，入口圣物被移出，黑市宝石始终没花",
            "阿龙一人扛下九成输出，枫哥与沙雕难一刮痧又抽错常驻池",
            "复杂RPG从装备拾取到“无限踩圈”，后段才摸到狩猎套装主流程",
            "CS拆弹完成“史上最伟大翻盘”，骑车速降后又转入无限螺旋2",
            "排队十分钟后转战CS，从一打四残局到5:13惜败，空枪拆包收尾",
        )

        audience_worded = 0
        for topic in topics:
            with self.subTest(topic=topic):
                self.assertGreaterEqual(len(topic), 18)
                self.assertLessEqual(len(topic), bridge.RECORDING_TITLE_TOPIC_LIMIT)
                self.assertFalse(bridge.recording_title_topic_is_vague(topic))
                self.assertNotRegex(topic, r"直播间(?:热议|讨论|关注)|[｜|]")
                audience_worded += bool(re.search(r"弹幕|观众|刷屏|起哄", topic))
        self.assertLessEqual(audience_worded, 5)

    def test_recording_cover_hero_rejects_only_explicit_title_conflict(self):
        self.assertTrue(
            bridge.recording_cover_hero_matches_title(
                "主宰",
                "YYF主宰打穿后露娜对线受压｜08-01 12:39",
            )
        )
        self.assertFalse(
            bridge.recording_cover_hero_matches_title(
                "帕吉",
                "YYF蓝猫残局送人头转小黑复健失败｜08-01 14:39",
            )
        )
        self.assertTrue(
            bridge.recording_cover_hero_matches_title(
                "冥魂大帝",
                "川神主锤骷髅王假3真1打爆下路｜08-01 18:02",
            )
        )
        self.assertTrue(
            bridge.recording_cover_hero_matches_title(
                "风暴之灵",
                "高地推进后基地爆炸｜08-07 02:05",
            )
        )

    def test_cover_context_uses_timestamp_free_verified_timeline(self):
        context, source = bridge.recording_cover_event_context(
            "00:06 YYF锁定1号位\n46:16 小狗主魔免引发争论"
        )

        self.assertEqual(source, "verified_timeline")
        self.assertEqual(context, "YYF锁定1号位；小狗主魔免引发争论")
        self.assertNotIn("00:06", context)

    def test_cover_context_keeps_event_timestamp_for_gsi_segment_matching(self):
        description = (
            "00:12 蓝猫更新紫怨后继续刷钱\n"
            "41:44 玛西补出BKB后发起团战"
        )
        context, _ = bridge.recording_cover_event_context(
            description,
            "玛西补出BKB后发起团战",
        )

        self.assertEqual(
            bridge.recording_cover_event_timestamp_seconds(description, context),
            41 * 60 + 44,
        )

    def test_verified_timeline_never_promotes_itself_to_hero_identity(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")

        self.assertNotIn("verified_timeline_streamer_hero", source)
        self.assertNotIn("timeline_hero_identity", source)

    def test_dota2_cover_prompt_resolves_common_hero_aliases(self):
        instruction = bridge.recording_cover_dota2_instruction(
            "蓝猫中路对线",
            "火猫与紫猫切入",
            "DOTA2 对局中老奶奶、小鱼人和白牛连续追击。",
        )
        self.assertIn("老奶奶＝电炎绝手（Snapfire）", instruction)
        self.assertIn("蓝猫＝风暴之灵（Storm Spirit）", instruction)
        self.assertIn("火猫＝灰烬之灵（Ember Spirit）", instruction)
        self.assertIn("紫猫＝虚无之灵（Void Spirit）", instruction)
        self.assertIn("小鱼人＝斯拉克（Slark）", instruction)
        self.assertIn("白牛＝裂魂人（Spirit Breaker）", instruction)
        self.assertIn("绝对不能画成蓝色猫", instruction)
        self.assertIn("绝对不能画成紫色猫", instruction)
        self.assertIn("禁止混入《英雄联盟》、宝可梦或其他作品", instruction)

    def test_dota2_metadata_prompt_disambiguates_old_lady_as_snapfire(self):
        self.assertIn("老奶奶", bridge.DOTA2_METADATA_DISAMBIGUATION)
        self.assertIn("电炎绝手（Snapfire）", bridge.DOTA2_METADATA_DISAMBIGUATION)
        self.assertIn("不得理解为普通老年女性", bridge.DOTA2_METADATA_DISAMBIGUATION)

    def test_dota2_cover_prompt_does_not_force_storm_spirit_without_blue_cat(self):
        instruction = bridge.recording_cover_dota2_instruction(
            "宝可梦挑战",
            "新地图探索",
        )
        self.assertNotIn("绝对不能画成蓝色猫", instruction)

    def test_dota2_item_context_does_not_treat_generic_words_as_items(self):
        self.assertFalse(
            bridge.recording_cover_has_dota2_context(
                "唱歌主播",
                "蝴蝶与天堂",
                "刷新页面继续听歌",
            )
        )
        self.assertTrue(
            bridge.recording_cover_has_dota2_context(
                "新主播",
                "蓝猫补出大电锤",
            )
        )
        self.assertFalse(
            bridge.recording_cover_has_dota2_context(
                "yyfyyf",
                "RPG圣物选择一路被催刷新，大柱子旁拿到宝石",
            )
        )
        self.assertTrue(
            bridge.recording_cover_has_dota2_context(
                "yyfyyf",
                "斧王十分钟经济六千，更新跳刀后开团",
            )
        )

    def test_dota2_streamer_aliases_use_stable_public_names(self):
        cases = {
            "FG": "YYF",
            "胖头": "YYF",
            "胖头鱼": "YYF",
            "B神": "BurNIng",
            "八师傅": "xiao8",
            "小明鞭": "Faith_bian",
            "拒绝者": "Paparazi",
            "龙神": "LongDD",
            "军体拳": "Sccc",
            "白毛": "国民大舅哥",
            "大舅哥": "国民大舅哥",
            "国名大舅哥": "国民大舅哥",
            "叫我老陈就好了": "川神",
            "老菜": "川神",
            "勇哥": "川神",
        }
        for alias, expected in cases.items():
            with self.subTest(alias=alias):
                self.assertEqual(
                    bridge.normalize_dota2_streamer_name(alias),
                    expected,
                )

    def test_cover_subject_uses_performance_alias_from_title(self):
        self.assertEqual(
            bridge.recording_cover_subject_name(
                "叫我老陈就好了",
                "被骗幻象换走后迟迟不买活，老菜把全场看急了",
            ),
            "老菜",
        )
        self.assertEqual(
            bridge.recording_cover_subject_name(
                "叫我老陈就好了",
                "川神关键换位救回必输局",
            ),
            "川神",
        )

    def test_cover_headline_adds_detected_subject_without_column_separator(self):
        headline = bridge.recording_cover_headline(
            "被骗幻象换走后迟迟不买活，老菜把全场看急了",
            "被骗换后迟迟买活",
            "叫我老陈就好了",
        )
        self.assertEqual(headline, "老菜被骗换后迟迟买活")
        self.assertNotIn("｜", headline)

    def test_cover_headline_keeps_complete_verified_event_without_forcing_owner(self):
        headline = bridge.recording_cover_headline(
            "老蔡三角区跳吼，马甲随后夺冠｜08-06 15:25",
            "",
            "yyfyyf",
        )
        self.assertEqual(headline, "老蔡三角区跳吼，马甲随后夺冠")
        self.assertNotIn("枫哥", headline)

    def test_cover_headline_is_not_cut_mid_sentence(self):
        event = "奶哥带队连续避开范围伤害，最终完成高难关卡挑战"
        self.assertEqual(
            bridge.recording_cover_headline(f"{event}｜08-06 14:00", "", "谢彬DD"),
            event,
        )

    def test_cover_uses_only_first_event_from_multi_event_title(self):
        self.assertEqual(
            bridge.recording_cover_headline(
                "奶哥蓝猫残局收割；队伍高难关卡翻车｜08-06 14:00",
                "",
                "谢彬DD",
            ),
            "奶哥蓝猫残局收割",
        )

    def test_cover_copy_can_be_shorter_without_inventing_or_cutting(self):
        source = "奶哥带队连续避开范围伤害，最终完成高难关卡挑战"
        self.assertEqual(
            bridge.recording_cover_display_text(
                source,
                "奶哥完成高难挑战",
                "谢彬DD",
            ),
            "奶哥完成高难挑战",
        )
        self.assertEqual(
            bridge.recording_cover_display_text(
                source,
                "奶哥突然宣布退役",
                "谢彬DD",
            ),
            source,
        )
        self.assertEqual(
            bridge.recording_cover_display_text(
                "弹幕称某选手因假赛被封禁",
                "假赛被封禁",
            ),
            "",
        )

    def test_cover_copy_layout_adapts_to_length_and_aspect_ratio(self):
        short_layout = bridge.recording_cover_text_layout_instruction(
            "极限翻盘",
            (1920, 1080),
        )
        long_layout = bridge.recording_cover_text_layout_instruction(
            "奶哥带队连续避开范围伤害并最终完成高难关卡挑战",
            (1600, 1200),
        )
        self.assertIn("单行大字", short_layout)
        self.assertIn("左侧或右侧约三分之一", short_layout)
        self.assertIn("两行大字", long_layout)
        self.assertIn("上方或下方约三分之一", long_layout)
        self.assertIn("至少8%的安全边距", long_layout)

    def test_card_hand_cover_instruction_requires_visible_five_of_a_kind(self):
        instruction = bridge.recording_cover_card_hand_instruction(
            "川神人工五条K轰出20500分，后程断牌卡对手"
        )

        self.assertIn("五张点数均为 K 的正面扑克牌", instruction)
        self.assertIn("不得按标准牌组常识缩减成四张", instruction)
        self.assertIn("不能被人物、文字或裁切遮住", instruction)

    def test_card_hand_cover_instruction_does_not_turn_failed_draw_into_win(self):
        instruction = bridge.recording_cover_card_hand_instruction(
            "冲击五条10却只差一张落空"
        )

        self.assertNotIn("五张点数均为 10", instruction)
        self.assertIn("不得画成已经完成的牌型", instruction)

    def test_card_hand_cover_instruction_preserves_blocked_royal_flush(self):
        instruction = bridge.recording_cover_card_hand_instruction(
            "抢J卡断皇家同花顺，冲同花顺错失方片4后垫底"
        )

        self.assertIn("不是已经完成皇家同花顺", instruction)
        self.assertIn("将关键 J 牌单独抽出", instruction)
        self.assertIn("错失方片4", instruction)
        self.assertIn("不得把缺牌状态画成已经完成的同花顺", instruction)

    def test_non_card_cover_has_no_card_hand_instruction(self):
        self.assertEqual(
            bridge.recording_cover_card_hand_instruction("枫哥钢背兽拆掉基地"),
            "",
        )

    @unittest.skip("removed cover behavior")
    def test_cover_subject_identity_locks_aliases_to_character_base(self):
        yyf_instruction = bridge.recording_cover_subject_identity_instruction(
            "YYF",
            "枫哥",
        )
        self.assertIn("“枫哥”、“YYF”", yyf_instruction)
        self.assertIn("同一位主播", yyf_instruction)
        self.assertIn("主播只能有一人", yyf_instruction)
        self.assertIn("只能依据随请求上传的封面人物底稿", yyf_instruction)

        laocai_instruction = bridge.recording_cover_subject_identity_instruction(
            "川神",
            "老菜",
        )
        self.assertIn("“老菜”、“川神”、“叫我老陈就好了”", laocai_instruction)
        self.assertIn("不得按字面画成枫叶、鱼、蔬菜", laocai_instruction)

    def test_cover_guest_candidates_exclude_room_owner_aliases(self):
        guests = bridge.recording_cover_guest_candidates(
            "YYF",
            "枫哥和B神一起复盘，拒绝者也来了",
        )
        self.assertEqual(
            [(guest["name"], guest["mentioned_as"]) for guest in guests],
            [("BurNIng", "B神"), ("Paparazi", "拒绝者")],
        )

    def test_cover_guest_candidates_do_not_treat_percentage_as_numeric_alias(self):
        self.assertEqual(
            bridge.recording_cover_guest_candidates(
                "YYF",
                "枫哥一小时RPG养成，实战输出仍为0.0%",
            ),
            [],
        )
        self.assertEqual(
            bridge.recording_cover_guest_candidates(
                "YYF",
                "0.0和枫哥一起复盘",
            ),
            [{"name": "Sylar", "mentioned_as": "0.0"}],
        )

    def test_pokemon_participant_aliases_keep_similar_people_distinct(self):
        self.assertEqual(bridge.normalize_dota2_streamer_name("狗哥"), "叁肆叁肆")
        self.assertEqual(bridge.normalize_dota2_streamer_name("三生三世"), "叁肆叁肆")
        self.assertEqual(bridge.normalize_dota2_streamer_name("叁肆叁肆"), "叁肆叁肆")
        self.assertEqual(bridge.normalize_dota2_streamer_name("三酒"), "三酒")
        self.assertEqual(bridge.normalize_dota2_streamer_name("faith"), "哈哈明")
        self.assertEqual(bridge.normalize_dota2_streamer_name("哈哈明"), "哈哈明")
        self.assertEqual(
            bridge.normalize_dota2_streamer_name("faithbian"),
            "Faith_bian",
        )

    def test_pokemon_historical_danmu_aliases_map_to_participants(self):
        expected = {
            "老蔡": "川神",
            "眼子": "Sylar",
            "彬子": "DD",
            "谢斌": "DD",
            "查猪": "Chalice",
            "马甲": "ZSMJ",
            "甲哥": "ZSMJ",
            "石业": "石页",
            "塔宝": "塔莉娅",
            "雅醋": "阿雅Midori",
            "饼子": "蛋饼",
            "糕神": "蛋糕",
            "林九哥": "林九鸽",
            "毛张": "炸毛张",
            "鲷哥": "Zhou",
            "sed": "MacSed",
            "阿雅": "阿雅Midori",
            "猴": "Hao",
            "HAOB": "Hao",
            "大猛一": "艾斯yoona",
            "王兆辉": "叁肆叁肆",
            "狗妹": "叁肆叁肆",
        }
        for alias, participant in expected.items():
            with self.subTest(alias=alias):
                self.assertEqual(
                    bridge.normalize_dota2_streamer_name(alias),
                    participant,
                )

    def test_pokemon_names_do_not_require_event_context_for_guest_avatar(self):
        ordinary_context_guests = bridge.recording_cover_guest_candidates(
            "YYF",
            "",
            "今天吃蛋糕时看到一只小蝴蝶",
        )
        self.assertEqual(
            [(guest["name"], guest["mentioned_as"])
             for guest in ordinary_context_guests],
            [("蛋糕", "蛋糕"), ("Spirit小蝴蝶", "小蝴蝶")],
        )
        guests = bridge.recording_cover_guest_candidates(
            "YYF",
            "宝可梦选人时Spirit小蝴蝶和小蝴蝶分到一组，饼子也来了",
        )
        self.assertEqual(
            [(guest["name"], guest["mentioned_as"]) for guest in guests],
            [
                ("Spirit小蝴蝶", "Spirit小蝴蝶"),
                ("蛋饼", "饼子"),
            ],
        )

    def test_pokemon_participant_named_only_in_title_gets_guest_avatar(self):
        guests = bridge.recording_cover_guest_candidates(
            "YYF",
            "狗哥与大猛一正面对决",
            "本局双方前期打得十分激烈。",
        )
        self.assertEqual(
            [(guest["name"], guest["mentioned_as"]) for guest in guests],
            [("叁肆叁肆", "狗哥"), ("艾斯yoona", "大猛一")],
        )

    def test_guest_avatar_uses_official_participant_room_id(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        from modules import live_recorder_manager as manager_module

        def search_rooms(query, limit):
            if query == "762484":
                self.assertEqual(limit, 1)
                return [
                    {
                        "room_id": "762484",
                        "name": "刘嘉俊Sylar1",
                        "avatar_url": "https://apic.douyucdn.cn/sylar.jpg",
                    },
                ]
            return []

        with patch.object(
            manager_module.live_recorder_manager,
            "_search_douyu_rooms",
            side_effect=search_rooms,
        ) as search:
            resolved = bridge.resolve_recording_guest_avatar(
                {"name": "Sylar", "mentioned_as": "眼子"},
                {"_recording_profiles": []},
            )

        self.assertEqual(resolved["room_id"], "762484")
        self.assertEqual(resolved["source"], "douyu_event_room")
        self.assertEqual(resolved["search_name"], "762484")
        self.assertEqual(
            [call.args[0] for call in search.call_args_list],
            ["762484"],
        )

    def test_official_participant_rooms_cover_every_linked_event_tile(self):
        self.assertEqual(len(bridge.DOTA2_POKEMON_PARTICIPANT_ROOM_IDS), 47)
        self.assertEqual(
            bridge.DOTA2_POKEMON_PARTICIPANT_ROOM_IDS["蛋饼"],
            "8758901",
        )
        self.assertEqual(
            bridge.DOTA2_POKEMON_PARTICIPANT_ROOM_IDS["哈哈明"],
            "331437",
        )
        self.assertEqual(
            bridge.normalize_dota2_streamer_name("小蝴蝶"),
            "Spirit小蝴蝶",
        )
        self.assertEqual(
            bridge.DOTA2_POKEMON_PARTICIPANT_ROOM_IDS["Spirit小蝴蝶"],
            "448014",
        )

    def test_guomin_dajiuge_s105_participants_are_room_scoped(self):
        identities = bridge.recording_room_participant_identity_map("国民大舅哥")

        self.assertEqual(len(identities), 93)
        by_name = {item["name"]: item for item in identities}
        self.assertEqual(by_name["小欣欣7v7"]["douyu_room_id"], "12174524")
        self.assertIn("小欣欣", by_name["小欣欣7v7"]["aliases"])
        self.assertEqual(by_name["蛋饼pp"]["douyu_room_id"], "12543616")
        self.assertIn("蛋饼", by_name["蛋饼pp"]["aliases"])
        self.assertIn("小胖", by_name["徐不快乐"]["aliases"])
        self.assertIn("栗子", by_name["是个好栗子"]["aliases"])
        self.assertIn("瑶瑶", by_name["一凹瑶wa"]["aliases"])
        self.assertIn("七安", by_name["TiAmo七安"]["aliases"])
        self.assertIn("北极星", by_name["北极昕"]["aliases"])
        self.assertEqual(
            bridge.recording_room_participant_identity_map("YYF"),
            [],
        )

    def test_guomin_dajiuge_guest_alias_prefers_current_activity_room(self):
        guests = bridge.recording_cover_guest_candidates(
            "国民大舅哥",
            "蛋饼和暖妹进入下一轮，小欣欣也来集合",
        )

        self.assertEqual(
            [(guest["name"], guest["mentioned_as"]) for guest in guests],
            [
                ("蛋饼pp", "蛋饼"),
                ("暖妹QWQ", "暖妹"),
                ("小欣欣7v7", "小欣欣"),
            ],
        )
        self.assertEqual(
            bridge.recording_cover_guest_candidates("YYF", "暖妹和小欣欣来集合"),
            [],
        )

    def test_guomin_dajiuge_title_nickname_avatar_rule_is_room_scoped(self):
        title = "小胖携手栗子、瑶瑶拿下关键一局"

        guests = bridge.recording_cover_guest_candidates("国民大舅哥", title)

        self.assertEqual(
            [(guest["name"], guest["mentioned_as"]) for guest in guests],
            [
                ("徐不快乐", "小胖"),
                ("是个好栗子", "栗子"),
                ("一凹瑶wa", "瑶瑶"),
            ],
        )
        self.assertEqual(
            bridge.recording_cover_guest_candidates("YYF", title),
            [],
        )

    def test_guomin_dajiuge_251_title_uses_yixiweno_room(self):
        guests = bridge.recording_cover_guest_candidates(
            "国民大舅哥",
            "251完成翻盘",
        )

        self.assertEqual(
            guests,
            [{"name": "易惜文O", "mentioned_as": "251"}],
        )
        self.assertEqual(
            bridge.GUOMIN_DAJIUGE_S105_PARTICIPANT_ROOM_IDS["易惜文O"],
            # 77251 is the public short room number; Douyu's search API
            # resolves it to this canonical room ID for avatar retrieval.
            "11518380",
        )
        self.assertEqual(
            bridge.recording_cover_guest_candidates("YYF", "251完成翻盘"),
            [],
        )

    def test_guomin_dajiuge_guest_avatar_uses_activity_room_id(self):
        from modules import live_recorder_manager as manager_module

        with patch.object(
            manager_module.live_recorder_manager,
            "_search_douyu_rooms",
            return_value=[{
                "room_id": "12174524",
                "name": "小欣欣7v7",
                "avatar_url": "https://apic.douyucdn.cn/xinxin.jpg",
            }],
        ) as search:
            resolved = bridge.resolve_recording_guest_avatar(
                {"name": "小欣欣7v7", "mentioned_as": "小欣欣"},
                {"streamer_name": "国民大舅哥", "_recording_profiles": []},
            )

        self.assertEqual(resolved["room_id"], "12174524")
        self.assertEqual(resolved["source"], "douyu_event_room")
        search.assert_called_once_with("12174524", 1)

    def test_guest_avatar_uses_unique_exact_douyu_search_result(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        from modules import live_recorder_manager as manager_module

        candidates = [
            {
                "room_id": "123",
                "name": "B神",
                "avatar_url": "https://apic.douyucdn.cn/burning.jpg",
            },
            {
                "room_id": "456",
                "name": "B神迷弟",
                "avatar_url": "https://apic.douyucdn.cn/fan.jpg",
            },
        ]
        with patch.object(
            manager_module.live_recorder_manager,
            "_search_douyu_rooms",
            return_value=candidates,
        ) as search:
            resolved = bridge.resolve_recording_guest_avatar(
                {"name": "BurNIng", "mentioned_as": "B神"},
                {"_recording_profiles": []},
            )

        self.assertEqual(resolved["room_id"], "123")
        self.assertEqual(resolved["source"], "douyu_api")
        search.assert_called_once_with("B神", 10)

    def test_guest_avatar_accepts_two_identity_aliases_but_rejects_ambiguity(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        from modules import live_recorder_manager as manager_module

        with patch.object(
            manager_module.live_recorder_manager,
            "_search_douyu_rooms",
            return_value=[
                {
                    "room_id": "562483",
                    "name": "拒绝者paparazi灌",
                    "avatar_url": "https://apic.douyucdn.cn/paparazi.jpg",
                },
                {
                    "room_id": "6110689",
                    "name": "Paparazi泽",
                    "avatar_url": "https://apic.douyucdn.cn/other.jpg",
                },
            ],
        ):
            resolved = bridge.resolve_recording_guest_avatar(
                {"name": "Paparazi", "mentioned_as": "拒绝者"},
                {"_recording_profiles": []},
            )
        self.assertEqual(resolved["room_id"], "562483")

        ambiguous = [
            {
                "room_id": "1",
                "name": "徐志雷BurNIng",
                "avatar_url": "https://apic.douyucdn.cn/one.jpg",
            },
            {
                "room_id": "2",
                "name": "burning徐志雷",
                "avatar_url": "https://apic.douyucdn.cn/two.jpg",
            },
        ]
        with patch.object(
            manager_module.live_recorder_manager,
            "_search_douyu_rooms",
            return_value=ambiguous,
        ):
            self.assertIsNone(bridge.resolve_recording_guest_avatar(
                {"name": "BurNIng", "mentioned_as": "B神"},
                {"_recording_profiles": []},
            ))

    def test_dota2_streamer_prompt_keeps_room_owner_as_cover_subject(self):
        instruction = bridge.recording_cover_dota2_streamer_instruction(
            "yyfyyf",
            "B神和拒绝者复盘比赛",
        )
        self.assertIn("yyfyyf＝Dota 2 主播/选手 YYF", instruction)
        self.assertIn("B神＝Dota 2 主播/选手 BurNIng", instruction)
        self.assertIn("拒绝者＝Dota 2 主播/选手 Paparazi", instruction)
        self.assertIn("封面主体仍必须以当前直播间的封面人物底稿", instruction)
        self.assertIn("其他被提及选手不能取代主播", instruction)

    @unittest.skip("removed cover behavior")
    def test_verified_dota2_gameplay_separates_streamer_and_official_hero(self):
        instruction = bridge.recording_cover_verified_hero_cosplay_instruction(
            "光之守卫",
            gameplay_verified=True,
        )
        self.assertIn("恢复经典双主体构图", instruction)
        self.assertIn("主播人物底稿作为独立前景反应主体", instruction)
        self.assertIn("光之守卫 作为独立的官方游戏英雄出现在侧后方", instruction)
        self.assertIn("身体、脸部、服装和轮廓必须清楚分开", instruction)
        self.assertIn("禁止主播 Cos 英雄", instruction)
        self.assertIn("禁止把头像贴到英雄身体上", instruction)
        self.assertIn("英雄只出现一次", instruction)

        unverified = bridge.recording_cover_verified_hero_cosplay_instruction(
            "光之守卫",
            gameplay_verified=False,
        )
        self.assertIn("不得让主播穿成被观战", unverified)
        self.assertNotIn("恢复经典双主体构图", unverified)

    @unittest.skip("removed cover behavior")
    def test_verified_dota2_gameplay_can_use_fusion_mode(self):
        instruction = bridge.recording_cover_verified_hero_cosplay_instruction(
            "光之守卫",
            gameplay_verified=True,
            layout_mode="fusion",
        )

        self.assertIn("使用英雄融合构图", instruction)
        self.assertIn("本人 Cos 本局英雄", instruction)
        self.assertIn("同一个完整人物", instruction)
        self.assertIn("唯一的 光之守卫 角色", instruction)
        self.assertNotIn("恢复经典双主体构图", instruction)

        avatar_instruction = bridge.recording_avatar_reference_instruction(
            "YYF",
            "fusion",
        )
        self.assertIn("自适应融合该英雄最有辨识度", avatar_instruction)
        self.assertIn("形成同一个完整 Cos 人物", avatar_instruction)

    @unittest.skip("removed cover behavior")
    def test_yyf_cover_expression_follows_segment_performance(self):
        instruction = bridge.recording_cover_streamer_expression_instruction(
            "枫哥",
            "蓝猫关键失误后惨遭翻盘",
            "YYF 最后一波团战无奈落败。",
        )
        self.assertIn("YYF 的最终表情必须与标题对应的已核验对局情况联动", instruction)
        self.assertIn("先判断最终结果", instruction)
        self.assertIn("只选择一种占主导的情绪", instruction)
        self.assertIn("眉眼开合、嘴角、视线方向", instruction)
        self.assertIn("本段优先表情建议：从错愕转为懊恼", instruction)
        self.assertIn("参考图不是人物或角色时不要强行添加表情", instruction)
        self.assertNotIn("蓝色鱼形头套", instruction)

    @unittest.skip("removed cover behavior")
    def test_yyf_cover_expression_prefers_successful_comeback_over_generic_win(self):
        instruction = bridge.recording_cover_streamer_expression_instruction(
            "YYF",
            "绝地翻盘成功拿下胜利",
        )
        self.assertIn("本段优先表情建议：如释重负后的兴奋", instruction)
        self.assertNotIn("本段优先表情建议：开心满足", instruction)

    @unittest.skip("removed cover behavior")
    def test_cover_expression_prioritizes_title_event_over_conflicting_later_context(self):
        instruction = bridge.recording_cover_streamer_expression_instruction(
            "YYF",
            "绝地翻盘成功拿下胜利",
            "中途一度看起来可能被翻盘",
        )
        self.assertIn("本段优先表情建议：如释重负后的兴奋", instruction)
        self.assertNotIn("本段优先表情建议：从错愕转为懊恼", instruction)

    @unittest.skip("removed cover behavior")
    def test_cover_expression_does_not_treat_plain_game_end_as_a_loss(self):
        instruction = bridge.recording_cover_streamer_expression_instruction(
            "YYF",
            "游戏结束后进入下一局",
        )
        self.assertIn("本段优先表情建议：专注自然", instruction)
        self.assertNotIn("本段优先表情建议：疲惫无奈", instruction)

    @unittest.skip("removed cover behavior")
    def test_yyf_cover_expression_uses_neutral_fallback_without_result(self):
        instruction = bridge.recording_cover_streamer_expression_instruction(
            "月夜枫",
            "天梯对局精彩内容",
        )
        self.assertIn("本段优先表情建议：专注自然", instruction)

    def test_yyf_has_no_fixed_bundled_reference_instruction(self):
        instruction = bridge.recording_cover_reference_instruction("YYF")
        self.assertIn("主播 YYF 本人", instruction)
        self.assertNotIn("Q 版角色", instruction)
        self.assertNotIn("蓝色鱼形头套", instruction)

    @unittest.skip("removed cover behavior")
    def test_expression_rule_applies_to_every_streamer(self):
        instruction = bridge.recording_cover_streamer_expression_instruction(
            "果小果",
            "关键团极限反杀",
        )
        self.assertIn("果小果 的最终表情必须与标题对应的已核验对局情况联动", instruction)
        self.assertIn("本段优先表情建议：高度专注并带瞬间惊喜", instruction)

    def test_guoxiaoguo_reference_requires_fried_egg_hair_accessory(self):
        instruction = bridge.recording_cover_reference_instruction("果小果")
        self.assertIn("荷包蛋发饰", instruction)
        self.assertIn("不规则白色蛋白", instruction)
        self.assertIn("圆润的金黄色蛋黄", instruction)
        self.assertIn("荷包蛋下方", instruction)
        self.assertIn("红色大蝴蝶结", instruction)
        self.assertIn("绝对不能画成蛋壳", instruction)

    @unittest.skip("removed cover behavior")
    def test_ai_recording_cover_uses_ai_title_and_forbids_time(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            character_base = root / "character-base.png"
            character_base.write_bytes(b"character-base")
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            image_edit = Mock(return_value=response)
            client = types.SimpleNamespace(
                images=types.SimpleNamespace(edit=image_edit, generate=Mock())
            )
            ai_module = types.ModuleType("modules.ai_enhancer")
            ai_module.get_openai_client = Mock(return_value=client)
            config_module = types.ModuleType("modules.config_manager")
            config_module.load_config = Mock(return_value={
                "AI_GENERATE_RECORDING_COVER": True,
                "OPENAI_API_KEY": "global-key",
                "OPENAI_IMAGE_API_KEY": "image-key",
                "OPENAI_BASE_URL": "https://example.com/v1",
                "OPENAI_IMAGE_BASE_URL": "https://images.example.com/v1",
                "OPENAI_IMAGE_MODEL_NAME": "gpt-image-2",
                "OPENAI_IMAGE_SIZE": "1536x1024",
            })
            ffmpeg_commands = []

            def fake_ffmpeg(command, **_kwargs):
                ffmpeg_commands.append(command)
                Path(command[-1]).write_bytes(b"jpeg")
                return types.SimpleNamespace(returncode=0, stderr="")

            with patch.dict(sys.modules, {
                "modules.ai_enhancer": ai_module,
                "modules.config_manager": config_module,
            }), patch.object(bridge.subprocess, "run", side_effect=fake_ffmpeg):
                cover, details = bridge.generate_recording_cover_with_ai(
                    title="【直播回放】土豆｜新地图极限挑战｜弹幕版 07-23 21:30",
                    ai_topic="新地图极限挑战",
                    description="主播挑战新地图，弹幕反应热烈。",
                    streamer="土豆",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(character_base),
                        "ai_cover_prompt": "采用低饱和蓝紫色，并突出 Roshan 团战。",
                    },
                    work_dir=work_dir,
                    target_size=(1920, 1080),
                    output_path=work_dir / "record-only.jpg",
                    cover_text="弹幕版 土豆新地图极限挑战",
                )

        self.assertEqual(cover.name, "record-only.jpg")
        self.assertTrue(details["ai_cover_generated"])
        image_client_config = ai_module.get_openai_client.call_args.args[0]
        self.assertEqual(image_client_config["OPENAI_API_KEY"], "image-key")
        self.assertEqual(
            image_client_config["OPENAI_BASE_URL"],
            "https://images.example.com/v1",
        )
        self.assertEqual(details["ai_cover_headline"], "土豆新地图极限挑战")
        self.assertTrue(details["ai_cover_submission_marker_removed"])
        self.assertEqual(details["ai_cover_subject_name"], "土豆")
        self.assertEqual(details["ai_cover_width"], 1920)
        self.assertEqual(details["ai_cover_height"], 1080)
        self.assertIn(
            "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
            ffmpeg_commands[0],
        )
        prompt = image_edit.call_args.kwargs["prompt"]
        self.assertIn("横向 1920:1080 视频封面", prompt)
        self.assertIn("完整投稿标题中的第一核心事件", prompt)
        self.assertIn("封面短文案：土豆新地图极限挑战", prompt)
        self.assertIn("封面主角称呼：土豆", prompt)
        self.assertIn("不得排成“主角｜主题”", prompt)
        self.assertIn("封面主角身份锁定", prompt)
        self.assertIn("人物外观只能依据随请求上传的封面人物底稿", prompt)
        self.assertIn("Image 1: 当前直播间主播", prompt)
        self.assertIn("Image 1 始终是当前主播的唯一身份来源", prompt)
        self.assertIn("Dota 2 游戏角色消歧规则", prompt)
        self.assertIn("Dota 2 装备规则", prompt)
        self.assertIn("斗鱼 Dota 2 主播昵称规则", prompt)
        self.assertIn("绝对禁止出现日期", prompt)
        self.assertIn("采用低饱和蓝紫色，并突出 Roshan 团战", prompt)
        self.assertNotIn("2026-07-23", prompt)
        self.assertNotIn("弹幕版", prompt)

    @unittest.skip("removed cover behavior")
    def test_empty_custom_cover_prompt_does_not_duplicate_system_defaults(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")

        self.assertIn(
            'custom_cover_style_prompt or "未单独设置；仅遵循以上系统规则。"',
            source,
        )
        self.assertNotIn(
            'cfg.get("ai_cover_prompt") or DEFAULT_RECORDING_COVER_AI_PROMPT',
            source,
        )

    @unittest.skip("removed cover behavior")
    def test_yyf_recording_cover_defaults_to_current_room_avatar(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            avatar = root / "yyf-current-avatar.jpg"
            avatar.write_bytes(b"current-avatar")
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
                bridge,
                "download_recording_avatar_reference",
                return_value=avatar,
            ) as avatar_download:
                cover, details = bridge.generate_recording_cover_with_ai(
                    title="【直播回放】YYF｜天梯翻盘局｜2026-07-24",
                    ai_topic="天梯翻盘局",
                    description=(
                        "YYF进行天梯对局并完成翻盘。最后弹幕顺带提到骇客徐杰，"
                        "并对比老蔡此前的表现。"
                    ),
                    streamer="yyfyyf",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "streamer_avatar_url": "https://example.com/yyf-avatar.jpg",
                    },
                    work_dir=work_dir,
                )

        self.assertEqual(cover.name, "ai_cover.jpg")
        self.assertTrue(details["ai_cover_reference_used"])
        self.assertEqual(details["ai_cover_reference_name"], "YYF")
        self.assertEqual(details["ai_cover_reference_path"], str(avatar))
        image_generate.assert_not_called()
        image_edit.assert_called_once()
        edit_kwargs = image_edit.call_args.kwargs
        self.assertEqual(edit_kwargs["model"], "gpt-image-2")
        self.assertEqual(edit_kwargs["size"], "1536x1024")
        self.assertEqual(edit_kwargs["input_fidelity"], "high")
        self.assertEqual(Path(edit_kwargs["image"].name), avatar)
        self.assertIn("直播间头像", edit_kwargs["prompt"])
        self.assertIn("独立人物底稿", edit_kwargs["prompt"])
        self.assertIn("不得把主播画成英雄 Cos", edit_kwargs["prompt"])
        self.assertIn("不得把头像的脸贴到英雄身体上", edit_kwargs["prompt"])
        self.assertIn("主播头像人物在前景独立出现", edit_kwargs["prompt"])
        self.assertIn("官方英雄在侧后方独立出现", edit_kwargs["prompt"])
        self.assertNotIn("蓝色鱼形头套", edit_kwargs["prompt"])
        self.assertIn("YYF 的最终表情必须与标题对应的已核验对局情况联动", edit_kwargs["prompt"])
        self.assertIn("不得根据零散弹幕猜测胜负", edit_kwargs["prompt"])
        self.assertIn("碾压或连胜", edit_kwargs["prompt"])
        self.assertIn("被翻盘用错愕后的懊恼", edit_kwargs["prompt"])
        self.assertNotIn("骇客徐杰", edit_kwargs["prompt"])
        self.assertNotIn("老蔡", edit_kwargs["prompt"])
        self.assertNotIn("已核验时间线事件", edit_kwargs["prompt"])
        self.assertIn("人物出镜白名单：YYF", edit_kwargs["prompt"])
        self.assertIn("Image 1: 当前直播间主播 YYF", edit_kwargs["prompt"])
        self.assertEqual(details["ai_cover_reference_kind"], "avatar")
        self.assertEqual(details["ai_cover_reference_count"], 1)
        self.assertEqual(details["ai_cover_guest_streamers"], [])
        self.assertEqual(
            details["ai_cover_guest_candidate_source"],
            "submission_title",
        )
        self.assertEqual(
            details["ai_cover_reference_paths"],
            [str(avatar)],
        )
        self.assertEqual(len(details["ai_cover_reference_roles"]), 1)
        self.assertIn("唯一身份来源", details["ai_cover_reference_roles"][0])
        avatar_download.assert_called_once_with(
            "https://example.com/yyf-avatar.jpg",
            ANY,
        )

    @unittest.skip("removed cover behavior")
    def test_recording_cover_rejects_danmaku_only_streamer_hero(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            character_base = root / "character-base.png"
            character_base.write_bytes(b"character-base")
            hero_reference = root / "witch-doctor.png"
            hero_reference.write_bytes(b"hero-reference")
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            image_edit = Mock(return_value=response)
            client = types.SimpleNamespace(
                images=types.SimpleNamespace(edit=image_edit, generate=Mock())
            )
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
            }), patch.object(
                bridge,
                "build_dota2_hero_reference",
                return_value=(
                    hero_reference,
                    types.SimpleNamespace(
                        chinese_name="巫医",
                        english_name="Witch Doctor",
                        icon_slug="witch_doctor",
                    ),
                    "",
                ),
            ), patch.object(bridge.subprocess, "run", side_effect=fake_ffmpeg):
                _, details = bridge.generate_recording_cover_with_ai(
                    title="果小果巫医关键团救场｜08-02 00:08",
                    ai_topic="果小果巫医关键团救场",
                    description="果小果使用巫医在关键团战救场。",
                    streamer="果小果",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(character_base),
                        "douyu_stats_enabled": True,
                        "douyu_stats_cover_context_enabled": True,
                    },
                    work_dir=work_dir,
                    game_context={
                        "hero": "巫医",
                        "items": [],
                        "neutral": "",
                        "identity_source": "xml_dominant_hero_only",
                    },
                    game_context_locked=True,
                )

        prompt = image_edit.call_args.kwargs["prompt"]
        self.assertNotIn("ai_cover_tooltip_hero", details)
        self.assertEqual(details["ai_cover_dota2_source"], "locked_no_match")
        self.assertEqual(
            details["ai_cover_unverified_game_context_rejected"],
            {"hero": "巫医", "identity_source": "xml_dominant_hero_only"},
        )
        self.assertIn("没有可靠匹配到主播同一场对局", prompt)
        self.assertNotIn("主播本局最终六格主装备", prompt)
        edit_image = image_edit.call_args.kwargs["image"]
        self.assertEqual(Path(edit_image.name), character_base)
        self.assertEqual(len(details["ai_cover_reference_roles"]), 1)
        self.assertIn("唯一身份来源", details["ai_cover_reference_roles"][0])
        self.assertNotIn("Image 2: Valve 官方 Dota 2 英雄 巫医 参考", prompt)

    @unittest.skip("removed cover behavior")
    def test_dota2_item_icon_sheet_is_sent_to_image_model(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            item_sheet = root / "dota2-items.png"
            item_sheet.write_bytes(b"official-item-reference")
            character_base = root / "character-base.png"
            character_base.write_bytes(b"character-base")
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            image_edit = Mock(return_value=response)
            client = types.SimpleNamespace(images=types.SimpleNamespace(
                edit=image_edit,
                generate=Mock(),
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
            }), patch.object(
                bridge,
                "build_dota2_item_reference_sheet",
                return_value=(item_sheet, []),
            ), patch.object(bridge.subprocess, "run", side_effect=fake_ffmpeg):
                _, details = bridge.generate_recording_cover_with_ai(
                    title="DOTA2 蓝猫裸BKB后补羊刀",
                    ai_topic="BKB羊刀翻盘",
                    description="蓝猫更新黑皇杖和邪恶镰刀后赢下团战。",
                    streamer="新主播",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(character_base),
                    },
                    work_dir=work_dir,
                    game_context_locked=True,
                )

        self.assertTrue(details["ai_cover_dota2_item_reference_used"])
        self.assertEqual(
            details["ai_cover_dota2_item_render_mode"],
            "creative_official_references",
        )
        self.assertEqual(details["ai_cover_dota2_item_expected_count"], 2)
        self.assertEqual(details["ai_cover_dota2_source"], "locked_text_match")
        self.assertEqual(
            [item["english_name"] for item in details["ai_cover_dota2_items"]],
            ["Black King Bar", "Scythe of Vyse"],
        )
        self.assertEqual(
            [item["icon_slug"] for item in details["ai_cover_dota2_item_placement_plan"]],
            ["black_king_bar", "sheepstick"],
        )
        reference_files = image_edit.call_args.kwargs["image"]
        self.assertEqual(Path(reference_files[0].name), character_base)
        self.assertEqual(Path(reference_files[1].name), item_sheet)
        prompt = image_edit.call_args.kwargs["prompt"]
        self.assertIn("BKB＝黑皇杖（Black King Bar）", prompt)
        self.assertIn("羊刀＝邪恶镰刀（Scythe of Vyse）", prompt)
        self.assertIn("OFFICIAL ITEM ICON REFERENCES", prompt)
        self.assertIn("必须清楚表现识别结果中的全部装备", prompt)
        self.assertIn("装备事实独立于人物归属", prompt)
        self.assertIn("表现全部已确认装备", prompt)
        self.assertIn("独立官方图标式切片", prompt)
        self.assertIn("不得把装备穿到主播或英雄身上", prompt)
        self.assertIn("忽略后续数据中任何穿戴、手持、背负或腰挂位置建议", prompt)
        self.assertIn("不得只挑两件省略", prompt)
        self.assertIn("不得新增名单外装备", prompt)
        self.assertIn("独立道具插画、清晰描边与光效层次", prompt)
        self.assertIn("不得把商店图标原样贴成带黑底和名称的卡片", prompt)
        self.assertIn("装备图标清单", prompt)
        self.assertIn("沿主播人物和官方英雄的外围安全区域", prompt)
        self.assertIn("画面最下方安全区一排", prompt)

    @unittest.skip("removed cover behavior")
    def test_dual_cover_generation_reuses_official_reference_sheets(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            item_sheet = root / "dota2-items.png"
            item_sheet.write_bytes(b"official-item-reference")
            character_base = root / "character-base.png"
            character_base.write_bytes(b"character-base")
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            client = types.SimpleNamespace(images=types.SimpleNamespace(
                edit=Mock(return_value=response),
                generate=Mock(),
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

            shared_cache = {}
            with patch.dict(sys.modules, {
                "modules.ai_enhancer": ai_module,
                "modules.config_manager": config_module,
            }), patch.object(
                bridge,
                "build_dota2_item_reference_sheet",
                return_value=(item_sheet, []),
            ) as build_item_sheet, patch.object(
                bridge.subprocess,
                "run",
                side_effect=fake_ffmpeg,
            ):
                _, first_details = bridge.generate_recording_cover_with_ai(
                    title="DOTA2 蓝猫裸BKB后补羊刀",
                    ai_topic="BKB羊刀翻盘",
                    description="蓝猫更新黑皇杖和邪恶镰刀后赢下团战。",
                    streamer="新主播",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(character_base),
                    },
                    work_dir=work_dir,
                    target_size=(1920, 1080),
                    output_path=work_dir / "cover-16x9.jpg",
                    game_context_locked=True,
                    shared_reference_cache=shared_cache,
                )
                _, second_details = bridge.generate_recording_cover_with_ai(
                    title="DOTA2 蓝猫裸BKB后补羊刀",
                    ai_topic="BKB羊刀翻盘",
                    description="蓝猫更新黑皇杖和邪恶镰刀后赢下团战。",
                    streamer="新主播",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(character_base),
                    },
                    work_dir=work_dir,
                    target_size=(1600, 1200),
                    output_path=work_dir / "cover-4x3.jpg",
                    game_context_locked=True,
                    shared_reference_cache=shared_cache,
                )

        build_item_sheet.assert_called_once()
        self.assertEqual(first_details["ai_cover_shared_reference_cache_hits"], [])
        self.assertIn(
            "dota2_items",
            second_details["ai_cover_shared_reference_cache_hits"],
        )

    @unittest.skip("removed cover behavior")
    def test_cover_visual_matching_ignores_unselected_later_events(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            character_base = root / "character-base.png"
            character_base.write_bytes(b"character-base")
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            client = types.SimpleNamespace(images=types.SimpleNamespace(
                edit=Mock(return_value=response),
                generate=Mock(),
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
            }), patch.object(
                bridge,
                "match_dota2_abilities",
                return_value=[],
            ) as ability_match, patch.object(
                bridge.subprocess,
                "run",
                side_effect=fake_ffmpeg,
            ):
                _, details = bridge.generate_recording_cover_with_ai(
                    title="蓝猫关键团完成翻盘",
                    ai_topic="蓝猫关键团完成翻盘",
                    description=(
                        "00:12 蓝猫关键团完成翻盘。\n"
                        "38:40 卡尔天火命中结束另一局。"
                    ),
                    streamer="新主播",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(character_base),
                    },
                    work_dir=root / "artifacts",
                )

        ability_inputs = "\n".join(str(value) for value in ability_match.call_args.args)
        self.assertNotIn("卡尔天火", ability_inputs)
        self.assertEqual(
            details["ai_cover_visual_fact_scope"],
            "primary_verified_event",
        )
        prompt = client.images.edit.call_args.kwargs["prompt"]
        self.assertIn("先按以下固定顺序完成设计", prompt)
        self.assertIn("忽略简介中其他时间点的英雄、技能、装备和人物", prompt)

    def test_unknown_streamer_uses_room_avatar_as_character_base(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            avatar = root / "avatar.jpg"
            avatar.write_bytes(b"avatar")
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            image_edit = Mock(return_value=response)
            client = types.SimpleNamespace(images=types.SimpleNamespace(
                edit=image_edit,
                generate=Mock(),
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
                _, details = bridge.generate_recording_cover_with_ai(
                    title="【直播回放】新主播｜欢乐游戏｜07-24 11:20",
                    ai_topic="欢乐游戏",
                    description="直播间欢乐游戏。",
                    streamer="新主播",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "streamer_avatar_url": "https://example.com/avatar.jpg",
                    },
                    work_dir=work_dir,
                )

        avatar_download.assert_called_once()
        self.assertEqual(
            avatar_download.call_args.args[0],
            "https://example.com/avatar.jpg",
        )
        self.assertEqual(details["ai_cover_reference_kind"], "avatar")
        self.assertEqual(details["ai_cover_reference_path"], str(avatar))
        self.assertEqual(Path(image_edit.call_args.kwargs["image"].name), avatar)
        self.assertIn("直播间头像", image_edit.call_args.kwargs["prompt"])
        self.assertIn("不要替换成无关人物或角色", image_edit.call_args.kwargs["prompt"])

    def test_unknown_streamer_without_room_avatar_still_stops_cover_generation(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        ai_module = types.ModuleType("modules.ai_enhancer")
        ai_module.get_openai_client = Mock()
        config_module = types.ModuleType("modules.config_manager")
        config_module.load_config = Mock(return_value={
            "AI_GENERATE_RECORDING_COVER": True,
            "OPENAI_API_KEY": "test-key",
        })
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {
            "modules.ai_enhancer": ai_module,
            "modules.config_manager": config_module,
        }):
            with self.assertRaisesRegex(ValueError, "未获取到该直播间头像"):
                bridge.generate_recording_cover_with_ai(
                    title="新主播欢乐游戏",
                    ai_topic="欢乐游戏",
                    description="直播间欢乐游戏。",
                    streamer="新主播",
                    cfg={
                        "_config_dir": temp,
                        "app_root": str(app_root),
                    },
                    work_dir=Path(temp) / "artifacts",
                )

    def test_custom_room_reference_overrides_bundled_streamer_reference(self):
        app_root = Path(bridge.__file__).resolve().parent / "potatoflow-app"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_dir = root / "artifacts"
            custom_reference = root / "custom-yyf.png"
            custom_reference.write_bytes(b"custom-character")
            response = types.SimpleNamespace(data=[
                types.SimpleNamespace(b64_json="aW1hZ2UtYnl0ZXM=", url=None)
            ])
            image_edit = Mock(return_value=response)
            client = types.SimpleNamespace(images=types.SimpleNamespace(
                edit=image_edit,
                generate=Mock(),
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
            }), patch.object(bridge.subprocess, "run", side_effect=fake_ffmpeg):
                _, details = bridge.generate_recording_cover_with_ai(
                    title="YYF｜天梯翻盘局｜07-26 12:00｜【直播回放】",
                    ai_topic="天梯翻盘局",
                    description="YYF进行天梯对局。",
                    streamer="YYF",
                    cfg={
                        "_config_dir": str(root),
                        "app_root": str(app_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(custom_reference),
                    },
                    work_dir=work_dir,
                )

        self.assertEqual(details["ai_cover_reference_kind"], "custom")
        self.assertEqual(details["ai_cover_reference_path"], str(custom_reference))
        self.assertEqual(Path(image_edit.call_args.kwargs["image"].name), custom_reference)
        self.assertIn("用户为主播 YYF 指定的人物形象底稿", image_edit.call_args.kwargs["prompt"])

    def test_yyf_aliases_do_not_use_a_bundled_reference(self):
        for alias in (
            "YYF",
            "yyfyyf",
            "月夜枫",
            "枫哥",
            "峰哥",
            "姜岑",
            "胖头鱼",
            "石佛",
            "僵尸王",
            "毒瘤枫",
        ):
            with self.subTest(alias=alias):
                reference = bridge.recording_cover_reference(alias)
                self.assertIsNone(reference)

    def test_guoxiaoguo_reference_aliases_are_recognized(self):
        self.assertTrue(bridge.GUOXIAOGUO_COVER_REFERENCE.is_file())
        for alias in ("果小果", "果小果是个弟弟", "果小果是个弟弟_直播间"):
            with self.subTest(alias=alias):
                reference = bridge.recording_cover_reference(alias)
                self.assertIsNotNone(reference)
                self.assertEqual(reference[0], "果小果")
                self.assertEqual(reference[1], bridge.GUOXIAOGUO_COVER_REFERENCE)
        instruction = bridge.recording_cover_reference_instruction("果小果")
        self.assertIn("头顶荷包蛋发饰", instruction)
        self.assertIn("绝对不能画成蛋壳", instruction)
        self.assertIn("禁止重绘成另一种动漫脸或改成真人", instruction)

    def test_guomin_dajiuge_reference_aliases_are_recognized(self):
        self.assertTrue(bridge.GUOMIN_DAJIUGE_COVER_REFERENCE.is_file())
        for alias in ("国民大舅哥", "大舅哥", "182102"):
            with self.subTest(alias=alias):
                reference = bridge.recording_cover_reference(alias)
                self.assertIsNotNone(reference)
                self.assertEqual(reference[0], "国民大舅哥")
                self.assertEqual(
                    reference[1],
                    bridge.GUOMIN_DAJIUGE_COVER_REFERENCE,
                )

    def test_xiebin_dd_reference_aliases_are_recognized(self):
        self.assertTrue(bridge.XIEBIN_DD_COVER_REFERENCE.is_file())
        for alias in ("DD", "谢彬DD", "谢彬", "谢斌", "奶哥", "奶D"):
            with self.subTest(alias=alias):
                reference = bridge.recording_cover_reference(alias)
                self.assertIsNotNone(reference)
                self.assertEqual(reference[0], "谢彬DD")
                self.assertEqual(reference[1], bridge.XIEBIN_DD_COVER_REFERENCE)
        instruction = bridge.recording_cover_reference_instruction("谢彬DD")
        self.assertIn("经过裁切的固定人物底稿", instruction)
        self.assertIn("短黑发、脸型、眉眼、鼻唇", instruction)
        self.assertIn("保留底稿的真人原貌与原始画风", instruction)
        self.assertIn("不得动漫化、Q版化、换脸", instruction)

    def test_load_config_rejects_non_object(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaises(ValueError):
                bridge.load_config(path)

    def test_record_only_ass_is_language_tagged_simplified_chinese(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "阿怪MrWeird_茅山后裔_2026-07-26_14-59.flv"
            xml = video.with_suffix(".xml")
            legacy_ass = video.with_suffix(".ass")
            legacy_language_ass = video.with_name(f"{video.stem}.zh-CN.ass")
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">中文弹幕</d></i>',
                encoding="utf-8",
            )
            legacy_ass.write_text("legacy", encoding="utf-8")
            legacy_language_ass.write_text("legacy", encoding="utf-8")

            with patch.object(bridge, "probe_video_size", return_value=(1920, 1080)):
                result = bridge.generate_record_only_ass(
                    video,
                    {"record_only_xml_wait_seconds": 0},
                    [video, xml],
                )

            self.assertEqual(
                result,
                root / "ass" / "阿怪MrWeird_茅山后裔_2026-07-26_14-59.zh-CN.ass",
            )
            self.assertTrue(result.is_file())
            self.assertFalse(legacy_ass.exists())
            self.assertFalse(legacy_language_ass.exists())
            self.assertIn(
                "中文弹幕",
                result.read_text(encoding="utf-8-sig"),
            )

    def test_record_only_empty_xml_does_not_generate_empty_ass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "阿怪MrWeird_茅山后裔_2026-07-26_14-59.flv"
            xml = video.with_suffix(".xml")
            video.write_bytes(b"video")
            xml.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<i>\n</i>\n',
                encoding="utf-8",
            )

            with patch.object(bridge, "probe_video_size") as probe:
                result = bridge.generate_record_only_ass(
                    video,
                    {"record_only_xml_wait_seconds": 0},
                    [video, xml],
                )

            self.assertIsNone(result)
            probe.assert_not_called()
            self.assertFalse(video.with_suffix(".ass").exists())
            self.assertFalse(
                video.with_name(f"{video.stem}.zh-CN.ass").exists()
            )
            self.assertFalse(
                (video.parent / "ass" / f"{video.stem}.zh-CN.ass").exists()
            )

    def test_record_only_empty_xml_marks_ass_failed_and_preserves_video(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "empty-danmaku.flv"
            xml = root / "empty-danmaku.xml"
            state = root / "state.sqlite3"
            config = root / "bridge.config.json"
            video.write_bytes(b"video")
            xml.write_text("<i></i>", encoding="utf-8")
            config.write_text(
                json.dumps({"state_db": str(state)}),
                encoding="utf-8",
            )

            with patch.object(
                bridge,
                "generate_record_only_cover",
            ) as generate_cover:
                result = bridge.main([
                    "--config", str(config),
                    "record-only", "--room-id", "room-1", str(video), str(xml),
                ])

            self.assertEqual(result, 1)
            self.assertTrue(video.is_file())
            generate_cover.assert_not_called()
            with closing(sqlite3.connect(state)) as db, db:
                task = db.execute(
                    "SELECT platform, status, error FROM uploads"
                ).fetchone()
                ass_stage = db.execute(
                    "SELECT status, error FROM upload_stages WHERE stage='ass'"
                ).fetchone()
            self.assertEqual(task[0:2], ("record_only", "failed"))
            self.assertIn("弹幕 XML 为空", task[2])
            self.assertEqual(ass_stage[0], "failed")
            self.assertIn("弹幕 XML 为空", ass_stage[1])

    def test_dry_run_validates_without_importing_app(self):
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

    def test_ass_stage_completes_before_burn_stage_starts(self):
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
                "danmaku_enabled": True,
                "danmaku_burn_in": True,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video, xml)

            def fail_after_checking_ass(*_args, **_kwargs):
                self.assertEqual(store.stage_state(key, "ass")["status"], "completed")
                raise RuntimeError("test burn failure")

            with patch.object(
                bridge,
                "probe_video_size",
                return_value=(1920, 1080),
            ), patch.object(
                bridge,
                "recording_effective_duration_seconds",
                return_value=60.0,
            ), patch.object(
                bridge,
                "burn_ass",
                side_effect=fail_after_checking_ass,
            ):
                self.assertFalse(
                    bridge.upload_one(video, cfg, store, danmaku_xml=xml)
                )

            self.assertEqual(store.stage_state(key, "ass")["status"], "completed")
            self.assertEqual(store.stage_state(key, "burn")["status"], "failed")

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

    def test_default_ai_title_does_not_regenerate_successful_description(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "主播_abcdef2026-07-23_09-00-00.flv"
            xml = root / "主播_abcdef2026-07-23_09-00-00.xml"
            cover = root / "cover.jpg"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": True,
                "ai_danmaku_summary_enabled": True,
                "delete_recording_after_upload": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video, xml)

            with patch.object(
                bridge,
                "generate_danmaku_metadata_with_ai",
                return_value=("简介正文", "直播精彩内容"),
            ) as generate_metadata, patch.object(
                bridge,
                "enhance_recording_metadata",
                return_value=(
                    ["主播", "直播录像"],
                    "65",
                    {
                        "partition_recommendation_enabled": True,
                        "recommended_partition_id": "65",
                        "selected_partition_id": "65",
                        "partition_source": "ai",
                    },
                ),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                side_effect=AssertionError("默认标题不得继续生图"),
            ), patch.object(
                bridge,
                "probe_video_size",
                return_value=(1920, 1080),
            ):
                self.assertFalse(
                    bridge.upload_one(video, cfg, store, danmaku_xml=xml)
                )

            ai_stage = store.stage_state(key, "ai")
            self.assertEqual(generate_metadata.call_count, 1)
            self.assertEqual(ai_stage["status"], "warning")
            self.assertTrue(ai_stage["details"]["continued_with_fallback"])
            self.assertEqual(
                ai_stage["details"]["description_generation_retry_count"],
                0,
            )
            self.assertFalse(
                ai_stage["details"]["description_generation_retries_exhausted"]
            )
            self.assertEqual(ai_stage["details"]["recommended_partition_id"], "65")
            self.assertEqual(ai_stage["details"]["selected_partition_id"], "65")
            self.assertEqual(ai_stage["details"]["partition_source"], "ai")
            self.assertIn("默认标题", ai_stage["error"])
            self.assertIn("继续后续流程", ai_stage["error"])
            self.assertEqual(store.stage_state(key, "cover_16x9")["status"], "warning")
            with store.connect() as db:
                upload_status = db.execute(
                    "SELECT status FROM uploads WHERE fingerprint=?",
                    (key,),
                ).fetchone()["status"]
            self.assertEqual(upload_status, "failed")

    def test_live_room_title_fallback_after_evidence_filter_continues_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "yyfyyf_陪伴每一天_2026-08-01_23-07.flv"
            xml = root / "yyfyyf_陪伴每一天_2026-08-01_23-07.xml"
            cover = root / "cover.jpg"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            cfg = {
                "_config_dir": str(root),
                "streamer_name": "YYF",
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": True,
                "ai_danmaku_summary_enabled": True,
                "delete_recording_after_upload": False,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video, xml)

            with patch.object(
                bridge,
                "generate_danmaku_metadata_with_ai",
                return_value=("简介正文", "YYF虚空假面冰眼出装刮痧"),
            ), patch.object(
                bridge,
                "enhance_recording_metadata",
                return_value=(["YYF", "虚空假面"], "171", {}),
            ), patch.object(
                bridge,
                "filter_unverified_dota2_metadata",
                return_value=(
                    "",
                    "简介正文",
                    ["YYF"],
                    {"unverified_hero_topic_removed": True},
                ),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                side_effect=AssertionError("证据过滤清空标题后不得继续生图"),
            ), patch.object(
                bridge,
                "probe_video_size",
                return_value=(1920, 1080),
            ):
                self.assertFalse(
                    bridge.upload_one(video, cfg, store, danmaku_xml=xml)
                )

            ai_stage = store.stage_state(key, "ai")
            self.assertEqual(ai_stage["status"], "warning")
            self.assertTrue(ai_stage["details"]["continued_with_fallback"])
            self.assertTrue(ai_stage["details"]["title_topic_is_fallback"])
            self.assertEqual(ai_stage["details"]["fallback_title_topic"], "陪伴每一天")
            self.assertTrue(
                ai_stage["details"]["title_topic_rejected_after_evidence_filter"]
            )
            self.assertIn("证据过滤后", ai_stage["error"])
            self.assertEqual(store.stage_state(key, "cover_16x9")["status"], "warning")

    def test_evidence_filter_recovers_title_from_verified_description(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "yyfyyf_陪伴每一天_2026-08-01_23-07.flv"
            xml = root / "yyfyyf_陪伴每一天_2026-08-01_23-07.xml"
            cover = root / "cover.jpg"
            cookie = root / "cookie.json"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            cookie.write_text("[]", encoding="utf-8")
            xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            timeline = (
                "04:01 谢彬在BP阶段吃下大量ban位\n"
                "43:17 弹幕讨论虚空假面冰眼出装刮痧"
            )
            cfg = {
                "_config_dir": str(root),
                "streamer_name": "YYF",
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "bilibili_cookies": str(cookie),
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": True,
                "ai_danmaku_summary_enabled": True,
                "delete_recording_after_upload": False,
            }
            uploaded = []

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **kwargs):
                    uploaded.append(kwargs)
                    return True, {
                        "bvid": "BV1recovered",
                        "aid": 123,
                        "url": "https://www.bilibili.com/video/BV1recovered",
                    }

            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video, xml)
            with patch.object(
                bridge,
                "generate_danmaku_metadata_with_ai",
                return_value=(timeline, "YYF虚空假面冰眼出装刮痧"),
            ), patch.object(
                bridge,
                "enhance_recording_metadata",
                return_value=(["YYF", "DOTA2"], "171", {}),
            ), patch.object(
                bridge,
                "filter_unverified_dota2_metadata",
                return_value=(
                    "",
                    timeline,
                    ["YYF", "DOTA2"],
                    {"unverified_hero_topic_removed": True},
                ),
            ), patch.object(
                bridge,
                "generate_recording_cover_with_ai",
                return_value=(None, {"ai_cover_enabled": False}),
            ), patch.object(
                bridge,
                "import_app",
                return_value=(FakeUploader, None),
            ), patch.object(
                bridge,
                "probe_video_size",
                return_value=(1920, 1080),
            ):
                self.assertTrue(
                    bridge.upload_one(video, cfg, store, danmaku_xml=xml)
                )

            self.assertEqual(
                uploaded[0]["title"],
                "谢彬在BP阶段吃下大量ban位｜08-01 23:07",
            )
            ai_stage = store.stage_state(key, "ai")
            self.assertTrue(
                ai_stage["details"]["title_topic_recovered_from_description"]
            )
            self.assertEqual(
                ai_stage["details"]["title_topic_recovery_source"],
                "verified_description_timeline",
            )

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
            self.assertEqual(stages["cover_16x9"]["status"], "failed")
            self.assertIn("broken frames", stages["cover_16x9"]["error"])
            self.assertEqual(stages["ass"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
