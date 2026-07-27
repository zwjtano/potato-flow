import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import dota2_items


class Dota2ItemsTests(unittest.TestCase):
    def test_common_chinese_aliases_resolve_to_official_items(self):
        matches = dota2_items.match_dota2_items(
            "蓝猫先出BKB，随后补羊刀和大根秒人。"
        )
        names = [match.item.english_name for match in matches]
        self.assertEqual(names, ["Black King Bar", "Scythe of Vyse", "Dagon"])

    def test_longer_alias_does_not_also_match_component_item(self):
        matches = dota2_items.match_dota2_items("后期换出大电锤和大隐刀")
        names = [match.item.english_name for match in matches]
        self.assertEqual(names, ["Mjollnir", "Silver Edge"])
        self.assertNotIn("Maelstrom", names)
        self.assertNotIn("Shadow Blade", names)

    def test_ambiguous_single_characters_do_not_trigger_items(self):
        matches = dota2_items.match_dota2_items(
            "他跳上高台开雾后捡到一块盾牌。"
        )
        names = [match.item.english_name for match in matches]
        self.assertEqual(names, ["Smoke of Deceit"])
        self.assertNotIn("Blink Dagger", names)
        self.assertNotIn("Aegis of the Immortal", names)

    def test_item_prompt_explicitly_disambiguates_real_world_objects(self):
        matches = dota2_items.match_dota2_items("羊刀接大炮")
        instruction = dota2_items.dota2_item_prompt_instruction(matches)
        self.assertIn("羊刀＝邪恶镰刀（Scythe of Vyse）", instruction)
        self.assertIn("大炮＝代达罗斯之殇（Daedalus）", instruction)
        self.assertIn("不能按字面画成现实物品", instruction)
        self.assertIn("不得把两件装备融合成一件", instruction)

    def test_reference_sheet_uses_matched_official_icons(self):
        matches = dota2_items.match_dota2_items("BKB和羊刀")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_download(item, cache_dir):
                cache_dir.mkdir(parents=True, exist_ok=True)
                icon = cache_dir / f"{item.icon_slug}.png"
                Image.new("RGBA", (88, 64), "#f59e0b").save(icon)
                return icon

            with patch.object(
                dota2_items,
                "download_dota2_item_icon",
                side_effect=fake_download,
            ):
                sheet, errors = dota2_items.build_dota2_item_reference_sheet(
                    matches,
                    root / "cache",
                    root / "references.png",
                )

            self.assertEqual(errors, [])
            self.assertIsNotNone(sheet)
            self.assertTrue(sheet.is_file())
            with Image.open(sheet) as image:
                self.assertGreaterEqual(image.width, 560)
                self.assertGreater(image.height, 190)


if __name__ == "__main__":
    unittest.main()
