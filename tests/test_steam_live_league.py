import importlib.util
import json
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "potatoflow-app" / "modules" / "steam_live_league.py"
SPEC = importlib.util.spec_from_file_location("steam_live_league", MODULE_PATH)
steam = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(steam)


class SteamLiveLeagueTests(unittest.TestCase):
    def test_normalizes_scoreboard_and_series_fields(self):
        result = steam.normalize_live_game({
            "match_id": 123,
            "league_id": 987,
            "game_number": 2,
            "stream_delay_s": 120,
            "radiant_series_wins": 1,
            "dire_series_wins": 0,
            "radiant_team": {"team_name": "LGD Gaming"},
            "dire_team": {"team_name": "Team Falcons"},
            "scoreboard": {
                "duration": 2535.5,
                "radiant": {
                    "score": 31,
                    "picks": [{"hero_id": 13}],
                    "players": [{"hero_id": 13, "kills": 10, "death": 2, "assists": 15, "net_worth": 22000}],
                },
                "dire": {"score": 24, "picks": [{"hero_id": 1}], "players": []},
            },
        })
        self.assertEqual(result["match_id"], 123)
        self.assertEqual(result["duration_seconds"], 2535.5)
        self.assertEqual(result["radiant"]["kills"], 31)
        self.assertEqual(result["radiant"]["players"][0]["deaths"], 2)
        self.assertEqual(result["radiant_series_wins"], 1)

    def test_missing_key_fails_without_secret_details(self):
        with mock.patch.dict(steam.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "STEAM_WEB_API_KEY"):
                steam.fetch_live_league_games(api_key="", config={})

    def test_config_key_takes_precedence_over_environment(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"result": {"games": []}}).encode()
        with mock.patch.dict(steam.os.environ, {"STEAM_WEB_API_KEY": "env-key"}), mock.patch.object(
            steam.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(
                steam.fetch_live_league_games(config={"STEAM_WEB_API_KEY": "config-key"}),
                [],
            )
        requested_url = urlopen.call_args.args[0].full_url
        self.assertIn("config-key", requested_url)
        self.assertNotIn("env-key", requested_url)


if __name__ == "__main__":
    unittest.main()
