#!/usr/bin/env python3
"""Generate a Markdown catalogue of Valve Dota 2 item names and icons."""

from __future__ import annotations

import concurrent.futures
import html
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from dota2_items import DOTA2_ITEMS


DATA_URL = "https://www.dota2.com/datafeed/itemlist?language=schinese"
ICON_BASE_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/"
    "dota_react/items"
)
OUTPUT = Path(__file__).resolve().parent / "docs" / "DOTA2装备中英文图片对照.md"


def markdown_cell(value: object) -> str:
    """Keep official labels inside one Markdown table cell."""
    normalized = " ".join(str(value or "").split()).replace("|", "&#124;")
    return html.escape(normalized, quote=False)


def request(url: str, method: str = "GET"):
    return urllib.request.urlopen(
        urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"},
        ),
        timeout=20,
    )


def load_items() -> list[dict[str, object]]:
    with request(DATA_URL) as response:
        payload = json.load(response)
    rows = payload.get("result", {}).get("data", {}).get("itemabilities", [])
    return [
        row
        for row in rows
        if str(row.get("name") or "").startswith("item_")
        and not str(row.get("name") or "").startswith("item_recipe_")
        and str(row.get("name_loc") or "").strip()
        and str(row.get("name_english_loc") or "").strip()
    ]


def icon_for(row: dict[str, object]) -> str | None:
    slug = str(row["name"]).removeprefix("item_")
    url = f"{ICON_BASE_URL}/{slug}.png"
    try:
        with request(url, "HEAD") as response:
            content_type = str(response.headers.get("Content-Type") or "")
            return url if response.status == 200 and content_type.startswith("image/") else None
    except (OSError, urllib.error.URLError):
        return None


def main() -> None:
    rows = load_items()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        icons = list(pool.map(icon_for, rows))

    resolved = [(row, icon) for row, icon in zip(rows, icons) if icon]
    resolved.sort(key=lambda pair: (str(pair[0].get("name_english_loc") or "").casefold(), int(pair[0]["id"])))
    aliases_by_slug = {
        item.icon_slug: tuple(
            alias
            for alias in item.aliases
            if alias not in {item.chinese_name, item.english_name}
        )
        for item in DOTA2_ITEMS
    }

    lines = [
        "# DOTA 2 装备中英文名与官方图片对照",
        "",
        f"> 生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"> 数据来源：[Dota 2 官方简体中文物品数据]({DATA_URL})",
        "> 图片来源：Valve Dota 2 官方 CDN",
        f"> 共收录 {len(resolved)} 件具有中英文名称且官方图片可访问的物品；图纸类条目已排除。",
        "",
        "| 图片 | 中文名 | 常用中文俗称 | English | 物品 ID | 内部名 |",
        "|---|---|---|---|---:|---|",
    ]
    for row, icon in resolved:
        chinese = markdown_cell(row["name_loc"])
        english = markdown_cell(row["name_english_loc"])
        internal = str(row["name"])
        slug = internal.removeprefix("item_")
        aliases = markdown_cell("、".join(aliases_by_slug.get(slug, ())))
        image_alt = html.escape(" ".join(str(row["name_english_loc"]).split()).replace("|", "-"), quote=True)
        lines.append(
            f'| <img src="{icon}" alt="{image_alt}" width="88"> '
            f"| {chinese} | {aliases} | {english} | {row['id']} | `{internal}` |"
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"generated={OUTPUT} items={len(resolved)} skipped_without_icon={len(rows) - len(resolved)}")


if __name__ == "__main__":
    main()
