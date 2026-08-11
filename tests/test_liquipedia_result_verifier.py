import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "potatoflow-app" / "modules" / "liquipedia_result_verifier.py"
SPEC = importlib.util.spec_from_file_location("liquipedia_result_verifier", MODULE_PATH)
verifier = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(verifier)


class LiquipediaResultVerifierTests(unittest.TestCase):
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
