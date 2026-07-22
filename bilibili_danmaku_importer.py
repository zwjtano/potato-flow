"""Import timestamped comments into a published Bilibili video's native danmaku pool."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from danmaku_pipeline import DanmakuComment, select_summary_comments


@dataclass(frozen=True)
class ImportResult:
    cid: int
    requested: int
    imported: int
    skipped: int


class BilibiliDanmakuImporter:
    def __init__(self, credential: Any, *, timeout: float = 20.0):
        import requests

        self.requests = requests
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/135.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        })
        cookies = credential.get_cookies()
        for name, value in cookies.items():
            if value:
                self.session.cookies.set(str(name), str(value), domain=".bilibili.com", path="/")
        self.csrf = str(getattr(credential, "bili_jct", "") or "")
        self.timeout = max(5.0, float(timeout))
        if not self.csrf or not cookies.get("SESSDATA"):
            raise ValueError("Bilibili Cookie 缺少 SESSDATA 或 bili_jct，无法导入弹幕")

    def wait_for_cid(self, bvid: str, *, wait_seconds: int = 300, interval: float = 8.0) -> int:
        deadline = time.monotonic() + max(0, int(wait_seconds))
        last_error = ""
        while True:
            try:
                response = self.session.get(
                    "https://api.bilibili.com/x/player/pagelist",
                    params={"bvid": bvid, "jsonp": "jsonp"},
                    timeout=self.timeout,
                )
                payload = response.json()
                pages = payload.get("data") if isinstance(payload, dict) else None
                if response.ok and payload.get("code") == 0 and pages:
                    cid = int(pages[0].get("cid") or 0)
                    if cid > 0:
                        return cid
                last_error = str(payload)[:500]
            except Exception as exc:
                last_error = str(exc)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等待 Bilibili CID 超时: {last_error}")
            time.sleep(max(1.0, float(interval)))

    def _post_one(self, bvid: str, cid: int, comment: DanmakuComment) -> tuple[bool, str]:
        mode = comment.mode if comment.mode in {1, 4, 5} else 1
        data = {
            "type": 1,
            "oid": cid,
            "msg": comment.text[:100],
            "bvid": bvid,
            "progress": max(0, int(comment.time * 1000)),
            "color": int(comment.color),
            "fontsize": 25,
            "pool": 0,
            "mode": mode,
            "rnd": int(time.time()),
            "plat": 1,
            "csrf": self.csrf,
            "csrf_token": self.csrf,
        }
        response = self.session.post(
            "https://api.bilibili.com/x/v2/dm/post",
            data=data,
            headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
            timeout=self.timeout,
        )
        payload = response.json()
        if response.ok and isinstance(payload, dict) and payload.get("code") == 0:
            return True, ""
        return False, str(payload)[:500]

    def import_comments(
        self,
        bvid: str,
        comments: list[DanmakuComment],
        *,
        max_comments: int = 0,
        interval_seconds: float = 0.6,
        cid_wait_seconds: int = 300,
    ) -> ImportResult:
        cid = self.wait_for_cid(bvid, wait_seconds=cid_wait_seconds)
        # max_comments <= 0 means lossless import: preserve every valid XML
        # comment, including repeated reactions. A positive value keeps the
        # previous deduplicated/time-distributed sampling behavior.
        selected = list(comments) if int(max_comments) <= 0 else select_summary_comments(comments, max_comments)
        imported = 0
        failures: list[str] = []
        for index, comment in enumerate(selected):
            ok = False
            detail = ""
            for attempt in range(3):
                try:
                    ok, detail = self._post_one(bvid, cid, comment)
                except Exception as exc:
                    detail = str(exc)
                if ok:
                    break
                time.sleep(1.5 * (attempt + 1))
            if ok:
                imported += 1
            else:
                failures.append(f"{comment.time:.1f}s:{detail}")
            if index + 1 < len(selected):
                time.sleep(max(0.2, float(interval_seconds)))

        # A small number of filtered/rejected comments is expected. A mostly
        # failed import should remain retryable instead of looking successful.
        if selected and imported < max(1, int(len(selected) * 0.8)):
            raise RuntimeError(
                f"Bilibili 弹幕导入成功率过低: {imported}/{len(selected)}; "
                + "; ".join(failures[:3])
            )
        return ImportResult(
            cid=cid,
            requested=len(selected),
            imported=imported,
            skipped=max(0, len(comments) - len(selected)),
        )
