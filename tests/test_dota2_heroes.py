import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import dota2_heroes


class Dota2HeroesTests(unittest.TestCase):
    def tearDown(self):
        dota2_heroes.load_official_dota2_heroes.cache_clear()

    def test_official_primary_attribute_is_preserved(self):
        payload = {
            "result": {
                "data": {
                    "heroes": [
                        {
                            "id": 5,
                            "name": "npc_dota_hero_crystal_maiden",
                            "name_loc": "水晶室女",
                            "name_english_loc": "Crystal Maiden",
                            "primary_attr": 2,
                        },
                        {
                            "id": 137,
                            "name": "npc_dota_hero_primal_beast",
                            "name_loc": "獸",
                            "name_english_loc": "Primal Beast",
                            "primary_attr": 0,
                        },
                    ]
                }
            }
        }
        response = io.BytesIO(json.dumps(payload).encode())
        with patch.object(dota2_heroes.urllib.request, "urlopen", return_value=response):
            heroes = dota2_heroes.load_official_dota2_heroes()
        by_slug = {hero.icon_slug: hero for hero in heroes}
        self.assertEqual(by_slug["crystal_maiden"].primary_attribute, "intelligence")
        self.assertTrue(by_slug["crystal_maiden"].is_intelligence)
        self.assertEqual(by_slug["primal_beast"].primary_attribute, "strength")
        self.assertFalse(by_slug["primal_beast"].is_intelligence)

    def test_liquipedia_draft_aliases_resolve_to_valve_heroes(self):
        heroes = tuple(
            dota2_heroes.Dota2Hero(slug, slug, slug)
            for slug in (
                "rattletrap",
                "winter_wyvern",
                "abyssal_underlord",
                "earth_spirit",
                "windrunner",
                "viper",
                "wisp",
            )
        )
        with patch.object(dota2_heroes, "load_official_dota2_heroes", return_value=heroes):
            expected = {
                "cw": "rattletrap",
                "ww": "winter_wyvern",
                "ul": "abyssal_underlord",
                "esp": "earth_spirit",
                "wr": "windrunner",
                "vip": "viper",
                "io": "wisp",
            }
            for alias, slug in expected.items():
                with self.subTest(alias=alias):
                    self.assertEqual(
                        dota2_heroes.find_official_dota2_hero(alias).icon_slug,
                        slug,
                    )


if __name__ == "__main__":
    unittest.main()
