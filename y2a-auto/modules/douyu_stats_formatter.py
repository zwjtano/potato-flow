#!/usr/bin/env python3
"""Format time-scoped Douyu statistics for one recording.

The recording XML is the source of truth for both the recording timeframe and
the streamer hero.  A DOTA2 player is only selected when hero mentions in the
XML provide unique, repeatable evidence; there is deliberately no slot-based
fallback.
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional


HERO_ALIASES: dict[str, tuple[str, ...]] = {
    "风暴之灵": ("蓝猫",),
    "灰烬之灵": ("火猫",),
    "大地之灵": ("土猫",),
    "虚无之灵": ("紫猫",),
    "变体精灵": ("水人",),
    "天穹守望者": ("电狗",),
    "狙击手": ("火枪",),
    "撼地者": ("小牛", "神牛"),
    "熊战士": ("拍拍", "拍拍熊"),
    "影魔": ("sf",),
    "敌法师": ("敌法", "am"),
    "幻影长矛手": ("猴子", "pl"),
    "幻影刺客": ("幻刺", "pa"),
    "圣堂刺客": ("圣堂", "ta"),
    "矮人直升机": ("飞机",),
    "斯拉克": ("小鱼", "小鱼人"),
    "斯拉达": ("大鱼", "大鱼人"),
    "卓尔游侠": ("小黑",),
    "克林克兹": ("骨弓", "小骷髅"),
    "水晶室女": ("冰女", "cm"),
    "痛苦女王": ("女王", "qop"),
    "祈求者": ("卡尔",),
    "修补匠": ("tk",),
    "食人魔魔法师": ("蓝胖",),
    "光之守卫": ("光法",),
    "自然先知": ("先知",),
    "干扰者": ("萨尔",),
    "工程师": ("炸弹人", "炸弹"),
    "噬魂鬼": ("小狗",),
    "主宰": ("剑圣",),
    "冥魂大帝": ("骷髅王",),
    "露娜": ("月骑",),
    "恐怖利刃": ("tb",),
    "虚空假面": ("虚空",),
    "钢背兽": ("钢背", "刚背", "刚被"),
    "半人马战行者": ("人马",),
    "马格纳斯": ("猛犸",),
    "裂魂人": ("白牛",),
    "赏金猎人": ("赏金", "bh"),
    "帕吉": ("屠夫", "胖子"),
}


def load_stats(stats_path: str | os.PathLike[str]) -> dict:
    """Load one atomic stats snapshot, returning an empty dict on failure."""
    try:
        with open(stats_path, "r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _normalise_text(value: object) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


@lru_cache(maxsize=8)
def _load_xml_comments_cached(
    signature: tuple[tuple[str, int, int], ...],
) -> tuple[tuple[float, str], ...]:
    comments: list[tuple[float, str]] = []
    for path, _size, _mtime_ns in signature:
        xml_path = Path(path)
        try:
            for _event, element in ET.iterparse(xml_path, events=("end",)):
                if element.tag != "d":
                    element.clear()
                    continue
                fields = str(element.attrib.get("p") or "").split(",")
                if len(fields) >= 5:
                    try:
                        unix_ts = float(fields[4])
                    except ValueError:
                        unix_ts = 0
                    text = str(element.text or "").strip()
                    if unix_ts > 0 and text:
                        comments.append((unix_ts, text))
                element.clear()
        except (OSError, ET.ParseError):
            continue
    comments.sort(key=lambda item: item[0])
    return tuple(comments)


def load_xml_comments(video_dir: str | os.PathLike[str]) -> list[tuple[float, str]]:
    """Return absolute timestamp/text pairs from every XML in a recording dir."""
    root = Path(video_dir)
    xml_paths = sorted(root.glob("*.xml")) if root.is_dir() else [root]
    signature: list[tuple[str, int, int]] = []
    for xml_path in xml_paths:
        try:
            stat = xml_path.stat()
        except OSError:
            continue
        signature.append((str(xml_path.resolve()), stat.st_size, stat.st_mtime_ns))
    return list(_load_xml_comments_cached(tuple(signature)))


def recording_timeframe(
    video_dir: str | os.PathLike[str],
    comments: Optional[list[tuple[float, str]]] = None,
) -> tuple[float, float]:
    """Use XML absolute timestamps as the recording boundary."""
    values = comments if comments is not None else load_xml_comments(video_dir)
    if values:
        return values[0][0], values[-1][0]
    root = Path(video_dir)
    try:
        modified = root.stat().st_mtime
    except OSError:
        modified = time.time()
    return 0.0, modified


def filter_by_timeframe(
    items: Iterable[dict],
    start_ts: float,
    end_ts: float,
    ts_key: str = "unix_ts",
) -> list[dict]:
    return [
        item for item in items
        if isinstance(item, dict)
        and start_ts <= float(item.get(ts_key) or 0) <= end_ts
    ]


def select_anchor_player(
    players: Iterable[dict],
    comments: Iterable[tuple[float, str]],
    start_ts: float,
    end_ts: float,
) -> dict | None:
    """Select the streamer hero only from unique XML mention evidence."""
    texts = [
        _normalise_text(text)
        for unix_ts, text in comments
        if start_ts <= unix_ts <= end_ts
    ]
    scores: list[tuple[int, dict]] = []
    for player in players:
        hero = str(player.get("hero") or "").strip()
        hero_id = str(player.get("id") or "").strip()
        if not hero or hero.startswith("未知(") or hero_id in {"", "0"}:
            continue
        aliases = {_normalise_text(hero)}
        aliases.update(_normalise_text(alias) for alias in HERO_ALIASES.get(hero, ()))
        aliases.discard("")
        score = sum(
            sum(text.count(alias) for alias in aliases)
            for text in texts
        )
        scores.append((score, player))
    scores.sort(key=lambda item: item[0], reverse=True)
    if not scores or scores[0][0] < 2:
        return None
    runner_up = scores[1][0] if len(scores) > 1 else 0
    if scores[0][0] <= runner_up:
        return None
    selected = dict(scores[0][1])
    selected["xml_mention_score"] = scores[0][0]
    return selected


def _stats_path(video_dir: str | os.PathLike[str]) -> Path:
    root = Path(video_dir)
    recordings_root = Path(os.environ.get("RECORDINGS_DIR", "/data/recordings"))
    room_name = root.parent.name if root.parent.name else root.name
    candidates = (
        root / ".potato-flow" / "douyu-stats.json",
        root.parent / ".potato-flow" / "douyu-stats.json",
        root / "stats_current.json",
        root.parent / "stats_current.json",
        recordings_root / room_name / ".potato-flow" / "douyu-stats.json",
        recordings_root / room_name / "stats_current.json",
    )
    return next((path for path in candidates if path.is_file()), candidates[1])


def _overlapping_games(stats: dict, start_ts: float, end_ts: float) -> list[dict]:
    games = [game for game in stats.get("games", []) if isinstance(game, dict)]
    active = stats.get("active_game")
    if isinstance(active, dict):
        games.append(active)
    result = []
    for game in games:
        game_start = float(game.get("start_unix_ts") or game.get("unix_ts") or 0)
        game_end = float(game.get("end_unix_ts") or game.get("last_seen_unix_ts") or time.time())
        if game_end >= start_ts and game_start <= end_ts:
            result.append(game)
    return result


def get_game_for_cover(video_dir: str | os.PathLike[str]) -> dict | None:
    """Return the latest XML-identified streamer player for cover prompting."""
    comments = load_xml_comments(video_dir)
    if not comments:
        return None
    start_ts, end_ts = recording_timeframe(video_dir, comments)
    stats = load_stats(_stats_path(video_dir))
    selected: dict | None = None
    for game in _overlapping_games(stats, start_ts, end_ts):
        game_start = max(start_ts, float(game.get("start_unix_ts") or start_ts))
        game_end = min(end_ts, float(game.get("end_unix_ts") or game.get("last_seen_unix_ts") or end_ts))
        candidate = select_anchor_player(game.get("players", []), comments, game_start, game_end)
        if candidate:
            selected = candidate
    return selected


def get_identity_diagnostics(video_dir: str | os.PathLike[str]) -> dict:
    """Expose the type_tooltips evidence available to XML identity matching."""
    stats_path = _stats_path(video_dir)
    stats = load_stats(stats_path)
    tooltip = stats.get("tooltip_diagnostics", {})
    if not isinstance(tooltip, dict):
        tooltip = {}
    games = [item for item in stats.get("games", []) if isinstance(item, dict)]
    if isinstance(stats.get("active_game"), dict):
        games.append(stats["active_game"])
    return {
        "stats_path": str(stats_path),
        "stats_available": bool(stats),
        "type_tooltips_messages": int(tooltip.get("messages") or 0),
        "type_tooltips_http_polls": int(tooltip.get("http_polls") or 0),
        "type_tooltips_http_snapshots": int(tooltip.get("http_snapshots") or 0),
        "type_tooltips_valid_snapshots": int(tooltip.get("valid_snapshots") or 0),
        "type_tooltips_invalid_snapshots": int(tooltip.get("invalid_snapshots") or 0),
        "type_tooltips_last_player_count": int(tooltip.get("last_raw_player_count") or 0),
        "type_tooltips_game_snapshots": len(games),
    }


def format_stats(
    stats: dict,
    start_ts: float,
    end_ts: float,
    xml_comments: Optional[list[tuple[float, str]]] = None,
) -> str:
    """Format events that overlap exactly one recording timeframe."""
    gift_events = filter_by_timeframe(stats.get("gift_events", []), start_ts, end_ts)
    gift_totals: dict[tuple[str, int], dict[str, int | str]] = {}
    for event in gift_events:
        name = str(event.get("name") or "未知礼物")
        unit_price = int(event.get("unit_price") or 0)
        key = (name, unit_price)
        summary = gift_totals.setdefault(key, {"name": name, "count": 0, "total": 0})
        summary["count"] = int(summary["count"]) + int(event.get("count") or 0)
        summary["total"] = int(summary["total"]) + int(event.get("total_value") or 0)

    high_events = filter_by_timeframe(
        stats.get("high_energy", {}).get("details", []), start_ts, end_ts
    )
    online = filter_by_timeframe(stats.get("online_samples", []), start_ts, end_ts)

    game_lines: list[str] = []
    comments = xml_comments or []
    for game in _overlapping_games(stats, start_ts, end_ts):
        game_start = max(start_ts, float(game.get("start_unix_ts") or start_ts))
        game_end = min(end_ts, float(game.get("end_unix_ts") or game.get("last_seen_unix_ts") or end_ts))
        anchor = select_anchor_player(game.get("players", []), comments, game_start, game_end)
        if not anchor:
            continue
        hero = str(anchor.get("hero") or "").strip()
        equipment = [str(item) for item in anchor.get("items", []) if str(item)]
        if anchor.get("neutral"):
            equipment.append(str(anchor["neutral"]))
        if anchor.get("scepter"):
            equipment.append("A杖")
        if anchor.get("shard"):
            equipment.append("魔晶")
        game_lines.append(f"{hero}({','.join(equipment)})" if equipment else hero)

    if not gift_totals and not high_events and not online and not game_lines:
        return ""

    lines = ["", "——— 直播数据 ———"]
    if gift_totals:
        ordered = sorted(gift_totals.values(), key=lambda item: -int(item["total"]))
        gift_text = " ".join(
            f"{item['name']}×{item['count']}({item['total']}元)" for item in ordered
        )
        lines.append(f"🎁 {gift_text} | 合计 {sum(int(item['total']) for item in ordered)}元")
    elif high_events or online or game_lines:
        lines.append("🎁 无高价值礼物(≥100元)")
    if high_events:
        lines.append(
            f"💬 高能弹幕 ×{len(high_events)} | "
            f"{sum(int(item.get('amount') or 0) for item in high_events)}元"
        )
    if online:
        values = [int(item.get("value") or 0) for item in online]
        minimum, maximum = min(values), max(values)
        lines.append(f"👥 在线 {minimum}" if minimum == maximum else f"👥 在线 {minimum}~{maximum}")
    if game_lines:
        lines.append(f"🎮 {' | '.join(game_lines)}")
    return "\n".join(lines)


def get_stats_for_description(
    video_dir: str | os.PathLike[str],
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> str:
    comments = load_xml_comments(video_dir)
    if start_ts is None or end_ts is None:
        detected_start, detected_end = recording_timeframe(video_dir, comments)
        start_ts = detected_start if start_ts is None else start_ts
        end_ts = detected_end if end_ts is None else end_ts
    stats = load_stats(_stats_path(video_dir))
    if not stats:
        return ""
    return format_stats(stats, float(start_ts), float(end_ts), comments)
