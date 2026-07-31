import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
Y2A_ROOT = ROOT / "y2a-auto"
if str(Y2A_ROOT) not in sys.path:
    sys.path.insert(0, str(Y2A_ROOT))

from modules import douyu_stats_daemon as daemon
from modules import douyu_stats_formatter as formatter
from modules import path_policy


def player(hero_id, hero, items=None):
    return {
        "id": str(hero_id),
        "hero": hero,
        "items": list(items or []),
        "neutral": "",
        "scepter": False,
        "shard": False,
    }


class DouyuStatsTests(unittest.TestCase):
    def test_packet_decoder_retains_fragmented_tail(self):
        first = daemon.encode_packet("type@=dgb/gfid@=1/")
        second = daemon.encode_packet("type@=oni/un@=100/")
        split = len(first) + 5
        messages, pending = daemon.decode_packets((first + second)[:split])
        self.assertEqual([item[0] for item in messages], ["dgb"])
        self.assertTrue(pending)
        messages, pending = daemon.decode_packets(pending + (first + second)[split:])
        self.assertEqual([item[0] for item in messages], ["oni"])
        self.assertEqual(pending, b"")

    def test_disabled_global_switch_stops_room_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "pipeline.json"
            config.write_text(json.dumps({
                "douyu_stats_enabled": False,
                "profiles": [{
                    "source_url": "https://www.douyu.com/9999",
                    "streamer_name": "主播",
                }],
            }), encoding="utf-8")
            previous = daemon.BRIDGE_CONFIG
            daemon.BRIDGE_CONFIG = str(config)
            try:
                self.assertEqual(daemon.load_streamers_from_config(), [])
                config.write_text(json.dumps({
                    "douyu_stats_enabled": True,
                    "profiles": [{
                        "source_url": "https://www.douyu.com/9999",
                        "streamer_name": "主播",
                    }],
                }), encoding="utf-8")
                self.assertEqual(
                    daemon.load_streamers_from_config(),
                    [{"room_id": "9999", "streamer": "主播"}],
                )
            finally:
                daemon.BRIDGE_CONFIG = previous

    def test_tooltips_require_ten_nonzero_players_and_stable_transition(self):
        daemon.dota_hero_map = {str(index): f"英雄{index}" for index in range(1, 21)}
        monitor = daemon.RoomMonitor("9999", "主播", {})

        invalid = {"top": [{"id": "0"}], "bottom": []}
        monitor.handle_tooltips({"content": json.dumps(invalid)})
        self.assertIsNone(monitor.state["active_game"])
        self.assertEqual(monitor.state["tooltip_diagnostics"]["invalid_snapshots"], 1)
        self.assertEqual(monitor.state["tooltip_diagnostics"]["last_raw_player_count"], 1)

        first = {
            "top": [{"id": str(index), "items": []} for index in range(1, 6)],
            "bottom": [{"id": str(index), "items": []} for index in range(6, 11)],
        }
        for _ in range(daemon.STABLE_SNAPSHOT_COUNT - 1):
            monitor.handle_tooltips({"content": json.dumps(first)})
        self.assertIsNone(monitor.state["active_game"])
        monitor.handle_tooltips({"content": json.dumps(first)})
        self.assertIsNotNone(monitor.state["active_game"])
        self.assertEqual(monitor.state["games"], [])
        self.assertEqual(
            monitor.state["tooltip_diagnostics"]["valid_snapshots"],
            daemon.STABLE_SNAPSHOT_COUNT,
        )

        second = {
            "top": [{"id": str(index), "items": []} for index in range(11, 16)],
            "bottom": [{"id": str(index), "items": []} for index in range(16, 21)],
        }
        for _ in range(daemon.STABLE_SNAPSHOT_COUNT):
            monitor.handle_tooltips({"content": json.dumps(second)})
        self.assertEqual(len(monitor.state["games"]), 1)

    def test_current_douyu_top_only_lineup_and_http_poll_are_supported(self):
        daemon.dota_hero_map = {str(index): f"英雄{index}" for index in range(1, 11)}
        monitor = daemon.RoomMonitor("74960", "主播", {})
        payload = {
            "status": 1,
            "timestamp": 123,
            "top": [{"id": index, "items": []} for index in range(1, 11)],
        }
        with mock.patch.object(
            daemon,
            "_request_json",
            return_value={"error": 0, "data": payload},
        ):
            for _ in range(daemon.STABLE_SNAPSHOT_COUNT):
                monitor.poll_dota2_data()

        self.assertIsNotNone(monitor.state["active_game"])
        diagnostics = monitor.state["tooltip_diagnostics"]
        self.assertEqual(diagnostics["messages"], 0)
        self.assertEqual(diagnostics["http_polls"], daemon.STABLE_SNAPSHOT_COUNT)
        self.assertEqual(diagnostics["http_snapshots"], daemon.STABLE_SNAPSHOT_COUNT)
        self.assertEqual(diagnostics["last_raw_player_count"], 10)
        self.assertEqual(diagnostics["last_source"], "http")

    def test_final_snapshot_keeps_only_six_main_inventory_slots(self):
        daemon.dota_hero_map = {str(index): f"英雄{index}" for index in range(1, 11)}
        daemon.dota_item_map = {str(index): f"装备{index}" for index in range(1, 20)}
        monitor = daemon.RoomMonitor("74960", "主播", {})

        def snapshot(item_start):
            return {
                "top": [
                    {
                        "id": index,
                        "items": list(range(item_start, item_start + 8)) if index == 1 else [],
                    }
                    for index in range(1, 11)
                ],
            }

        for _ in range(daemon.STABLE_SNAPSHOT_COUNT):
            monitor.handle_dota2_snapshot(snapshot(1), "http")
        monitor.handle_dota2_snapshot(snapshot(9), "type_tooltips")

        active = monitor.state["active_game"]
        self.assertEqual(
            active["players"][0]["items"],
            ["装备9", "装备10", "装备11", "装备12", "装备13", "装备14"],
        )

    def test_gsi_streamer_hero_is_authoritative_and_captures_kda(self):
        daemon.dota_hero_map = {str(index): f"英雄{index}" for index in range(1, 11)}
        daemon.dota_item_map = {str(index): f"装备{index}" for index in range(1, 10)}
        monitor = daemon.RoomMonitor("74960", "主播", {})
        payload = {
            "top": [{"id": index, "items": []} for index in range(1, 11)],
            "hero": {
                "id": 7,
                "items": list(range(1, 9)),
                "kills": 12,
                "deaths": 3,
                "assists": 9,
            },
        }
        for _ in range(daemon.STABLE_SNAPSHOT_COUNT):
            monitor.handle_dota2_snapshot(payload, "http")

        game = monitor.state["active_game"]
        self.assertEqual(game["anchor_player"]["hero"], "英雄7")
        self.assertEqual(game["anchor_player"]["items"], [f"装备{i}" for i in range(1, 7)])
        self.assertEqual(game["anchor_player"]["kda"], 7.0)
        self.assertEqual(len(game["anchor_history"]), 1)

    def test_item_mapping_accepts_current_douyu_internal_keys(self):
        daemon.dota_hero_map = {"7": "英雄7"}
        daemon.dota_item_map = {
            "item_phase_boots": "相位鞋",
            "50": "相位鞋",
            "item_urn_of_shadows": "影之灵龛",
        }
        parsed = daemon.RoomMonitor._player_from_raw({
            "id": 7,
            "items": ["item_phase_boots", "item_urn_of_shadows"],
            "neutral": "50",
        })
        self.assertEqual(parsed["items"], ["相位鞋", "影之灵龛"])
        self.assertEqual(parsed["neutral"], "相位鞋")

        daemon.dota_item_map = {"item_poor_mans_shield": "item_poor_mans_shield"}
        parsed = daemon.RoomMonitor._player_from_raw({
            "id": 7,
            "items": ["item_consecrated_wraps"],
            "neutral": "item_poor_mans_shield",
        })
        self.assertEqual(parsed["items"], ["Consecrated Wraps"])
        self.assertEqual(parsed["neutral"], "Poor Mans Shield")

    def test_gsi_hero_not_in_lineup_is_rejected(self):
        daemon.dota_hero_map = {str(index): f"英雄{index}" for index in range(1, 12)}
        monitor = daemon.RoomMonitor("74960", "主播", {})
        payload = {
            "top": [{"id": index, "items": []} for index in range(1, 11)],
            "hero": {"id": 11, "items": []},
        }
        for _ in range(daemon.STABLE_SNAPSHOT_COUNT):
            monitor.handle_dota2_snapshot(payload, "http")
        self.assertNotIn("anchor_player", monitor.state["active_game"])

    def test_formatter_uses_last_gsi_snapshot_inside_recording(self):
        game = {
            "start_unix_ts": 80,
            "last_seen_unix_ts": 250,
            "players": [player(11, "影魔"), player(22, "宙斯")],
            "anchor_history": [
                {
                    "start_unix_ts": 90,
                    "last_seen_unix_ts": 180,
                    "source": "http",
                    "player": player(11, "影魔", ["黑皇杖"]),
                },
                {
                    "start_unix_ts": 220,
                    "last_seen_unix_ts": 250,
                    "source": "http",
                    "player": player(22, "宙斯", ["刷新球"]),
                },
            ],
        }
        anchor = formatter.select_streamer_player(game, [], 100, 200)
        self.assertEqual(anchor["hero"], "影魔")
        self.assertEqual(anchor["equipment_snapshot_unix_ts"], 180)
        self.assertEqual(anchor["identity_source"], "gsi_hero:http")

    def test_formatter_appends_kda_only_when_source_provides_it(self):
        anchor = player(11, "影魔", ["黑皇杖"])
        anchor.update({"kills": 12, "deaths": 3, "assists": 9, "kda": 7.0})
        stats = {"games": [{
            "start_unix_ts": 100,
            "end_unix_ts": 200,
            "anchor_history": [{
                "start_unix_ts": 100,
                "last_seen_unix_ts": 200,
                "source": "http",
                "player": anchor,
            }],
        }]}
        text = formatter.format_stats(stats, 100, 200, [])
        self.assertIn("K/D/A 12/3/9 KDA 7.0", text)

        del anchor["kills"], anchor["deaths"], anchor["assists"], anchor["kda"]
        text = formatter.format_stats(stats, 100, 200, [])
        self.assertNotIn("K/D/A", text)

    def test_cover_identity_returns_final_equipment_snapshot_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "主播_直播_1970-01-01_00-01"
            session.mkdir()
            (session / "recording.xml").write_text(
                """<?xml version="1.0"?><i>
                <d p="1,1,25,1,100,0,1,0">影魔六神了</d>
                <d p="2,1,25,1,150,0,2,0">影魔装备成型</d>
                </i>""",
                encoding="utf-8",
            )
            metadata = root / ".potato-flow"
            metadata.mkdir()
            (metadata / "douyu-stats.json").write_text(
                json.dumps({"active_game": {
                    "start_unix_ts": 90,
                    "last_seen_unix_ts": 160,
                    "players": [player(11, "影魔", [f"装备{i}" for i in range(1, 9)])],
                }}, ensure_ascii=False),
                encoding="utf-8",
            )

            anchor = formatter.get_game_for_cover(session)

            self.assertEqual(anchor["items"], [f"装备{i}" for i in range(1, 7)])
            self.assertEqual(anchor["equipment_snapshot_unix_ts"], 160)

    def test_formatter_uses_xml_timeframe_and_xml_hero_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "主播_直播_1970-01-01_00-01"
            session.mkdir()
            (session / "recording.xml").write_text(
                """<?xml version="1.0"?><i>
                <d p="1,1,25,1,100,0,1,0">影魔这把很肥</d>
                <d p="2,1,25,1,150,0,2,0">影魔要出黑皇杖了</d>
                <d p="3,1,25,1,200,0,3,0">漂亮</d>
                </i>""",
                encoding="utf-8",
            )
            stats = {
                "gift_events": [
                    {"unix_ts": 90, "name": "火箭", "unit_price": 500, "count": 1, "total_value": 500},
                    {"unix_ts": 120, "name": "飞机", "unit_price": 100, "count": 2, "total_value": 200},
                ],
                "high_energy": {"details": [
                    {"unix_ts": 160, "amount": 300},
                    {"unix_ts": 210, "amount": 900},
                ]},
                "online_samples": [
                    {"unix_ts": 110, "value": 1000},
                    {"unix_ts": 190, "value": 1500},
                ],
                "games": [{
                    "start_unix_ts": 100,
                    "end_unix_ts": 200,
                    "players": [
                        player(11, "影魔", ["黑皇杖"]),
                        player(22, "宙斯"),
                    ],
                }],
            }
            metadata = root / ".potato-flow"
            metadata.mkdir()
            (metadata / "douyu-stats.json").write_text(
                json.dumps(stats, ensure_ascii=False), encoding="utf-8"
            )

            text = formatter.get_stats_for_description(session)
            self.assertIn("飞机×2(200元)", text)
            self.assertNotIn("火箭", text)
            self.assertIn("高能弹幕 ×1 | 300元", text)
            self.assertIn("在线 1000~1500", text)
            self.assertIn("影魔｜六格：黑皇杖", text)

            anchor = formatter.get_game_for_cover(session)
            self.assertEqual(anchor["hero"], "影魔")

    def test_formatter_omits_games_without_equipment_and_separates_categories(self):
        stats = {
            "online_samples": [{"unix_ts": 150, "value": 1352}],
            "games": [
                {
                    "start_unix_ts": 100,
                    "end_unix_ts": 200,
                    "anchor_player": player(35, "狙击手"),
                    "anchor_last_seen_unix_ts": 180,
                },
                {
                    "start_unix_ts": 200,
                    "end_unix_ts": 300,
                    "anchor_player": {
                        **player(2, "斧王", ["闪烁匕首", "刃甲", "相位鞋"]),
                        "neutral": "Rattlecage",
                        "shard": True,
                    },
                    "anchor_last_seen_unix_ts": 280,
                },
            ],
        }

        text = formatter.format_stats(stats, 100, 300)

        self.assertNotIn("狙击手", text)
        self.assertIn(
            "🎮 斧王｜六格：闪烁匕首、刃甲、相位鞋｜中立：Rattlecage｜魔晶",
            text,
        )
        self.assertNotIn("狙击手 | 斧王", text)

    def test_empty_item_placeholders_are_not_persisted_or_formatted(self):
        daemon.dota_item_map = {}
        parsed = daemon.RoomMonitor._player_from_raw({
            "id": 2,
            "items": ["empty", "item_empty", "0", "item_phase_boots"],
        })
        self.assertEqual(parsed["items"], ["Phase Boots"])

    def test_formatter_does_not_guess_anchor_without_xml_evidence(self):
        stats = {
            "games": [{
                "start_unix_ts": 100,
                "end_unix_ts": 200,
                "players": [player(11, "影魔"), player(22, "宙斯")],
            }]
        }
        text = formatter.format_stats(stats, 100, 200, [(150, "今天打得很好")])
        self.assertEqual(text, "")

    def test_formatter_recovers_stats_for_legacy_misplaced_recording(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recordings_root = root / "recordings"
            room = recordings_root / "主播"
            metadata = room / ".potato-flow"
            metadata.mkdir(parents=True)
            legacy_session = root / "runtime" / "data" / "recordings" / "主播" / "场次"
            legacy_session.mkdir(parents=True)
            (legacy_session / "recording.xml").write_text(
                """<?xml version="1.0"?><i>
                <d p="1,1,25,1,100,0,1,0">影魔这把很肥</d>
                <d p="2,1,25,1,150,0,2,0">影魔要出黑皇杖了</d>
                </i>""",
                encoding="utf-8",
            )
            (metadata / "douyu-stats.json").write_text(
                json.dumps({"games": [{
                    "start_unix_ts": 90,
                    "end_unix_ts": 160,
                    "players": [player(11, "影魔"), player(22, "宙斯")],
                }]}, ensure_ascii=False),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"RECORDINGS_DIR": str(recordings_root)}):
                anchor = formatter.get_game_for_cover(legacy_session)
                diagnostics = formatter.get_identity_diagnostics(legacy_session)

            self.assertEqual(anchor["hero"], "影魔")
            self.assertTrue(diagnostics["stats_available"])
            self.assertEqual(diagnostics["stats_path"], str(metadata / "douyu-stats.json"))
            self.assertEqual(diagnostics["type_tooltips_game_snapshots"], 1)

    def test_flush_replaces_snapshot_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = daemon.RECORDINGS_DIR
            daemon.RECORDINGS_DIR = temporary
            try:
                monitor = daemon.RoomMonitor("9999", "主播", {})
                monitor.flush()
                output = Path(temporary) / "主播" / ".potato-flow" / "douyu-stats.json"
                self.assertEqual(json.loads(output.read_text())["schema_version"], 2)
                self.assertEqual(list(output.parent.glob("*.tmp")), [])
                self.assertEqual(output.stat().st_mode & 0o777, 0o640)
                self.assertEqual(output.parent.stat().st_mode & 0o777, 0o750)
            finally:
                daemon.RECORDINGS_DIR = previous

    def test_monitor_restores_schema_two_events_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = daemon.RECORDINGS_DIR
            daemon.RECORDINGS_DIR = temporary
            try:
                output_dir = Path(temporary) / "主播"
                output_dir.mkdir()
                players = [player(index, f"英雄{index}") for index in range(1, 11)]
                snapshot = {
                    "schema_version": 2,
                    "room_id": "9999",
                    "streamer": "主播",
                    "started_at": "2026-07-29T18:00:00+08:00",
                    "gift_events": [{"unix_ts": 100, "name": "飞机"}],
                    "high_energy": {"details": []},
                    "online_samples": [],
                    "games": [],
                    "active_game": {"start_unix_ts": 100, "players": players},
                }
                (output_dir / "stats_current.json").write_text(json.dumps(snapshot))
                monitor = daemon.RoomMonitor("9999", "主播", {})
                self.assertEqual(len(monitor.state["gift_events"]), 1)
                self.assertIsNotNone(monitor.state["active_game"])
                self.assertEqual(
                    monitor._accepted_fingerprint,
                    tuple(sorted(str(index) for index in range(1, 11))),
                )
                monitor.flush()
                self.assertFalse((output_dir / "stats_current.json").exists())
                self.assertTrue(
                    (output_dir / ".potato-flow" / "douyu-stats.json").is_file()
                )
            finally:
                daemon.RECORDINGS_DIR = previous

    def test_streamer_folder_name_is_readable_safe_and_contained(self):
        self.assertEqual(path_policy.safe_path_component("  主播 / 测试:*  "), "主播_测试")
        self.assertEqual(path_policy.safe_path_component("CON"), "CON_room")
        with tempfile.TemporaryDirectory() as temporary:
            previous = daemon.RECORDINGS_DIR
            daemon.RECORDINGS_DIR = temporary
            try:
                monitor = daemon.RoomMonitor("9999", "../主播/../../越界", {})
                monitor.flush()
                output = Path(monitor.output_dir).resolve()
                self.assertTrue(output.is_relative_to(Path(temporary).resolve()))
                self.assertNotIn("..", output.parts)
            finally:
                daemon.RECORDINGS_DIR = previous


if __name__ == "__main__":
    unittest.main()
