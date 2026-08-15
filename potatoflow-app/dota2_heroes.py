"""Valve-backed Dota 2 hero names and official portrait references."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError


DOTA2_HERO_DATA_SOURCE = "https://www.dota2.com/datafeed/herolist?language=schinese"
DOTA2_HERO_IMAGE_BASE_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/"
    "dota_react/heroes"
)
MAX_HERO_IMAGE_BYTES = 8 * 1024 * 1024

# Chinese community names that do not equal Valve's localized hero names.
DOTA2_HERO_ALIASES: dict[str, str] = {
    "小松鼠": "森海飞霞",
    "sa": "力丸",
    "隐刺": "力丸",
    "隐形刺客": "力丸",
    "ta": "圣堂刺客",
    "圣堂": "圣堂刺客",
    "sf": "影魔",
    "am": "敌法师",
    "敌法": "敌法师",
    "pa": "幻影刺客",
    "幻刺": "幻影刺客",
    "pl": "幻影长矛手",
    "猴子": "幻影长矛手",
    "蓝猫": "风暴之灵",
    "火猫": "灰烬之灵",
    "土猫": "大地之灵",
    "紫猫": "虚无之灵",
    "水人": "变体精灵",
    "电狗": "天穹守望者",
    "火枪": "狙击手",
    "白牛": "裂魂人",
    "小牛": "撼地者",
    "神牛": "撼地者",
    "大牛": "上古巨神",
    "拍拍": "熊战士",
    "拍拍熊": "熊战士",
    "飞机": "矮人直升机",
    "蚂蚁": "编织者",
    "小鱼": "斯拉克",
    "小鱼人": "斯拉克",
    "大鱼": "斯拉达",
    "大鱼人": "斯拉达",
    "小黑": "卓尔游侠",
    "大娜迦": "美杜莎",
    "小娜迦": "娜迦海妖",
    "骨弓": "克林克兹",
    "小骷髅": "克林克兹",
    "骨法": "帕格纳",
    "冰女": "水晶室女",
    "cm": "水晶室女",
    "火女": "莉娜",
    "女王": "痛苦女王",
    "qop": "痛苦女王",
    "黑鸟": "殁境神蚀者",
    "od": "殁境神蚀者",
    "卡尔": "祈求者",
    "tk": "修补匠",
    "dp": "死亡先知",
    "仙女龙": "帕克",
    "老鹿": "莱席拉克",
    "蓝胖": "食人魔魔法师",
    "光法": "光之守卫",
    "kotl": "光之守卫",
    "死灵法": "瘟疫法师",
    "nec": "瘟疫法师",
    "先知": "自然先知",
    "furion": "自然先知",
    "小y": "暗影萨满",
    "小歪": "暗影萨满",
    "萨尔": "干扰者",
    "暗牧": "戴泽",
    "炸弹人": "工程师",
    "赏金": "赏金猎人",
    "bh": "赏金猎人",
    "小狗": "噬魂鬼",
    "大圣": "齐天大圣",
    "mk": "齐天大圣",
    "剑圣": "主宰",
    "jugg": "主宰",
    "骷髅王": "冥魂大帝",
    "wk": "冥魂大帝",
    "混沌": "混沌骑士",
    "ck": "混沌骑士",
    "月骑": "露娜",
    "tb": "恐怖利刃",
    "虚空": "虚空假面",
    "巨魔": "巨魔战将",
    "龙骑": "龙骑士",
    "dk": "龙骑士",
    "钢背": "钢背兽",
    "刚背": "钢背兽",
    "人马": "半人马战行者",
    "猛犸": "马格纳斯",
    "潮汐": "潮汐猎人",
    "军团": "军团指挥官",
    "lc": "军团指挥官",
    "末日": "末日使者",
    "船长": "昆卡",
    "大屁股": "孽主",
    "滚滚": "石鳞剑士",
    "花母鸡": "伐木机",
    "发条": "发条技师",
    "炼金": "炼金术士",
    "sk": "沙王",
    "电魂": "剃刀",
    "电棍": "剃刀",
    "神灵": "哈斯卡",
    "蝙蝠": "蝙蝠骑士",
    "屠夫": "帕吉",
    "胖子": "帕吉",
    "老奶奶": "电炎绝手",
}

# Liquipedia's compact draft template uses short codes rather than Valve's
# internal hero slugs. Keep this mapping explicit so lineup covers never ask an
# image model to guess what an abbreviation means.
LIQUIPEDIA_HERO_ALIASES: dict[str, str] = {
    "cw": "rattletrap",
    "ww": "winter_wyvern",
    "ul": "abyssal_underlord",
    "esp": "earth_spirit",
    "wr": "windrunner",
    "vip": "viper",
    "io": "wisp",
    "ring master": "ringmaster",
}

LIQUIPEDIA_HERO_METADATA: dict[str, tuple[str, str, str]] = {
    "lina": ("莉娜", "Lina", "lina"),
    "cw": ("发条技师", "Clockwerk", "rattletrap"),
    "ww": ("寒冬飞龙", "Winter Wyvern", "winter_wyvern"),
    "ul": ("孽主", "Underlord", "abyssal_underlord"),
    "kez": ("凯斯", "Kez", "kez"),
    "esp": ("大地之灵", "Earth Spirit", "earth_spirit"),
    "tusk": ("巨牙海民", "Tusk", "tusk"),
    "wr": ("风行者", "Windranger", "windrunner"),
    "vip": ("冥界亚龙", "Viper", "viper"),
    "io": ("艾欧", "Io", "wisp"),
}


@dataclass(frozen=True)
class Dota2Hero:
    chinese_name: str
    english_name: str
    icon_slug: str
    primary_attribute: str = ""

    @property
    def label(self) -> str:
        return f"{self.chinese_name}（{self.english_name}）"

    @property
    def is_intelligence(self) -> bool:
        return self.primary_attribute == "intelligence"


@lru_cache(maxsize=1)
def load_official_dota2_heroes() -> tuple[Dota2Hero, ...]:
    request = urllib.request.Request(
        DOTA2_HERO_DATA_SOURCE,
        headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as remote:
            payload = json.load(remote)
    except (OSError, ValueError, urllib.error.URLError):
        return ()
    rows = payload.get("result", {}).get("data", {}).get("heroes", [])
    result: list[Dota2Hero] = []
    for row in rows if isinstance(rows, list) else []:
        internal_name = str(row.get("name") or "")
        chinese_name = str(row.get("name_loc") or "").strip()
        english_name = str(row.get("name_english_loc") or "").strip()
        slug = internal_name.removeprefix("npc_dota_hero_")
        primary_attribute = {
            0: "strength",
            1: "agility",
            2: "intelligence",
            3: "universal",
        }.get(row.get("primary_attr"), "")
        if chinese_name and english_name and slug:
            result.append(
                Dota2Hero(
                    chinese_name,
                    english_name,
                    slug,
                    primary_attribute,
                )
            )
    return tuple(result)


def find_official_dota2_hero(name: str) -> Dota2Hero | None:
    normalized = str(name or "").strip().casefold()
    fixed = LIQUIPEDIA_HERO_METADATA.get(normalized)
    if fixed:
        return Dota2Hero(*fixed)
    normalized = LIQUIPEDIA_HERO_ALIASES.get(normalized, normalized)
    normalized = DOTA2_HERO_ALIASES.get(normalized, normalized).casefold()
    for hero in load_official_dota2_heroes():
        if normalized in {hero.chinese_name.casefold(), hero.english_name.casefold(), hero.icon_slug.casefold()}:
            return hero
    return None


def build_dota2_lineup_reference(
    lineups: dict[str, list[str]],
    cache_dir: Path,
    output_path: Path,
) -> tuple[Path | None, dict[str, list[dict[str, str]]], list[str]]:
    """Build one deterministic two-team sheet from Valve hero portraits."""
    resolved: dict[str, list[dict[str, str]]] = {}
    errors: list[str] = []
    rows: list[tuple[str, list[tuple[Dota2Hero, Path]]]] = []
    for team, names in list(lineups.items())[:2]:
        heroes: list[tuple[Dota2Hero, Path]] = []
        resolved[team] = []
        for name in names[:5]:
            hero = find_official_dota2_hero(name)
            if hero is None:
                errors.append(f"{team}: Valve 英雄数据中未找到 {name}")
                continue
            try:
                portrait = download_dota2_hero_image(hero, cache_dir)
            except (OSError, ValueError, urllib.error.URLError, UnidentifiedImageError) as exc:
                errors.append(f"{team}: {hero.english_name}: {exc}")
                continue
            heroes.append((hero, portrait))
            resolved[team].append({
                "source": str(name),
                "chinese_name": hero.chinese_name,
                "english_name": hero.english_name,
                "icon_slug": hero.icon_slug,
            })
        rows.append((team, heroes))
    if len(rows) != 2 or any(len(heroes) != 5 for _, heroes in rows):
        return None, resolved, errors or ["双方阵容不是完整的 5+5 英雄"]
    cell_width, cell_height = 256, 176
    sheet = Image.new("RGB", (cell_width * 5, cell_height * 2 + 72), "#070b16")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for row_index, (team, heroes) in enumerate(rows):
        top = 36 + row_index * cell_height
        draw.text((12, 12 + row_index * cell_height), f"TEAM {row_index + 1}: {team}", fill="white", font=font)
        for column, (hero, portrait_path) in enumerate(heroes):
            with Image.open(portrait_path) as source:
                portrait = source.convert("RGB")
                portrait.thumbnail((cell_width - 8, cell_height - 28), Image.Resampling.LANCZOS)
                left = column * cell_width + (cell_width - portrait.width) // 2
                sheet.paste(portrait, (left, top))
            draw.text(
                (column * cell_width + 8, top + cell_height - 24),
                f"{column + 1}. {hero.english_name}",
                fill="white",
                font=font,
            )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")
    return output_path, resolved, errors


def download_dota2_hero_image(hero: Dota2Hero, cache_dir: Path) -> Path:
    """Download and validate one Valve official hero portrait."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{hero.icon_slug}.png"
    if cached.is_file() and cached.stat().st_size > 0:
        try:
            with Image.open(cached) as existing:
                existing.verify()
            return cached
        except (OSError, UnidentifiedImageError):
            cached.unlink(missing_ok=True)
    url = f"{DOTA2_HERO_IMAGE_BASE_URL}/{hero.icon_slug}.png"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"})
    with urllib.request.urlopen(request, timeout=20) as remote:
        raw = remote.read(MAX_HERO_IMAGE_BYTES + 1)
    if not raw or len(raw) > MAX_HERO_IMAGE_BYTES:
        raise ValueError("官方英雄图片为空或过大")
    temporary = cached.with_suffix(".tmp")
    temporary.write_bytes(raw)
    try:
        with Image.open(temporary) as downloaded:
            downloaded.verify()
    except (OSError, UnidentifiedImageError):
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(cached)
    return cached


def build_dota2_hero_reference(
    hero_name: str,
    cache_dir: Path,
    output_path: Path,
) -> tuple[Path | None, Dota2Hero | None, str | None]:
    hero = find_official_dota2_hero(hero_name)
    if hero is None:
        return None, None, f"Valve 英雄数据中未找到：{hero_name}"
    try:
        cached = download_dota2_hero_image(hero, cache_dir)
    except (OSError, ValueError, urllib.error.URLError, UnidentifiedImageError) as exc:
        return None, hero, str(exc)
    try:
        with Image.open(cached) as source:
            portrait = source.convert("RGB")
            portrait.thumbnail((768, 432), Image.Resampling.LANCZOS)
            sheet = Image.new("RGB", (800, 520), "#111827")
            sheet.paste(portrait, ((800 - portrait.width) // 2, 24))
            draw = ImageDraw.Draw(sheet)
            font = ImageFont.load_default()
            draw.text((24, 474), f"DOTA 2 OFFICIAL HERO REFERENCE: {hero.english_name}", fill="white", font=font)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            sheet.save(output_path, format="PNG")
    except (OSError, UnidentifiedImageError) as exc:
        return None, hero, str(exc)
    return output_path, hero, None
