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
        self.assertIn("丰富表现全部已确认装备", instruction)
        self.assertIn("必须严格服从后续逐件位置计划", instruction)
        self.assertIn("必须清楚表现识别结果中的全部装备", instruction)
        self.assertIn("按整数倍无插值放大", instruction)
        self.assertIn("二创为边缘清楚、材质明确的高清实体装备", instruction)
        self.assertIn("不得偏离官方轮廓、主色、构件关系和核心符号", instruction)
        self.assertIn("每件确认装备在整张图中必须恰好出现一次", instruction)
        self.assertIn("穿戴、手持或身旁出现都已经计数", instruction)
        self.assertIn("背景回声、镜像、倒影", instruction)
        self.assertIn("独立道具插画、清晰描边与光效层次", instruction)
        self.assertIn("不得把商店图标原样贴成带黑底和名称的卡片", instruction)
        self.assertIn("只画该人物 Cos 已确认英雄作为唯一人物主视觉", instruction)
        self.assertIn("禁止再画英雄本体", instruction)
        self.assertIn("重点装备可以更大", instruction)
        self.assertIn("装备不得遮脸、压字或贴边裁断", instruction)
        self.assertNotIn("装备沿画面上缘和两侧错落分布", instruction)
        self.assertIn("只有没有被人物穿戴、手持、背负或腰挂的身旁道具", instruction)
        self.assertIn("禁止环形、放射状、花环式", instruction)
        self.assertIn("美观与识别度同等重要", instruction)
        self.assertIn("合理透视、材质光影、前后景和人物动作", instruction)
        self.assertNotIn("其余装备仍须作为边缘辅助信息", instruction)

    def test_item_visual_context_prompt_supports_natural_wearing_without_duplicates(self):
        instruction = dota2_items.dota2_item_visual_context_prompt_instruction(
            [
                {
                    "chinese_name": "恐鳌之心",
                    "english_name": "Heart of Tarrasque",
                    "icon_slug": "heart",
                    "lore": "保存完好的心脏，来自早已绝种的怪兽。",
                    "function": "提升携带者的耐久力。",
                    "item_cost": 5200,
                },
                {
                    "chinese_name": "远行鞋",
                    "english_name": "Boots of Travel",
                    "icon_slug": "travel_boots",
                    "lore": "足生双翼，上天入地。",
                    "function": "升级回城能力。",
                    "item_cost": 2500,
                },
            ]
        )
        self.assertIn("Valve 官方装备背景与功能", instruction)
        self.assertIn("官方价格：5200 金币", instruction)
        self.assertIn("自然设计成人物已经穿戴的护甲、鞋、披风或饰品", instruction)
        self.assertIn("人物身上、手中或身旁已经出现的装备就算完成一次展示", instruction)
        self.assertIn("不得再在边缘、背景、倒影或装饰层复制同一件", instruction)

    def test_item_placement_plan_assigns_one_manifestation_per_item(self):
        matches = dota2_items.match_dota2_items(
            "龙心 大跳 BKB 飞鞋 希瓦 克莱拉牧杖 咒术师触媒 A杖"
        )
        plans = dota2_items.dota2_item_placement_plan(
            matches,
            hero_primary_attribute="strength",
            item_visual_contexts=[
                {"icon_slug": "heart", "item_cost": 5200},
                {"icon_slug": "overwhelming_blink", "item_cost": 6800},
                {"icon_slug": "black_king_bar", "item_cost": 4050},
                {"icon_slug": "travel_boots", "item_cost": 2500},
                {"icon_slug": "shivas_guard", "item_cost": 5175},
                {"icon_slug": "ultimate_scepter", "item_cost": 4200},
            ],
        )
        by_slug = {plan["icon_slug"]: plan["placement"] for plan in plans}
        plan_by_slug = {plan["icon_slug"]: plan for plan in plans}
        self.assertIn("胸甲正中央", by_slug["heart"])
        self.assertIn("适合手持的装备中最高", by_slug["overwhelming_blink"])
        self.assertIn("6800 金币", by_slug["overwhelming_blink"])
        self.assertIn("双脚和小腿", by_slug["travel_boots"])
        self.assertIn("独立能量插槽", by_slug["conjurers_catalyst"])
        self.assertIn("阿哈利姆神杖就是 A 杖", by_slug["ultimate_scepter"])
        self.assertIn("不是智力英雄", by_slug["ultimate_scepter"])
        self.assertIn("禁止手持", by_slug["ultimate_scepter"])
        self.assertTrue(plan_by_slug["heart"]["high_value"])
        self.assertIn("属于高级装备", by_slug["heart"])
        self.assertTrue(plan_by_slug["travel_boots"]["high_value"])
        self.assertFalse(plan_by_slug["conjurers_catalyst"]["high_value"])
        a_scepter_matches = dota2_items.match_dota2_items("A杖 BKB")
        intelligence_plans = dota2_items.dota2_item_placement_plan(
            a_scepter_matches,
            hero_primary_attribute="intelligence",
            item_visual_contexts=[
                {"icon_slug": "ultimate_scepter", "item_cost": 4200},
                {"icon_slug": "black_king_bar", "item_cost": 4050},
            ],
        )
        intelligence_by_slug = {
            plan["icon_slug"]: plan["placement"]
            for plan in intelligence_plans
        }
        self.assertIn("该英雄为智力英雄", intelligence_by_slug["ultimate_scepter"])
        self.assertIn("官方价格最高", intelligence_by_slug["ultimate_scepter"])
        self.assertIn("直接拿在人物右手中", intelligence_by_slug["ultimate_scepter"])
        self.assertEqual(
            sum(bool(plan["is_primary_handheld"]) for plan in intelligence_plans),
            1,
        )
        instruction = dota2_items.dota2_item_placement_plan_prompt_instruction(plans)
        self.assertIn("逐件单次实体分配", instruction)
        self.assertIn("每个编号只能对应画面中的一个物理实体", instruction)
        self.assertIn("禁止再沿画面边缘展示它的图标", instruction)
        self.assertIn("自然穿戴或持握不能牺牲装备辨识度", instruction)
        self.assertIn("右手主装备和左手副武器", instruction)
        self.assertIn("禁止把两件融合、交叉遮没、连成一把", instruction)
        self.assertIn("缩略图中仍应逐件可辨", instruction)
        self.assertIn("达到 2500 金币的高级装备全部使用高辨识优先", instruction)
        self.assertIn("禁止重新排成围绕人物的图标圈", instruction)
        self.assertIn("泛化护甲、武器和装饰不得复刻名单内装备", instruction)

    def test_dual_hand_plan_keeps_wings_chest_core_and_back_blade_distinct(self):
        matches = dota2_items.match_dota2_items(
            "假腿 冰眼 大晕锤 金箍棒 蝴蝶 白银之锋"
        )
        plans = dota2_items.dota2_item_placement_plan(
            matches,
            hero_primary_attribute="agility",
            item_visual_contexts=[
                {"icon_slug": "power_treads", "item_cost": 1400},
                {"icon_slug": "skadi", "item_cost": 5300},
                {"icon_slug": "abyssal_blade", "item_cost": 6250},
                {"icon_slug": "monkey_king_bar", "item_cost": 4975},
                {"icon_slug": "butterfly", "item_cost": 5450},
                {"icon_slug": "silver_edge", "item_cost": 5450},
            ],
        )
        by_slug = {plan["icon_slug"]: plan for plan in plans}

        self.assertEqual(by_slug["power_treads"]["slot"], "feet")
        self.assertIn("双脚和小腿", by_slug["power_treads"]["placement"])
        self.assertEqual(by_slug["skadi"]["slot"], "chest_frost_core")
        self.assertIn("胸甲正中央", by_slug["skadi"]["placement"])
        self.assertIn("蓝白冰霜", by_slug["skadi"]["placement"])
        self.assertEqual(by_slug["abyssal_blade"]["slot"], "direct_hand")
        self.assertTrue(by_slug["abyssal_blade"]["is_primary_handheld"])
        self.assertIn("人物右手", by_slug["abyssal_blade"]["placement"])
        self.assertEqual(by_slug["monkey_king_bar"]["slot"], "direct_offhand")
        self.assertTrue(by_slug["monkey_king_bar"]["is_secondary_handheld"])
        self.assertIn("人物左手", by_slug["monkey_king_bar"]["placement"])
        self.assertEqual(by_slug["butterfly"]["slot"], "back_green_wings")
        self.assertIn("绿色发光双翼", by_slug["butterfly"]["placement"])
        self.assertEqual(by_slug["silver_edge"]["slot"], "back_center_blade")
        self.assertIn("从绿色双翼中央穿出", by_slug["silver_edge"]["placement"])
        self.assertFalse(by_slug["silver_edge"]["is_secondary_handheld"])
        self.assertEqual(
            sum(bool(plan["is_primary_handheld"]) for plan in plans),
            1,
        )
        self.assertEqual(
            sum(bool(plan["is_secondary_handheld"]) for plan in plans),
            1,
        )

    def test_orb_items_hover_near_character_without_forming_a_ring(self):
        matches = dota2_items.match_dota2_items("刷新球 玲珑心 林肯法球 血精石")
        plans = dota2_items.dota2_item_placement_plan(matches)
        by_slug = {plan["icon_slug"]: plan for plan in plans}

        for slug in ("refresher", "octarine_core", "sphere", "bloodstone"):
            self.assertEqual(by_slug[slug]["slot"], "floating_orb")
            self.assertTrue(by_slug[slug]["floating_allowed"])
            self.assertIn("手掌上方或单侧肩旁", by_slug[slug]["placement"])
            self.assertIn("不得嵌进身体", by_slug[slug]["placement"])
            self.assertIn("不得", by_slug[slug]["placement"])
            self.assertIn("排成一圈", by_slug[slug]["placement"])

    def test_all_maintained_and_runtime_items_have_explicit_placement_presets(self):
        maintained = {item.icon_slug for item in dota2_items.DOTA2_ITEMS}
        runtime = set(dota2_items.DOTA2_KNOWN_RUNTIME_ITEM_SLUGS)
        presets = set(dota2_items.DOTA2_ITEM_PLACEMENT_PRESETS)
        self.assertEqual(maintained - presets, set())
        self.assertEqual(runtime - presets, set())
        self.assertEqual(len(maintained | runtime), len(presets))
        floating = {
            slug
            for slug, preset in dota2_items.DOTA2_ITEM_PLACEMENT_PRESETS.items()
            if preset["floating_allowed"]
        }
        self.assertEqual(
            floating,
            {
                "refresher", "octarine_core", "lotus_orb", "sphere",
                "bloodstone", "moon_shard", "gem", "refresher_shard",
            },
        )

    def test_shape_specific_items_use_matching_physical_slots(self):
        presets = dota2_items.DOTA2_ITEM_PLACEMENT_PRESETS
        self.assertEqual(presets["witch_blade"]["slot"], "waist_short_weapon")
        self.assertEqual(presets["spellslinger"]["slot"], "neck_chest_accessory")
        self.assertEqual(presets["eagle"]["slot"], "back_long_weapon")
        self.assertEqual(presets["relic"]["slot"], "back_blade")
        self.assertEqual(presets["partisans_brand"]["slot"], "forearm")
        self.assertEqual(presets["stormcrafter"]["slot"], "body_core")

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
                self.assertGreaterEqual(image.width, 688)
                self.assertGreater(image.height, 264)

    def test_small_official_icon_is_upscaled_and_sharpened_for_model_reference(self):
        source = Image.new("RGBA", (88, 64), "#101820")
        for offset in range(20):
            source.putpixel((20 + offset, 20), (255, 40, 180, 255))

        prepared = dota2_items.prepare_dota2_item_reference_icon(source)

        self.assertEqual(prepared.size, (264, 192))
        self.assertEqual(prepared.mode, "RGBA")
        self.assertEqual(prepared.getpixel((60, 60)), source.getpixel((20, 20)))

if __name__ == "__main__":
    unittest.main()
