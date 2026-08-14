"""Deterministic TI 2026 identities, format rules, and editorial safeguards."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
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

CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
UTC_TIMEZONE = timezone.utc


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
    {"name": "Team Resilience", "aliases": ["Resilience", "TR", "韧性队"], "players": ["Erika", "Echozz", "niu", "planet", "zzq"]},
    {"name": "Vici Gaming", "aliases": ["Vici Gaming", "Vici", "VG", "维基"], "players": ["shiro", "Xm", "Faith_bian", "XinQ", "y`"]},
    {"name": "OG", "aliases": ["OG"], "players": ["Natsumi", "Yopaj-", "Raven", "TIMS", "skem"]},
    {"name": "LGD Gaming", "aliases": ["LGD Gaming", "LGD", "老干爹"], "players": ["Yuma", "Topson", "Wisper", "Thiolicor", "KJ"]},
    {"name": "GamerLegion", "aliases": ["GamerLegion", "GL"], "players": ["Ghost", "RCY", "Fayde", "Bignum", "Speeed"]},
)

TI2026_TEAM_MEDIA: dict[str, dict[str, Any]] = {
    "Aurora Gaming": {"team_id": 9467224, "slug": "aurora-gaming", "logo_source": "https://cdn.steamusercontent.com/ugc/13052583756685508/22B0338D7E09FB2F021E5DB5BBEFFD170D5E5E1A/"},
    "BoomBoys": {"team_id": 8255888, "slug": "boomboys", "logo_source": "https://cdn.steamusercontent.com/ugc/9995426432403529725/51E13136D4CCC8C7D8062861541A1D13B8ED87E0/"},
    "Team Falcons": {"team_id": 9247354, "slug": "team-falcons", "logo_source": "https://cdn.steamusercontent.com/ugc/2314350571781870059/2B5C9FE9BA0A2DC303A13261444532AA08352843/"},
    "Team Liquid": {"team_id": 2163, "slug": "team-liquid", "logo_source": "https://steamcdn-a.akamaihd.net/apps/dota2/images/team_logos/2163.png"},
    "1win Team": {"team_id": 10182357, "slug": "1win", "logo_source": "https://cdn.steamusercontent.com/ugc/10678669599334676082/E48827F4A163D4D02F817EA3C32166D5F1D5FC98/"},
    "Xtreme Gaming": {"team_id": 8261500, "slug": "xtreme-gaming", "logo_source": "https://cdn.steamusercontent.com/ugc/2402194226059610590/E3CF4B6C4B2CFB974A9B415141E4A37317AD4D80/"},
    "Team Yandex": {"team_id": 9823272, "slug": "team-yandex", "logo_source": "https://cdn.steamusercontent.com/ugc/12970505637628494427/B04C3358F4E815ADFC2F8B1B8BE3AB0CE75C8881/"},
    "Team Spirit": {"team_id": 7119388, "slug": "team-spirit", "logo_source": "https://cdn.steamusercontent.com/ugc/1839179120711951766/CD7E0885CB527334205CC7885E9C101B7BC17702/"},
    "TEAM VISION": {"team_id": 9572001, "slug": "team-vision", "logo_source": "https://cdn.steamusercontent.com/ugc/10380389074903512947/5D074799695A862D17D4205285315FE20399B28D/"},
    "Nigma Galaxy": {"team_id": 7554697, "slug": "nigma-galaxy", "logo_source": "https://cdn.steamusercontent.com/ugc/1827894588975105240/421C0D8318D71D5DD31FD08A7933AB622AE26590/"},
    "HULIGANI": {"team_id": 10149530, "slug": "huligani", "logo_source": "https://cdn.steamusercontent.com/ugc/14844266645370842778/47230D9640A722EAF06548C2EEB813ED4296AE3F/"},
    "Team Resilience": {"team_id": 5017210, "slug": "team-resilience", "logo_source": "https://cdn.steamusercontent.com/ugc/14326265454983833183/734A1D8A0938380A48221CDAE1AACB0C5C0AB585/"},
    "Vici Gaming": {"team_id": 726228, "slug": "vici-gaming", "logo_source": "https://steamcdn-a.akamaihd.net/apps/dota2/images/team_logos/726228.png"},
    "OG": {"team_id": 2586976, "slug": "og", "logo_source": "https://steamcdn-a.akamaihd.net/apps/dota2/images/team_logos/2586976.png"},
    "LGD Gaming": {"team_id": 10150538, "slug": "lgd-gaming", "logo_source": "https://cdn.steamusercontent.com/ugc/10055782735581672481/2B2BCEA9CC05286D7164E4548A2EB64CDBC77F31/"},
    "GamerLegion": {"team_id": 9964962, "slug": "gamerlegion", "logo_source": "https://cdn.steamusercontent.com/ugc/13245379764580870318/1048428BEFAC87EC1C64E15706A4758A173B5BFB/"},
}

for _team in TI2026_TEAMS:
    _media = TI2026_TEAM_MEDIA[str(_team["name"])]
    _team["team_id"] = _media["team_id"]
    _team["logo_path"] = f"static/img/ti2026/teams/{_media['slug']}.png"
    _team["logo_source"] = _media["logo_source"]
    _team["player_portraits"] = {
        player: {"status": "awaiting_official_asset", "path": "", "source": ""}
        for player in _team["players"]
    }

TI2026_PLAYER_ALIASES: dict[str, tuple[str, ...]] = {
    player: ()
    for team in TI2026_TEAMS
    for player in team["players"]
}
TI2026_PLAYER_ALIASES.update({
    "Ame": ("萧瑟", "哥哥", "Ame哥"),
    "NothingToSay": ("NTS", "莫言", "责任神"),
    "fy": ("fy神", "烟火神"),
    "tOfu": ("豆腐",),
    "Pure": ("普洱",),
    "Xxs": ("小学生",),
    "No[o]ne-": ("Noone", "No[o]ne", "NoOne"),
    "XinQ": ("行星神", "心情"),
    "Topson": ("普森", "汤普森", "森哥", "上帝之子", "托普森", "托皇"),
    "SumaiL": ("苏美尔", "苏皇", "跳跳"),
    "Erika": ("YSR-04E", "poyoyo"),
    "Faith_bian": ("Bach", "张睿达"),
})

TI2026_ROSTER_CHANGES = [
    {
        "team": "LGD Gaming",
        "removed_player": "TaiLung",
        "replacement_player": "Topson",
        "reason": "TaiLung is ineligible for The International 2026",
    },
    ]


_TI_IDENTITY_MARKERS = re.compile(
    r"(?:TI\s*(?:15|2026)|The\s+International\s+2026|国际邀请赛|上海TI|不朽盾|Aegis)",
    re.IGNORECASE,
)
_GENERIC_TI_MARKER = re.compile(r"(?<![A-Za-z0-9])TI(?![A-Za-z0-9])", re.IGNORECASE)
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


def _inside_ti2026_window(event_date: date | str | None) -> bool:
    raw = event_date or date.today()
    if isinstance(raw, date):
        value = raw.isoformat()
    else:
        value = str(raw or "")[:10]
    return TI2026_RULES["dates"]["start"] <= value <= TI2026_RULES["dates"]["end"]


def ti2026_event_date_from_filename(value: str) -> str:
    """Return an ISO recording date from PotatoFlow's dated filenames."""
    recording_time = ti2026_recording_datetime_from_filename(value)
    return recording_time[:10] if recording_time else ""


def ti2026_recording_datetime_from_filename(value: str) -> str:
    """Return a timezone-aware China recording timestamp from a dated filename."""
    match = re.search(
        r"(?<!\d)(20\d{2})[-_](0[1-9]|1[0-2])[-_]([0-2]\d|3[01])"
        r"(?:[-_](?:([01]\d|2[0-3]))[-_:]([0-5]\d)(?:[-_:]([0-5]\d))?)?(?!\d)",
        str(value or ""),
    )
    if not match:
        return ""
    try:
        year, month, day, hour, minute, second = match.groups()
        parsed = datetime(
            int(year), int(month), int(day),
            int(hour or 0), int(minute or 0), int(second or 0),
            tzinfo=CHINA_TIMEZONE,
        )
        return parsed.isoformat()
    except ValueError:
        return ""


def liquipedia_utc_window_for_china_date(value: date | str) -> dict[str, str]:
    """Map one China calendar day to the exact UTC interval used by Liquipedia."""
    china_date = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    start_china = datetime.combine(china_date, time.min, tzinfo=CHINA_TIMEZONE)
    end_china = start_china + timedelta(days=1)
    return {
        "start_utc": start_china.astimezone(UTC_TIMEZONE).isoformat().replace("+00:00", "Z"),
        "end_utc_exclusive": end_china.astimezone(UTC_TIMEZONE).isoformat().replace("+00:00", "Z"),
    }


def liquipedia_timestamp_to_china(value: Any) -> str:
    """Convert a Liquipedia UTC timestamp (ISO text or Unix seconds) to UTC+8."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = datetime.fromtimestamp(float(value), tz=UTC_TIMEZONE)
    else:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return ""
        # Liquipedia timestamps without an offset are documented/treated as UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC_TIMEZONE)
    return parsed.astimezone(CHINA_TIMEZONE).isoformat()


def _aware_china_datetime(value: Any) -> datetime | None:
    """Parse Unix/ISO timestamps and normalize them to China time."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw_number = float(value)
        if raw_number > 10_000_000_000:  # Accept millisecond timestamps too.
            raw_number /= 1000
        return datetime.fromtimestamp(raw_number, tz=UTC_TIMEZONE).astimezone(CHINA_TIMEZONE)
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TIMEZONE)
    return parsed.astimezone(CHINA_TIMEZONE)


def recording_match_end_cutoff(
    recording_start_china: Any,
    recording_duration_seconds: float,
    match_end_timestamp: Any,
) -> dict[str, Any]:
    """Return the exact data cutoff when a match ends inside one recording."""
    recording_start = _aware_china_datetime(recording_start_china)
    match_end = _aware_china_datetime(match_end_timestamp)
    try:
        duration = float(recording_duration_seconds)
    except (TypeError, ValueError):
        duration = -1
    if recording_start is None or match_end is None or duration < 0:
        return {
            "contains_match_end": False,
            "cutoff_seconds": None,
            "reason": "invalid_timestamp_or_duration",
        }
    recording_end = recording_start + timedelta(seconds=duration)
    contains = recording_start <= match_end <= recording_end
    return {
        "contains_match_end": contains,
        "cutoff_seconds": round((match_end - recording_start).total_seconds(), 3) if contains else None,
        "recording_start_china": recording_start.isoformat(),
        "recording_end_china": recording_end.isoformat(),
        "match_end_china": match_end.isoformat(),
        "reason": "match_ended_inside_recording" if contains else "match_end_outside_recording",
    }


def comments_through_match_end(
    comments: Iterable[Any], cutoff_seconds: float | None
) -> list[Any]:
    """Keep only comments at or before the verified match-end boundary."""
    if cutoff_seconds is None:
        return []
    boundary = max(0.0, float(cutoff_seconds))
    return [
        comment
        for comment in comments
        if float(getattr(comment, "time", 0) or 0) <= boundary
    ]


def build_ti2026_context(
    comments: Iterable[Any],
    base_description: str = "",
    event_date: date | str | None = None,
) -> dict[str, Any]:
    comment_list = list(comments)
    texts = [str(getattr(comment, "text", "") or "") for comment in comment_list]
    corpus = "\n".join([str(base_description or ""), *texts])
    mentioned_teams = [
        str(team["name"])
        for team in TI2026_TEAMS
        if any(_mentions(corpus, alias) for alias in (team["name"], *team["aliases"]))
]
    explicit_ti_identity = bool(_TI_IDENTITY_MARKERS.search(corpus))
    inside_event_window = _inside_ti2026_window(event_date)
    # Outside the event window, a normal tournament featuring the same teams is
    # not TI. During TI, require both opponents unless the content names TI itself.
    active = explicit_ti_identity or (
        inside_event_window
        and (len(mentioned_teams) >= 2 or bool(_GENERIC_TI_MARKER.search(corpus)))
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
    event_date_value = (
        event_date.isoformat() if isinstance(event_date, date) else str(event_date or "")[:10]
    )
    group_dates = TI2026_RULES["group_stage"]["dates"]
    main_dates = TI2026_RULES["main_event"]["dates"]
    if group_dates["start"] <= event_date_value <= group_dates["end"]:
        phase = "group_stage"
    elif re.search(r"(?:总决赛|GRAND\s*FINAL|决赛第五局)", corpus, re.I):
        phase = "grand_final"
    elif main_dates["start"] <= event_date_value <= main_dates["end"]:
        phase = "main_event"
    else:
        phase = "main_event" if re.search(
            r"(?:主赛事|胜者组|败者组|淘汰赛)", corpus, re.I
        ) else "group_stage"
    series_format = "bo5" if phase == "grand_final" else "bo3"
    return {
        "active": True,
        "mode": "ti_competition",
        "explicit_event_identity": explicit_ti_identity,
        "inside_event_window": inside_event_window,
        "recording_timezone": "Asia/Shanghai",
        "recording_date_china": event_date_value,
        "liquipedia_query_window": (
            liquipedia_utc_window_for_china_date(event_date_value)
            if event_date_value
            else {}
        ),
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
