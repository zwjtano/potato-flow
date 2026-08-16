import importlib.util
import unittest
from unittest.mock import patch
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "potatoflow-app" / "modules" / "liquipedia_result_verifier.py"
SPEC = importlib.util.spec_from_file_location("liquipedia_result_verifier", MODULE_PATH)
verifier = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(verifier)


class LiquipediaResultVerifierTests(unittest.TestCase):
    def test_schedule_page_follows_tournament_stage(self):
        self.assertEqual(
            verifier.ti2026_liquipedia_schedule_page(
                "2026-08-16T13:00:00+08:00"
            ),
            verifier.TI2026_GROUP_STAGE_PAGE,
        )
        self.assertEqual(
            verifier.ti2026_liquipedia_schedule_page(
                "2026-08-20T00:00:00+08:00"
            ),
            verifier.TI2026_MAIN_EVENT_PAGE,
        )

    def test_live_feed_resolves_current_ti_lineup_without_inventing_kda(self):
        live = [{
            "match_id": "99", "league_id": verifier.TI2026_LEAGUE_ID,
            "activate_time": 1786800000, "last_update_time": 1786801800,
            "game_time": 1800, "radiant_score": 20, "dire_score": 17,
            "radiant_lead": 4200,
            "team_name_radiant": "Team Liquid", "team_name_dire": "Aurora Gaming",
            "players": [
                {"account_id": 1, "name": "Nisha", "hero_id": 74, "team": 0},
                {"account_id": 2, "name": "Mikoto", "hero_id": 25, "team": 1},
            ],
        }]

        def fake_fetch(url, **_kwargs):
            if url == verifier.OPENDOTA_LIVE_API:
                return live
            if url == verifier.HERO_CONSTANTS_URL:
                return {
                    "74": {"localized_name": "Invoker", "name": "npc_dota_hero_invoker"},
                    "25": {"localized_name": "Lina", "name": "npc_dota_hero_lina"},
                }
            return {}

        with patch.object(verifier, "_fetch_json", side_effect=fake_fetch):
            result = verifier.discover_opendota_live_recording_match(
                recording_start_china="2026-08-15T20:37:00+08:00",
                recording_duration_seconds=3600,
                evidence_text="Nisha Invoker关键团",
                team_aliases={"Team Liquid": ["Nisha"], "Aurora Gaming": ["Mikoto"]},
            )
        self.assertEqual(result["status"], "live_confirmed")
        self.assertEqual(result["games"][0]["radiant_score"], 20)
        self.assertEqual(result["games"][0]["players"][0]["hero_name"], "Invoker")
        self.assertEqual(result["games"][0]["performance_candidates"], [])

    def test_opendota_fallback_resolves_vg_ti_series_by_recording_overlap(self):
        pro_matches = [
            {"match_id": 11, "start_time": 1786672825, "duration": 2828,
             "radiant_name": "Vici Gaming", "dire_name": "HULIGANI",
             "league_name": "The International 2026"},
            {"match_id": 12, "start_time": 1786682065, "duration": 3255,
             "radiant_name": "Vici Gaming", "dire_name": "HULIGANI",
             "league_name": "The International 2026"},
            {"match_id": 13, "start_time": 1786685880, "duration": 2400,
             "radiant_name": "Vici Gaming", "dire_name": "HULIGANI",
             "league_name": "The International 2026"},
        ]
        match_payloads = {
            11: {**pro_matches[0], "radiant_win": False, "radiant_score": 18, "dire_score": 20, "players": []},
            12: {**pro_matches[1], "radiant_win": True, "radiant_score": 24, "dire_score": 21, "players": []},
            13: {**pro_matches[2], "radiant_win": True, "radiant_score": 30, "dire_score": 10, "players": []},
        }

        def fake_fetch(url, **_kwargs):
            if url == verifier.OPENDOTA_PRO_MATCHES_API:
                return pro_matches
            if "/matches/" in url:
                return match_payloads[int(url.rsplit("/", 1)[-1])]
            return {}

        with patch.object(verifier, "_fetch_json", side_effect=fake_fetch):
            result = verifier.discover_opendota_recording_match(
                recording_start_china="2026-08-14T12:59:00+08:00",
                recording_duration_seconds=3600,
                evidence_text="TI 2026 VG 团战翻盘",
                team_aliases={"Vici Gaming": ["VG"]},
            )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["opponents"], ["Vici Gaming", "HULIGANI"])
        self.assertEqual([game["match_id"] for game in result["games"]], [11, 12])

    def test_tournament_schedule_extracts_embedded_match_maps(self):
        parsed = verifier.parse_liquipedia_tournament_matches(
            """|M1={{Match
|opponent1={{TeamOpponent|Team Falcons}}
|opponent2={{TeamOpponent|LGD Gaming}}
|date=August 13, 2026 - 11:00 {{Abbr/CST}}
|matchid1=8942993144
|map1={{Map
|team1side=radiant
|t1h1=hus|t1h2=cm
|team2side=dire
|t2h1=hw|t2h2=wr
|length=40m46s|winner=2
}}
}}
"""
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["opponents"], ["Team Falcons", "LGD Gaming"])
        self.assertEqual(parsed[0]["maps"][0]["match_id"], 8942993144)
        self.assertEqual(parsed[0]["maps"][0]["winner_side"], 2)
        self.assertEqual(parsed[0]["maps"][0]["team2_heroes"], ["hw", "wr"])

    def test_main_event_bracket_key_exposes_round(self):
        parsed = verifier.parse_liquipedia_tournament_matches(
            """|R5M1={{Match
|opponent1={{TeamOpponent|Team Spirit}}
|opponent2={{TeamOpponent|Team Liquid}}
|date=August 23, 2026 - 17:00 {{Abbr/CST}}
}}
"""
        )
        self.assertEqual(parsed[0]["match_key"], "R5M1")
        self.assertEqual(parsed[0]["round_label"], "Grand Final")

    def test_empty_team_alias_never_matches_every_recording(self):
        self.assertFalse(
            verifier._team_in_text(
                "Team Falcons",
                "Team Liquid Boxi Nisha Xm",
                {"Team Falcons": [""]},
            )
        )

    def test_short_player_alias_requires_a_standalone_token(self):
        aliases = {"Vici Gaming": ["y`"]}
        self.assertFalse(verifier._team_in_text("Vici Gaming", "today is exciting", aliases))
        self.assertTrue(verifier._team_in_text("Vici Gaming", "这波 y` 发挥很好", aliases))

    def test_page_url_is_converted_to_mediawiki_title(self):
        self.assertEqual(
            verifier.liquipedia_page_title(
                "https://liquipedia.net/dota2/Match:ID_m2rkltuVhw_0001"
            ),
            "Match:ID m2rkltuVhw 0001",
        )

    def test_match_page_extracts_opponents_and_map_ids(self):
        parsed = verifier.parse_liquipedia_match_wikitext(
            """{{MatchPage
|opponent1={{TeamOpponent|Nigma Galaxy}}
|opponent2={{TeamOpponent|OG}}
|date=August 2, 2026 - 11:00 {{Abbr/CEST}}
|map1={{ApiMap|matchid=8925460065}}
|map2={{ApiMap|matchid=8925564849|reversed=true}}
}}"""
        )
        self.assertEqual(parsed["opponents"], ["Nigma Galaxy", "OG"])
        self.assertEqual(
            parsed["maps"],
            [
                {"game_number": 1, "match_id": 8925460065, "reversed": False},
                {"game_number": 2, "match_id": 8925564849, "reversed": True},
            ],
        )

    def test_player_slot_binds_team_and_hero_without_guessing(self):
        page = {
            "opponents": ["Nigma Galaxy", "OG"],
            "maps": [{"game_number": 1, "match_id": 1, "reversed": False}],
        }
        result = verifier.build_verified_match_result(
            page,
            {1: {
                "start_time": 1785661502,
                "duration": 3600,
                "radiant_name": "Nigma Galaxy ",
                "dire_name": "OG",
                "radiant_win": True,
                "players": [
                    {"name": "lorenof", "player_slot": 4, "hero_id": 106, "kills": 16, "deaths": 0, "assists": 15},
                    {"name": "Yopaj-", "player_slot": 128, "hero_id": 13, "kills": 6, "deaths": 6, "assists": 5},
                ],
            }},
            {
                "106": {"localized_name": "Ember Spirit", "name": "npc_dota_hero_ember_spirit"},
                "13": {"localized_name": "Puck", "name": "npc_dota_hero_puck"},
            },
        )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["series_score"], {"Nigma Galaxy": 1, "OG": 0})
        self.assertEqual(result["games"][0]["players"][0]["team"], "Nigma Galaxy")
        self.assertEqual(result["games"][0]["players"][0]["hero_name"], "Ember Spirit")
        self.assertEqual(result["games"][0]["duration_seconds"], 3600.0)
        self.assertEqual(result["games"][0]["end_time_unix"], 1785665102.0)


if __name__ == "__main__":
    unittest.main()
