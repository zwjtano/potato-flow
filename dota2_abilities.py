"""Dota 2 ability names, community aliases, and official icon references."""

from __future__ import annotations

import concurrent.futures
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError


DOTA2_ABILITY_ICON_BASE_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/"
    "dota_react/abilities"
)
MAX_MATCHED_ABILITIES = 8
MAX_ABILITY_ICON_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Dota2Ability:
    hero_chinese_name: str
    hero_english_name: str
    chinese_name: str
    english_name: str
    icon_slug: str
    aliases: tuple[str, ...]

    @property
    def hero_label(self) -> str:
        return f"{self.hero_chinese_name}（{self.hero_english_name}）"

    @property
    def label(self) -> str:
        return f"{self.chinese_name}（{self.english_name}）"


@dataclass(frozen=True)
class Dota2AbilityMatch:
    ability: Dota2Ability
    alias: str
    start: int
    end: int


def _ability(
    hero_chinese_name: str,
    hero_english_name: str,
    chinese_name: str,
    english_name: str,
    icon_slug: str,
    *aliases: str,
) -> Dota2Ability:
    names = tuple(
        dict.fromkeys(
            (
                *aliases,
                chinese_name,
                english_name,
            )
        )
    )
    return Dota2Ability(
        hero_chinese_name,
        hero_english_name,
        chinese_name,
        english_name,
        icon_slug,
        names,
    )


# Chinese aliases are intentionally conservative. Ambiguous one-character slang
# such as “滚”“拉”“吼”“踩” is excluded unless it forms a stable phrase.
DOTA2_ABILITIES: tuple[Dota2Ability, ...] = (
    _ability("风暴之灵", "Storm Spirit", "球状闪电", "Ball Lightning", "storm_spirit_ball_lightning", "蓝猫飞", "蓝猫滚", "飞脸"),
    _ability("风暴之灵", "Storm Spirit", "电子涡流", "Electric Vortex", "storm_spirit_electric_vortex", "蓝猫拉", "涡流拉"),
    _ability("风暴之灵", "Storm Spirit", "残影", "Static Remnant", "storm_spirit_static_remnant", "蓝猫残影"),
    _ability("风暴之灵", "Storm Spirit", "超负荷", "Overload", "storm_spirit_overload", "蓝猫超负荷"),
    _ability("灰烬之灵", "Ember Spirit", "无影拳", "Sleight of Fist", "ember_spirit_sleight_of_fist", "无影拳", "火猫无影拳"),
    _ability("灰烬之灵", "Ember Spirit", "炎阳索", "Searing Chains", "ember_spirit_searing_chains", "火猫捆", "火猫锁"),
    _ability("灰烬之灵", "Ember Spirit", "残焰", "Fire Remnant", "ember_spirit_fire_remnant", "火猫魂", "火猫飞魂"),
    _ability("灰烬之灵", "Ember Spirit", "火焰壁垒", "Flame Guard", "ember_spirit_flame_guard", "火盾", "火猫火盾"),
    _ability("大地之灵", "Earth Spirit", "巨石翻滚", "Rolling Boulder", "earth_spirit_rolling_boulder", "土猫滚", "滚石"),
    _ability("大地之灵", "Earth Spirit", "巨石冲击", "Boulder Smash", "earth_spirit_boulder_smash", "土猫踢", "踢石头"),
    _ability("大地之灵", "Earth Spirit", "地磁之握", "Geomagnetic Grip", "earth_spirit_geomagnetic_grip", "土猫拉", "拉石头"),
    _ability("大地之灵", "Earth Spirit", "磁化", "Magnetize", "earth_spirit_magnetize", "土猫磁化"),
    _ability("虚无之灵", "Void Spirit", "残阴", "Aether Remnant", "void_spirit_aether_remnant", "紫猫拉", "紫猫残阴"),
    _ability("虚无之灵", "Void Spirit", "异化", "Dissimilate", "void_spirit_dissimilate", "紫猫钻地"),
    _ability("虚无之灵", "Void Spirit", "共鸣脉冲", "Resonant Pulse", "void_spirit_resonant_pulse", "紫猫盾"),
    _ability("虚无之灵", "Void Spirit", "太虚之径", "Astral Step", "void_spirit_astral_step", "紫猫大", "紫猫冲"),
    _ability("影魔", "Shadow Fiend", "毁灭阴影", "Shadowraze", "nevermore_shadowraze1", "影压", "三连压", "三压"),
    _ability("影魔", "Shadow Fiend", "魂之挽歌", "Requiem of Souls", "nevermore_requiem", "影魔摇大", "魂挽"),
    _ability("祈求者", "Invoker", "阳炎冲击", "Sun Strike", "invoker_sun_strike", "天火", "卡尔天火"),
    _ability("祈求者", "Invoker", "强袭飓风", "Tornado", "invoker_tornado", "卡尔吹风", "卡尔龙卷风"),
    _ability("祈求者", "Invoker", "混沌陨石", "Chaos Meteor", "invoker_chaos_meteor", "卡尔陨石", "大火球"),
    _ability("祈求者", "Invoker", "电磁脉冲", "EMP", "invoker_emp", "磁暴", "卡尔磁暴"),
    _ability("祈求者", "Invoker", "急速冷却", "Cold Snap", "invoker_cold_snap", "急冷", "卡尔急冷"),
    _ability("祈求者", "Invoker", "超震声波", "Deafening Blast", "invoker_deafening_blast", "推波", "卡尔推波"),
    _ability("祈求者", "Invoker", "幽灵漫步", "Ghost Walk", "invoker_ghost_walk", "卡尔隐身", "幽灵漫步"),
    _ability("祈求者", "Invoker", "熔炉精灵", "Forge Spirit", "invoker_forge_spirit", "火人", "卡尔火人"),
    _ability("帕吉", "Pudge", "肉钩", "Meat Hook", "pudge_meat_hook", "钩子", "神钩", "盲钩"),
    _ability("帕吉", "Pudge", "肢解", "Dismember", "pudge_dismember", "咬住", "屠夫咬"),
    _ability("斧王", "Axe", "狂战士之吼", "Berserker's Call", "axe_berserkers_call", "斧王吼", "跳吼"),
    _ability("斧王", "Axe", "淘汰之刃", "Culling Blade", "axe_culling_blade", "斧王斩", "斧王斩杀"),
    _ability("撼地者", "Earthshaker", "沟壑", "Fissure", "earthshaker_fissure", "小牛沟", "封路沟"),
    _ability("撼地者", "Earthshaker", "回音击", "Echo Slam", "earthshaker_echo_slam", "小牛跳大", "神牛跳大", "回音击"),
    _ability("马格纳斯", "Magnus", "两极反转", "Reverse Polarity", "magnataur_reverse_polarity", "猛犸大", "猛犸踩大", "rp"),
    _ability("马格纳斯", "Magnus", "巨角冲撞", "Skewer", "magnataur_skewer", "猛犸拱", "拱回来"),
    _ability("谜团", "Enigma", "黑洞", "Black Hole", "enigma_black_hole", "谜团黑洞", "完美黑洞"),
    _ability("潮汐猎人", "Tidehunter", "毁灭", "Ravage", "tidehunter_ravage", "潮汐大", "潮汐踩大"),
    _ability("虚空假面", "Faceless Void", "时间结界", "Chronosphere", "faceless_void_chronosphere", "虚空大", "罩大", "时间罩"),
    _ability("主宰", "Juggernaut", "剑刃风暴", "Blade Fury", "juggernaut_blade_fury", "剑圣转", "剑刃风暴"),
    _ability("主宰", "Juggernaut", "无敌斩", "Omnislash", "juggernaut_omni_slash", "无敌斩", "剑圣斩"),
    _ability("裂魂人", "Spirit Breaker", "暗影冲刺", "Charge of Darkness", "spirit_breaker_charge_of_darkness", "白牛冲", "全球冲脸"),
    _ability("裂魂人", "Spirit Breaker", "幽冥一击", "Nether Strike", "spirit_breaker_nether_strike", "白牛大", "白牛踹"),
    _ability("军团指挥官", "Legion Commander", "决斗", "Duel", "legion_commander_duel", "军团决斗", "拉决斗"),
    _ability("末日使者", "Doom", "末日", "Doom", "doom_bringer_doom", "末日大", "给末日"),
    _ability("昆卡", "Kunkka", "洪流", "Torrent", "kunkka_torrent", "船长水", "接水"),
    _ability("昆卡", "Kunkka", "幽灵船", "Ghostship", "kunkka_ghostship", "船长开船", "幽灵船"),
    _ability("斯拉达", "Slardar", "鱼人碎击", "Slithereen Crush", "slardar_slithereen_crush", "大鱼踩", "大鱼晕"),
    _ability("斯拉达", "Slardar", "侵蚀雾霭", "Corrosive Haze", "slardar_amplify_damage", "大鱼点灯", "点灯"),
    _ability("斯拉克", "Slark", "突袭", "Pounce", "slark_pounce", "小鱼跳", "小鱼拴"),
    _ability("斯拉克", "Slark", "黑暗契约", "Dark Pact", "slark_dark_pact", "小鱼开c", "小鱼解控"),
    _ability("斯拉克", "Slark", "暗影之舞", "Shadow Dance", "slark_shadow_dance", "小鱼开大", "小鱼隐身"),
    _ability("幻影刺客", "Phantom Assassin", "窒碍短匕", "Stifling Dagger", "phantom_assassin_stifling_dagger", "幻刺飞镖", "pa飞镖"),
    _ability("幻影刺客", "Phantom Assassin", "恩赐解脱", "Coup de Grace", "phantom_assassin_coup_de_grace", "幻刺暴击", "pa暴击"),
    _ability("痛苦女王", "Queen of Pain", "痛苦尖叫", "Scream of Pain", "queenofpain_scream_of_pain", "女王吼", "女王尖叫"),
    _ability("痛苦女王", "Queen of Pain", "超声冲击波", "Sonic Wave", "queenofpain_sonic_wave", "女王大", "超声波"),
    _ability("变体精灵", "Morphling", "波浪形态", "Waveform", "morphling_waveform", "水人波", "波过去"),
    _ability("变体精灵", "Morphling", "属性变换", "Attribute Shift", "morphling_attribute_shift_str", "水人转血", "转力量"),
    _ability("敌法师", "Anti-Mage", "法力虚空", "Mana Void", "antimage_mana_void", "敌法大", "爆蓝"),
    _ability("露娜", "Luna", "月蚀", "Eclipse", "luna_eclipse", "月骑大", "露娜开大"),
    _ability("沙王", "Sand King", "地震", "Epicenter", "sandking_epicenter", "沙王摇大", "沙王大"),
    _ability("水晶室女", "Crystal Maiden", "极寒领域", "Freezing Field", "crystal_maiden_freezing_field", "冰女大", "冰女摇大"),
    _ability("莉娜", "Lina", "神灭斩", "Laguna Blade", "lina_laguna_blade", "火女大", "神灭斩"),
    _ability("莱恩", "Lion", "死亡一指", "Finger of Death", "lion_finger_of_death", "莱恩大", "死亡一指"),
    _ability("宙斯", "Zeus", "雷神之怒", "Thundergod's Wrath", "zuus_thundergods_wrath", "宙斯大", "全球雷"),
    _ability("狙击手", "Sniper", "暗杀", "Assassinate", "sniper_assassinate", "火枪大", "狙击大"),
    _ability("米拉娜", "Mirana", "月神之箭", "Sacred Arrow", "mirana_arrow", "白虎箭", "五秒箭", "月神箭"),
    _ability("修补匠", "Tinker", "再装填", "Rearm", "tinker_rearm", "tk刷新", "修补匠刷新"),
    _ability("帕克", "Puck", "梦境缠绕", "Dream Coil", "puck_dream_coil", "帕克大", "梦境缠绕"),
    _ability("干扰者", "Disruptor", "恶念瞥视", "Glimpse", "disruptor_glimpse", "萨尔瞥视", "萨尔拉回"),
    _ability("干扰者", "Disruptor", "静态风暴", "Static Storm", "disruptor_static_storm", "萨尔大", "静态风暴"),
    _ability("娜迦海妖", "Naga Siren", "海妖之歌", "Song of the Siren", "naga_siren_song_of_the_siren", "小娜迦唱歌", "娜迦睡"),
    _ability("恐怖利刃", "Terrorblade", "魔化", "Metamorphosis", "terrorblade_metamorphosis", "tb变身", "恐怖利刃变身"),
    _ability("齐天大圣", "Monkey King", "猴子猴孙", "Wukong's Command", "monkey_king_wukongs_command", "大圣开大", "猴阵"),
    _ability("石鳞剑士", "Pangolier", "地雷滚滚", "Rolling Thunder", "pangolier_gyroshell", "滚滚开车", "滚滚大"),
)


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias.casefold())
    if re.fullmatch(r"[a-z][a-z0-9' ._-]*", alias.casefold()):
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
    return re.compile(escaped)


def match_dota2_abilities(
    *content: str,
    limit: int = MAX_MATCHED_ABILITIES,
) -> list[Dota2AbilityMatch]:
    combined = "\n".join(str(value or "") for value in content)
    folded = combined.casefold()
    candidates: list[Dota2AbilityMatch] = []
    for ability in DOTA2_ABILITIES:
        for alias in sorted(ability.aliases, key=len, reverse=True):
            for found in _alias_pattern(alias).finditer(folded):
                candidates.append(
                    Dota2AbilityMatch(
                        ability=ability,
                        alias=combined[found.start():found.end()] or alias,
                        start=found.start(),
                        end=found.end(),
                    )
                )

    candidates.sort(key=lambda match: (match.start, -(match.end - match.start)))
    selected: list[Dota2AbilityMatch] = []
    selected_abilities: set[Dota2Ability] = set()
    occupied: list[tuple[int, int]] = []
    for candidate in candidates:
        if candidate.ability in selected_abilities:
            continue
        if any(candidate.start < end and candidate.end > start for start, end in occupied):
            continue
        selected.append(candidate)
        selected_abilities.add(candidate.ability)
        occupied.append((candidate.start, candidate.end))
        if len(selected) >= max(1, limit):
            break
    return selected


def dota2_ability_prompt_instruction(
    matches: Iterable[Dota2AbilityMatch],
) -> str:
    normalized = list(matches)
    if not normalized:
        return (
            "Dota 2 技能规则：未检出可确定的技能俗称；不要把普通动作词强行解释为技能。"
        )
    resolved = "；".join(
        f"{match.alias}＝{match.ability.hero_label}的{match.ability.label}"
        for match in normalized
    )
    return (
        "Dota 2 技能与俗称消歧规则：技能俗称必须先还原到唯一英雄及技能正式名称，"
        "不能按字面画成现实动作，也不能套用《英雄联盟》或其他游戏的同名技能。"
        f"本次技能识别结果：{resolved}。"
        "随附的 DOTA 2 OFFICIAL ABILITY ICON REFERENCES 是命中技能的官方图标参考板；"
        "若画面表现施法，英雄身份、能量颜色、技能形状和关键视觉符号必须与参考板及"
        "Dota 2 原设一致。不要因为俗称相似而更换英雄，也不要把多个技能融合成一个。"
    )


def download_dota2_ability_icon(
    ability: Dota2Ability,
    cache_dir: Path,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{ability.icon_slug}.png"
    if destination.is_file() and destination.stat().st_size > 0:
        try:
            with Image.open(destination) as cached:
                cached.verify()
            return destination
        except (OSError, UnidentifiedImageError):
            destination.unlink(missing_ok=True)

    url = f"{DOTA2_ABILITY_ICON_BASE_URL}/{ability.icon_slug}.png"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as remote:
        raw = remote.read(MAX_ABILITY_ICON_BYTES + 1)
    if not raw or len(raw) > MAX_ABILITY_ICON_BYTES:
        raise ValueError(f"{ability.hero_label}的{ability.label}官方图标为空或过大")
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(raw)
    try:
        with Image.open(temporary) as downloaded:
            downloaded.verify()
    except (OSError, UnidentifiedImageError) as exc:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"{ability.hero_label}的{ability.label}官方图标无效") from exc
    temporary.replace(destination)
    return destination


def build_dota2_ability_reference_sheet(
    matches: Iterable[Dota2AbilityMatch],
    cache_dir: Path,
    output_path: Path,
) -> tuple[Path | None, list[str]]:
    normalized = list(matches)
    icons: list[tuple[Dota2Ability, Image.Image]] = []
    errors: list[str] = []

    def load_icon(
        match: Dota2AbilityMatch,
    ) -> tuple[Dota2Ability, Image.Image]:
        icon_path = download_dota2_ability_icon(match.ability, cache_dir)
        with Image.open(icon_path) as source:
            return match.ability, source.convert("RGBA").copy()

    loaded: list[tuple[Dota2Ability, Image.Image] | Exception] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(4, max(1, len(normalized))),
    ) as pool:
        futures = [pool.submit(load_icon, match) for match in normalized]
        for future in futures:
            try:
                loaded.append(future.result())
            except Exception as exc:
                loaded.append(exc)

    for match, result in zip(normalized, loaded):
        if isinstance(result, Exception):
            if isinstance(result, (OSError, ValueError, urllib.error.URLError)):
                errors.append(f"{match.ability.hero_label}的{match.ability.label}: {result}")
                continue
            raise result
        icons.append(result)
    if not icons:
        return None, errors

    columns = min(3, len(icons))
    rows = (len(icons) + columns - 1) // columns
    tile_width, tile_height = 300, 205
    header_height = 70
    canvas = Image.new(
        "RGB",
        (columns * tile_width, header_height + rows * tile_height),
        "#111827",
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text(
        (20, 18),
        "DOTA 2 OFFICIAL ABILITY ICON REFERENCES",
        fill="#ffffff",
        font=font,
    )
    draw.text(
        (20, 40),
        "Match the correct hero, spell shape, color and visual symbol.",
        fill="#93c5fd",
        font=font,
    )
    for index, (ability, icon) in enumerate(icons):
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
        icon.thumbnail((128, 128), Image.Resampling.LANCZOS)
        icon_left = left + (tile_width - icon.width) // 2
        canvas.paste(icon, (icon_left, top + 18), icon)
        draw.text(
            (left + 16, top + 154),
            f"{ability.hero_english_name}: {ability.english_name}"[:43],
            fill="#ffffff",
            font=font,
        )
        draw.text(
            (left + 16, top + 175),
            ability.icon_slug[:45],
            fill="#94a3b8",
            font=font,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path, errors
