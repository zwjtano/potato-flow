import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import dota2_players
from ti2026_context import ti2026_player_portrait_slot


class Dota2PlayerPortraitTests(unittest.TestCase):
    def test_json_fetch_falls_back_to_curl_after_macos_tls_failure(self):
        completed = types.SimpleNamespace(returncode=0, stdout=b'{"ok": true}')
        with patch.object(
            dota2_players.urllib.request,
            "urlopen",
            side_effect=dota2_players.urllib.error.URLError("TLS EOF"),
        ), patch.object(
            dota2_players.shutil, "which", return_value="/usr/bin/curl"
        ), patch.object(
            dota2_players.subprocess, "run", return_value=completed
        ) as run:
            result = dota2_players._fetch_json("https://example.test/data", timeout=3)
        self.assertEqual(result, {"ok": True})
        self.assertIn("--max-time", run.call_args.args[0])

    def test_only_team_representative_portrait_is_downloaded_and_cached(self):
        image = Image.new("RGBA", (1024, 1280), (10, 20, 30, 255))
        raw = io.BytesIO()
        image.save(raw, format="PNG")
        raw.seek(0)

        def fake_fetch(url, _timeout=20):
            if "prop=images" in url:
                return {"parse": {"images": ["Nisha_2026_Team_Liquid.webp"]}}
            if "action=parse" in url:
                return {"parse": {"wikitext": (
                    "{{FileInfo\n|license=permission\n"
                    "|featured=Nisha\n|featured2=Team Liquid\n"
                    "|note=Provided by representative of PARIVISION\n"
                    "|source=https://example.test/team\n}}"
                )}}
            return {"query": {"pages": [{"imageinfo": [{
                "width": 1200, "height": 1500, "mime": "image/webp",
                "url": "https://example.test/noone.webp",
                "descriptionurl": "https://liquipedia.net/commons/File:Nisha_2026_Team_Liquid.webp",
            }]}]}}

        with tempfile.TemporaryDirectory() as temp, patch.object(
            dota2_players, "_fetch_json", side_effect=fake_fetch
        ), patch.object(
            dota2_players.urllib.request, "urlopen", return_value=raw
        ):
            portrait = dota2_players.download_ti_player_portrait(
                "Nisha", "Team Liquid", Path(temp)
            )
            self.assertTrue(Path(portrait.path).is_file())
            self.assertIn("representative", portrait.source_note)
            metadata = json.loads(Path(portrait.path).with_suffix(".json").read_text())
            self.assertEqual(metadata["player_name"], "Nisha")
            slot = ti2026_player_portrait_slot("Nisha", "Team Liquid")
            self.assertEqual(slot["status"], "ready")
            self.assertEqual(slot["path"], portrait.path)
            self.assertEqual(slot["source_page"], portrait.source_page)
            self.assertEqual(portrait.source_kind, "team_representative")

    def test_global_commons_index_recovers_portrait_omitted_from_player_page(self):
        image = Image.new("RGBA", (1024, 1400), (40, 50, 60, 255))
        raw = io.BytesIO()
        image.save(raw, format="PNG")
        raw.seek(0)

        def fake_fetch(url, _timeout=20):
            if "prop=images" in url:
                return {"parse": {"images": []}}
            if "list=allimages" in url:
                return {"query": {"allimages": [
                    {"name": "Ame_2026_Xtreme_Gaming.webp"}
                ]}}
            if "action=parse" in url:
                return {"parse": {"wikitext": (
                    "{{FileInfo\n|license=permission\n"
                    "|featured=Ame\n|featured2=Xtreme Gaming\n"
                    "|note=Provided by Fan, Manager of Xtreme Gaming\n"
                    "|source=https://example.test/xg\n}}"
                )}}
            return {"query": {"pages": [{"imageinfo": [{
                "width": 1200, "height": 1500, "mime": "image/webp",
                "url": "https://example.test/ame.webp",
                "descriptionurl": "https://liquipedia.net/commons/File:Ame.webp",
            }]}]}}

        with tempfile.TemporaryDirectory() as temp, patch.object(
            dota2_players, "_fetch_json", side_effect=fake_fetch
        ), patch.object(
            dota2_players.urllib.request, "urlopen", return_value=raw
        ):
            portrait = dota2_players.download_ti_player_portrait(
                "Ame", "Xtreme Gaming", Path(temp)
            )
        self.assertEqual(portrait.source_kind, "team_representative")
        self.assertEqual(portrait.image_name, "Ame_2026_Xtreme_Gaming.webp")

    def test_official_team_roster_portrait_is_accepted_without_2026_filename(self):
        image = Image.new("RGBA", (1024, 1400), (70, 80, 90, 255))
        raw = io.BytesIO()
        image.save(raw, format="PNG")
        raw.seek(0)
        with tempfile.TemporaryDirectory() as temp, patch.object(
            dota2_players.urllib.request, "urlopen", return_value=raw
        ):
            portrait = dota2_players.download_ti_player_portrait(
                "Raven", "OG", Path(temp)
            )
        self.assertEqual(portrait.source_kind, "official_team_website")
        self.assertEqual(portrait.source_page, "https://ogs.gg/players/raven/")

    def test_failed_fetch_is_visible_in_original_roster_slot(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            dota2_players, "_fetch_json", return_value={"parse": {"images": []}}
        ):
            with self.assertRaisesRegex(ValueError, "未找到"):
                dota2_players.download_ti_player_portrait(
                    "Ame", "Xtreme Gaming", Path(temp)
                )
        slot = ti2026_player_portrait_slot("Ame", "Xtreme Gaming")
        self.assertEqual(slot["status"], "fetch_failed")
        self.assertIn("未找到", slot["error"])

    def test_rate_limit_is_visible_in_original_roster_slot(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            dota2_players, "_fetch_json", side_effect=RuntimeError("rate limited")
        ):
            with self.assertRaisesRegex(RuntimeError, "rate limited"):
                dota2_players.download_ti_player_portrait(
                    "Nisha", "Team Liquid", Path(temp)
                )
        slot = ti2026_player_portrait_slot("Nisha", "Team Liquid")
        self.assertEqual(slot["status"], "fetch_failed")
        self.assertIn("rate limited", slot["error"])


if __name__ == "__main__":
    unittest.main()
