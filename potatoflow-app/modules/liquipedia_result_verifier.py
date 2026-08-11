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
HERO_CONSTANTS_URL = (
    "https://raw.githubusercontent.com/odota/dotaconstants/master/build/heroes.json"
)
DEFAULT_USER_AGENT = "PotatoFlow/1.6.78 (+https://github.com/zwjtano/potato-flow)"
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _fetch_json(url: str, *, timeout: float, user_agent: str) -> dict[str, Any]:
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
    if not isinstance(payload, dict):
        raise ValueError("上游接口没有返回 JSON object")
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
