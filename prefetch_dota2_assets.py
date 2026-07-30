#!/usr/bin/env python3
"""Prefetch and validate the complete Valve Dota 2 hero/item image catalogue."""

from __future__ import annotations

import concurrent.futures
import json
from datetime import datetime, timezone
from pathlib import Path

from dota2_heroes import download_dota2_hero_image, load_official_dota2_heroes
from dota2_items import _all_dota2_items, download_dota2_item_icon


ROOT = Path("/data/cache/dota2")
HERO_DIR = ROOT / "heroes"
ITEM_DIR = ROOT / "items"
REPORT = ROOT / "prefetch-report.json"


def fetch(label, rows, downloader, directory):
    ok, errors = [], []

    def one(row):
        try:
            path = downloader(row, directory)
            return {"name": row.chinese_name, "slug": row.icon_slug, "path": str(path)}, None
        except Exception as exc:
            return None, {"name": row.chinese_name, "slug": row.icon_slug, "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for success, error in pool.map(one, rows):
            if success:
                ok.append(success)
            if error:
                errors.append(error)
    return {"kind": label, "total": len(rows), "downloaded": len(ok), "errors": errors}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    heroes = list(load_official_dota2_heroes())
    items = list(_all_dota2_items())
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "heroes": fetch("heroes", heroes, download_dota2_hero_image, HERO_DIR),
        "items": fetch("items", items, download_dota2_item_icon, ITEM_DIR),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
