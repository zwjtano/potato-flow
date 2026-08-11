"""Deterministic TI 2026 identities, format rules, and editorial safeguards."""

from __future__ import annotations

import re
from typing import Any, Iterable


TI2026_RULES: dict[str, Any] = {
    "event": "The International 2026",
    "aliases": ["TI2026", "TI 2026", "TI15", "TI 15", "国际邀请赛", "上海TI"],
    "location": "上海",
    "dates": {"start": "2026-08-13", "end": "2026-08-23"},
    "group_stage": {
        "dates": {"start": "2026-08-13", "end": "2026-08-16"},
        "format": "sixteen_team_swiss_style",
        "series_format": "bo3",
        "direct_playoff_places": [1, 2, 3],
        "elimination_round_places": list(range(4, 14)),
        "eliminated_places": [14, 15, 16],
    },
    "elimination_round": {
        "series_format": "bo3",
        "playoff_places_awarded": 5,
    },
    "main_event": {
        "dates": {"start": "2026-08-20", "end": "2026-08-23"},
        "format": "double_elimination",
        "default_series_format": "bo3",
        "grand_final_series_format": "bo5",
    },
}


TI2026_TEAMS: tuple[dict[str, Any], ...] = (
    {"name": "Aurora Gaming", "aliases": ["Aurora", "极光"], "players": ["Nightfall", "Mikoto", "Ws", "Mira", "kaori"]},
    {"name": "BoomBoys", "aliases": ["BoomBoys", "BetBoom Team", "BetBoom", "BB Team", "BB"], "players": ["Kiritych~", "gpk~", "MieRo`", "Save-", "Kataomi"]},
    {"name": "Team Falcons", "aliases": ["Falcons", "猎鹰", "石油队"], "players": ["skiter", "Malr1ne", "ATF", "Cr1t-", "Sneyking"]},
    {"name": "Team Liquid", "aliases": ["Liquid", "TL", "液体"], "players": ["m1CKe", "Nisha", "Ace", "Boxi", "tOfu"]},
    {"name": "1win Team", "aliases": ["1win", "1w Team", "1w", "Tundra", "Tundra Esports"], "players": ["Pure", "bzm", "33", "Ari", "Whitemon"]},
    {"name": "Xtreme Gaming", "aliases": ["Xtreme Gaming", "Xtreme", "XG"], "players": ["Ame", "NothingToSay", "Xxs", "fy", "xNova"]},
    {"name": "Team Yandex", "aliases": ["Yandex", "Yandex Team", "杨德克斯"], "players": ["watson", "CHIRA_JUNIOR", "DM", "Saksa", "Malady"]},
    {"name": "Team Spirit", "aliases": ["Spirit", "TS", "雪碧"], "players": ["Yatoro", "Larl", "Collapse", "not me", "rue"]},
    {"name": "TEAM VISION", "aliases": ["TEAM VISION", "Team Vision", "VISION", "PARIVISION", "PVISION", "PV"], "players": ["Satanic", "No[o]ne-", "Noticed", "9Class", "Dukalis"]},
    {"name": "Nigma Galaxy", "aliases": ["Nigma", "NGX", "尼格玛"], "players": ["SumaiL", "lorenof", "Davai", "OmaR", "GH"]},
    {"name": "HULIGANI", "aliases": ["HULIGANI", "Huligani"], "players": ["ssnovv1", "Mirage`", "Corrupted", "sayuw", "RESPECT"]},
    {"name": "Team Resilience", "aliases": ["Resilience", "TR", "韧性队"], "players": ["YSR-04E", "Erika", "poyoyo", "Echozz", "niu", "planet", "zzq"]},
    {"name": "Vici Gaming", "aliases": ["Vici Gaming", "Vici", "VG", "维基"], "players": ["shiro", "Xm", "Bach", "Faith_bian", "XinQ", "y`"]},
    {"name": "OG", "aliases": ["OG"], "players": ["Natsumi", "Yopaj-", "Raven", "TIMS", "skem"]},
    {"name": "LGD Gaming", "aliases": ["LGD Gaming", "LGD", "老干爹"], "players": ["Yuma", "Topson", "Wisper", "Thiolicor", "KJ"]},
    {"name": "GamerLegion", "aliases": ["GamerLegion", "GL"], "players": ["Ghost", "RCY", "Fayde", "Bignum", "Speeed"]},
)

TI2026_PLAYER_ALIASES: dict[str, tuple[str, ...]] = {
    "Topson": ("普森", "汤普森", "森哥", "上帝之子", "托普森", "托皇"),
}

TI2026_ROSTER_CHANGES = [
    {
        "team": "LGD Gaming",
        "removed_player": "TaiLung",
        "replacement_player": "Topson",
        "reason": "TaiLung is ineligible for The International 2026",
    },
    ]


_TI_MARKERS = re.compile(
    r"(?:TI\s*(?:15|2026)|国际邀请赛|不朽盾|Aegis|瑞士轮|主赛事|败者组|胜者组)",
    re.IGNORECASE,
)
_SERIES_MARKERS = re.compile(r"(?:BO\s*[35]|第\s*[一二三四五1-5]\s*局|比分|赛点|BP|对阵)", re.IGNORECASE)
_GAME_NUMBER_PATTERNS = (
    re.compile(r"第\s*([一二三四五1-5])\s*局"),
    re.compile(r"(?:GAME|G)\s*([1-5])\b", re.IGNORECASE),
)
_CHINESE_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def _compact(value: Any) -> str:
    return re.sub(r"[\s_.`'~\-]+", "", str(value or "")).casefold()


def _mentions(text: str, name: str) -> bool:
    candidate = str(name or "").strip()
    if not candidate:
        return False
    if re.fullmatch(r"[A-Za-z0-9]{1,3}", candidate):
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])", text, re.I))
    return _compact(candidate) in _compact(text)


def normalize_ti2026_team(value: str) -> str:
    for team in TI2026_TEAMS:
        if any(_compact(value) == _compact(alias) for alias in (team["name"], *team["aliases"])):
            return str(team["name"])
    return str(value or "").strip()


def ti2026_team_for_player(value: str) -> str:
    key = _compact(value)
    if not key:
        return ""
    for team in TI2026_TEAMS:
        for player in team["players"]:
            if any(key == _compact(alias) for alias in (player, *TI2026_PLAYER_ALIASES.get(player, ()))):
                return str(team["name"])
    return ""


def _game_number(text: str) -> int | None:
    for pattern in _GAME_NUMBER_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1)
            return int(raw) if raw.isdigit() else _CHINESE_DIGITS.get(raw)
    return None


def infer_ti2026_series_markers(comments: Iterable[Any]) -> list[dict[str, Any]]:
    """Return conservative, non-result series boundaries from explicit XML text."""
    markers: list[dict[str, Any]] = []
    seen_games: set[int] = set()
    for comment in sorted(comments, key=lambda item: float(getattr(item, "time", 0) or 0)):
        text = str(getattr(comment, "text", "") or "").strip()
        number = _game_number(text)
        if number is None or number in seen_games:
            continue
        if not re.search(r"(?:第\s*[一二三四五1-5]\s*局|GAME|开局|BP|开始|来了)", text, re.I):
            continue
        seen_games.add(number)
        markers.append({
            "game_number": number,
            "start_seconds": max(0, int(float(getattr(comment, "time", 0) or 0))),
            "evidence": text[:160],
        })
    return markers


def build_ti2026_context(comments: Iterable[Any], base_description: str = "") -> dict[str, Any]:
    comment_list = list(comments)
    texts = [str(getattr(comment, "text", "") or "") for comment in comment_list]
    corpus = "\n".join([str(base_description or ""), *texts])
    mentioned_teams = [
        str(team["name"])
        for team in TI2026_TEAMS
        if any(_mentions(corpus, alias) for alias in (team["name"], *team["aliases"]))
]
    active = bool(
        _TI_MARKERS.search(corpus)
        or len(mentioned_teams) >= 2
        or (mentioned_teams and _SERIES_MARKERS.search(corpus))
    )
    if not active:
        return {"active": False}
    mentioned_players: list[dict[str, str]] = []
    for team in TI2026_TEAMS:
        if mentioned_teams and str(team["name"]) not in mentioned_teams:
            continue
        for player in team["players"]:
            aliases = (player, *TI2026_PLAYER_ALIASES.get(player, ()))
            if any(_mentions(corpus, alias) for alias in aliases):
                mentioned_players.append({"name": player, "team": str(team["name"])})
    phase = "grand_final" if re.search(r"(?:总决赛|GRAND\s*FINAL|决赛第五局)", corpus, re.I) else (
        "main_event" if re.search(r"(?:主赛事|胜者组|败者组|淘汰赛)", corpus, re.I) else "group_stage"
    )
    series_format = "bo5" if phase == "grand_final" else "bo3"
    return {
        "active": True,
        "event": TI2026_RULES["event"],
        "phase": phase,
        "series_format": series_format,
        "mentioned_teams": mentioned_teams,
        "mentioned_players": mentioned_players,
        "roster_changes": TI2026_ROSTER_CHANGES,
        "series_markers": infer_ti2026_series_markers(comment_list),
        "rules": TI2026_RULES,
        "editorial_policy": {
            "observer_is_not_player": True,
            "current_camera_hero_is_not_streamer_hero": True,
            "team_player_action_requires_same_window_evidence": True,
            "series_result_requires_verified_score_or_explicit_result": True,
        },
    }


_SENSITIVE_CLAIMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("match_point", ("赛点",)),
    ("advance", ("晋级", "出线", "挺进", "锁定主赛事", "直通主赛事")),
    ("eliminate", ("淘汰", "出局", "告别TI", "止步")),
    ("lower_bracket", ("掉入败者组", "跌入败者组", "落入败者组")),
    ("champion", ("夺冠", "冠军", "捧盾", "不朽盾")),
    ("sweep", ("横扫", "零封", "让一追二", "让二追三")),
)


def unsupported_ti2026_claim(
    candidate: str,
    verified_description: str,
    tournament_context: dict[str, Any] | None,
) -> str:
    """Return the unsupported strong tournament claim, or an empty string."""
    if not isinstance(tournament_context, dict) or not tournament_context.get("active"):
        return ""
    title = str(candidate or "")
    evidence = str(verified_description or "")
    for claim, terms in _SENSITIVE_CLAIMS:
        if not any(term.casefold() in title.casefold() for term in terms):
            continue
        if any(term.casefold() in evidence.casefold() for term in terms):
            continue
        if claim == "match_point":
            score = re.search(r"(?<!\d)([123])\s*(?:[-:比])\s*([0123])(?!\d)", evidence)
            target = 3 if tournament_context.get("series_format") == "bo5" else 2
            if score and max(int(score.group(1)), int(score.group(2))) == target - 1:
                continue
        if claim == "sweep" and re.search(r"(?<!\d)(?:2\s*[-:比]\s*0|3\s*[-:比]\s*0)(?!\d)", evidence):
            continue
        return claim
    return ""
