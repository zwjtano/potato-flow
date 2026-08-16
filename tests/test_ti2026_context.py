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
    def test_plain_ti_live_title_activates_during_event_window(self):
        context = ti.build_ti2026_context(
            [], "Ti全程解说~", event_date="2026-08-14"
        )
        self.assertTrue(context["active"])
        self.assertEqual(context["mode"], "ti_competition")

    def test_group_stage_date_overrides_incidental_main_event_words(self):
        context = ti.build_ti2026_context(
            [], "TI 2026 讨论淘汰赛形势", event_date="2026-08-14"
        )
        self.assertEqual(context["phase"], "group_stage")

    def test_august_16_pregame_rolls_into_elimination_round(self):
        context = ti.build_ti2026_context(
            [], "TI 2026 Falcons 对阵 VG", event_date="2026-08-16T09:47:00+08:00"
        )
        self.assertEqual(context["phase"], "elimination_round")
        self.assertEqual(ti.ti2026_phase_label(context["phase"]), "淘汰轮")

    def test_august_16_schedule_overrides_incidental_grand_final_chat(self):
        context = ti.build_ti2026_context(
            [],
            "TI 2026 Spirit 对阵 Resilience，弹幕顺带讨论总决赛",
            event_date="2026-08-16T12:47:00+08:00",
        )
        self.assertEqual(context["phase"], "elimination_round")
        self.assertEqual(context["series_format"], "bo3")

    def test_august_16_early_recording_remains_group_stage_without_pairing(self):
        context = ti.build_ti2026_context(
            [], "TI 2026 赛程回顾", event_date="2026-08-16T08:00:00+08:00"
        )
        self.assertEqual(context["phase"], "group_stage")

    def test_elimination_pairing_overrides_early_recording_time(self):
        context = ti.build_ti2026_context(
            [], "TI 2026 Spirit 对阵 Resilience", event_date="2026-08-16T09:00:00+08:00"
        )
        self.assertEqual(context["phase"], "elimination_round")

    def test_august_17_to_19_are_rest_days(self):
        for day in (17, 18, 19):
            with self.subTest(day=day):
                context = ti.build_ti2026_context(
                    [],
                    "TI 2026 Falcons 对阵 VG 总决赛预测",
                    event_date=f"2026-08-{day:02d}T14:00:00+08:00",
                )
                self.assertEqual(context["phase"], "intermission")
                self.assertEqual(ti.ti2026_phase_label(context["phase"]), "休赛日")

    def test_verified_grand_final_uses_specific_label_and_bo5(self):
        context = ti.build_ti2026_context(
            [], "TI 2026 决赛日", event_date="2026-08-23T18:00:00+08:00"
        )
        match = {"round_label": "Grand Final"}
        self.assertEqual(ti.ti2026_match_round_label(match["round_label"]), "总决赛")
        self.assertEqual(ti.ti2026_match_series_format(context, match), "bo5")

    def test_match_end_inside_recording_sets_exact_cutoff(self):
        result = ti.recording_match_end_cutoff(
            "2026-08-20T18:00:00+08:00",
            3600,
            "2026-08-20T18:42:15+08:00",
        )
        self.assertTrue(result["contains_match_end"])
        self.assertEqual(result["cutoff_seconds"], 2535.0)

    def test_match_end_after_recording_is_not_assigned(self):
        result = ti.recording_match_end_cutoff(
            "2026-08-20T18:00:00+08:00",
            3600,
            "2026-08-20T19:00:01+08:00",
        )
        self.assertFalse(result["contains_match_end"])
        self.assertIsNone(result["cutoff_seconds"])

    def test_comments_after_match_end_are_removed(self):
        comments = [comment(10, "BP"), comment(2535, "GG"), comment(2600, "下一场")]
        kept = ti.comments_through_match_end(comments, 2535)
        self.assertEqual([item.text for item in kept], ["BP", "GG"])

    def test_team_and_player_aliases_are_normalized(self):
        self.assertEqual(ti.normalize_ti2026_team("液体"), "Team Liquid")
        self.assertEqual(ti.normalize_ti2026_team("雪碧"), "Team Spirit")
        self.assertEqual(ti.ti2026_team_for_player("Ame"), "Xtreme Gaming")
        self.assertEqual(ti.ti2026_team_for_player("萧瑟"), "Xtreme Gaming")
        self.assertEqual(ti.ti2026_team_for_player("哥哥"), "Xtreme Gaming")
        self.assertEqual(ti.ti2026_team_for_player("责任神"), "Xtreme Gaming")
        self.assertEqual(ti.ti2026_team_for_player("豆腐"), "Team Liquid")
        self.assertEqual(ti.ti2026_team_for_player("普洱"), "Iron Wing")
        self.assertEqual(ti.normalize_ti2026_team("1win Team"), "Iron Wing")
        self.assertEqual(ti.normalize_ti2026_team("IW"), "Iron Wing")
        self.assertEqual(ti.ti2026_team_for_player("小学生"), "Xtreme Gaming")
        self.assertEqual(ti.ti2026_team_for_player("Noone"), "TEAM VISION")
        self.assertEqual(ti.ti2026_team_for_player("心情"), "Vici Gaming")
        self.assertEqual(ti.ti2026_team_for_player("Faith_bian"), "Vici Gaming")
        self.assertEqual(ti.ti2026_team_for_player("Bach"), "Vici Gaming")
        self.assertEqual(ti.ti2026_team_for_player("poyoyo"), "Team Resilience")
        self.assertEqual(ti.ti2026_team_for_player("Topson"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("普森"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("汤普森"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("森哥"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("托皇"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("上帝之子"), "LGD Gaming")
        self.assertEqual(ti.ti2026_team_for_player("TaiLung"), "")

    def test_player_portrait_alias_resolves_original_roster_slot(self):
        slot = ti.ti2026_player_portrait_slot("Noone", "PARIVISION")
        self.assertIsNotNone(slot)
        self.assertEqual(slot["status"], "awaiting_official_asset")
        self.assertIs(slot, ti.TI2026_TEAMS[8]["player_portraits"]["No[o]ne-"])
        self.assertIsNone(ti.ti2026_player_portrait_slot("Noone", "Team Liquid"))

    def test_every_ti_team_has_exactly_five_current_players(self):
        self.assertTrue(all(len(team["players"]) == 5 for team in ti.TI2026_TEAMS))
        roster = {
            player
            for team in ti.TI2026_TEAMS
            for player in team["players"]
        }
        self.assertEqual(set(ti.TI2026_PLAYER_ALIASES), roster)
        self.assertTrue(all(team["aliases"] for team in ti.TI2026_TEAMS))
        self.assertTrue(all(team["team_id"] for team in ti.TI2026_TEAMS))
        self.assertTrue(all(team["logo_path"].endswith(".png") for team in ti.TI2026_TEAMS))
        self.assertTrue(all(len(team["player_portraits"]) == 5 for team in ti.TI2026_TEAMS))
        app_root = MODULE_PATH.parent
        self.assertTrue(all((app_root / team["logo_path"]).is_file() for team in ti.TI2026_TEAMS))

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

    def test_same_teams_outside_ti_window_do_not_activate_competition_mode(self):
        context = ti.build_ti2026_context(
            [comment(10, "XG 对阵液体，第一局BP")],
            event_date="2026-07-01",
        )
        self.assertEqual(context, {"active": False})

    def test_two_ti_teams_during_event_activate_competition_mode(self):
        context = ti.build_ti2026_context(
            [comment(10, "XG 对阵液体，第一局BP")],
            event_date="2026-08-15",
        )
        self.assertTrue(context["active"])
        self.assertEqual(context["mode"], "ti_competition")
        self.assertFalse(context["explicit_event_identity"])
        self.assertTrue(context["inside_event_window"])

    def test_explicit_ti_identity_activates_outside_window(self):
        context = ti.build_ti2026_context(
            [comment(10, "TI15 XG 对阵液体")],
            event_date="2026-07-01",
        )
        self.assertEqual(context["mode"], "ti_competition")
        self.assertTrue(context["explicit_event_identity"])

    def test_recording_date_is_extracted_from_standard_filename(self):
        self.assertEqual(
            ti.ti2026_event_date_from_filename(
                "国民大舅哥_二点s10.5赛季百人逃杀_2026-08-15_15-33.flv"
            ),
            "2026-08-15",
        )
        self.assertEqual(
            ti.ti2026_event_date_from_filename("recording_2026_08_23_20-00.mp4"),
            "2026-08-23",
        )
        self.assertEqual(
            ti.ti2026_event_date_from_filename("recording_2026-02-30.mp4"),
            "",
        )

    def test_recording_datetime_uses_china_timezone(self):
        self.assertEqual(
            ti.ti2026_recording_datetime_from_filename(
                "recording_2026-08-15_00-30-05.flv"
            ),
            "2026-08-15T00:30:05+08:00",
        )

    def test_china_date_maps_to_previous_utc_date_at_midnight(self):
        self.assertEqual(
            ti.liquipedia_utc_window_for_china_date("2026-08-15"),
            {
                "start_utc": "2026-08-14T16:00:00Z",
                "end_utc_exclusive": "2026-08-15T16:00:00Z",
            },
        )

    def test_liquipedia_utc_timestamp_is_displayed_in_china_time(self):
        self.assertEqual(
            ti.liquipedia_timestamp_to_china("2026-08-14T16:30:00Z"),
            "2026-08-15T00:30:00+08:00",
        )
        self.assertEqual(
            ti.liquipedia_timestamp_to_china("2026-08-14 16:30:00"),
            "2026-08-15T00:30:00+08:00",
        )

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
