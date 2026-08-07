"""Dota 2 item names, Chinese community aliases, and official icon references."""

from __future__ import annotations

import concurrent.futures
import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError


DOTA2_ITEM_DATA_SOURCE = "https://www.dota2.com/datafeed/itemlist?language=schinese"
DOTA2_ITEM_DETAIL_SOURCE = (
    "https://www.dota2.com/datafeed/itemdata?item_id={item_id}&language=schinese"
)
DOTA2_ITEM_ICON_BASE_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/"
    "dota_react/items"
)
MAX_MATCHED_ITEMS = 8
MAX_ITEM_ICON_BYTES = 4 * 1024 * 1024
HIGH_VALUE_ITEM_COST = 2500
DOTA2_COVER_LAYOUT_CLASSIC = "classic"
DOTA2_COVER_LAYOUT_FUSION = "fusion"
DOTA2_COVER_LAYOUT_MODES = {
    DOTA2_COVER_LAYOUT_CLASSIC,
    DOTA2_COVER_LAYOUT_FUSION,
}


def normalize_dota2_cover_layout_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in DOTA2_COVER_LAYOUT_MODES else DOTA2_COVER_LAYOUT_CLASSIC


@dataclass(frozen=True)
class Dota2Item:
    chinese_name: str
    english_name: str
    icon_slug: str
    aliases: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.chinese_name}（{self.english_name}）"


@dataclass(frozen=True)
class Dota2ItemMatch:
    item: Dota2Item
    alias: str
    start: int
    end: int


def _item(
    chinese_name: str,
    english_name: str,
    icon_slug: str,
    *aliases: str,
) -> Dota2Item:
    names = tuple(
        dict.fromkeys(
            (
                *aliases,
                chinese_name,
                english_name,
            )
        )
    )
    return Dota2Item(chinese_name, english_name, icon_slug, names)


# Official names and icon slugs are aligned with Valve's Dota 2 Datafeed.
# Chinese aliases are an explicitly maintained PotatoFlow compatibility table.
# Avoid ambiguous single Chinese characters such as “羊”“跳”“盾” and “雾”.
DOTA2_ITEMS: tuple[Dota2Item, ...] = (
    _item("奥术鞋", "Arcane Boots", "arcane_boots", "秘法"),
    _item("速度之靴", "Boots of Speed", "boots", "鞋", "鞋子"),
    _item("闪烁匕首", "Blink Dagger", "blink", "跳刀"),
    _item("盛势闪光", "Overwhelming Blink", "overwhelming_blink", "力量跳", "大跳", "红跳"),
    _item("迅疾闪光", "Swift Blink", "swift_blink", "敏捷跳", "绿跳"),
    _item("秘奥闪光", "Arcane Blink", "arcane_blink", "智力跳", "蓝跳"),
    _item("动力鞋", "Power Treads", "power_treads", "假腿"),
    _item("相位鞋", "Phase Boots", "phase_boots", "相位"),
    _item("远行鞋", "Boots of Travel", "travel_boots", "飞鞋", "旅行鞋"),
    _item("迈达斯之手", "Hand of Midas", "hand_of_midas", "点金", "点金手"),
    _item("黑皇杖", "Black King Bar", "black_king_bar", "BKB", "黑皇"),
    _item("邪恶镰刀", "Scythe of Vyse", "sheepstick", "羊刀", "妖术杖"),
    _item("紫怨", "Orchid Malevolence", "orchid", "紫苑"),
    _item("血棘", "Bloodthorn", "bloodthorn", "大紫怨", "血辣"),
    _item("Eul的神圣法杖", "Eul's Scepter of Divinity", "cyclone", "风杖", "吹风杖", "吹风"),
    _item("风之杖", "Wind Waker", "wind_waker", "大吹风"),
    _item("原力法杖", "Force Staff", "force_staff", "推推", "推推棒"),
    _item("飓风长戟", "Hurricane Pike", "hurricane_pike", "大推推", "飓风长矛"),
    _item("达贡之神力", "Dagon", "dagon", "大根", "红杖"),
    _item("阿托斯之棍", "Rod of Atos", "rod_of_atos", "阿托斯", "阿托斯之杖"),
    _item("缚灵索", "Gleipnir", "gungir", "雷托斯", "缚灵锁"),
    _item("以太透镜", "Aether Lens", "aether_lens", "以太", "以太镜"),
    _item("巫师之刃", "Witch Blade", "witch_blade", "巫师刀", "女巫刀"),
    _item("绝刃", "Khanda", "angels_demise", "大灵匣"),
    _item("灵匣", "Phylactery", "phylactery", "小灵匣"),
    _item("阿哈利姆神杖", "Aghanim's Scepter", "ultimate_scepter", "A杖", "蓝杖", "神杖"),
    _item("阿哈利姆魔晶", "Aghanim's Shard", "aghanims_shard", "魔晶", "A魔晶"),
    _item("阿哈利姆福佑", "Aghanim's Blessing", "ultimate_scepter_2", "A杖福佑", "吃A"),
    _item("刷新球", "Refresher Orb", "refresher", "刷新"),
    _item("玲珑心", "Octarine Core", "octarine_core", "减CD球", "玲珑"),
    _item("否决坠饰", "Nullifier", "nullifier", "否决", "否决挂件"),
    _item("清莲宝珠", "Lotus Orb", "lotus_orb", "莲花", "莲花球"),
    _item("林肯法球", "Linken's Sphere", "sphere", "林肯", "林肯球"),
    _item("希瓦的守护", "Shiva's Guard", "shivas_guard", "冰甲", "希瓦"),
    _item("血精石", "Bloodstone", "bloodstone", "血精"),
    _item("恐鳌之心", "Heart of Tarrasque", "heart", "龙心", "大心"),
    _item("强袭胸甲", "Assault Cuirass", "assault", "强袭"),
    _item("永恒之盘", "Aeon Disk", "aeon_disk", "盘子", "永恒盘"),
    _item("洞察烟斗", "Pipe of Insight", "pipe", "笛子", "烟斗"),
    _item("梅肯斯姆", "Mekansm", "mekansm", "梅肯"),
    _item("卫士胫甲", "Guardian Greaves", "guardian_greaves", "大鞋", "卫士鞋"),
    _item("影之灵龛", "Urn of Shadows", "urn_of_shadows", "骨灰", "骨灰盒"),
    _item("魂之灵瓮", "Spirit Vessel", "spirit_vessel", "大骨灰", "魂瓮"),
    _item("微光披风", "Glimmer Cape", "glimmer_cape", "微光"),
    _item("圣洁吊坠", "Holy Locket", "holy_locket", "奶坠", "圣洁"),
    _item("纷争面纱", "Veil of Discord", "veil_of_discord", "纷争", "纷争面罩"),
    _item("先锋盾", "Vanguard", "vanguard", "先锋"),
    _item("赤红甲", "Crimson Guard", "crimson_guard", "赤红", "赤甲"),
    _item("刃甲", "Blade Mail", "blade_mail", "反甲"),
    _item("挑战头巾", "Hood of Defiance", "hood_of_defiance", "挑战头", "挑战"),
    _item("永世法衣", "Eternal Shroud", "eternal_shroud", "永恒法衣", "法衣"),
    _item("幽魂权杖", "Ghost Scepter", "ghost", "绿杖", "幽魂杖"),
    _item("圣剑", "Divine Rapier", "rapier", "圣剑"),
    _item("金箍棒", "Monkey King Bar", "monkey_king_bar", "MKB", "金箍棒"),
    _item("辉耀", "Radiance", "radiance", "辉耀"),
    _item("蝴蝶", "Butterfly", "butterfly", "蝴蝶"),
    _item("代达罗斯之殇", "Daedalus", "greater_crit", "大炮", "代达罗斯"),
    _item("水晶剑", "Crystalys", "lesser_crit"),
    _item("碎颅锤", "Skull Basher", "basher", "晕锤", "小晕锤"),
    _item("深渊之刃", "Abyssal Blade", "abyssal_blade", "大晕锤", "深渊"),
    _item("狂战斧", "Battle Fury", "bfury", "狂战"),
    _item("幻影斧", "Manta Style", "manta", "分身斧", "分身"),
    _item("莫尔迪基安的臂章", "Armlet of Mordiggian", "armlet", "臂章"),
    _item("影刃", "Shadow Blade", "invis_sword", "隐刀"),
    _item("白银之锋", "Silver Edge", "silver_edge", "大隐刀", "白银锋"),
    _item("撒旦之邪力", "Satanic", "satanic", "撒旦"),
    _item("斯嘉蒂之眼", "Eye of Skadi", "skadi", "冰眼", "斯嘉蒂"),
    _item("漩涡", "Maelstrom", "maelstrom", "小电锤", "电锤"),
    _item("雷神之锤", "Mjollnir", "mjollnir", "大电锤", "雷锤"),
    _item("黯灭", "Desolator", "desolator", "暗灭", "减甲刀"),
    _item("疯狂面具", "Mask of Madness", "mask_of_madness", "疯脸", "疯面", "疯狂面具"),
    _item("净魂之刃", "Diffusal Blade", "diffusal_blade", "散失", "散失之刃"),
    _item("散魂剑", "Disperser", "disperser", "大散失", "散魂"),
    _item("虚灵之刃", "Ethereal Blade", "ethereal_blade", "虚灵刀", "虚灵之刃"),
    _item("魔龙枪", "Dragon Lance", "dragon_lance", "龙枪"),
    _item("法师克星", "Mage Slayer", "mage_slayer", "法师杀手", "法克"),
    _item("回音战刃", "Echo Sabre", "echo_sabre", "回音刀"),
    _item("天堂之戟", "Heaven's Halberd", "heavens_halberd", "天堂", "天堂戟"),
    _item("散华", "Sange", "sange", "散华"),
    _item("夜叉", "Yasha", "yasha", "夜叉"),
    _item("慧光", "Kaya", "kaya", "慧光"),
    _item("散夜对剑", "Sange and Yasha", "sange_and_yasha", "双刀", "散夜"),
    _item("散慧对剑", "Kaya and Sange", "kaya_and_sange", "散慧"),
    _item("慧夜对剑", "Yasha and Kaya", "yasha_and_kaya", "慧夜"),
    _item("支配头盔", "Helm of the Dominator", "helm_of_the_dominator", "支配"),
    _item("统御头盔", "Helm of the Overlord", "helm_of_the_overlord", "大支配", "统御"),
    _item("炎阳纹章", "Solar Crest", "solar_crest", "大勋章", "炎阳"),
    _item("韧鼓", "Drum of Endurance", "ancient_janggo", "战鼓"),
    _item("宽容之靴", "Boots of Bearing", "boots_of_bearing", "大鼓", "宽容鞋"),
    _item("陨星锤", "Meteor Hammer", "meteor_hammer", "陨星", "流星锤"),
    _item("灵魂之戒", "Soul Ring", "soul_ring", "魂戒"),
    _item("银月之晶", "Moon Shard", "moon_shard", "银月", "月亮碎片"),
    _item("真视宝石", "Gem of True Sight", "gem", "宝石", "真眼宝石"),
    _item("诡计之雾", "Smoke of Deceit", "smoke_of_deceit", "开雾"),
    _item("不朽之守护", "Aegis of the Immortal", "aegis", "肉山盾", "不朽盾"),
    _item("刷新球碎片", "Refresher Shard", "refresher_shard", "刷新碎片"),
    _item("奶酪", "Cheese", "cheese", "奶酪"),
    _item("片甲", "Splintmail", "splintmail", "片甲"),
    _item("披巾", "Shawl", "shawl", "披巾"),
    _item("巫师帽", "Wizard Hat", "wizard_hat", "法师帽", "巫师帽"),
    _item("古龙诗集", "Eldwurm's Edda", "eldwurms_edda", "古龙书", "古龙诗集"),
    _item("精之灵器", "Essence Distiller", "essence_distiller", "精华蒸馏器", "精之灵器"),
    _item("圣化护服", "Consecrated Wraps", "consecrated_wraps", "圣化护服"),
    _item("克莱拉牧杖", "Crella's Crozier", "crellas_crozier", "克莱拉牧杖"),
    _item("魔瓶", "Bottle", "bottle", "瓶子"),
    _item("净化药水", "Clarity", "clarity", "小蓝"),
    _item("崎岖外衣", "Craggy Coat", "craggy_coat", "情趣外衣"),
    _item("治疗药膏", "Healing Salve", "flask", "大药"),
    _item("疗伤莲花", "Healing Lotus", "famango", "小莲"),
    _item("寒铁钢爪", "Iron Talon", "iron_talon", "打野爪"),
    _item("魔杖", "Magic Wand", "magic_wand", "大魔棒"),
    _item("魔棒", "Magic Stick", "magic_stick", "小魔棒"),
    _item("勇气勋章", "Medallion of Courage", "medallion_of_courage", "勋章", "小勋章"),
    _item("侦察守卫", "Observer Ward", "ward_observer", "假眼"),
    _item("岗哨守卫", "Sentry Ward", "ward_sentry", "真眼"),
    _item("压制之刃", "Quelling Blade", "quelling_blade", "补刀斧"),
    _item("天鹰之戒", "Ring of Aquila", "ring_of_aquila", "天鹰戒"),
    _item("治疗指环", "Ring of Health", "ring_of_health", "回5", "回五"),
    _item("占卜之铲", "Scrying Shovel", "ofrenda_shovel", "铲子"),
    _item("树之祭祀", "Tango", "tango", "吃树"),
    _item("树之祭祀（共享）", "Tango (Shared)", "tango_single", "吃树"),
    _item("知识之书", "Tome of Knowledge", "tome_of_knowledge", "经验书"),
    _item("静谧之鞋", "Tranquil Boots", "tranquil_boots", "绿鞋"),
    _item("回城卷轴", "Town Portal Scroll", "tpscroll", "TP", "tp"),
)


# Official slugs observed in the runtime icon cache but not required by the
# curated alias table above. Keep this list so placement coverage is testable.
DOTA2_KNOWN_RUNTIME_ITEM_SLUGS: tuple[str, ...] = (
    "bracer", "branches", "chipped_vest", "cloak_of_flames",
    "conjurers_catalyst", "dandelion_amulet", "defiant_shell", "devastator",
    "dormant_curio", "dust", "eagle", "enchanters_bauble", "essence_ring",
    "faerie_fire", "falcon_blade", "gauntlets", "giant_maul", "great_famango",
    "gunpowder_gauntlets", "harpoon", "heavy_blade", "hydras_breath",
    "idol_of_screeauk", "kobold_cup", "lifesteal", "mana_draught",
    "null_talisman", "occult_bracelet", "ogre_axe", "partisans_brand",
    "pogo_stick", "point_booster", "poor_mans_shield", "prophets_pendulum",
    "rattlecage", "relic", "ring_of_protection", "ring_of_tarrasque",
    "searing_signet", "serrated_shiv", "spellslinger", "staff_of_wizardry",
    "stormcrafter", "talisman_of_evasion", "wind_lace", "wraith_band",
)


# Explicit physical slots for every item in the maintained catalogue plus every
# official item icon currently cached on server 141. A future Valve item may use
# the semantic fallback below, but known items never ask the image model to pick
# its own wearing mode.
DOTA2_ITEM_PLACEMENT_PRESET_GROUPS: tuple[
    tuple[str, str, tuple[str, ...]], ...
] = (
    (
        "aghanims_scepter",
        "阿哈利姆神杖就是 A 杖；仅智力英雄可参与唯一手持主装备比较，其他英雄固定在背部法器槽",
        ("ultimate_scepter",),
    ),
    (
        "back_right_staff",
        "固定在背部右侧纵向装备槽，露出完整识别特征，不占用手持 A 杖的位置",
        (
            "black_king_bar", "force_staff", "rod_of_atos", "staff_of_wizardry",
        ),
    ),
    (
        "back_left_staff",
        "固定在背部左侧纵向装备槽，露出完整识别特征，不占用手持 A 杖的位置",
        (
            "crellas_crozier", "sheepstick", "cyclone", "wind_waker", "ghost",
        ),
    ),
    (
        "feet",
        "直接穿在双脚和小腿位置，保留官方鞋靴轮廓，禁止再展示鞋类图标",
        (
            "arcane_boots", "boots", "power_treads", "phase_boots", "travel_boots",
            "guardian_greaves", "boots_of_bearing", "tranquil_boots", "pogo_stick",
        ),
    ),
    (
        "head_face",
        "直接穿戴在头部或面部的唯一装备位，保留官方头盔或面具轮廓",
        (
            "shivas_guard", "veil_of_discord", "hood_of_defiance", "mask_of_madness",
            "helm_of_the_dominator", "helm_of_the_overlord", "wizard_hat", "satanic",
            "lifesteal",
        ),
    ),
    (
        "chest_armor",
        "直接融合为胸甲和肩甲的一个独立护甲模块，不在画面边缘重复",
        (
            "assault", "mekansm", "blade_mail", "splintmail", "craggy_coat",
            "chipped_vest", "defiant_shell", "rattlecage",
        ),
    ),
    (
        "back_garment",
        "直接穿在背部和双肩，作为唯一披风、护服、翼饰或背负装置",
        (
            "pipe", "glimmer_cape", "eternal_shroud", "shawl",
            "consecrated_wraps", "cloak_of_flames", "ancient_janggo",
        ),
    ),
    (
        "back_green_wings",
        "将蝴蝶装备自然展开为背部一对绿色发光双翼，左右两翼属于同一件装备，禁止再画蝴蝶图标或第二对翅膀",
        ("butterfly",),
    ),
    (
        "forearm",
        "直接穿在前臂、手腕或手掌，作为唯一护臂、手套或爪具",
        (
            "hand_of_midas", "armlet", "iron_talon", "bracer", "gauntlets",
            "gunpowder_gauntlets", "occult_bracelet", "wraith_band",
            "partisans_brand",
        ),
    ),
    (
        "finger_ring",
        "直接戴在手指或手背的独立戒指位，以宝石和环形轮廓保持可辨",
        (
            "soul_ring", "ring_of_aquila", "ring_of_health", "ring_of_protection",
            "ring_of_tarrasque", "essence_ring", "searing_signet",
        ),
    ),
    (
        "neck_chest_accessory",
        "直接佩戴在颈部或胸前的独立项链、徽记或护符位，不得另画悬浮副本",
        (
            "orchid", "phylactery", "aeon_disk", "urn_of_shadows", "spirit_vessel",
            "holy_locket", "solar_crest", "medallion_of_courage", "null_talisman",
            "dandelion_amulet", "enchanters_bauble", "prophets_pendulum",
            "talisman_of_evasion", "wind_lace", "spellslinger",
        ),
    ),
    (
        "chest_heart",
        "嵌入胸甲正中央，作为唯一的红色心脏核心",
        ("heart",),
    ),
    (
        "body_core",
        "固定在胸甲、肩甲或腰带的独立能量插槽内；必须与身体连接，禁止自由悬浮",
        (
            "aether_lens", "aghanims_shard", "ultimate_scepter_2", "essence_distiller",
            "conjurers_catalyst", "dormant_curio", "point_booster", "idol_of_screeauk",
            "stormcrafter",
        ),
    ),
    (
        "floating_orb",
        "保持官方球体或核心轮廓，作为人物手掌上方或单侧肩旁的独立悬浮能量焦点；"
        "不得嵌进身体、改成技能光球、贴到画面边缘或与其他球类排成一圈",
        (
            "refresher", "octarine_core", "lotus_orb", "sphere", "bloodstone",
            "moon_shard", "gem", "refresher_shard",
        ),
    ),
    (
        "chest_frost_core",
        "将斯嘉蒂之眼嵌入胸甲正中央，表现为轮廓完整、蓝白冰霜清晰的唯一胸前核心，禁止在身旁重复悬浮",
        ("skadi",),
    ),
    (
        "waist_short_weapon",
        "收纳在腰带外侧的独立短武器或短法器槽；露出核心轮廓但不得离体悬浮",
        (
            "blink", "overwhelming_blink", "swift_blink", "arcane_blink", "dagon",
            "witch_blade", "angels_demise", "lesser_crit", "ethereal_blade",
            "magic_wand", "magic_stick", "quelling_blade", "serrated_shiv",
            "heavy_blade", "falcon_blade", "bloodthorn",
        ),
    ),
    (
        "back_long_weapon",
        "固定在背部外侧的独立长武器槽，保持完整轮廓并与其他武器分层错开",
        (
            "hurricane_pike", "gungir", "nullifier", "monkey_king_bar", "basher",
            "abyssal_blade", "maelstrom", "mjollnir", "dragon_lance",
            "heavens_halberd", "meteor_hammer", "devastator", "giant_maul",
            "harpoon", "hydras_breath", "ogre_axe", "eagle",
        ),
    ),
    (
        "back_blade",
        "装入背部斜向刀鞘的独立刀剑位，多件时平行分层，禁止融合成一把",
        (
            "rapier", "radiance", "greater_crit", "bfury", "manta", "invis_sword",
            "desolator", "diffusal_blade", "disperser", "mage_slayer",
            "echo_sabre", "sange", "yasha", "kaya", "sange_and_yasha",
            "kaya_and_sange", "yasha_and_kaya", "relic",
        ),
    ),
    (
        "back_center_blade",
        "将白银之锋从背部中央斜向背负；若同时有蝴蝶，必须从绿色双翼中央穿出并完整露出独立刀身，禁止与双翼融合",
        ("silver_edge",),
    ),
    (
        "offhand_shield",
        "佩戴在左前臂或背后盾架的唯一盾牌位，保留完整盾面符号",
        (
            "vanguard", "crimson_guard", "aegis", "poor_mans_shield",
        ),
    ),
    (
        "belt_supply",
        "固定在腰带的独立补给袋、瓶架或挂件中；只露出一件，不得漂浮",
        (
            "bottle", "clarity", "flask", "famango", "great_famango", "faerie_fire",
            "dust", "smoke_of_deceit", "cheese", "tango", "tango_single",
            "tpscroll", "branches", "mana_draught", "kobold_cup",
        ),
    ),
    (
        "book_scroll",
        "固定在腰后书匣或卷轴筒内，露出封面或卷轴识别特征，不得悬浮",
        ("eldwurms_edda", "tome_of_knowledge"),
    ),
    (
        "ground_tool",
        "只在人物脚边地面出现一次，作为插地守卫或落地工具，禁止人物身上再复制",
        ("ward_observer", "ward_sentry", "ofrenda_shovel"),
    ),
)


def _build_item_placement_presets() -> dict[str, dict[str, object]]:
    presets: dict[str, dict[str, object]] = {}
    for slot, placement, slugs in DOTA2_ITEM_PLACEMENT_PRESET_GROUPS:
        for slug in slugs:
            if slug in presets:
                raise RuntimeError(f"Dota 2 装备穿戴预设重复：{slug}")
            presets[slug] = {
                "slot": slot,
                "placement": placement,
                "floating_allowed": slot == "floating_orb",
            }
    return presets


DOTA2_ITEM_PLACEMENT_PRESETS = _build_item_placement_presets()


@lru_cache(maxsize=1)
def _official_dota2_item_rows() -> tuple[dict[str, object], ...]:
    """Load the current raw item catalogue once from Valve's Datafeed."""
    request = urllib.request.Request(
        DOTA2_ITEM_DATA_SOURCE,
        headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as remote:
            payload = json.load(remote)
    except (OSError, ValueError, urllib.error.URLError):
        return ()
    rows = payload.get("result", {}).get("data", {}).get("itemabilities", [])
    return tuple(row for row in rows if isinstance(row, dict))


@lru_cache(maxsize=1)
def _official_dota2_items() -> tuple[Dota2Item, ...]:
    """Load the complete current item catalogue from Valve's Datafeed."""
    result: list[Dota2Item] = []
    for row in _official_dota2_item_rows():
        internal_name = str(row.get("name") or "")
        chinese_name = str(row.get("name_loc") or "").strip()
        english_name = str(row.get("name_english_loc") or "").strip()
        if not internal_name.startswith("item_") or internal_name.startswith("item_recipe_"):
            continue
        icon_slug = internal_name.removeprefix("item_")
        if chinese_name and english_name and icon_slug:
            result.append(_item(chinese_name, english_name, icon_slug))
    return tuple(result)


@lru_cache(maxsize=1)
def _official_dota2_item_id_map() -> dict[str, int]:
    result: dict[str, int] = {}
    for row in _official_dota2_item_rows():
        internal_name = str(row.get("name") or "")
        try:
            item_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            item_id = 0
        if internal_name.startswith("item_") and item_id > 0:
            result[internal_name.removeprefix("item_")] = item_id
    return result


def _clean_official_item_text(value: object, limit: int = 180) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "；", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"%[a-zA-Z0-9_]+%", "", text)
    text = text.replace("%", "")
    text = re.sub(r"\s+", " ", text).strip(" ；")
    text = re.sub(r"；+", "；", text)
    if len(text) <= limit:
        return text
    shortened = text[:limit].rsplit("；", 1)[0].rstrip(" ，。；")
    return shortened or text[:limit].rstrip(" ，。；")


@lru_cache(maxsize=256)
def _official_dota2_item_visual_context(icon_slug: str) -> tuple[str, str, int]:
    item_id = _official_dota2_item_id_map().get(str(icon_slug or "").strip())
    if not item_id:
        return "", "", 0
    request = urllib.request.Request(
        DOTA2_ITEM_DETAIL_SOURCE.format(item_id=item_id),
        headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as remote:
            payload = json.load(remote)
    except (OSError, ValueError, urllib.error.URLError):
        return "", "", 0
    rows = payload.get("result", {}).get("data", {}).get("items", [])
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    try:
        item_cost = max(0, int(row.get("item_cost") or 0))
    except (TypeError, ValueError):
        item_cost = 0
    return (
        _clean_official_item_text(row.get("lore_loc")),
        _clean_official_item_text(row.get("desc_loc")),
        item_cost,
    )


def load_dota2_item_visual_contexts(
    matches: Iterable[Dota2ItemMatch],
) -> list[dict[str, object]]:
    """Load Valve lore and function text used to integrate equipment naturally."""
    normalized = list(matches)

    def load(match: Dota2ItemMatch) -> dict[str, object]:
        lore, function, item_cost = _official_dota2_item_visual_context(
            match.item.icon_slug
        )
        return {
            "chinese_name": match.item.chinese_name,
            "english_name": match.item.english_name,
            "icon_slug": match.item.icon_slug,
            "lore": lore,
            "function": function,
            "item_cost": item_cost,
        }

    if not normalized:
        return []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(4, len(normalized)),
    ) as pool:
        contexts = list(pool.map(load, normalized))
    return [
        row
        for row in contexts
        if row.get("lore") or row.get("function") or row.get("item_cost")
    ]


def dota2_item_visual_context_prompt_instruction(
    contexts: Iterable[dict[str, object]],
    layout_mode: str = DOTA2_COVER_LAYOUT_CLASSIC,
) -> str:
    normalized = list(contexts)
    if not normalized:
        return ""
    rows: list[str] = []
    for context in normalized:
        name = str(context.get("chinese_name") or "").strip()
        lore = str(context.get("lore") or "").strip()
        function = str(context.get("function") or "").strip()
        try:
            item_cost = max(0, int(context.get("item_cost") or 0))
        except (TypeError, ValueError):
            item_cost = 0
        facts = "；".join(
            part
            for part in (
                f"背景：{lore}" if lore else "",
                f"功能：{function}" if function else "",
                f"官方价格：{item_cost} 金币" if item_cost else "",
            )
            if part
        )
        if name and facts:
            rows.append(f"{name}（{facts}）")
    if not rows:
        return ""
    if normalize_dota2_cover_layout_mode(layout_mode) == DOTA2_COVER_LAYOUT_FUSION:
        return (
            "Valve 官方装备背景与功能（只用于理解造型、材质、用途和穿戴方式，不得新增剧情事实）："
            + "；".join(rows)
            + "。先以官方图标锁定每件装备身份，再结合背景与功能，将适合的装备自然设计为人物"
            "穿戴的护甲、鞋、披风或饰品，或手持、背负、腰挂的武器和法器；球体、消耗品和"
            "特殊物件可作为身旁道具或能量焦点。每件只出现一次，不得在边缘、背景、倒影或装饰层复制。"
        )
    return (
        "Valve 官方装备背景与功能（只用于理解造型、材质和用途，不得新增剧情事实）："
        + "；".join(rows)
        + "。只以官方图标锁定每件装备身份，全部作为独立切片图标；可沿主体外围错落展示，"
        "也可根据构图统一放在画面最下方安全区一排；"
        "背景和功能不得用于把装备改造成人物服装、护甲、肢体、手持武器或技能光效。"
        "每件只出现一次，不得在人物、英雄、背景、倒影或装饰层复制同款。"
    )


def dota2_item_placement_plan(
    matches: Iterable[Dota2ItemMatch],
    *,
    hero_primary_attribute: str = "",
    item_visual_contexts: Iterable[dict[str, object]] = (),
) -> list[dict[str, object]]:
    """Assign one physical manifestation to each item before image generation."""
    normalized_matches = list(matches)
    item_costs: dict[str, int] = {}
    for context in item_visual_contexts:
        slug = str(context.get("icon_slug") or "").strip().lower()
        try:
            item_cost = max(0, int(context.get("item_cost") or 0))
        except (TypeError, ValueError):
            item_cost = 0
        if slug:
            item_costs[slug] = item_cost
    handheld_slots = {
        "back_right_staff",
        "back_left_staff",
        "waist_short_weapon",
        "back_long_weapon",
        "back_blade",
        "back_center_blade",
    }
    # Keep purpose-built back blades on the back when a second weapon is
    # available. Long weapons, staves and short weapons can form a readable
    # left-hand offhand without undoing their distinctive back composition.
    offhand_slots = {
        "back_right_staff",
        "back_left_staff",
        "waist_short_weapon",
        "back_long_weapon",
    }
    handheld_candidates: list[tuple[int, int, str]] = []
    for index, match in enumerate(normalized_matches):
        slug = match.item.icon_slug.lower()
        if slug == "ultimate_scepter":
            eligible = hero_primary_attribute == "intelligence"
        else:
            preset = DOTA2_ITEM_PLACEMENT_PRESETS.get(slug, {})
            eligible = preset.get("slot") in handheld_slots
        item_cost = item_costs.get(slug, 0)
        if eligible and item_cost > 0:
            handheld_candidates.append((item_cost, -index, slug))
    ranked_handheld_slugs = [
        slug
        for _cost, _stable_index, slug in sorted(
            handheld_candidates,
            reverse=True,
        )
    ]
    if ranked_handheld_slugs:
        primary_handheld_slug = ranked_handheld_slugs[0]
    elif hero_primary_attribute == "intelligence" and any(
        match.item.icon_slug.lower() == "ultimate_scepter"
        for match in normalized_matches
    ):
        # Preserve the hard Aghanim rule if Valve's price endpoint is temporarily
        # unavailable; no other unknown-price weapon is promoted speculatively.
        primary_handheld_slug = "ultimate_scepter"
    else:
        primary_handheld_slug = ""
    secondary_handheld_slug = ""
    for candidate_slug in ranked_handheld_slugs:
        if candidate_slug == primary_handheld_slug:
            continue
        if candidate_slug == "ultimate_scepter":
            secondary_handheld_slug = candidate_slug
            break
        candidate_preset = DOTA2_ITEM_PLACEMENT_PRESETS.get(candidate_slug, {})
        if candidate_preset.get("slot") in offhand_slots:
            secondary_handheld_slug = candidate_slug
            break
    plans: list[dict[str, object]] = []
    worn_tokens = (
        "boots", "greaves", "shoe", "cuirass", "guard", "mail", "armor",
        "cloak", "cape", "hood", "helm", "mask", "coat", "shawl", "hat",
        "wrap", "gauntlet", "bracer", "band", "armlet", "shroud", "crest",
        "medallion", "talisman",
    )
    held_tokens = (
        "blade", "sword", "rapier", "scepter", "staff", "crozier", "scythe",
        "dagger", "blink", "pike", "halberd", "hammer", "bar", "gungir",
        "axe", "lance", "wand", "rod", "sabre",
    )
    focus_tokens = (
        "orb", "catalyst", "gem", "stone", "shard", "bottle", "sphere",
        "lotus", "heart", "eye", "moon", "aegis", "cheese", "smoke",
        "ward", "locket", "pendant", "ring",
    )
    floating_tokens = ("orb", "sphere")
    def has_token(slug: str, tokens: tuple[str, ...]) -> bool:
        parts = set(slug.split("_"))
        return any(token in parts for token in tokens)

    slot_counts: dict[str, int] = {}
    for match in normalized_matches:
        slug = match.item.icon_slug.lower()
        item_cost = item_costs.get(slug, 0)
        high_value = item_cost >= HIGH_VALUE_ITEM_COST
        if slug == primary_handheld_slug:
            slot = "direct_hand"
            if slug == "ultimate_scepter":
                placement = (
                    "阿哈利姆神杖就是 A 杖；该英雄为智力英雄，且它在本局适合手持的装备中"
                    f"官方价格最高（{item_cost} 金币），作为画面右手主装备直接拿在人物右手中"
                )
            else:
                placement = (
                    f"Valve 官方价格为 {item_cost} 金币，在本局适合手持的装备中最高；"
                    "作为画面右手主装备直接拿在人物右手中，原预设背负或腰挂位置取消"
                )
            preset_used = True
            floating_allowed = False
        elif slug == secondary_handheld_slug:
            slot = "direct_offhand"
            if slug == "ultimate_scepter":
                placement = (
                    "阿哈利姆神杖就是 A 杖；该英雄为智力英雄，且它是主装备之后价格最高的合适副手法器"
                    f"（{item_cost} 金币），直接拿在人物左手中；必须与右手主装备保持两个完整独立实体"
                )
            else:
                placement = (
                    f"Valve 官方价格为 {item_cost} 金币，是右手主装备之外价格最高且适合副手的武器；"
                    "作为画面左手副武器直接拿在人物左手中，原预设背负或腰挂位置取消；"
                    "必须与右手主装备保持两个完整独立实体，不得融合、交叉遮没或画成同一把"
                )
            preset_used = True
            floating_allowed = False
        elif slug == "ultimate_scepter":
            slot = "back_center_scepter"
            if hero_primary_attribute == "intelligence":
                placement = (
                    "阿哈利姆神杖就是 A 杖；该英雄虽为智力英雄，但本局手持价格与位置优先级"
                    "没有让它进入主手或副手，因此固定在背部中央法器槽，禁止再手持"
                )
            else:
                placement = "阿哈利姆神杖就是 A 杖；该英雄不是智力英雄，固定在背部中央法器槽，禁止手持"
            preset_used = True
            floating_allowed = False
        elif slug in DOTA2_ITEM_PLACEMENT_PRESETS:
            preset = DOTA2_ITEM_PLACEMENT_PRESETS[slug]
            slot = str(preset["slot"])
            placement = str(preset["placement"])
            preset_used = True
            floating_allowed = bool(preset["floating_allowed"])
        elif has_token(slug, worn_tokens):
            slot = "future_worn_item"
            placement = "按官方轮廓自然穿在对应身体部位，画面中不再出现同款漂浮图标"
            preset_used = False
            floating_allowed = False
        elif has_token(slug, held_tokens):
            slot = "future_back_weapon"
            placement = "固定在背部独立装备槽，禁止临时改成手持或悬浮图标"
            preset_used = False
            floating_allowed = False
        elif has_token(slug, floating_tokens):
            slot = "future_floating_orb"
            placement = (
                "按官方球体轮廓作为人物手掌上方或单侧肩旁的唯一悬浮能量焦点；"
                "不得改成技能光球、贴边或与其他装备围成一圈"
            )
            preset_used = False
            floating_allowed = True
        elif has_token(slug, focus_tokens):
            slot = "future_body_core"
            placement = "固定在胸甲或腰带的独立能量插槽内，禁止自由悬浮"
            preset_used = False
            floating_allowed = False
        else:
            slot = "future_belt_item"
            placement = "固定在腰带独立挂件位，禁止模型自行改成手持、穿戴或悬浮"
            preset_used = False
            floating_allowed = False
        slot_counts[slot] = slot_counts.get(slot, 0) + 1
        slot_index = slot_counts[slot]
        if slot_index > 1:
            placement += f"；这是该位置第{slot_index}件，必须与前一件分层分开"
        if high_value:
            placement += (
                f"；官方价格 {item_cost} 金币，属于高级装备，必须获得高辨识优先："
                "完整露出官方轮廓、主色、材质和核心符号，尺寸与明暗对比不得低于普通补给品，"
                "也不得因穿戴、持握或挂载而缩成难以辨认的小装饰"
            )
        plans.append(
            {
                "chinese_name": match.item.chinese_name,
                "english_name": match.item.english_name,
                "icon_slug": match.item.icon_slug,
                "slot": slot,
                "placement": placement,
                "floating_allowed": floating_allowed,
                "preset_used": preset_used,
                "hero_primary_attribute": hero_primary_attribute,
                "item_cost": item_cost,
                "high_value": high_value,
                "is_primary_handheld": slug == primary_handheld_slug,
                "is_secondary_handheld": slug == secondary_handheld_slug,
            }
        )
    return plans


def dota2_item_placement_plan_prompt_instruction(
    plans: Iterable[dict[str, object]],
    layout_mode: str = DOTA2_COVER_LAYOUT_CLASSIC,
) -> str:
    normalized = list(plans)
    if not normalized:
        return ""
    if normalize_dota2_cover_layout_mode(layout_mode) == DOTA2_COVER_LAYOUT_FUSION:
        rows = [
            f"{index}. {plan['chinese_name']}：{plan['placement']}；"
            f"{'允许一处独立悬浮' if plan.get('floating_allowed') else '禁止独立悬浮'}"
            for index, plan in enumerate(normalized, start=1)
        ]
        return (
            "融合模式逐件位置计划，成图必须逐项执行："
            + "；".join(rows)
            + "。每件只能对应一个物理实体；穿戴、手持、背负或腰挂后不得再显示同款图标。"
            "主手和副手装备必须分别完整持握，不得融合、遮没或连成一把。"
            "每件仍须保留官方轮廓、主色、材质和核心符号，在缩略图中清楚可辨。"
        )
    rows = [f"{index}. {plan['chinese_name']}" for index, plan in enumerate(normalized, start=1)]
    return (
        "生成前锁定以下装备图标清单，成图必须逐项展示且各出现一次："
        + "；".join(rows)
        + "。忽略内部穿戴、手持、背负或腰挂位置计划；这些计划不再用于封面。"
        "全部装备恢复为独立官方图标式切片；根据标题与主体占位，可沿主播人物和官方英雄的外围安全区域"
        "错落环绕，也可统一放在画面最下方安全区一排。"
        "每件必须完整露出官方轮廓、主色、材质和核心符号，缩略图中逐件可辨；"
        "不得穿到人物或英雄身上，不得变成服装、普通武器、肢体、技能光效或场景装饰。"
        "图标可以有不同大小与轻微角度以形成层次，但不能互相融合、遮挡、重复，也不能遮脸、压字或贴边裁断。"
    )


def _all_dota2_items() -> tuple[Dota2Item, ...]:
    """Merge curated aliases with every unchanged/current official item."""
    merged = {item.icon_slug: item for item in _official_dota2_items()}
    merged.update({item.icon_slug: item for item in DOTA2_ITEMS})
    return tuple(merged.values())


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias.casefold())
    if re.fullmatch(r"[a-z][a-z0-9' ._-]*", alias.casefold()):
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
    return re.compile(escaped)


def match_dota2_items(*content: str, limit: int = MAX_MATCHED_ITEMS) -> list[Dota2ItemMatch]:
    combined = "\n".join(str(value or "") for value in content)
    folded = combined.casefold()
    candidates: list[Dota2ItemMatch] = []
    for item in _all_dota2_items():
        for alias in sorted(item.aliases, key=len, reverse=True):
            for found in _alias_pattern(alias).finditer(folded):
                candidates.append(
                    Dota2ItemMatch(
                        item=item,
                        alias=combined[found.start():found.end()] or alias,
                        start=found.start(),
                        end=found.end(),
                    )
                )

    # Prefer longer slang at the same location: 大电锤 must not also become 电锤.
    candidates.sort(key=lambda match: (match.start, -(match.end - match.start)))
    selected: list[Dota2ItemMatch] = []
    selected_items: set[Dota2Item] = set()
    occupied: list[tuple[int, int]] = []
    for candidate in candidates:
        if candidate.item in selected_items:
            continue
        if any(candidate.start < end and candidate.end > start for start, end in occupied):
            continue
        selected.append(candidate)
        selected_items.add(candidate.item)
        occupied.append((candidate.start, candidate.end))
        if len(selected) >= max(1, limit):
            break
    return selected


def prioritize_dota2_item_matches(
    matches: Iterable[Dota2ItemMatch],
    *primary_content: str,
    limit: int = MAX_MATCHED_ITEMS,
) -> list[Dota2ItemMatch]:
    """Put title/event items first without inventing items outside GSI."""

    available = list(matches)
    available_items = {match.item for match in available}
    primary = [
        match
        for match in match_dota2_items(*primary_content, limit=limit)
        if match.item in available_items
    ]
    ordered: list[Dota2ItemMatch] = []
    seen: set[Dota2Item] = set()
    for match in (*primary, *available):
        if match.item in seen:
            continue
        ordered.append(match)
        seen.add(match.item)
        if len(ordered) >= max(1, limit):
            break
    return ordered


def dota2_item_prompt_instruction(
    matches: Iterable[Dota2ItemMatch],
    layout_mode: str = DOTA2_COVER_LAYOUT_CLASSIC,
) -> str:
    normalized = list(matches)
    if not normalized:
        return (
            "Dota 2 装备规则：未检出可确定的装备俗称；不要凭普通名词臆造游戏装备。"
        )
    resolved = "；".join(
        f"{match.alias}＝{match.item.label}"
        for match in normalized
    )
    if normalize_dota2_cover_layout_mode(layout_mode) == DOTA2_COVER_LAYOUT_FUSION:
        return (
            "Dota 2 装备与俗称消歧规则：所有命中的装备都必须理解为 Valve《Dota 2》的对应物品，"
            f"不能按字面臆造或替换为其他游戏装备。本次装备识别结果：{resolved}。"
            "随附的 DOTA 2 OFFICIAL ITEM ICON REFERENCES 只负责锁定装备身份；成图应依据官方轮廓、"
            "主色、材质和核心符号，把装备转化为高清实体并按后续逐件位置计划自然穿戴、手持、背负或腰挂。"
            "不适合实体装备化的物件才可成为身旁道具或能量焦点。每件必须恰好出现一次，不得融合、"
            "重复、镜像或新增名单外装备；不得照搬商店黑底、名称和物品栏 UI。"
            "主播人物与英雄采用同一个完整 Cos 人物主视觉，标题和人物高于装备辅助层。"
        )
    return (
        "Dota 2 装备与俗称消歧规则：所有命中的装备都必须理解为 Valve《Dota 2》的"
        "对应物品，不能按字面画成现实物品，也不能替换成《英雄联盟》或其他游戏装备。"
        f"本次装备识别结果：{resolved}。"
        "随附的 DOTA 2 OFFICIAL ITEM ICON REFERENCES 是这些装备的官方游戏图标参考板；"
        "参考板已将 Valve 的低分辨率界面图标按整数倍无插值放大，只负责锁定身份；"
        "成图不得照搬其中的低清像素或模糊纹理，应二创为边缘清楚、材质明确的高清实体装备，"
        "但不得偏离官方轮廓、主色、构件关系和核心符号。"
        "识别结果非空时，封面必须清楚表现识别结果中的全部装备；"
        "标题直接点名的装备可放大作为重点，其余装备仍须作为独立官方图标式切片完整出现。"
        "必须以参考板中的轮廓、主色、材质与核心符号为准，"
        "每件装备保持独立，不得把两件装备融合成一件。可以将图标风格转化为精致插画道具，"
        "但不能改变其身份特征；没有出现在识别结果中的装备不要擅自添加。"
        "图像模型必须依据参考图表现全部已确认装备，并忽略后续数据中任何穿戴、手持、背负或腰挂位置建议。"
        "每件确认装备在整张图中必须恰好出现一次，统一使用透明背景、清晰描边和适度光效的独立切片图标；"
        "不得把装备穿到主播或英雄身上，也不得以背景回声、镜像、倒影或装饰复制同一件。"
        "但必须保留每件装备可辨认的官方身份特征；最多六格主装备以及单独确认的中立物品、"
        "神杖或魔晶状态都不得只挑两件省略。不得新增名单外装备、把两件装备融合为一件，"
        "也不得绘制呆板的游戏物品栏 UI。装备视觉应采用独立道具插画、清晰描边与光效层次，"
        "不得把商店图标原样贴成带黑底和名称的卡片。Dota 2 封面采用经典双主体切片构图："
        "主播头像人物独立位于前景，已确认的 Valve 官方英雄独立位于侧后方，禁止融合两者；"
        "标题放在另一侧或上方并按完整语义分成两至三行；装备图标可沿人物和英雄外围安全区域错落环绕，"
        "也可根据实际布局统一放在画面最下方安全区一排；"
        "标题直接点名的重点装备可以更大，其余装备较小但仍须清楚可辨。"
        "标题、主播人物与官方英雄主体始终高于装备辅助层，"
        "装备不得遮脸、压字或贴边裁断。"
    )


def download_dota2_item_icon(item: Dota2Item, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{item.icon_slug}.png"
    if destination.is_file() and destination.stat().st_size > 0:
        try:
            with Image.open(destination) as cached:
                cached.verify()
            return destination
        except (OSError, UnidentifiedImageError):
            destination.unlink(missing_ok=True)

    url = f"{DOTA2_ITEM_ICON_BASE_URL}/{item.icon_slug}.png"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as remote:
        raw = remote.read(MAX_ITEM_ICON_BYTES + 1)
    if not raw or len(raw) > MAX_ITEM_ICON_BYTES:
        raise ValueError(f"{item.label} 官方图标为空或过大")
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(raw)
    try:
        with Image.open(temporary) as downloaded:
            downloaded.verify()
    except (OSError, UnidentifiedImageError) as exc:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"{item.label} 官方图标无效") from exc
    temporary.replace(destination)
    return destination


def prepare_dota2_item_reference_icon(
    icon: Image.Image,
    scale: int = 3,
) -> Image.Image:
    """Pixel-upscale Valve's small UI icon without adding false details."""
    source = icon.convert("RGBA")
    factor = max(1, int(scale))
    return source.resize(
        (source.width * factor, source.height * factor),
        Image.Resampling.NEAREST,
    )


def build_dota2_item_reference_sheet(
    matches: Iterable[Dota2ItemMatch],
    cache_dir: Path,
    output_path: Path,
) -> tuple[Path | None, list[str]]:
    normalized = list(matches)
    icons: list[tuple[Dota2Item, Image.Image]] = []
    errors: list[str] = []

    def load_icon(match: Dota2ItemMatch) -> tuple[Dota2Item, Image.Image]:
        icon_path = download_dota2_item_icon(match.item, cache_dir)
        with Image.open(icon_path) as source:
            return match.item, source.convert("RGBA").copy()

    loaded: list[tuple[Dota2Item, Image.Image] | Exception] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(4, max(1, len(normalized))),
    ) as pool:
        futures = [pool.submit(load_icon, match) for match in normalized]
        for future in futures:
            try:
                loaded.append(future.result())
            except Exception as exc:  # surfaced below with the matching item name
                loaded.append(exc)

    for match, result in zip(normalized, loaded):
        if isinstance(result, Exception):
            if isinstance(result, (OSError, ValueError, urllib.error.URLError)):
                errors.append(f"{match.item.label}: {result}")
                continue
            raise result
        try:
            icons.append(result)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"{match.item.label}: {exc}")
    if not icons:
        return None, errors

    columns = min(3, len(icons))
    rows = (len(icons) + columns - 1) // columns
    tile_width, tile_height = 344, 264
    header_height = 76
    canvas = Image.new(
        "RGB",
        (columns * tile_width, header_height + rows * tile_height),
        "#111827",
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text(
        (20, 18),
        "DOTA 2 OFFICIAL ITEM ICON REFERENCES",
        fill="#ffffff",
        font=font,
    )
    draw.text(
        (20, 40),
        "Match each item silhouette and color; do not merge items.",
        fill="#93c5fd",
        font=font,
    )
    for index, (item, icon) in enumerate(icons):
        column = index % columns
        row = index // columns
        left = column * tile_width
        top = header_height + row * tile_height
        draw.rounded_rectangle(
            (left + 8, top + 8, left + tile_width - 8, top + tile_height - 8),
            radius=14,
            fill="#1f2937",
            outline="#475569",
            width=2,
        )
        icon = prepare_dota2_item_reference_icon(icon)
        icon_left = left + (tile_width - icon.width) // 2
        icon_top = top + 12
        canvas.paste(icon, (icon_left, icon_top), icon)
        label = item.english_name[:38]
        text_box = draw.textbbox((0, 0), label, font=font)
        text_width = text_box[2] - text_box[0]
        draw.text(
            (left + (tile_width - text_width) // 2, top + 230),
            label,
            fill="#ffffff",
            font=font,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path, errors
