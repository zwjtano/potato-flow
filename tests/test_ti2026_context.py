import importlib.util
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "potatoflow-app" / "ti2026_context.py"
SPEC = importlib.util.spec_from_file_location("ti2026_context", MODULE_PATH)
ti = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ti)


def comment(second, text):
    return types.SimpleNamespace(time=second, text=text)


class Ti2026ContextTests(unittest.TestCase):
    def test_team_and_player_aliases_are_normalized(self):
        self.assertEqual(ti.normalize_ti2026_team("液体"), "Team Liquid")
        self.assertEqual(ti.normalize_ti2026_team("雪碧"), "Team Spirit")
        self.assertEqual(ti.ti2026_team_for_player("Ame"), "Xtreme Gaming")
        self.assertEqual(ti.ti2026_team_for_player("Faith_bian"), "Vici Gaming")
        self.assertEqual(ti.ti2026_team_for_player("Topson"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("普森"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("汤普森"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("森哥"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("托皇"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("上帝之子"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("TaiLung"), "")

    def test_context_detects_ti_series_and_explicit_game_boundaries(self):
        context = ti.build_ti2026_context([
            comment(10, "TI15 XG 对阵液体"),
            comment(120, "第一局BP开始了"),
            comment(2800, "第二局来了"),
            comment(2810, "Ame这把选了水人"),
        ])
        self.assertTrue(context["active"])
        self.assertEqual(context["series_format"], "bo3")
        self.assertEqual(
            context["mentioned_teams"],
            ["Team Liquid", "Xtreme Gaming"],
        )
        self.assertIn(
            {"name": "Ame", "team": "Xtreme Gaming"},
            context["mentioned_players"],
        )
        self.assertEqual(
            [marker["game_number"] for marker in context["series_markers"]],
            [1, 2],
        )

    def test_generic_dota_discussion_does_not_activate_ti_context(self):
        context = ti.build_ti2026_context([
            comment(10, "今天玩一把水人"),
            comment(20, "这个出装不错"),
        ])
        self.assertEqual(context, {"active": False})

    def test_strong_claims_require_verified_timeline_evidence(self):
        context = {"active": True, "series_format": "bo3"}
        self.assertEqual(
            ti.unsupported_ti2026_claim("XG晋级主赛事", "12:00 XG赢下第一局", context),
            "advance",
        )
        self.assertEqual(
            ti.unsupported_ti2026_claim("XG晋级主赛事", "48:00 XG确认晋级主赛事", context),
            "",
        )
        self.assertEqual(
            ti.unsupported_ti2026_claim("XG拿到赛点", "32:00 XG 1比0领先", context),
            "",
        )
        self.assertEqual(
            ti.unsupported_ti2026_claim("XG横扫液体", "55:00 XG 2-0击败液体", context),
            "",
        )

    def test_grand_final_uses_bo5_match_point_threshold(self):
        context = ti.build_ti2026_context([
            comment(1, "TI15总决赛 XG 对阵 Falcons"),
            comment(2, "现在比分2比1"),
        ])
        self.assertEqual(context["phase"], "grand_final")
        self.assertEqual(context["series_format"], "bo5")
        self.assertEqual(
            ti.unsupported_ti2026_claim("XG拿到赛点", "比分2比1", context),
            "",
        )


if __name__ == "__main__":
    unittest.main()
