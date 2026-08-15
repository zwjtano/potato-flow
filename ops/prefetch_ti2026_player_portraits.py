#!/usr/bin/env python3
"""Warm the provenance-verified TI 2026 player portrait cache."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from dota2_players import download_ti_player_portrait  # noqa: E402
from ti2026_context import TI2026_TEAMS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("/data/cache/dota2/players"),
    )
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--player", action="append", default=[])
    args = parser.parse_args()

    requested = {str(name).casefold() for name in args.player}
    results: list[dict[str, object]] = []
    for team in TI2026_TEAMS:
        team_name = str(team["name"])
        for player_name in team["players"]:
            if requested and str(player_name).casefold() not in requested:
                continue
            try:
                portrait = download_ti_player_portrait(
                    str(player_name),
                    team_name,
                    args.cache_dir,
                    timeout=args.timeout,
                )
                results.append({"status": "cached", **asdict(portrait)})
                print(f"OK {team_name} / {player_name}: {portrait.image_name}")
            except Exception as exc:
                results.append({
                    "status": "unavailable",
                    "team_name": team_name,
                    "player_name": str(player_name),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                print(f"WARN {team_name} / {player_name}: {type(exc).__name__}: {exc}")
            if args.delay > 0:
                time.sleep(args.delay)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = args.cache_dir / "manifest.json"
    manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    cached = sum(row["status"] == "cached" for row in results)
    print(f"cached={cached} unavailable={len(results) - cached} manifest={manifest}")
    return 0 if results and cached == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
