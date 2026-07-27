"""Dota 2 item names, Chinese community aliases, and official icon references."""

from __future__ import annotations

import concurrent.futures
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError


DOTA2_ITEM_DATA_SOURCE = "https://www.dota2.com/datafeed/itemlist?language=schinese"
DOTA2_ITEM_ICON_BASE_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/"
    "dota_react/items"
)
MAX_MATCHED_ITEMS = 8
MAX_ITEM_ICON_BYTES = 4 * 1024 * 1024


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
    _item("血棘", "Bloodthorn", "bloodthorn", "大紫怨"),
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
    _item("刃甲", "Blade Mail", "blade_mail", "刀甲"),
    _item("挑战头巾", "Hood of Defiance", "hood_of_defiance", "挑战头"),
    _item("永世法衣", "Eternal Shroud", "eternal_shroud", "永恒法衣", "法衣"),
    _item("幽魂权杖", "Ghost Scepter", "ghost", "绿杖", "幽魂杖"),
    _item("圣剑", "Divine Rapier", "rapier", "圣剑"),
    _item("金箍棒", "Monkey King Bar", "monkey_king_bar", "MKB", "金箍棒"),
    _item("辉耀", "Radiance", "radiance", "辉耀"),
    _item("蝴蝶", "Butterfly", "butterfly", "蝴蝶"),
    _item("代达罗斯之殇", "Daedalus", "greater_crit", "大炮", "代达罗斯"),
    _item("水晶剑", "Crystalys", "lesser_crit", "小炮", "水晶剑"),
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
    _item("法师克星", "Mage Slayer", "mage_slayer", "法师杀手"),
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
)


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias.casefold())
    if re.fullmatch(r"[a-z][a-z0-9' ._-]*", alias.casefold()):
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
    return re.compile(escaped)


def match_dota2_items(*content: str, limit: int = MAX_MATCHED_ITEMS) -> list[Dota2ItemMatch]:
    combined = "\n".join(str(value or "") for value in content)
    folded = combined.casefold()
    candidates: list[Dota2ItemMatch] = []
    for item in DOTA2_ITEMS:
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


def dota2_item_prompt_instruction(matches: Iterable[Dota2ItemMatch]) -> str:
    normalized = list(matches)
    if not normalized:
        return (
            "Dota 2 装备规则：未检出可确定的装备俗称；不要凭普通名词臆造游戏装备。"
        )
    resolved = "；".join(
        f"{match.alias}＝{match.item.label}"
        for match in normalized
    )
    return (
        "Dota 2 装备与俗称消歧规则：所有命中的装备都必须理解为 Valve《Dota 2》的"
        "对应物品，不能按字面画成现实物品，也不能替换成《英雄联盟》或其他游戏装备。"
        f"本次装备识别结果：{resolved}。"
        "随附的 DOTA 2 OFFICIAL ITEM ICON REFERENCES 是这些装备的官方游戏图标参考板；"
        "如果封面需要表现装备，必须以参考板中的轮廓、主色、材质与核心符号为准，"
        "每件装备保持独立，不得把两件装备融合成一件。可以将图标风格转化为精致插画道具，"
        "但不能改变其身份特征；没有出现在识别结果中的装备不要擅自添加。"
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
    tile_width, tile_height = 280, 190
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
        icon.thumbnail((220, 128), Image.Resampling.NEAREST)
        icon_left = left + (tile_width - icon.width) // 2
        icon_top = top + 18
        canvas.paste(icon, (icon_left, icon_top), icon)
        label = item.english_name[:38]
        text_box = draw.textbbox((0, 0), label, font=font)
        text_width = text_box[2] - text_box[0]
        draw.text(
            (left + (tile_width - text_width) // 2, top + 155),
            label,
            fill="#ffffff",
            font=font,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path, errors
