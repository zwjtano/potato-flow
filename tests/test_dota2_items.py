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
        self.assertIn("自由表现全部已确认装备", instruction)
        self.assertIn("必须清楚表现识别结果中的全部装备", instruction)
        self.assertIn("每件确认装备在整张图中必须恰好出现一次", instruction)
        self.assertIn("穿戴、手持或身旁出现都已经计数", instruction)
        self.assertIn("背景回声、镜像、倒影", instruction)
        self.assertIn("独立道具插画、清晰描边与光效层次", instruction)
        self.assertIn("不得把商店图标原样贴成带黑底和名称的卡片", instruction)
        self.assertIn("主播与英雄一前一后构成主视觉", instruction)
        self.assertIn("重点装备可以更大", instruction)
        self.assertIn("装备不得遮脸、压字或贴边裁断", instruction)
        self.assertNotIn("装备沿画面上缘和两侧错落分布", instruction)
        self.assertIn("只有没有被人物穿戴、手持、背负或腰挂的身旁道具", instruction)

    def test_item_visual_context_prompt_supports_natural_wearing_without_duplicates(self):
        instruction = dota2_items.dota2_item_visual_context_prompt_instruction(
            [
                {
                    "chinese_name": "恐鳌之心",
                    "english_name": "Heart of Tarrasque",
                    "icon_slug": "heart",
                    "lore": "保存完好的心脏，来自早已绝种的怪兽。",
                    "function": "提升携带者的耐久力。",
                },
                {
                    "chinese_name": "远行鞋",
                    "english_name": "Boots of Travel",
                    "icon_slug": "travel_boots",
                    "lore": "足生双翼，上天入地。",
                    "function": "升级回城能力。",
                },
            ]
        )
        self.assertIn("Valve 官方装备背景与功能", instruction)
        self.assertIn("自然设计成人物已经穿戴的护甲、鞋、披风或饰品", instruction)
        self.assertIn("人物身上、手中或身旁已经出现的装备就算完成一次展示", instruction)
        self.assertIn("不得再在边缘、背景、倒影或装饰层复制同一件", instruction)

    def test_item_placement_plan_assigns_one_manifestation_per_item(self):
        matches = dota2_items.match_dota2_items(
            "龙心 大跳 BKB 飞鞋 希瓦 克莱拉牧杖 咒术师触媒 A杖"
        )
        plans = dota2_items.dota2_item_placement_plan(matches)
        by_slug = {plan["icon_slug"]: plan["placement"] for plan in plans}
        self.assertIn("胸甲正中央", by_slug["heart"])
        self.assertIn("手持、背负或腰挂三选一", by_slug["overwhelming_blink"])
        self.assertIn("对应身体部位", by_slug["travel_boots"])
        self.assertIn("随身能量核心", by_slug["conjurers_catalyst"])
        instruction = dota2_items.dota2_item_placement_plan_prompt_instruction(plans)
        self.assertIn("逐件单次实体分配", instruction)
        self.assertIn("每个编号只能对应画面中的一个物理实体", instruction)
        self.assertIn("禁止再沿画面边缘展示它的图标", instruction)
        self.assertIn("泛化护甲、武器和装饰不得复刻名单内装备", instruction)

    def test_official_item_text_removes_html_and_template_tokens(self):
        cleaned = dota2_items._clean_official_item_text(
            "<h1>主动：闪烁</h1>传送到%blink_range%距离。<br><br>持续%duration%秒。"
        )
        self.assertEqual(cleaned, "主动：闪烁传送到距离。；持续秒。")

    def test_title_item_is_prioritized_without_adding_items_outside_gsi(self):
        available = dota2_items.match_dota2_items("魔瓶 BKB 羊刀")
        ordered = dota2_items.prioritize_dota2_item_matches(
            available,
            "羊刀控住对手后翻盘，随后讨论圣剑",
        )
        self.assertEqual(
            [match.item.english_name for match in ordered],
            ["Scythe of Vyse", "Bottle", "Black King Bar"],
        )
        self.assertNotIn(
            "Divine Rapier",
            [match.item.english_name for match in ordered],
        )

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
