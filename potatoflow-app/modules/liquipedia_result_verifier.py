"""Low-frequency post-recording verification for Liquipedia Dota 2 matches."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


LIQUIPEDIA_API = "https://liquipedia.net/dota2/api.php"
OPENDOTA_MATCH_API = "https://api.opendota.com/api/matches/{match_id}"
OPENDOTA_PRO_MATCHES_API = "https://api.opendota.com/api/proMatches"
OPENDOTA_LIVE_API = "https://api.opendota.com/api/live"
HERO_CONSTANTS_URL = (
    "https://raw.githubusercontent.com/odota/dotaconstants/master/build/heroes.json"
)
DEFAULT_USER_AGENT = "PotatoFlow/1.6.78 (+https://github.com/zwjtano/potato-flow)"
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
TI2026_GROUP_STAGE_PAGE = "The International/2026/Group Stage"
TI2026_MAIN_EVENT_PAGE = "The International/2026/Main Event"
TI2026_MAIN_EVENT_START_CHINA = datetime(
    2026, 8, 20, tzinfo=CHINA_TIMEZONE
)
TI2026_LEAGUE_ID = 19719


def _fetch_json(url: str, *, timeout: float, user_agent: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if str(response.headers.get("Content-Encoding") or "").casefold() == "gzip":
            import gzip

            raw = gzip.decompress(raw)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, (dict, list)):
        raise ValueError("上游接口没有返回 JSON object 或 array")
    return payload


def liquipedia_page_title(value: str) -> str:
    raw = urllib.parse.unquote(str(value or "").strip())
    if raw.startswith(("http://", "https://")):
        raw = urllib.parse.urlparse(raw).path.rsplit("/", 1)[-1]
    return raw.replace("_", " ").strip()


def parse_liquipedia_match_wikitext(wikitext: str) -> dict[str, Any]:
    """Extract stable MatchPage fields without scraping rendered HTML."""
    text = str(wikitext or "")
    opponents = [
        match.group(1).strip()
        for match in re.finditer(
            r"\|opponent[12]\s*=\s*\{\{TeamOpponent\|([^}|]+)", text, re.I
        )
    ]
    date_match = re.search(r"\|date\s*=\s*([^\n]+)", text, re.I)
    maps: list[dict[str, Any]] = []
    for map_match in re.finditer(r"\|map(\d+)\s*=\s*\{\{ApiMap\|([^}]+)\}\}", text, re.I):
        arguments = map_match.group(2)
        match_id = re.search(r"(?:^|\|)matchid\s*=\s*(\d+)", arguments, re.I)
        if not match_id:
            continue
        maps.append({
            "game_number": int(map_match.group(1)),
            "match_id": int(match_id.group(1)),
            "reversed": bool(re.search(r"(?:^|\|)reversed\s*=\s*true", arguments, re.I)),
        })
    return {
        "opponents": opponents,
        "scheduled_time_source": date_match.group(1).strip() if date_match else "",
        "maps": maps,
    }


def parse_liquipedia_tournament_matches(wikitext: str) -> list[dict[str, Any]]:
    """Extract embedded Match blocks from a tournament schedule page."""
    text = str(wikitext or "")
    starts = list(re.finditer(
        r"(?m)^\|(?P<match_key>(?:R\d+)?M\d+)\s*=\s*\{\{Match\s*$",
        text,
        re.I,
    ))
    matches: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        block = text[start.start() : starts[index + 1].start() if index + 1 < len(starts) else len(text)]
        opponents = [
            match.group(1).strip()
            for match in re.finditer(
                r"(?m)^\|opponent[12]\s*=\s*\{\{TeamOpponent\|([^}|]+)", block, re.I
            )
        ]
        date_match = re.search(r"(?m)^\|date\s*=\s*([^\n]+)", block, re.I)
        if len(opponents) != 2 or not date_match:
            continue
        maps: list[dict[str, Any]] = []
        for number in range(1, 6):
            match_id = re.search(rf"(?m)^\|matchid{number}\s*=\s*(\d+)", block, re.I)
            map_body = re.search(
                rf"(?ms)^\|map{number}\s*=\s*\{{\{{Map\s*(.*?)(?=^\|map\d+\s*=|^\}}\}}\s*$)",
                block,
                re.I,
            )
            if not match_id and not map_body:
                continue
            body = map_body.group(1) if map_body else ""
            winner = re.search(r"(?:^|\|)winner\s*=\s*([12])(?:\||\s*$)", body, re.I)
            length = re.search(r"(?:^|\|)length\s*=\s*([^|\n]*)", body, re.I)
            maps.append({
                "game_number": number,
                "match_id": int(match_id.group(1)) if match_id else 0,
                "winner_side": int(winner.group(1)) if winner else 0,
                "length": str(length.group(1) or "").strip() if length else "",
                "team1_heroes": re.findall(r"(?:^|\|)t1h\d+\s*=\s*([^|\n]+)", body, re.I),
                "team2_heroes": re.findall(r"(?:^|\|)t2h\d+\s*=\s*([^|\n]+)", body, re.I),
            })
        match_key = str(start.group("match_key") or "").upper()
        round_label = {
            "R1M1": "Upper Bracket Quarterfinals",
            "R1M2": "Upper Bracket Quarterfinals",
            "R1M3": "Upper Bracket Quarterfinals",
            "R1M4": "Upper Bracket Quarterfinals",
            "R1M5": "Lower Bracket Round 1",
            "R1M6": "Lower Bracket Round 1",
            "R2M1": "Upper Bracket Semifinals",
            "R2M2": "Upper Bracket Semifinals",
            "R2M3": "Lower Bracket Quarterfinals",
            "R2M4": "Lower Bracket Quarterfinals",
            "R3M1": "Lower Bracket Semifinal",
            "R4M1": "Upper Bracket Final",
            "R4M2": "Lower Bracket Final",
            "R5M1": "Grand Final",
        }.get(match_key, "")
        matches.append({
            "match_key": match_key,
            "round_label": round_label,
            "opponents": opponents,
            "scheduled_time_source": date_match.group(1).strip(),
            "maps": maps,
        })
    return matches


def _parse_liquipedia_china_schedule(value: str) -> datetime | None:
    cleaned = re.sub(r"\s*\{\{Abbr/(?:CST|UTC\+8)\}\}\s*", "", str(value or ""), flags=re.I)
    try:
        return datetime.strptime(cleaned.strip(), "%B %d, %Y - %H:%M").replace(tzinfo=CHINA_TIMEZONE)
    except ValueError:
        return None


def _team_in_text(team: str, text: str, aliases: dict[str, list[str]]) -> bool:
    compact_text = _compact_team(text)
    for raw_name in (team, *(aliases.get(team) or [])):
        raw_candidate = str(raw_name or "").strip()
        candidate = _compact_team(raw_candidate)
        if not candidate:
            continue
        # Short handles such as y`, DM, 33 and fy must match a standalone
        # token. Substring matching them against a full hour of chat makes
        # almost every simultaneous TI series look relevant.
        if re.fullmatch(r"[a-z0-9]{1,3}", candidate):
            if re.search(
                rf"(?<![A-Za-z0-9]){re.escape(raw_candidate)}(?![A-Za-z0-9])",
                text,
                re.I,
            ):
                return True
            continue
        if candidate in compact_text:
            return True
    return False


def ti2026_liquipedia_schedule_page(recording_start_china: str) -> str:
    """Select the schedule page that owns the recording's tournament stage."""
    recording_start = datetime.fromisoformat(
        str(recording_start_china).replace("Z", "+00:00")
    )
    if recording_start.tzinfo is None:
        recording_start = recording_start.replace(tzinfo=CHINA_TIMEZONE)
    if recording_start.astimezone(CHINA_TIMEZONE) >= TI2026_MAIN_EVENT_START_CHINA:
        return TI2026_MAIN_EVENT_PAGE
    # Liquipedia keeps the August 16 elimination round on the Group Stage page.
    return TI2026_GROUP_STAGE_PAGE


def liquipedia_page_url(page: str) -> str:
    return "https://liquipedia.net/dota2/" + str(page).replace(" ", "_")


def discover_liquipedia_recording_match(
    *, recording_start_china: str, recording_duration_seconds: float, evidence_text: str,
    team_aliases: dict[str, list[str]] | None = None, timeout: float = 20,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """Find the unique TI series supported by both recording time and local text."""
    aliases = team_aliases or {}
    schedule_page = ti2026_liquipedia_schedule_page(recording_start_china)
    schedule_url = liquipedia_page_url(schedule_page)
    query = urllib.parse.urlencode({
        "action": "parse", "page": schedule_page,
        "prop": "wikitext", "format": "json", "formatversion": 2,
    })
    payload = _fetch_json(f"{LIQUIPEDIA_API}?{query}", timeout=timeout, user_agent=user_agent)
    parsed = payload.get("parse") or {}
    schedule = parse_liquipedia_tournament_matches(str(parsed.get("wikitext") or ""))
    recording_start = datetime.fromisoformat(str(recording_start_china).replace("Z", "+00:00"))
    if recording_start.tzinfo is None:
        recording_start = recording_start.replace(tzinfo=CHINA_TIMEZONE)
    recording_start = recording_start.astimezone(CHINA_TIMEZONE)
    recording_end = recording_start + timedelta(seconds=max(0.0, float(recording_duration_seconds)))
    time_candidates: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for series in schedule:
        scheduled = _parse_liquipedia_china_schedule(series["scheduled_time_source"])
        if scheduled is None or scheduled > recording_end or scheduled + timedelta(hours=4) < recording_start:
            continue
        row = dict(series)
        row["scheduled_time_china"] = scheduled.isoformat()
        time_candidates.append(row)
        mentions = [team for team in series["opponents"] if _team_in_text(team, evidence_text, aliases)]
        if not mentions:
            continue
        row["mentioned_opponents"] = mentions
        candidates.append(row)
    exact = [row for row in candidates if len(row["mentioned_opponents"]) == 2]
    selected_pool = exact or candidates
    matched_by = ["event", "recording_time", "local_team_evidence"]
    # Sequential elimination/main-event series can be identified safely from
    # the official schedule alone when exactly one series overlaps the entire
    # recording. This keeps fresh live segments usable while OpenDota and the
    # local title/evidence lag behind, without guessing during parallel rounds.
    # Do not prefer a newly scheduled series merely because its start time is
    # close to an hourly recording boundary. TI series overlap in wall-clock
    # time, and a streamer can still be covering the previous BO3 after the
    # next official series starts. Let the caller retry with the full local
    # title/description so team evidence decides between overlapping series.
    if len(selected_pool) != 1 and len(time_candidates) == 1:
        selected_pool = time_candidates
        matched_by = ["event", "recording_time"]
    if len(selected_pool) != 1:
        # Liquipedia's Swiss-stage schedule is updated in-place and can stop
        # exposing an already-played round in the current page wikitext. Use
        # OpenDota's parsed pro-match feed as the post-recording fallback. It
        # still requires the TI league, recording-time overlap and local team
        # evidence, so an unrelated Dota match cannot be selected silently.
        fallback = discover_opendota_recording_match(
            recording_start_china=recording_start_china,
            recording_duration_seconds=recording_duration_seconds,
            evidence_text=evidence_text,
            team_aliases=aliases,
            timeout=timeout,
            user_agent=user_agent,
        )
        if fallback.get("status") == "confirmed":
            return fallback
        live_fallback = discover_opendota_live_recording_match(
            recording_start_china=recording_start_china,
            recording_duration_seconds=recording_duration_seconds,
            evidence_text=evidence_text,
            team_aliases=aliases,
            timeout=timeout,
            user_agent=user_agent,
        )
        if live_fallback.get("status") == "live_confirmed":
            return live_fallback
        return {
            "status": "not_found" if not selected_pool else "ambiguous",
            "source": "liquipedia_mediawiki+opendota",
            "candidate_count": len(selected_pool),
            "reason": str(fallback.get("reason") or "没有唯一的时间与弹幕队伍交集"),
        }
    selected = selected_pool[0]
    match_payloads: dict[int, dict[str, Any]] = {}
    match_errors: list[dict[str, Any]] = []
    for game in selected["maps"]:
        if game["match_id"]:
            try:
                match_payloads[game["match_id"]] = _fetch_json(
                    OPENDOTA_MATCH_API.format(match_id=game["match_id"]),
                    timeout=timeout, user_agent=user_agent,
                )
            except Exception as exc:
                match_errors.append({
                    "game_number": game["game_number"],
                    "match_id": game["match_id"],
                    "error": type(exc).__name__,
                })
    try:
        hero_constants = _fetch_json(HERO_CONSTANTS_URL, timeout=timeout, user_agent=user_agent)
    except Exception:
        hero_constants = {}
    completed_match_ids = {
        match_id for match_id, match_payload in match_payloads.items()
        if datetime.fromtimestamp(
            float(match_payload.get("start_time") or 0)
            + float(match_payload.get("duration") or 0),
            tz=timezone.utc,
        ).astimezone(CHINA_TIMEZONE) <= recording_end
    }
    page = {"opponents": selected["opponents"], "maps": [
        {"game_number": game["game_number"], "match_id": game["match_id"], "reversed": False}
        for game in selected["maps"] if game["match_id"] in completed_match_ids
    ]}
    result = build_verified_match_result(page, match_payloads, hero_constants)
    # A newly-started map may exist on Liquipedia before OpenDota has parsed it.
    # Enrich that unique schedule match from the live feed so current heroes
    # and the live score are available without inventing KDA or an MVP.
    if not result.get("games") and selected_pool:
        live_result = discover_opendota_live_recording_match(
            recording_start_china=recording_start_china,
            recording_duration_seconds=recording_duration_seconds,
            evidence_text=evidence_text,
            team_aliases=aliases,
            timeout=timeout,
            user_agent=user_agent,
        )
        expected_pair = {_compact_team(team) for team in selected["opponents"]}
        actual_pair = {_compact_team(team) for team in live_result.get("opponents", [])}
        if live_result.get("status") == "live_confirmed" and actual_pair == expected_pair:
            live_result.update({
                "source": "liquipedia_mediawiki+opendota_live",
                "source_url": schedule_url,
                "scheduled_time_china": selected["scheduled_time_china"],
                "liquipedia_maps": selected["maps"],
                "match_data_errors": match_errors,
            })
            return live_result
        result.update({
            "status": "matched_pending_data",
            "opponents": selected["opponents"],
            "series_score": {team: 0 for team in selected["opponents"]},
            "games": [],
        })
    result.update({
        "source": "liquipedia_mediawiki+opendota",
        "source_url": schedule_url,
        "scheduled_time_china": selected["scheduled_time_china"],
        "matched_by": matched_by,
        "match_data_errors": match_errors,
        "liquipedia_maps": selected["maps"],
    })
    return result


def discover_opendota_live_recording_match(
    *, recording_start_china: str, recording_duration_seconds: float,
    evidence_text: str, team_aliases: dict[str, list[str]] | None = None,
    timeout: float = 20, user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """Resolve an in-progress TI game from OpenDota's public live feed.

    The live feed exposes the current teams, score and all ten player/hero
    identities before a parsed match exists in ``proMatches``.  It does not
    expose reliable per-player KDA, so callers must not derive an MVP from this
    snapshot.
    """
    aliases = team_aliases or {}
    payload = _fetch_json(OPENDOTA_LIVE_API, timeout=timeout, user_agent=user_agent)
    rows = payload if isinstance(payload, list) else []
    recording_start = datetime.fromisoformat(str(recording_start_china).replace("Z", "+00:00"))
    if recording_start.tzinfo is None:
        recording_start = recording_start.replace(tzinfo=CHINA_TIMEZONE)
    recording_start = recording_start.astimezone(CHINA_TIMEZONE)
    recording_end = recording_start + timedelta(seconds=max(0.0, float(recording_duration_seconds)))

    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or int(row.get("league_id") or 0) != TI2026_LEAGUE_ID:
            continue
        radiant = str(row.get("team_name_radiant") or "").strip()
        dire = str(row.get("team_name_dire") or "").strip()
        if not radiant or not dire or not any(
            _team_in_text(team, evidence_text, aliases) for team in (radiant, dire)
        ):
            continue
        activated = datetime.fromtimestamp(
            float(row.get("activate_time") or 0), tz=timezone.utc
        ).astimezone(CHINA_TIMEZONE)
        last_update = datetime.fromtimestamp(
            float(row.get("last_update_time") or row.get("activate_time") or 0),
            tz=timezone.utc,
        ).astimezone(CHINA_TIMEZONE)
        deactivated_raw = float(row.get("deactivate_time") or 0)
        deactivated = (
            datetime.fromtimestamp(deactivated_raw, tz=timezone.utc).astimezone(CHINA_TIMEZONE)
            if deactivated_raw > 0 else None
        )
        if activated > recording_end:
            continue
        if (deactivated or last_update) < recording_start - timedelta(minutes=15):
            continue
        key = tuple(sorted((_compact_team(radiant), _compact_team(dire))))
        previous = candidates.get(key)
        if previous is None or float(row.get("last_update_time") or 0) > float(previous.get("last_update_time") or 0):
            candidates[key] = row
    if len(candidates) != 1:
        return {
            "status": "not_found" if not candidates else "ambiguous",
            "source": "opendota_live",
            "candidate_count": len(candidates),
            "reason": "OpenDota 实时数据没有唯一的 TI 时间与本地队伍证据交集",
        }

    row = next(iter(candidates.values()))
    radiant = str(row.get("team_name_radiant") or "").strip()
    dire = str(row.get("team_name_dire") or "").strip()
    try:
        hero_constants = _fetch_json(HERO_CONSTANTS_URL, timeout=timeout, user_agent=user_agent)
    except Exception:
        hero_constants = {}
    players: list[dict[str, Any]] = []
    for player in row.get("players") or []:
        if not isinstance(player, dict):
            continue
        team = radiant if int(player.get("team") or 0) == 0 else dire
        players.append({
            "name": str(player.get("name") or "").strip(),
            "account_id": player.get("account_id"),
            "team": team,
            **_hero_lookup(hero_constants, player.get("hero_id")),
        })
    activated = float(row.get("activate_time") or 0)
    last_update = float(row.get("last_update_time") or activated)
    game = {
        "game_number": 0,
        "match_id": int(row.get("match_id") or 0),
        "start_time_china": _china_timestamp(activated),
        "last_update_time_china": _china_timestamp(last_update),
        "radiant": radiant,
        "dire": dire,
        "radiant_score": int(row.get("radiant_score") or 0),
        "dire_score": int(row.get("dire_score") or 0),
        "radiant_lead": int(row.get("radiant_lead") or 0),
        "game_time_seconds": int(row.get("game_time") or 0),
        "players": players,
        "performance_candidates": [],
        "teams_verified": True,
        "live": True,
    }
    return {
        "status": "live_confirmed",
        "source": "opendota_live",
        "source_url": OPENDOTA_LIVE_API,
        "candidate_count": 1,
        "opponents": [radiant, dire],
        "series_score": {radiant: 0, dire: 0},
        "games": [game],
        "matched_by": ["event", "recording_time", "live_team_evidence"],
        "live_snapshot_time_china": game["last_update_time_china"],
        "performance_label": "实时数据无可靠 KDA，不能生成 MVP 候选",
    }


def discover_opendota_recording_match(
    *, recording_start_china: str, recording_duration_seconds: float,
    evidence_text: str, team_aliases: dict[str, list[str]] | None = None,
    timeout: float = 20, user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """Resolve a TI series from recent parsed pro matches when the schedule moved."""
    aliases = team_aliases or {}
    payload = _fetch_json(OPENDOTA_PRO_MATCHES_API, timeout=timeout, user_agent=user_agent)
    rows = payload if isinstance(payload, list) else []
    recording_start = datetime.fromisoformat(str(recording_start_china).replace("Z", "+00:00"))
    if recording_start.tzinfo is None:
        recording_start = recording_start.replace(tzinfo=CHINA_TIMEZONE)
    recording_start = recording_start.astimezone(CHINA_TIMEZONE)
    recording_end = recording_start + timedelta(seconds=max(0.0, float(recording_duration_seconds)))

    overlapping_pairs: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict) or "international 2026" not in str(row.get("league_name") or "").casefold():
            continue
        radiant = str(row.get("radiant_name") or "").strip()
        dire = str(row.get("dire_name") or "").strip()
        if not radiant or not dire or not any(
            _team_in_text(team, evidence_text, aliases) for team in (radiant, dire)
        ):
            continue
        started = datetime.fromtimestamp(float(row.get("start_time") or 0), tz=timezone.utc).astimezone(CHINA_TIMEZONE)
        ended = started + timedelta(seconds=float(row.get("duration") or 0))
        if started <= recording_end and ended >= recording_start - timedelta(minutes=15):
            key = tuple(sorted((_compact_team(radiant), _compact_team(dire))))
            overlapping_pairs[key] = (radiant, dire)
    if len(overlapping_pairs) != 1:
        return {
            "status": "not_found" if not overlapping_pairs else "ambiguous",
            "source": "opendota_pro_matches",
            "candidate_count": len(overlapping_pairs),
            "reason": "OpenDota 没有唯一的 TI 时间与本地队伍证据交集",
        }

    pair_key, opponents = next(iter(overlapping_pairs.items()))
    series_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        actual = tuple(sorted((
            _compact_team(row.get("radiant_name")), _compact_team(row.get("dire_name")),
        )))
        if actual != pair_key:
            continue
        started = datetime.fromtimestamp(float(row.get("start_time") or 0), tz=timezone.utc).astimezone(CHINA_TIMEZONE)
        ended = started + timedelta(seconds=float(row.get("duration") or 0))
        if recording_start - timedelta(hours=4) <= started and ended <= recording_end:
            series_rows.append(row)
    series_rows.sort(key=lambda row: float(row.get("start_time") or 0))
    match_payloads = {
        int(row["match_id"]): _fetch_json(
            OPENDOTA_MATCH_API.format(match_id=int(row["match_id"])),
            timeout=timeout, user_agent=user_agent,
        )
        for row in series_rows if int(row.get("match_id") or 0)
    }
    try:
        hero_constants = _fetch_json(HERO_CONSTANTS_URL, timeout=timeout, user_agent=user_agent)
    except Exception:
        hero_constants = {}
    page = {
        "opponents": list(opponents),
        "maps": [
            {"game_number": index, "match_id": int(row["match_id"]), "reversed": False}
            for index, row in enumerate(series_rows, start=1)
            if int(row.get("match_id") or 0) in match_payloads
        ],
    }
    result = build_verified_match_result(page, match_payloads, hero_constants)
    result.update({
        "source": "opendota_pro_matches",
        "matched_by": ["event", "recording_time", "local_team_evidence"],
        "source_url": OPENDOTA_PRO_MATCHES_API,
        "candidate_count": 1,
    })
    return result


def _compact_team(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _china_timestamp(unix_seconds: Any) -> str:
    return datetime.fromtimestamp(float(unix_seconds), tz=timezone.utc).astimezone(
        CHINA_TIMEZONE
    ).isoformat()


def _hero_lookup(constants: dict[str, Any], hero_id: Any) -> dict[str, str]:
    row = constants.get(str(hero_id), {})
    if not isinstance(row, dict):
        row = {}
    return {
        "hero_id": int(hero_id or 0),
        "hero_name": str(row.get("localized_name") or ""),
        "hero_internal_name": str(row.get("name") or ""),
    }


def build_verified_match_result(
    page: dict[str, Any],
    match_payloads: dict[int, dict[str, Any]],
    hero_constants: dict[str, Any],
) -> dict[str, Any]:
    opponents = list(page.get("opponents") or [])
    expected = {_compact_team(name) for name in opponents}
    series_wins = {name: 0 for name in opponents}
    games: list[dict[str, Any]] = []
    for map_row in page.get("maps") or []:
        match_id = int(map_row["match_id"])
        payload = match_payloads.get(match_id) or {}
        radiant = str(payload.get("radiant_name") or "").strip()
        dire = str(payload.get("dire_name") or "").strip()
        actual = {_compact_team(radiant), _compact_team(dire)}
        teams_verified = bool(expected and actual == expected)
        radiant_win = bool(payload.get("radiant_win"))
        winner = radiant if radiant_win else dire
        if teams_verified:
            for opponent in opponents:
                if _compact_team(opponent) == _compact_team(winner):
                    series_wins[opponent] += 1
                    winner = opponent
                    break
        players: list[dict[str, Any]] = []
        for player in payload.get("players") or []:
            if not isinstance(player, dict):
                continue
            slot = int(player.get("player_slot") or 0)
            player_team = radiant if slot < 128 else dire
            players.append({
                "name": str(player.get("name") or player.get("personaname") or "").strip(),
                "account_id": player.get("account_id"),
                "team": player_team,
                **_hero_lookup(hero_constants, player.get("hero_id")),
                "kills": int(player.get("kills") or 0),
                "deaths": int(player.get("deaths") or 0),
                "assists": int(player.get("assists") or 0),
            })
        performance_candidates = sorted(
            players,
            key=lambda row: (
                row["kills"] + row["assists"] - row["deaths"],
                row["kills"],
                -row["deaths"],
            ),
            reverse=True,
        )[:3]
        start_time = float(payload.get("start_time") or 0)
        duration_seconds = float(payload.get("duration") or 0)
        games.append({
            "game_number": int(map_row["game_number"]),
            "match_id": match_id,
            "start_time_china": _china_timestamp(start_time),
            "duration_seconds": duration_seconds,
            "end_time_unix": start_time + duration_seconds,
            "end_time_china": _china_timestamp(start_time + duration_seconds),
            "radiant": radiant,
            "dire": dire,
            "radiant_score": int(payload.get("radiant_score") or 0),
            "dire_score": int(payload.get("dire_score") or 0),
            "winner": winner,
            "teams_verified": teams_verified,
            "players": players,
            "performance_candidates": performance_candidates,
        })
    return {
        "status": "confirmed" if games and all(game["teams_verified"] for game in games) else "conflict",
        "opponents": opponents,
        "series_score": series_wins,
        "games": games,
        "performance_label": "KDA候选，不能直接视为MVP",
    }


def verify_liquipedia_match_page(
    page_url_or_title: str,
    *,
    timeout: float = 20,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    title = liquipedia_page_title(page_url_or_title)
    query = urllib.parse.urlencode({
        "action": "parse",
        "page": title,
        "prop": "wikitext|displaytitle|categories",
        "format": "json",
        "formatversion": 2,
    })
    api_payload = _fetch_json(
        f"{LIQUIPEDIA_API}?{query}", timeout=timeout, user_agent=user_agent
    )
    parsed = api_payload.get("parse") or {}
    wikitext = str((parsed.get("wikitext") if isinstance(parsed, dict) else "") or "")
    page = parse_liquipedia_match_wikitext(wikitext)
    page.update({
        "title": str(parsed.get("title") or title),
        "display_title": str(parsed.get("displaytitle") or ""),
        "source_url": page_url_or_title,
    })
    match_payloads = {
        int(row["match_id"]): _fetch_json(
            OPENDOTA_MATCH_API.format(match_id=row["match_id"]),
            timeout=timeout,
            user_agent=user_agent,
        )
        for row in page["maps"]
    }
    hero_constants = _fetch_json(
        HERO_CONSTANTS_URL, timeout=timeout, user_agent=user_agent
    )
    result = build_verified_match_result(page, match_payloads, hero_constants)
    result["page"] = page
    return result
