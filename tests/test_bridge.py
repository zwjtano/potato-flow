import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, Mock, patch

import bridge


class BridgeTests(unittest.TestCase):
    def test_default_title_prompt_integrates_subject_without_label_prefix(self):
        prompt = bridge.DEFAULT_RECORDING_TITLE_AI_PROMPT

        self.assertIn("主语不必放在最前", prompt)
        self.assertIn("主播名｜事件", prompt)
        self.assertIn("必须同时进入重要时间点", prompt)

    def test_upload_pipeline_persists_duration_before_optional_ass_stage(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        self.assertIn('"video_duration_seconds": recording_duration_seconds', source)
        self.assertIn('recording_duration_seconds = video_duration_seconds(', source)

    def test_default_cover_prompt_requires_official_dota2_item_references(self):
        prompt = bridge.DEFAULT_RECORDING_COVER_AI_PROMPT

        self.assertIn("Valve 官方装备图标参考", prompt)
        self.assertIn("缺少官方参考时不得表现具体装备", prompt)
        self.assertIn("禁止自绘或仿冒装备图标", prompt)
        self.assertIn("封面核心文案必须自然包含主角名", prompt)
        self.assertIn("封面人物底稿", prompt)
        self.assertIn("不得替换主角或混合人脸", prompt)

    def test_recording_tags_dedupe_repeated_streamer_aliases(self):
        self.assertEqual(
            bridge.dedupe_recording_tags(["yyfyyf", "YYF", "直播回放"]),
            ["yyfyyf", "直播回放"],
        )

    def test_unverified_dota_hero_metadata_is_removed(self):
        topic, description, tags, details = bridge.filter_unverified_dota2_metadata(
            "死灵法师翻盘",
            "弹幕热议输赢。影魔最终六神装。\n观众讨论赛后复盘。",
            ["YYF", "影魔", "DOTA2"],
        )

        self.assertEqual(topic, "")
        self.assertEqual(description, "弹幕热议输赢。\n观众讨论赛后复盘。")
        self.assertEqual(tags, ["YYF", "DOTA2"])
        self.assertEqual(details["unverified_hero_tags_removed"], ["影魔"])

    def test_live_stats_are_placed_after_archive_description(self):
        description = bridge.append_live_stats_to_description(
            "直播录播：YYF《休赛期改名狂欢》。",
            "【直播信息】\n峰值人气：123 万",
        )

        self.assertEqual(
            description,
            "【直播信息】\n峰值人气：123 万",
        )

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

        self.assertEqual(description, f"直播录播正文\n\n{stats}")
        self.assertEqual(description.count("——— 直播数据 ———"), 1)

    def test_existing_duplicate_live_stats_are_collapsed_on_retry(self):
        stats = "——— 直播数据 ———\n🎁 狂欢飞机×2(200元)｜合计 200元\n👥 在线 8257~10000"
        duplicated = f"{stats}\n\n{stats}\n\n直播录播正文"

        description = bridge.append_live_stats_to_description(duplicated, stats)

        self.assertEqual(description, f"直播录播正文\n\n{stats}")
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
            "录播前缀\n\n正文\n\n重要时间点\n21:00 弹幕质疑 BP 顺位",
        )
        self.assertNotIn("00:21", description)

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
                "timeline": [{
                    "event": f"事件{index}",
                    "evidence_texts": [f"事件证据{index}"],
                    "evidence_keywords": [f"证据{index}"],
                } for index in range(2, 9)],
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
        self.assertEqual(diagnostics["timeline_shortfall"], 3)
        self.assertEqual(diagnostics["timeline_anchor_policy"], "exact_xml_evidence")
        self.assertEqual(diagnostics["timeline_cluster_window_seconds"], 30)

    def test_generic_recording_intro_is_removed_from_final_body(self):
        stats = "——— 直播数据 ———\n👥 在线 8257~10000"
        description = bridge.append_live_stats_to_description(
            "直播录播：YYF。正文从这里开始。",
            stats,
        )

        self.assertEqual(description, f"正文从这里开始。\n\n{stats}")
        self.assertNotIn("直播录播：YYF。", description)

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

    def test_timeline_target_scales_with_recording_duration(self):
        self.assertEqual(bridge.timeline_target_range(None), (4, 8))
        self.assertEqual(bridge.timeline_target_range(1800), (2, 6))
        self.assertEqual(bridge.timeline_target_range(3600), (4, 8))
        self.assertEqual(bridge.timeline_target_range(7200), (8, 12))

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
            comments = bridge.parse_biliup_xml(xml)

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
        self.assertIn("内容充实", prompt)
        self.assertIn("完整 XML", prompt)
        self.assertIn("不要在简介正文中手写时间点", prompt)
        self.assertIn("不得编造时间或事件", prompt)
        self.assertIn("按弹幕内容随直播时间的变化向前推进", prompt)
        self.assertIn("不要为了突出标题而打乱实际顺序", prompt)
        self.assertIn("赛后复盘", prompt)
        self.assertIn("重要时间点必须覆盖标题的核心事件", prompt)
        self.assertIn("事件文案只做证据的最小忠实改写", prompt)

    def test_timeline_prompt_is_generic_and_only_adds_game_events_conditionally(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")

        self.assertIn("适用于所有直播类型", source)
        self.assertIn("只有输入明确属于", source)
        self.assertIn("不得把聊天、访谈", source)
        self.assertIn("streamer_identity", source)
        self.assertIn("其他主播、选手或嘉宾", source)
        self.assertIn("谁做了什么", source)
        self.assertIn("绝不能用关键词去搜索更早", source)
        self.assertIn("必须先从有精确证据的 timeline 事件中选择 title_topic", source)
        self.assertIn("两个先后发生的独立转折", source)
        self.assertIn("不得只收录铺垫或次要事件而遗漏标题落点", source)
        self.assertIn("每条 event 必须是 evidence_texts 的最小忠实改写", source)
        self.assertIn("一条像总结稿的超长弹幕不能独自支撑", source)
        self.assertIn("开场承接", source)

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
        self.assertEqual(title, "妮可罗宾｜中韩流行歌单·点歌闲聊｜07-23 09:45")

    def test_default_recording_title_falls_back_to_live_title(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "主播_abcdef2026-07-23_09-45-06_深夜歌回.flv"
            video.write_bytes(b"video")
            title, _, _ = bridge.render_metadata(
                video,
                {"title_template": bridge.DEFAULT_TITLE_TEMPLATE},
            )
        self.assertEqual(title, "主播｜深夜歌回｜07-23 09:45")

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

        self.assertEqual(title, "果小果｜凤凰翻盘｜07-24 13:00")
        self.assertEqual(
            bridge.recording_part_title(video, 1, "凤凰翻盘"),
            "13:00 凤凰翻盘",
        )

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
            with sqlite3.connect(state) as db:
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
            ass = video.with_name(f"{video.stem}.zh-CN.ass")
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
            with sqlite3.connect(state) as db:
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

    def test_record_only_failed_cover_is_visible_and_preserves_flv(self):
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
            ):
                result = bridge.main([
                    "--config", str(config),
                    "record-only", "--room-id", "room-1", str(video), str(xml),
                ])

            self.assertEqual(result, 1)
            self.assertTrue(video.is_file())
            with sqlite3.connect(state) as db:
                task = db.execute("SELECT platform, status, error FROM uploads").fetchone()
                cover_stage = db.execute(
                    "SELECT status, error FROM upload_stages WHERE stage='cover'"
                ).fetchone()
            self.assertEqual(task[0:2], ("record_only", "failed"))
            self.assertIn("图片模型不可用", task[2])
            self.assertEqual(cover_stage[0], "failed")
            self.assertIn("图片模型不可用", cover_stage[1])

    def test_record_only_retry_reuses_completed_burn_when_cover_failed(self):
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
            cover = video.with_suffix(".jpg")
            final_video = video.with_suffix(".mp4")

            def finish_burn(*_args, **_kwargs):
                burned.write_bytes(b"already-burned-video")
                return burned

            def finish_cover(_video, _cfg):
                cover.write_bytes(b"cover")
                return cover

            with patch.object(bridge, "probe_video_size", return_value=(1280, 720)), \
                    patch.object(bridge, "burn_ass", side_effect=finish_burn) as burn, \
                    patch.object(
                        bridge,
                        "generate_record_only_cover",
                        side_effect=[RuntimeError("图片模型不可用"), finish_cover(video, {})],
                    ) as generate_cover, patch.object(
                        bridge,
                        "remux_record_only_flv_with_cover",
                        return_value=final_video,
                    ) as remux:
                first = bridge.main([
                    "--config", str(config),
                    "record-only", "--room-id", "room-1", str(video), str(xml),
                ])
                self.assertEqual(first, 1)
                self.assertTrue(burned.is_file())
                with sqlite3.connect(state) as db:
                    exclusions = db.execute(
                        "SELECT video_path FROM recording_exclusions ORDER BY video_path"
                    ).fetchall()
                self.assertIn((str(burned.resolve()),), exclusions)

                second = bridge.main([
                    "--config", str(config),
                    "record-only", "--room-id", "room-1", str(video), str(xml),
                ])

            self.assertEqual(second, 0)
            self.assertEqual(burn.call_count, 1)
            self.assertEqual(generate_cover.call_count, 2)
            self.assertEqual(remux.call_args.args[0], burned)
            with sqlite3.connect(state) as db:
                task = db.execute(
                    "SELECT status, attempts FROM uploads"
                ).fetchone()
                ass_details = json.loads(db.execute(
                    "SELECT details_json FROM upload_stages WHERE stage='ass'"
                ).fetchone()[0])
                burn_details = json.loads(db.execute(
                    "SELECT details_json FROM upload_stages WHERE stage='burn'"
                ).fetchone()[0])
            self.assertEqual(task, ("completed", 2))
            self.assertTrue(ass_details["reused_on_retry"])
            self.assertTrue(burn_details["reused_on_retry"])

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
                    "cover_16x9", "cover_4x3", "upload", "cleanup",
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
            ), patch.object(bridge, "import_y2a", return_value=(MustNotUpload, None)):
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
                "import_y2a",
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
                bridge, "import_y2a", return_value=(FakeUploader, None)
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

    def test_recording_cover_hero_must_match_reviewed_title(self):
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
        self.assertTrue(
            bridge.recording_cover_has_dota2_context(
                "yyfyyf",
                "对局复盘",
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
            "叫我老陈就好了": "川神",
            "老菜": "川神",
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

    def test_guest_avatar_uses_unique_exact_douyu_search_result(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
        if str(y2a_root) not in sys.path:
            sys.path.insert(0, str(y2a_root))
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
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
        if str(y2a_root) not in sys.path:
            sys.path.insert(0, str(y2a_root))
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

    def test_yyf_cover_expression_follows_segment_performance(self):
        instruction = bridge.recording_cover_streamer_expression_instruction(
            "枫哥",
            "蓝猫关键失误后惨遭翻盘",
            "YYF 最后一波团战无奈落败。",
        )
        self.assertIn("YYF 表情与本段对局联动", instruction)
        self.assertIn("失误、被翻盘或惨败", instruction)
        self.assertIn("震惊、懊恼、无奈或气急", instruction)
        self.assertIn("必须保持该 Q 版角色的脸型、五官比例", instruction)
        self.assertIn("蓝色鱼形头套", instruction)
        self.assertIn("不能换脸、真人化", instruction)
        self.assertIn("不能仅照抄底稿中的原始表情", instruction)

    def test_yyf_reference_requires_fixed_fish_hat_character(self):
        instruction = bridge.recording_cover_reference_instruction("YYF")
        self.assertIn("唯一固定 Q 版角色形象", instruction)
        self.assertIn("右侧脸颊小痣", instruction)
        self.assertIn("黑红色连帽外套", instruction)
        self.assertIn("胸前红色 YYF 字样", instruction)
        self.assertIn("蓝色鱼形头套", instruction)
        self.assertIn("头套顶部有提环和鱼鳍", instruction)
        self.assertIn("禁止改成真人", instruction)

    def test_expression_rule_is_not_forced_on_other_streamers(self):
        self.assertEqual(
            bridge.recording_cover_streamer_expression_instruction(
                "果小果",
                "关键团战翻盘",
            ),
            "",
        )

    def test_guoxiaoguo_reference_requires_fried_egg_hair_accessory(self):
        instruction = bridge.recording_cover_reference_instruction("果小果")
        self.assertIn("荷包蛋发饰", instruction)
        self.assertIn("不规则白色蛋白", instruction)
        self.assertIn("圆润的金黄色蛋黄", instruction)
        self.assertIn("荷包蛋下方", instruction)
        self.assertIn("红色大蝴蝶结", instruction)
        self.assertIn("绝对不能画成蛋壳", instruction)

    def test_ai_recording_cover_uses_ai_title_and_forbids_time(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
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
                    title="【直播回放】土豆｜新地图极限挑战｜2026-07-23",
                    ai_topic="新地图极限挑战",
                    description="主播挑战新地图，弹幕反应热烈。",
                    streamer="土豆",
                    cfg={
                        "_config_dir": str(root),
                        "y2a_root": str(y2a_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(character_base),
                        "ai_cover_prompt": "采用低饱和蓝紫色，并突出 Roshan 团战。",
                    },
                    work_dir=work_dir,
                    target_size=(1920, 1080),
                    output_path=work_dir / "record-only.jpg",
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
        self.assertEqual(details["ai_cover_subject_name"], "土豆")
        self.assertEqual(details["ai_cover_width"], 1920)
        self.assertEqual(details["ai_cover_height"], 1080)
        self.assertIn(
            "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
            ffmpeg_commands[0],
        )
        prompt = image_edit.call_args.kwargs["prompt"]
        self.assertIn("横向 1920:1080 视频封面", prompt)
        self.assertIn("AI 生成的核心标题：土豆新地图极限挑战", prompt)
        self.assertIn("封面主角称呼：土豆", prompt)
        self.assertIn("不得排成“主角｜主题”", prompt)
        self.assertIn("封面主角身份锁定", prompt)
        self.assertIn("人物外观只能依据随请求上传的封面人物底稿", prompt)
        self.assertIn("Dota 2 游戏角色消歧规则", prompt)
        self.assertIn("Dota 2 装备规则", prompt)
        self.assertIn("斗鱼 Dota 2 主播昵称规则", prompt)
        self.assertIn("绝对禁止出现日期", prompt)
        self.assertIn("采用低饱和蓝紫色，并突出 Roshan 团战", prompt)
        self.assertNotIn("2026-07-23", prompt)

    def test_yyf_recording_cover_uses_identity_reference_image(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
        self.assertTrue(bridge.YYF_COVER_REFERENCE.is_file())
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
        self.assertIn("唯一固定 Q 版角色形象", edit_kwargs["prompt"])
        self.assertIn("蓝色鱼形头套", edit_kwargs["prompt"])
        self.assertIn("右侧脸颊小痣", edit_kwargs["prompt"])
        self.assertIn("YYF 表情与本段对局联动", edit_kwargs["prompt"])
        self.assertIn("优势、高光或连胜", edit_kwargs["prompt"])
        self.assertIn("失误、被翻盘或惨败", edit_kwargs["prompt"])
        self.assertEqual(details["ai_cover_reference_kind"], "dedicated")
        self.assertEqual(details["ai_cover_reference_count"], 1)
        self.assertEqual(
            details["ai_cover_reference_paths"],
            [str(bridge.YYF_COVER_REFERENCE)],
        )
        avatar_download.assert_not_called()

    def test_dota2_item_icon_sheet_is_sent_to_image_model(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
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
                        "y2a_root": str(y2a_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(character_base),
                    },
                    work_dir=work_dir,
                )

        self.assertTrue(details["ai_cover_dota2_item_reference_used"])
        self.assertEqual(
            [item["english_name"] for item in details["ai_cover_dota2_items"]],
            ["Black King Bar", "Scythe of Vyse"],
        )
        reference_files = image_edit.call_args.kwargs["image"]
        self.assertEqual(Path(reference_files[0].name), character_base)
        self.assertEqual(Path(reference_files[1].name), item_sheet)
        prompt = image_edit.call_args.kwargs["prompt"]
        self.assertIn("BKB＝黑皇杖（Black King Bar）", prompt)
        self.assertIn("羊刀＝邪恶镰刀（Scythe of Vyse）", prompt)
        self.assertIn("OFFICIAL ITEM ICON REFERENCES", prompt)
        self.assertIn("禁止在封面底部或任何位置生成物品栏", prompt)
        self.assertIn("不得绘制仿冒的装备图标", prompt)

    def test_unknown_streamer_uses_room_avatar_as_character_base(self):
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
                        "y2a_root": str(y2a_root),
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
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
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
                        "y2a_root": str(y2a_root),
                    },
                    work_dir=Path(temp) / "artifacts",
                )

    def test_custom_room_reference_overrides_bundled_streamer_reference(self):
        y2a_root = Path(bridge.__file__).resolve().parent / "y2a-auto"
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
                        "y2a_root": str(y2a_root),
                        "ffmpeg": "ffmpeg",
                        "cover_reference_path": str(custom_reference),
                    },
                    work_dir=work_dir,
                )

        self.assertEqual(details["ai_cover_reference_kind"], "custom")
        self.assertEqual(details["ai_cover_reference_path"], str(custom_reference))
        self.assertEqual(Path(image_edit.call_args.kwargs["image"].name), custom_reference)
        self.assertIn("用户为主播 YYF 指定的人物形象底稿", image_edit.call_args.kwargs["prompt"])

    def test_yyf_reference_aliases_are_recognized(self):
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
        self.assertIn("头顶荷包蛋发饰", instruction)
        self.assertIn("绝对不能画成蛋壳", instruction)
        self.assertIn("禁止改成真人", instruction)

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
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">中文弹幕</d></i>',
                encoding="utf-8",
            )
            legacy_ass.write_text("legacy", encoding="utf-8")

            with patch.object(bridge, "probe_video_size", return_value=(1920, 1080)):
                result = bridge.generate_record_only_ass(
                    video,
                    {"record_only_xml_wait_seconds": 0},
                    [video, xml],
                )

            self.assertEqual(
                result,
                root / "阿怪MrWeird_茅山后裔_2026-07-26_14-59.zh-CN.ass",
            )
            self.assertTrue(result.is_file())
            self.assertFalse(legacy_ass.exists())
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
            with sqlite3.connect(state) as db:
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
            self.assertEqual(stages["cover_16x9"]["status"], "failed")
            self.assertIn("broken frames", stages["cover_16x9"]["error"])
            self.assertEqual(stages["ass"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
