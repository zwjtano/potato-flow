#!/usr/bin/env python3
"""Format time-scoped Douyu statistics for one recording.

Recording XML supplies the exact recording timeframe. Douyu's explicit
streamer-view ``hero`` object is the primary identity/equipment source; XML
hero mentions remain a compatibility fallback for older snapshots. There is
deliberately no fixed player-slot guess.
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

GSI_MIN_OBSERVATION_SECONDS = 60.0
XML_MIN_MENTION_SCORE = 25
XML_MIN_DOMINANCE_RATIO = 2.0
XML_MIN_MENTION_SHARE = 0.6


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
    """Return comments only from XML files belonging to this recording dir."""
    root = Path(video_dir)
    if root.is_dir():
        dir_name = root.name
        xml_paths = [
            path
            for path in sorted(root.glob("*.xml"))
            if path.stem == dir_name or path.stem.startswith(f"{dir_name}_")
        ]
    else:
        xml_paths = [root]
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
    """Use matching XML timestamps, then the Beijing-time directory name."""
    values = comments if comments is not None else load_xml_comments(video_dir)
    if values:
        return values[0][0], values[-1][0]
    root = Path(video_dir)
    match = re.search(
        r"(20\d{2}-\d{2}-\d{2})_(\d{2})-(\d{2})(?:-(\d{2}))?(?:$|_)",
        root.name,
    )
    if match:
        date_value, hour, minute, second = match.groups()
        parsed = time.strptime(
            f"{date_value} {hour}:{minute}:{second or '00'}",
            "%Y-%m-%d %H:%M:%S",
        )
        start_ts = time.mktime(parsed)
        return start_ts, start_ts + 3600.0
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
    """Select a hero only from strong, dominant XML mention evidence."""
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
    if not scores or scores[0][0] < XML_MIN_MENTION_SCORE:
        return None
    runner_up = scores[1][0] if len(scores) > 1 else 0
    top_score = scores[0][0]
    total_score = sum(score for score, _player in scores)
    if runner_up and top_score < runner_up * XML_MIN_DOMINANCE_RATIO:
        return None
    if total_score and top_score / total_score < XML_MIN_MENTION_SHARE:
        return None
    selected = dict(scores[0][1])
    selected["xml_mention_score"] = top_score
    selected["xml_runner_up_score"] = runner_up
    selected["xml_mention_share"] = round(top_score / total_score, 4)
    return selected


def _covered_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def select_streamer_player(
    game: dict,
    comments: Iterable[tuple[float, str]],
    start_ts: float,
    end_ts: float,
) -> dict | None:
    """Select a stable streamer view, with strong XML evidence as fallback."""
    candidates: list[tuple[float, float, dict, str]] = []
    raw_history = game.get("anchor_history")
    history = raw_history if isinstance(raw_history, list) else []
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict) or not isinstance(entry.get("player"), dict):
                continue
            entry_start = float(entry.get("start_unix_ts") or 0)
            entry_end = float(entry.get("last_seen_unix_ts") or entry_start)
            if entry_end < start_ts or entry_start > end_ts:
                continue
            candidates.append((
                max(entry_start, start_ts),
                min(entry_end, end_ts),
                entry["player"],
                str(entry.get("source") or game.get("anchor_source") or "gsi"),
            ))
    if candidates:
        hero_ids = {
            str(player.get("id") or _normalise_text(player.get("hero")))
            for _entry_start, _entry_end, player, _source in candidates
        }
        evidence_span = max(end for _start, end, _player, _source in candidates) - min(
            start for start, _end, _player, _source in candidates
        )
        observed_seconds = _covered_seconds(
            (start, end) for start, end, _player, _source in candidates
        )
        stable_enough = observed_seconds >= GSI_MIN_OBSERVATION_SECONDS or (
            len(candidates) >= 3 and evidence_span >= GSI_MIN_OBSERVATION_SECONDS
        )
        if len(hero_ids) == 1 and stable_enough:
            _entry_start, snapshot_ts, player, source = max(
                candidates, key=lambda item: item[1]
            )
            selected = dict(player)
            selected["identity_source"] = f"gsi_hero:{source}"
            selected["equipment_snapshot_unix_ts"] = snapshot_ts
            selected["gsi_observed_seconds"] = round(observed_seconds, 3)
            return selected

    anchor = game.get("anchor_player")
    anchor_seen = float(game.get("anchor_last_seen_unix_ts") or 0)
    game_start = max(start_ts, float(game.get("start_unix_ts") or start_ts))
    legacy_observed_seconds = anchor_seen - game_start
    if (
        not isinstance(raw_history, list)
        and isinstance(anchor, dict)
        and start_ts <= anchor_seen <= end_ts
        and legacy_observed_seconds >= GSI_MIN_OBSERVATION_SECONDS
    ):
        selected = dict(anchor)
        selected["identity_source"] = f"gsi_hero:{game.get('anchor_source') or 'gsi'}"
        selected["equipment_snapshot_unix_ts"] = min(anchor_seen, end_ts)
        selected["gsi_observed_seconds"] = round(legacy_observed_seconds, 3)
        return selected

    selected = select_anchor_player(game.get("players", []), comments, start_ts, end_ts)
    if selected:
        selected["identity_source"] = "xml_dominant_mention"
        selected["equipment_snapshot_unix_ts"] = float(
            game.get("last_seen_unix_ts") or game.get("end_unix_ts") or end_ts
        )
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
    """Return the streamer hero, KDA and final in-recording equipment snapshot."""
    comments = load_xml_comments(video_dir)
    start_ts, end_ts = recording_timeframe(video_dir, comments)
    stats = load_stats(_stats_path(video_dir))
    best: tuple[float, dict] | None = None
    for game in _overlapping_games(stats, start_ts, end_ts):
        game_start = max(start_ts, float(game.get("start_unix_ts") or start_ts))
        game_end = min(end_ts, float(game.get("end_unix_ts") or game.get("last_seen_unix_ts") or end_ts))
        candidate = select_streamer_player(game, comments, game_start, game_end)
        if candidate:
            candidate["items"] = [
                str(item) for item in candidate.get("items", [])[:6] if str(item)
            ]
            overlap = max(0.0, game_end - game_start)
            if best is None or overlap > best[0]:
                best = (overlap, candidate)
    return best[1] if best is not None else None


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
        "gsi_streamer_anchor_snapshots": int(tooltip.get("streamer_anchor_snapshots") or 0),
        "gsi_streamer_anchor_available": any(
            isinstance(game.get("anchor_player"), dict) for game in games
        ),
    }


def format_stats(
    stats: dict,
    start_ts: float,
    end_ts: float,
    xml_comments: Optional[list[tuple[float, str]]] = None,
) -> str:
    """Format events that overlap exactly one recording timeframe."""
    def format_yuan(cents: int) -> str:
        if cents % 100 == 0:
            return str(cents // 100)
        return f"{cents / 100:.2f}".rstrip("0").rstrip(".")

    gift_events = filter_by_timeframe(stats.get("gift_events", []), start_ts, end_ts)
    gift_totals: dict[tuple[str, int], dict[str, int | str]] = {}
    for event in gift_events:
        name = str(event.get("name") or "未知礼物")
        if "unit_price_cents" in event:
            unit_price_cents = int(event.get("unit_price_cents") or 0)
            total_value_cents = int(
                event.get("total_value_cents")
                or unit_price_cents * int(event.get("count") or 0)
            )
            is_paid = bool(event.get("paid"))
        else:
            # Schema-v2 snapshots written before full gift collection used yuan.
            unit_price_cents = int(round(float(event.get("unit_price") or 0) * 100))
            total_value_cents = int(round(float(
                event.get("total_value")
                or float(event.get("unit_price") or 0) * int(event.get("count") or 0)
            ) * 100))
            is_paid = unit_price_cents > 0
        if not is_paid or unit_price_cents < 10_000:
            continue
        key = (name, unit_price_cents)
        summary = gift_totals.setdefault(
            key, {"name": name, "count": 0, "total_cents": 0}
        )
        summary["count"] = int(summary["count"]) + int(event.get("count") or 0)
        summary["total_cents"] = int(summary["total_cents"]) + total_value_cents

    high_events = filter_by_timeframe(
        stats.get("high_energy", {}).get("details", []), start_ts, end_ts
    )
    diamond_events = filter_by_timeframe(
        stats.get("diamond_fans", {}).get("events", []), start_ts, end_ts
    )
    online = filter_by_timeframe(stats.get("online_samples", []), start_ts, end_ts)

    game_lines: list[str] = []
    comments = xml_comments or []
    for game in _overlapping_games(stats, start_ts, end_ts):
        game_start = max(start_ts, float(game.get("start_unix_ts") or start_ts))
        game_end = min(end_ts, float(game.get("end_unix_ts") or game.get("last_seen_unix_ts") or end_ts))
        anchor = select_streamer_player(game, comments, game_start, game_end)
        if not anchor:
            continue
        hero = str(anchor.get("hero") or "").strip()
        main_items = [
            str(item).strip()
            for item in anchor.get("items", [])[:6]
            if str(item).strip()
            and _normalise_text(item) not in {"empty", "unknown"}
            and not str(item).startswith("未知(empty)")
        ]
        equipment_parts: list[str] = []
        if main_items:
            equipment_parts.append(f"六格：{'、'.join(main_items)}")
        neutral = str(anchor.get("neutral") or "").strip()
        if (
            neutral
            and _normalise_text(neutral) not in {"empty", "unknown"}
            and not neutral.startswith("未知(empty)")
        ):
            equipment_parts.append(f"中立：{neutral}")
        if anchor.get("scepter"):
            equipment_parts.append("A杖")
        if anchor.get("shard"):
            equipment_parts.append("魔晶")
        if not equipment_parts:
            continue
        summary = f"{hero}｜{'｜'.join(equipment_parts)}"
        if all(key in anchor for key in ("kills", "deaths", "assists")):
            summary += (
                f" K/D/A {anchor['kills']}/{anchor['deaths']}/{anchor['assists']}"
                f" KDA {anchor.get('kda')}"
            )
        game_lines.append(summary)

    if not gift_totals and not diamond_events and not high_events and not online and not game_lines:
        return ""

    lines = ["", "——— 直播数据 ———"]
    if gift_totals:
        ordered = sorted(gift_totals.values(), key=lambda item: -int(item["total_cents"]))
        gift_text = " ".join(
            f"{item['name']}×{item['count']}({format_yuan(int(item['total_cents']))}元)"
            for item in ordered
        )
        total_cents = sum(int(item["total_cents"]) for item in ordered)
        lines.append(f"🎁 {gift_text} | 礼物价值合计 {format_yuan(total_cents)}元")
    elif diamond_events or high_events or online or game_lines:
        lines.append("🎁 无高价值礼物(≥100元)")
    if diamond_events:
        diamond_parts = []
        for action, label in (("open", "开通"), ("renew", "续费")):
            selected = [item for item in diamond_events if item.get("action") == action]
            if selected:
                months = sum(max(1, int(item.get("months") or 1)) for item in selected)
                diamond_parts.append(f"{label}{len(selected)}次/{months}个月")
        if diamond_parts:
            lines.append(f"💎 钻粉 {' '.join(diamond_parts)}")
    if high_events:
        high_total_cents = sum(
            int(item.get("price_cents") or round(float(item.get("amount") or 0) * 100))
            for item in high_events
        )
        lines.append(
            f"💬 高能弹幕 ×{len(high_events)} | "
            f"{format_yuan(high_total_cents)}元"
        )
    if online:
        values = [int(item.get("value") or 0) for item in online]
        minimum, maximum = min(values), max(values)
        lines.append(f"👥 在线 {minimum}" if minimum == maximum else f"👥 在线 {minimum}~{maximum}")
    if game_lines:
        if len(game_lines) == 1:
            lines.append(f"🎮 {game_lines[0]}")
        else:
            lines.extend(
                f"🎮 第{index}局：{summary}"
                for index, summary in enumerate(game_lines, start=1)
            )
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
