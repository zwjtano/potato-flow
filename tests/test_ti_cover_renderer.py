import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from dota2_heroes import Dota2Hero  # noqa: E402
from modules.ti_cover_renderer import render_ti_cover  # noqa: E402


class TiCoverRendererTests(unittest.TestCase):
    def _hero(self, name):
        return Dota2Hero(name, name, name.casefold().replace(" ", "_"), "")

    def test_pending_match_renders_exact_lineups_without_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            background = root / "background.jpg"
            output = root / "cover.jpg"
            portrait = root / "hero.png"
            Image.new("RGB", (400, 225), "#21334a").save(background)
            Image.new("RGB", (256, 144), "#b45b33").save(portrait)
            match = {
                "status": "matched_pending_data",
                "opponents": ["Team Liquid", "Vici Gaming"],
                "liquipedia_maps": [{
                    "game_number": 1,
                    "team1_heroes": ["a", "b", "c", "d", "e"],
                    "team2_heroes": ["f", "g", "h", "i", "j"],
                }],
            }
            with (
                patch("dota2_heroes.find_official_dota2_hero", side_effect=self._hero),
                patch("dota2_heroes.download_dota2_hero_image", return_value=portrait),
                patch("modules.ti_cover_renderer._team_logo", return_value=None),
            ):
                details = render_ti_cover(
                    background, output, app_root=APP_ROOT,
                    tournament_context={"phase": "group_stage", "series_format": "bo3"},
                    tournament_match=match, headline="TI 焦点战",
                    hero_cache_dir=root / "cache",
                )
            self.assertTrue(output.is_file())
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (400, 225))
            self.assertFalse(details["confirmed"])
            self.assertEqual(details["kills"], "待确认")
            self.assertEqual(len(details["lineups"]["Team Liquid"]), 5)
            self.assertEqual(len(details["lineups"]["Vici Gaming"]), 5)

    def test_elimination_round_uses_elimination_label(self):
        from modules.ti_cover_renderer import PHASE_LABELS

        self.assertEqual(PHASE_LABELS["elimination_round"], "淘汰轮")
        self.assertEqual(PHASE_LABELS["intermission"], "休赛日")

    def test_confirmed_match_maps_kills_to_displayed_team_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            background = root / "background.jpg"
            output = root / "cover.jpg"
            portrait = root / "hero.png"
            Image.new("RGB", (800, 450), "#162219").save(background)
            Image.new("RGB", (256, 144), "#3388aa").save(portrait)
            players = [
                {"team": team, "hero_name": f"hero-{index}", "name": f"p{index}",
                 "kills": index, "deaths": 1, "assists": 2}
                for index, team in enumerate(["Radiant"] * 5 + ["Dire"] * 5)
            ]
            game = {
                "game_number": 3, "radiant": "Radiant", "dire": "Dire",
                "radiant_score": 41, "dire_score": 30, "players": players,
                "performance_candidates": [players[-1]],
            }
            match = {
                "status": "confirmed", "opponents": ["Dire", "Radiant"],
                "series_score": {"Dire": 1, "Radiant": 2}, "games": [game],
            }
            with (
                patch("dota2_heroes.find_official_dota2_hero", side_effect=self._hero),
                patch("dota2_heroes.download_dota2_hero_image", return_value=portrait),
                patch("modules.ti_cover_renderer._team_logo", return_value=None),
            ):
                details = render_ti_cover(
                    background, output, app_root=APP_ROOT,
                    tournament_context={"phase": "main_event", "series_format": "bo3"},
                    tournament_match=match, headline="决胜局",
                    hero_cache_dir=root / "cache",
                )
            self.assertTrue(details["confirmed"])
            self.assertEqual(details["kills"], "30:41")
            self.assertEqual(details["game_number"], 3)


if __name__ == "__main__":
    unittest.main()
