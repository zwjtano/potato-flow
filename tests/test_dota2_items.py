import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from PIL import Image

APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

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

    def test_streamer_slang_xuela_resolves_to_bloodthorn(self):
        matches = dota2_items.match_dota2_items("最后补一把血辣")
        self.assertEqual([match.item.english_name for match in matches], ["Bloodthorn"])

    def test_streamer_item_aliases_resolve_to_official_items(self):
        cases = {
            "秘法": "Arcane Boots",
            "鞋子": "Boots of Speed",
            "瓶子": "Bottle",
            "小蓝": "Clarity",
            "情趣外衣": "Craggy Coat",
            "大药": "Healing Salve",
            "小莲": "Healing Lotus",
            "挑战": "Hood of Defiance",
            "打野爪": "Iron Talon",
            "法克": "Mage Slayer",
            "大魔棒": "Magic Wand",
            "小魔棒": "Magic Stick",
            "小勋章": "Medallion of Courage",
            "假眼": "Observer Ward",
            "真眼": "Sentry Ward",
            "补刀斧": "Quelling Blade",
            "天鹰戒": "Ring of Aquila",
            "回五": "Ring of Health",
            "铲子": "Scrying Shovel",
            "吃树": "Tango",
            "经验书": "Tome of Knowledge",
            "绿鞋": "Tranquil Boots",
            "tp": "Town Portal Scroll",
        }
        for alias, expected in cases.items():
            with self.subTest(alias=alias):
                matches = dota2_items.match_dota2_items(f"主播出了{alias}")
                self.assertEqual(matches[0].item.english_name, expected)

        shared = dota2_items.match_dota2_items("树之祭祀（共享）")
        self.assertEqual(shared[0].item.english_name, "Tango (Shared)")

    def test_removed_item_aliases_no_longer_resolve(self):
        self.assertEqual(dota2_items.match_dota2_items("刀甲和小炮"), [])

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
