"""Valve Dota 2 live league snapshots used by TI recording analysis."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any


STEAM_LIVE_LEAGUE_API = (
    "https://api.steampowered.com/IDOTA2Match_570/GetLiveLeagueGames/v1/"
)


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_live_game(game: dict[str, Any]) -> dict[str, Any]:
    """Keep only the stable fields needed by title and cover generation."""
    scoreboard = game.get("scoreboard") if isinstance(game.get("scoreboard"), dict) else {}
    radiant = scoreboard.get("radiant") if isinstance(scoreboard.get("radiant"), dict) else {}
    dire = scoreboard.get("dire") if isinstance(scoreboard.get("dire"), dict) else {}

    def team_snapshot(team: dict[str, Any]) -> dict[str, Any]:
        players = team.get("players") if isinstance(team.get("players"), list) else []
        return {
            "kills": _integer(team.get("score")),
            "picks": [_integer(row.get("hero_id")) for row in team.get("picks", []) if isinstance(row, dict)],
            "players": [
                {
                    "account_id": row.get("account_id"),
                    "hero_id": _integer(row.get("hero_id")),
                    "kills": _integer(row.get("kills")),
                    "deaths": _integer(row.get("death" if "death" in row else "deaths")),
                    "assists": _integer(row.get("assists")),
                    "net_worth": _integer(row.get("net_worth")),
                }
                for row in players
                if isinstance(row, dict)
            ],
        }

    return {
        "match_id": _integer(game.get("match_id")),
        "league_id": _integer(game.get("league_id")),
        "game_number": _integer(game.get("game_number")),
        "stream_delay_seconds": _integer(game.get("stream_delay_s")),
        "radiant_series_wins": _integer(game.get("radiant_series_wins")),
        "dire_series_wins": _integer(game.get("dire_series_wins")),
        "duration_seconds": float(scoreboard.get("duration") or 0),
        "radiant_team": str((game.get("radiant_team") or {}).get("team_name") or ""),
        "dire_team": str((game.get("dire_team") or {}).get("team_name") or ""),
        "radiant": team_snapshot(radiant),
        "dire": team_snapshot(dire),
    }


def fetch_live_league_games(
    *, api_key: str | None = None, config: dict[str, Any] | None = None, timeout: float = 20
) -> list[dict[str, Any]]:
    """Fetch live DotaTV league games without logging or returning the API key."""
    configured_key = (config or {}).get("STEAM_WEB_API_KEY")
    if configured_key is None and config is None:
        try:
            from modules.config_manager import load_config

            configured_key = load_config().get("STEAM_WEB_API_KEY")
        except (ImportError, OSError, ValueError, TypeError):
            configured_key = None
    key = str(
        api_key or configured_key or os.environ.get("STEAM_WEB_API_KEY") or ""
    ).strip()
    if not key:
        raise ValueError("未配置 STEAM_WEB_API_KEY")
    url = f"{STEAM_LIVE_LEAGUE_API}?{urllib.parse.urlencode({'key': key})}"
    request = urllib.request.Request(url, headers={"User-Agent": "PotatoFlow/1.6.78"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Steam 实时比赛接口请求失败：{type(exc).__name__}") from None
    games = ((payload.get("result") or {}).get("games") or []) if isinstance(payload, dict) else []
    return [normalize_live_game(game) for game in games if isinstance(game, dict)]
