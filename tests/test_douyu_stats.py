import json
import os
import sys
import tempfile
import time
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
    def test_dota_maps_retry_after_partial_startup_timeout(self):
        original_heroes = daemon.dota_hero_map
        original_items = daemon.dota_item_map
        original_hero_loaded = daemon._dota_hero_source_loaded
        original_item_loaded = daemon._dota_item_source_loaded
        self.addCleanup(setattr, daemon, "dota_hero_map", original_heroes)
        self.addCleanup(setattr, daemon, "dota_item_map", original_items)
        self.addCleanup(setattr, daemon, "_dota_hero_source_loaded", original_hero_loaded)
        self.addCleanup(setattr, daemon, "_dota_item_source_loaded", original_item_loaded)
        daemon.dota_hero_map = {}
        daemon.dota_item_map = {}
        daemon._dota_hero_source_loaded = False
        daemon._dota_item_source_loaded = False
        hero_attempts = 0

        def request(url, referer=""):
            nonlocal hero_attempts
            if url == daemon.DOTA2_HEROES_URL:
                hero_attempts += 1
                if hero_attempts == 1:
                    raise TimeoutError("hero source timed out")
                return {
                    "heroes": {
                        "npc_dota_hero_nevermore": {"ID": "11", "Name": "影魔"}
                    }
                }
            if url == daemon.DOTA2_ITEMS_URL:
                return {
                    "items": {
                        "item_black_king_bar": {
                            "ID": "116",
                            "Key": "item_black_king_bar",
                            "Name": "黑皇杖",
                        }
                    }
                }
            if url == daemon.DOTA2_OFFICIAL_ITEMS_URL:
                return {"result": {"data": {"itemabilities": []}}}
            raise AssertionError(url)

        with mock.patch.object(daemon, "_request_json", side_effect=request):
            daemon.load_dota2_maps()
            self.assertEqual(daemon.dota_hero_map, {})
            self.assertEqual(daemon.dota_item_map["116"], "黑皇杖")

            last_attempt = daemon.refresh_dota2_maps_if_needed(
                0.0,
                now=float(daemon.DOTA2_MAP_RETRY_INTERVAL),
            )

        self.assertEqual(last_attempt, float(daemon.DOTA2_MAP_RETRY_INTERVAL))
        self.assertEqual(daemon.dota_hero_map["11"], "影魔")
        self.assertEqual(hero_attempts, 2)

    def test_official_item_names_fill_douyu_translation_gaps(self):
        responses = {
            daemon.DOTA2_HEROES_URL: {"heroes": {"hero": {"ID": "1", "Name": "敌法师"}}},
            daemon.DOTA2_ITEMS_URL: {
                "items": {
                    "item_duelist_gloves": {
                        "ID": "2097",
                        "Key": "item_duelist_gloves",
                        "Name": "",
                    }
                }
            },
            daemon.DOTA2_OFFICIAL_ITEMS_URL: {
                "result": {
                    "data": {
                        "itemabilities": [
                            {
                                "id": 2097,
                                "name": "item_duelist_gloves",
                                "name_loc": "决斗家手套",
                            },
                            {
                                "id": 1858,
                                "name": "item_hydras_breath",
                                "name_loc": "怪蛇之息",
                            },
                        ]
                    }
                }
            },
        }

        with mock.patch.object(daemon, "_request_json", side_effect=lambda url, referer="": responses[url]):
            daemon.load_dota2_maps()

        self.assertEqual(daemon.dota_item_map["2097"], "决斗家手套")
        self.assertEqual(daemon.dota_item_map["item_duelist_gloves"], "决斗家手套")
        self.assertEqual(daemon.dota_item_map["1858"], "怪蛇之息")
        self.assertEqual(daemon.dota_item_map["item_hydras_breath"], "怪蛇之息")

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

    def test_high_energy_uses_voice_danmu_price_and_deduplicates_queue(self):
        monitor = daemon.RoomMonitor("9999", "主播", {})
        socket_record = {
            "voiceRecordId": "voice-123",
            "price": "50000",
            "realPrice": "30000",
            "hoverTime": "1800",
            "expireV2At": "1700001800",
            "acptime": "1700000000",
            "un": "付费用户",
        }
        monitor.handle_voice_trlt({
            "mtype": "2",
            "list": json.dumps([json.dumps(socket_record, ensure_ascii=False)]),
        })

        details = monitor.state["high_energy"]["details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["event_id"], "voice-123")
        self.assertEqual(details[0]["amount"], 300)
        self.assertEqual(details[0]["price_cents"], 30000)
        self.assertEqual(details[0]["listed_price_cents"], 50000)
        self.assertEqual(details[0]["hover_seconds"], 1800)
        self.assertEqual(details[0]["user"], "付费用户")
        self.assertEqual(details[0]["accepted_unix_ts"], 1700000000)
        self.assertEqual(details[0]["timestamp_source"], "acptime")

        queue_record = {
            **socket_record,
            "userNick": "完整昵称",
            "content": "审核通过后的正文",
        }
        with mock.patch.object(
            daemon,
            "_request_json",
            return_value={"error": 0, "msg": "success", "data": [json.dumps(queue_record)]},
        ):
            monitor.poll_high_energy_queue()

        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["content"], "审核通过后的正文")
        diagnostics = monitor.state["high_energy"]["diagnostics"]
        self.assertEqual(diagnostics["socket_messages"], 1)
        self.assertEqual(diagnostics["socket_records"], 1)
        self.assertEqual(diagnostics["queue_polls"], 1)
        self.assertEqual(diagnostics["queue_records"], 1)
        self.assertEqual(diagnostics["duplicate_records"], 1)
        self.assertEqual(details[0]["sources"], ["voice_trlt", "query_queue"])

    def test_high_energy_socket_promotes_accept_time_after_queue(self):
        monitor = daemon.RoomMonitor("88660", "主播", {})
        queue_record = {
            "voiceRecordId": "voice-cross-source",
            "price": 10000,
            "realPrice": 5000,
            "hoverTime": 300,
            "userNick": "队列昵称",
        }
        socket_record = {
            **queue_record,
            "acptime": 1700000000,
            "un": "Socket昵称",
            "content": "Socket正文",
        }

        monitor._record_high_energy(queue_record, "query_queue", now=1700000010)
        monitor._record_high_energy(socket_record, "voice_trlt", now=1700000012)

        event = monitor.state["high_energy"]["details"][0]
        self.assertEqual(len(monitor.state["high_energy"]["details"]), 1)
        self.assertEqual(event["unix_ts"], 1700000000)
        self.assertEqual(event["accepted_unix_ts"], 1700000000)
        self.assertEqual(event["observed_unix_ts"], 1700000010)
        self.assertEqual(event["timestamp_source"], "acptime")
        self.assertEqual(event["amount"], 50)
        self.assertEqual(event["sources"], ["query_queue", "voice_trlt"])
        self.assertEqual(event["content"], "Socket正文")

    def test_high_energy_accepts_single_nested_stt_record(self):
        records = daemon.decode_high_energy_records(
            "voiceRecordId@=voice-456/realPrice@=1000/hoverTime@=60/"
        )
        self.assertEqual(records, [{
            "voiceRecordId": "voice-456",
            "realPrice": "1000",
            "hoverTime": "60",
        }])

    def test_high_energy_marks_missing_accept_time_as_observed(self):
        event = daemon.normalize_high_energy_record({
            "voiceRecordId": "voice-no-acptime",
            "price": 5000,
            "realPrice": 0,
            "hoverTime": 120,
        }, "query_queue", now=1700000000)

        self.assertEqual(event["amount"], 0)
        self.assertEqual(event["listed_price_cents"], 5000)
        self.assertIsNone(event["accepted_unix_ts"])
        self.assertEqual(event["observed_unix_ts"], 1700000000)
        self.assertEqual(event["timestamp_source"], "observed_at")

    def test_high_energy_keeps_fractional_yuan_amount(self):
        event = daemon.normalize_high_energy_record({
            "voiceRecordId": "voice-fraction",
            "realPrice": 50,
            "hoverTime": 60,
        }, "query_queue", now=1700000000)

        self.assertEqual(event["price_cents"], 50)
        self.assertEqual(event["amount"], 0.5)
        text = formatter.format_stats(
            {"high_energy": {"details": [event]}},
            1699999999,
            1700000001,
            [],
        )
        self.assertIn("高能弹幕 ×1 | 0.5元", text)

    def test_high_energy_decodes_voice_trlt_gson_array_from_packet(self):
        packet = daemon.encode_packet(
            "type@=voice_trlt/mtype@=2/"
            "list@=voiceRecordId@AA=voice-1@ASrealPrice@AA=1000@AS"
            "userIcon@AA=https:@AS@ASapic.douyucdn.cn@ASavatar.jpg@AS@S"
            "voiceRecordId@AA=voice-2@ASrealPrice@AA=3000@AS/"
        )
        messages, pending = daemon.decode_packets(packet)
        self.assertEqual(pending, b"")
        message = messages[0][1]
        self.assertEqual(message["list"], (
            "voiceRecordId@A=voice-1/realPrice@A=1000/"
            "userIcon@A=https://apic.douyucdn.cn/avatar.jpg//"
            "voiceRecordId@A=voice-2/realPrice@A=3000/"
        ))

        monitor = daemon.RoomMonitor("9999", "主播", {})
        monitor.handle_voice_trlt(message)
        self.assertEqual(
            [item["event_id"] for item in monitor.state["high_energy"]["details"]],
            ["voice-1", "voice-2"],
        )
        self.assertEqual(
            [item["amount"] for item in monitor.state["high_energy"]["details"]],
            [10, 30],
        )

    def test_legacy_24597_dgb_is_ignored(self):
        monitor = daemon.RoomMonitor("9999", "主播", {
            "24597": {"name": "高能弹幕", "price": 500},
            "1": {"name": "飞机", "price": 100},
        })
        monitor.handle_dgb({"gfid": "24597", "gfn": "高能弹幕", "gfcnt": "9"})
        monitor.handle_dgb({"gfid": "1", "gfn": "飞机", "gfcnt": "2"})

        self.assertEqual(monitor.state["high_energy"]["details"], [])
        self.assertEqual(len(monitor.state["gift_events"]), 1)
        self.assertEqual(monitor.state["gift_events"][0]["name"], "飞机")
        self.assertEqual(monitor.state["gift_events"][0]["total_value"], 200)

    def test_gift_catalog_prefers_v5_and_uses_v2_for_special_gifts(self):
        responses = {
            "v5": {
                "error": 0,
                "data": {"giftList": [
                    {"id": 1, "name": "新飞机", "priceInfo": {
                        "price": 10000, "priceType": "YUCHI",
                    }},
                ]},
            },
            "v2": {
                "error": 0,
                "data": {"giftList": [
                    {"id": 1, "name": "旧飞机", "priceInfo": {
                        "price": 9000, "priceType": "YUCHI",
                    }},
                    {"id": 2, "name": "特殊礼物", "priceInfo": {
                        "price": 600, "priceType": "YUCHI",
                    }},
                    {"id": 3, "name": "鱼丸礼物", "priceInfo": {
                        "price": 100, "priceType": "YUWAN",
                    }},
                ]},
            },
        }

        def request(url, _referer=""):
            return responses["v5" if "/v5/" in url else "v2"]

        with mock.patch.object(daemon, "_request_json", side_effect=request):
            prices = daemon.load_gift_prices("9999")

        self.assertEqual(prices["1"]["name"], "新飞机")
        self.assertEqual(prices["1"]["price_cents"], 10000)
        self.assertEqual(prices["1"]["catalog_source"], "v5")
        self.assertEqual(prices["2"]["price_cents"], 600)
        self.assertEqual(prices["2"]["catalog_source"], "v2")
        self.assertEqual(prices["3"]["price_cents"], 0)
        self.assertEqual(prices["3"]["raw_price"], 100)

    def test_gift_catalog_falls_back_when_v5_fails(self):
        def request(url, _referer=""):
            if "/v5/" in url:
                raise OSError("v5 unavailable")
            return {"error": 0, "data": {"giftList": [{
                "id": 7,
                "name": "火箭",
                "priceInfo": {"price": 50000, "priceType": "YUCHI"},
            }]}}

        with mock.patch.object(daemon, "_request_json", side_effect=request):
            prices = daemon.load_gift_prices("9999")

        self.assertEqual(prices["7"]["price_cents"], 50000)
        self.assertEqual(prices["7"]["catalog_source"], "v2")

    def test_gift_catalog_uses_v2_to_fill_missing_v5_price_for_same_id(self):
        responses = {
            "v5": {"error": 0, "data": {"giftList": [{
                "id": 9, "name": "当前礼物", "priceInfo": {}, "showStatus": 1,
            }]}},
            "v2": {"error": 0, "data": {"giftList": [{
                "id": 9, "name": "旧目录名",
                "priceInfo": {"price": 10000, "priceType": "YUCHI"},
            }]}},
        }

        with mock.patch.object(
            daemon,
            "_request_json",
            side_effect=lambda url, _referer="": responses[
                "v5" if "/v5/" in url else "v2"
            ],
        ):
            gift = daemon.load_gift_prices("9999")["9"]

        self.assertEqual(gift["name"], "当前礼物")
        self.assertEqual(gift["show_status"], 1)
        self.assertEqual(gift["price_cents"], 10000)
        self.assertEqual(gift["catalog_source"], "v5")
        self.assertEqual(gift["price_catalog_source"], "v2")

    def test_dgb_keeps_low_value_and_unknown_prop_events_for_diagnostics(self):
        monitor = daemon.RoomMonitor("9999", "主播", {
            "24677": {
                "name": "钻粉卡", "price": 6, "price_cents": 600,
                "price_type": "YUCHI", "catalog_source": "v5",
            },
            "24678": {
                "name": "钻粉飞机", "price": 100, "price_cents": 10000,
                "price_type": "YUCHI", "catalog_source": "v5",
            },
        })
        monitor.handle_dgb({
            "gfid": "24677", "gfn": "钻粉卡", "gfcnt": "2", "hits": "2",
            "uid": "42", "nn": "用户", "hc": "event-low",
        })
        monitor.handle_dgb({
            "gfid": "824", "gfn": "粉丝荧光棒", "gfcnt": "3", "gpf": "1",
            "pid": "268", "bcnt": "3", "skinid": "9", "hc": "event-prop",
        })
        monitor.handle_dgb({"gfid": "24678", "gfn": "钻粉飞机", "gfcnt": "1"})

        events = monitor.state["gift_events"]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["unit_price_cents"], 600)
        self.assertEqual(events[0]["total_value_cents"], 1200)
        self.assertEqual(events[0]["hits"], 2)
        self.assertEqual(events[1]["prop_id"], 268)
        self.assertEqual(events[1]["gift_prop_flag"], 1)
        self.assertEqual(events[1]["skin_id"], 9)
        self.assertTrue(events[1]["price_unknown"])
        self.assertFalse(events[1]["paid"])
        diagnostics = monitor.state["gift_diagnostics"]
        self.assertEqual(diagnostics["messages"], 3)
        self.assertEqual(diagnostics["recorded_events"], 3)
        self.assertEqual(diagnostics["priced_events"], 2)
        self.assertEqual(diagnostics["unpriced_events"], 1)
        self.assertEqual(diagnostics["prop_events"], 1)
        self.assertEqual(diagnostics["unknown_gift_ids"], {"824": 1})

    def test_dgb_does_not_deduplicate_reused_hc_style_hash(self):
        monitor = daemon.RoomMonitor("9999", "主播", {
            "1": {
                "name": "飞机", "price": 100, "price_cents": 10000,
                "price_type": "YUCHI", "catalog_source": "v5",
            },
            "2": {
                "name": "火箭", "price": 500, "price_cents": 50000,
                "price_type": "YUCHI", "catalog_source": "v5",
            },
        })
        first_message = {
            "gfid": "1", "gfn": "飞机", "gfcnt": "1",
            "uid": "42", "hc": "same-event",
        }
        second_message = {
            "gfid": "2", "gfn": "火箭", "gfcnt": "1",
            "uid": "84", "hc": "same-event",
        }

        monitor.handle_dgb(first_message)
        monitor.handle_dgb(second_message)

        self.assertEqual(len(monitor.state["gift_events"]), 2)
        self.assertEqual(monitor.state["gift_events"][0]["total_value_cents"], 10000)
        self.assertEqual(monitor.state["gift_events"][1]["total_value_cents"], 50000)
        diagnostics = monitor.state["gift_diagnostics"]
        self.assertEqual(diagnostics["messages"], 2)
        self.assertEqual(diagnostics["recorded_events"], 2)
        self.assertEqual(diagnostics["duplicate_messages"], 0)

    def test_spbc_records_only_gifts_targeting_monitored_room(self):
        monitor = daemon.RoomMonitor("9999", "主播", {
            "20003": {
                "name": "飞机", "price": 100, "price_cents": 10000,
                "price_type": "YUCHI", "catalog_source": "v5",
            },
        })

        monitor.handle_spbc({
            "gfid": "20003", "gn": "飞机", "gc": "1",
            "sid": "42", "sn": "用户", "drid": "9999",
        })
        monitor.handle_spbc({
            "gfid": "20003", "gn": "飞机", "gc": "1",
            "sid": "84", "sn": "其他用户", "drid": "226037",
        })

        self.assertEqual(len(monitor.state["gift_events"]), 1)
        event = monitor.state["gift_events"][0]
        self.assertEqual(event["name"], "飞机")
        self.assertEqual(event["unit_price_cents"], 10000)
        self.assertEqual(event["source"], "spbc")
        diagnostics = monitor.state["gift_diagnostics"]
        self.assertEqual(diagnostics["spbc_messages"], 2)
        self.assertEqual(diagnostics["spbc_other_room_messages"], 1)

    def test_spbc_and_dgb_same_gift_are_deduplicated_across_sources(self):
        monitor = daemon.RoomMonitor("9999", "主播", {
            "20004": {
                "name": "火箭", "price": 500, "price_cents": 50000,
                "price_type": "YUCHI", "catalog_source": "v5",
            },
        })

        monitor.handle_dgb({
            "gfid": "20004", "gfn": "火箭", "gfcnt": "1",
            "uid": "42", "nn": "用户",
        })
        monitor.handle_spbc({
            "gfid": "20004", "gn": "火箭", "gc": "1",
            "sid": "42", "sn": "用户", "drid": "9999",
        })

        self.assertEqual(len(monitor.state["gift_events"]), 1)
        self.assertEqual(
            monitor.state["gift_diagnostics"]["cross_source_duplicates"],
            1,
        )

    def test_gift_catalog_refresh_replaces_only_with_nonempty_catalog(self):
        monitor = daemon.RoomMonitor("9999", "主播", {
            "1": {"name": "旧礼物", "price_cents": 100},
        })
        refreshed = {
            "2": {"name": "新礼物", "price_cents": 200},
        }

        with mock.patch.object(daemon, "load_gift_prices", return_value=refreshed):
            self.assertTrue(monitor.refresh_gift_prices())
        self.assertEqual(monitor.prices, refreshed)
        self.assertEqual(monitor.state["gift_diagnostics"]["catalog_refreshes"], 1)

        with mock.patch.object(daemon, "load_gift_prices", return_value={}):
            self.assertFalse(monitor.refresh_gift_prices())
        self.assertEqual(monitor.prices, refreshed)
        self.assertEqual(monitor.state["gift_diagnostics"]["catalog_refresh_failures"], 1)

    def test_unknown_gift_requests_one_rate_limited_catalog_refresh(self):
        monitor = daemon.RoomMonitor("9999", "主播", {})

        with mock.patch.object(daemon.time, "time", return_value=10000):
            monitor.handle_dgb({"gfid": "unknown-1", "gfcnt": "1"})
            monitor.handle_dgb({"gfid": "unknown-2", "gfcnt": "1"})

        self.assertTrue(monitor._gift_catalog_refresh_requested)
        self.assertEqual(
            monitor.state["gift_diagnostics"]["unknown_catalog_refresh_requests"],
            1,
        )

    def test_formatter_filters_priced_gifts_with_unit_price_below_100_yuan(self):
        stats = {"gift_events": [
            {
                "unix_ts": 150, "name": "钻粉卡", "paid": True,
                "unit_price_cents": 600, "total_value_cents": 1200, "count": 2,
            },
            {
                "unix_ts": 160, "name": "未知道具", "paid": False,
                "unit_price_cents": 0, "total_value_cents": 0, "count": 9,
            },
            {
                "unix_ts": 170, "name": "钻粉飞机", "paid": True,
                "unit_price_cents": 10000, "total_value_cents": 20000, "count": 2,
            },
        ]}

        text = formatter.format_stats(stats, 100, 200, [])

        self.assertIn("钻粉飞机×2(单价100元/总价200元)", text)
        self.assertNotIn("钻粉卡", text)
        self.assertIn("礼物价值合计 200元", text)
        self.assertNotIn("未核价", text)

    def test_formatter_applies_100_yuan_threshold_to_unit_price(self):
        stats = {"gift_events": [
            {
                "unix_ts": 150, "name": "丹药盒", "paid": True,
                "unit_price_cents": 100, "total_value_cents": 20000, "count": 200,
            },
            {
                "unix_ts": 160, "name": "飞机", "paid": True,
                "unit_price_cents": 10000, "total_value_cents": 10000, "count": 1,
            },
            {
                "unix_ts": 170, "name": "低于门槛", "paid": True,
                "unit_price_cents": 9999, "total_value_cents": 19998, "count": 2,
            },
        ]}

        text = formatter.format_stats(stats, 100, 200, [])

        self.assertIn("飞机×1(单价100元/总价100元)", text)
        self.assertIn("礼物价值合计 100元", text)
        self.assertNotIn("丹药盒", text)
        self.assertNotIn("低于门槛", text)

    def test_formatter_hides_unpriced_props(self):
        stats = {
            "gift_events": [{
                "unix_ts": 150,
                "name": "未知道具",
                "paid": False,
                "unit_price_cents": 0,
                "total_value_cents": 0,
                "count": 3,
            }],
        }

        text = formatter.format_stats(stats, 100, 200, [])

        self.assertEqual(text, "")

    def test_diamond_fan_membership_events_are_separate_from_gifts(self):
        monitor = daemon.RoomMonitor("9999", "主播", {})
        with mock.patch.object(daemon.time, "time", return_value=1700000000):
            monitor.handle_diamond_fan("dfobc", {
                "rid": "9999", "uid": "42", "nn": "新钻粉", "dfl": "1",
            })
            monitor.handle_diamond_fan("odfpbc", {
                "drid": "9999", "uid": "42", "nn": "新钻粉", "dfl": "1",
            })
            monitor.handle_diamond_fan("rdfpbc", {
                "drid": "9999", "uid": "43", "nn": "续费钻粉", "mn": "3",
                "ct": "1700000010", "dfl": "5", "hc": "diamond-renew-1",
            })
            monitor.handle_diamond_fan("odfpbc", {
                "drid": "10000", "uid": "44", "nn": "其他房间", "mn": "12",
            })

        events = monitor.state["diamond_fans"]["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["action"], "open")
        self.assertEqual(events[0]["months"], 1)
        self.assertEqual(events[1]["action"], "renew")
        self.assertEqual(events[1]["months"], 3)
        self.assertEqual(events[1]["diamond_level"], 5)
        self.assertTrue(events[1]["broadcast"])
        self.assertEqual(monitor.state["gift_events"], [])
        diagnostics = monitor.state["diamond_fans"]["diagnostics"]
        self.assertEqual(diagnostics["messages"], 4)
        self.assertEqual(diagnostics["recorded_events"], 2)
        self.assertEqual(diagnostics["open_events"], 1)
        self.assertEqual(diagnostics["renew_events"], 1)
        self.assertEqual(diagnostics["multi_month_events"], 1)
        self.assertEqual(diagnostics["duplicate_messages"], 1)
        self.assertEqual(diagnostics["ignored_other_room_events"], 1)
        self.assertEqual(events[0]["sources"], ["dfobc", "odfpbc"])

    def test_formatter_reports_diamond_membership_without_inventing_amount(self):
        stats = {
            "diamond_fans": {"events": [
                {"unix_ts": 150, "action": "open", "months": 1},
                {"unix_ts": 160, "action": "renew", "months": 3},
            ]},
        }

        text = formatter.format_stats(stats, 100, 200, [])

        self.assertIn("💎 钻粉 开通1次/1个月 续费1次/3个月", text)
        self.assertNotIn("礼物价值合计", text)
        self.assertNotIn("高能弹幕", text)

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

    def test_explicit_gsi_hero_survives_missing_lineup_after_stability_window(self):
        original_heroes = daemon.dota_hero_map
        self.addCleanup(setattr, daemon, "dota_hero_map", original_heroes)
        daemon.dota_hero_map = {30: "巫医", "30": "巫医"}
        monitor = daemon.RoomMonitor("9999", "任意主播", {})
        payload = {
            "top": [],
            "bottom": [],
            "hero": {"id": 30, "items": ["item_phase_boots"]},
        }

        with mock.patch.object(daemon.time, "time", return_value=100.0):
            monitor.handle_dota2_snapshot(payload, "http")
        with mock.patch.object(daemon.time, "time", return_value=170.0):
            monitor.handle_dota2_snapshot(payload, "http")

        self.assertIsNone(monitor.state["active_game"])
        history = monitor.state["gsi_hero_history"]
        self.assertEqual(len(history), 1)
        anchor = formatter.select_gsi_history_player(history, 100, 170)
        self.assertEqual(anchor["hero"], "巫医")
        self.assertEqual(anchor["identity_source"], "gsi_explicit_hero:http")
        self.assertFalse(anchor["gsi_verified_in_lineup"])
        text = formatter.format_stats(
            {"gsi_hero_history": history},
            100,
            170,
            [],
        )
        self.assertIn("🎮 巫医", text)

    def test_explicit_gsi_hero_is_rejected_before_stability_window(self):
        history = [{
            "start_unix_ts": 100,
            "last_seen_unix_ts": 159,
            "source": "http",
            "verified_in_lineup": False,
            "player": player(30, "巫医"),
        }]

        self.assertIsNone(formatter.select_gsi_history_player(history, 100, 200))

    def test_explicit_gsi_switching_heroes_is_rejected(self):
        history = [
            {
                "start_unix_ts": 100,
                "last_seen_unix_ts": 170,
                "source": "http",
                "verified_in_lineup": False,
                "player": player(30, "巫医"),
            },
            {
                "start_unix_ts": 171,
                "last_seen_unix_ts": 250,
                "source": "http",
                "verified_in_lineup": False,
                "player": player(71, "食人魔魔法师"),
            },
        ]

        self.assertIsNone(formatter.select_gsi_history_player(history, 100, 250))

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

    def test_formatter_rejects_switching_observer_view_and_uses_dominant_xml(self):
        game = {
            "start_unix_ts": 100,
            "end_unix_ts": 400,
            "players": [player(41, "虚空假面", ["狂战斧"]), player(14, "帕吉")],
            "anchor_history": [
                {
                    "start_unix_ts": 100,
                    "last_seen_unix_ts": 220,
                    "source": "http",
                    "player": player(14, "帕吉"),
                },
                {
                    "start_unix_ts": 220,
                    "last_seen_unix_ts": 400,
                    "source": "http",
                    "player": player(41, "虚空假面", ["狂战斧"]),
                },
            ],
        }
        comments = [
            (150 + index, "这波虚空被集中讨论") for index in range(30)
        ] + [
            (300 + index, "这波屠夫被讨论") for index in range(4)
        ]

        anchor = formatter.select_streamer_player(game, comments, 100, 400)

        self.assertEqual(anchor["hero"], "虚空假面")
        self.assertEqual(anchor["identity_source"], "xml_dominant_mention")
        self.assertEqual(anchor["xml_mention_score"], 30)

    def test_xml_fallback_counts_distinct_comments_not_repeated_words(self):
        players = [player(41, "虚空假面"), player(14, "帕吉")]

        self.assertIsNone(formatter.select_anchor_player(
            players,
            [(150, "虚空" * 100)],
            100,
            200,
        ))

    def test_formatter_rejects_sparse_gsi_and_ambiguous_xml(self):
        game = {
            "start_unix_ts": 100,
            "end_unix_ts": 400,
            "players": [player(11, "影魔"), player(74, "祈求者")],
            "anchor_history": [{
                "start_unix_ts": 390,
                "last_seen_unix_ts": 390,
                "source": "type_tooltips",
                "player": player(11, "影魔"),
            }],
        }
        comments = [(150, "影魔" * 49), (300, "卡尔" * 47)]

        self.assertIsNone(
            formatter.select_streamer_player(game, comments, 100, 400)
        )

    def test_formatter_short_ascii_alias_requires_token_boundary(self):
        players = [player(1, "敌法师"), player(2, "宙斯")]
        unrelated = [(100 + index, "good game") for index in range(25)]

        self.assertIsNone(
            formatter.select_anchor_player(players, unrelated, 100, 200)
        )

        explicit = [(100 + index, "这把AM太肥了") for index in range(25)]
        selected = formatter.select_anchor_player(players, explicit, 100, 200)
        self.assertEqual(selected["hero"], "敌法师")

    def test_formatter_does_not_double_count_overlapping_chinese_aliases(self):
        players = [player(1, "拍拍熊"), player(2, "宙斯")]
        thirteen_mentions = [(100 + index, "这把拍拍熊很肥") for index in range(13)]

        self.assertIsNone(
            formatter.select_anchor_player(players, thirteen_mentions, 100, 200)
        )

        twenty_five_mentions = [(100 + index, "这把拍拍熊很肥") for index in range(25)]
        selected = formatter.select_anchor_player(players, twenty_five_mentions, 100, 200)
        self.assertEqual(selected["hero"], "拍拍熊")
        self.assertEqual(selected["xml_mention_score"], 25)

    def test_formatter_rejects_legacy_single_late_anchor_snapshot(self):
        game = {
            "start_unix_ts": 100,
            "end_unix_ts": 400,
            "players": [player(11, "影魔"), player(22, "宙斯")],
            "anchor_player": player(22, "宙斯"),
            "anchor_last_seen_unix_ts": 390,
            "anchor_source": "http",
        }

        self.assertIsNone(formatter.select_streamer_player(game, [], 100, 400))

    def test_formatter_keeps_stable_gsi_even_when_xml_discusses_another_hero(self):
        game = {
            "start_unix_ts": 100,
            "end_unix_ts": 400,
            "players": [player(11, "影魔", ["黑皇杖"]), player(74, "祈求者")],
            "anchor_history": [{
                "start_unix_ts": 100,
                "last_seen_unix_ts": 400,
                "source": "http",
                "player": player(11, "影魔", ["黑皇杖"]),
            }],
        }

        anchor = formatter.select_streamer_player(
            game, [(200, "卡尔" * 100)], 100, 400
        )

        self.assertEqual(anchor["hero"], "影魔")
        self.assertEqual(anchor["identity_source"], "gsi_hero:http")
        self.assertEqual(anchor["gsi_observed_seconds"], 300)

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
            hero_comments = "".join(
                f'<d p="1,1,25,1,{100 + index * 2},0,{index},0">影魔装备讨论{index}</d>'
                for index in range(25)
            )
            (session / f"{session.name}.xml").write_text(
                """<?xml version="1.0"?><i>""" + hero_comments + """
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

    def test_formatter_ignores_xml_from_another_recording_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "YYF_单排影魔_2026-07-30_11-47"
            session.mkdir()
            (session / f"{session.name}.xml").write_text(
                '<i><d p="1,1,25,1,100,0,1,0">本段弹幕</d></i>',
                encoding="utf-8",
            )
            (session / "YYF_下一段_2026-07-30_12-47.xml").write_text(
                '<i><d p="1,1,25,1,200,0,1,0">下一段弹幕</d></i>',
                encoding="utf-8",
            )

            comments = formatter.load_xml_comments(session)

            self.assertEqual(comments, [(100.0, "本段弹幕")])

    def test_formatter_uses_directory_time_when_matching_xml_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "YYF_单排影魔_2026-07-30_11-47"
            session.mkdir()
            (session / "YYF_下一段_2026-07-30_12-47.xml").write_text(
                '<i><d p="1,1,25,1,200,0,1,0">下一段弹幕</d></i>',
                encoding="utf-8",
            )
            expected = time.mktime(time.strptime("2026-07-30 11:47:00", "%Y-%m-%d %H:%M:%S"))

            start_ts, end_ts = formatter.recording_timeframe(session)

            self.assertEqual(start_ts, expected)
            self.assertEqual(end_ts, expected + 3600.0)

    def test_cover_identity_selects_game_with_longest_recording_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "主播_直播_1970-01-01_00-01"
            session.mkdir()
            (session / f"{session.name}.xml").write_text(
                '<i><d p="1,1,25,1,100,0,1,0">开始</d>'
                '<d p="2,1,25,1,200,0,2,0">结束</d></i>',
                encoding="utf-8",
            )
            stats = {
                "games": [
                    {
                        "start_unix_ts": 90,
                        "end_unix_ts": 190,
                        "anchor_player": player(11, "影魔", ["黑皇杖"]),
                        "anchor_last_seen_unix_ts": 180,
                        "anchor_history": [{
                            "start_unix_ts": 100,
                            "last_seen_unix_ts": 180,
                            "source": "http",
                            "player": player(11, "影魔", ["黑皇杖"]),
                        }],
                    },
                    {
                        "start_unix_ts": 190,
                        "end_unix_ts": 260,
                        "anchor_player": player(22, "敌法师", ["狂战斧"]),
                        "anchor_last_seen_unix_ts": 195,
                    },
                ]
            }
            metadata = root / ".potato-flow"
            metadata.mkdir()
            (metadata / "douyu-stats.json").write_text(
                json.dumps(stats, ensure_ascii=False), encoding="utf-8"
            )

            anchor = formatter.get_game_for_cover(session)

            self.assertEqual(anchor["hero"], "影魔")
            self.assertEqual(anchor["items"], ["黑皇杖"])

    def test_formatter_uses_xml_timeframe_and_xml_hero_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "主播_直播_1970-01-01_00-01"
            session.mkdir()
            hero_comments = "".join(
                f'<d p="1,1,25,1,{100 + index * 2},0,{index},0">影魔对局讨论{index}</d>'
                for index in range(25)
            )
            (session / f"{session.name}.xml").write_text(
                """<?xml version="1.0"?><i>""" + hero_comments + """
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
            self.assertIn("飞机×2(单价100元/总价200元)", text)
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
                    "anchor_history": [{
                        "start_unix_ts": 100,
                        "last_seen_unix_ts": 180,
                        "source": "http",
                        "player": player(35, "狙击手"),
                    }],
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
                    "anchor_history": [{
                        "start_unix_ts": 200,
                        "last_seen_unix_ts": 280,
                        "source": "http",
                        "player": {
                            **player(2, "斧王", ["闪烁匕首", "刃甲", "相位鞋"]),
                            "neutral": "Rattlecage",
                            "shard": True,
                        },
                    }],
                },
            ],
        }

        text = formatter.format_stats(stats, 100, 300)

        self.assertIn("🎮 第1局：狙击手", text)
        self.assertIn(
            "🎮 第2局：斧王｜六格：闪烁匕首、刃甲、相位鞋｜中立：Rattlecage｜魔晶",
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
            hero_comments = "".join(
                f'<d p="1,1,25,1,{100 + index * 2},0,{index},0">影魔对局讨论{index}</d>'
                for index in range(25)
            )
            (legacy_session / f"{legacy_session.name}.xml").write_text(
                """<?xml version="1.0"?><i>""" + hero_comments + """
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
