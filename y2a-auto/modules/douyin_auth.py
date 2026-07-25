#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Douyin QR login backed by a short-lived headless Chromium session."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger("douyin_auth")


def load_douyin_cookie(cookie_file: str | Path) -> str:
    """Load a browser-exported JSON cookie file as a HTTP Cookie header."""
    path = Path(cookie_file)
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
        payload = json.loads(content)
    except (OSError, json.JSONDecodeError):
        try:
            return path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            return ""
    items = payload.get("cookies", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return ""
    pairs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


class DouyinQrLoginSession:
    """Own a browser on one background thread so Flask request threads stay safe."""

    def __init__(self, cookie_file: str | Path, timeout_seconds: int = 180) -> None:
        self.cookie_file = Path(cookie_file)
        self.timeout_seconds = timeout_seconds
        self.created_at = time.time()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._status: dict[str, Any] = {
            "status": "loading",
            "message": "正在打开抖音登录页",
            "cookies_saved": False,
        }
        self._thread: threading.Thread | None = None

    def _set_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def generate(self) -> dict[str, Any]:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._browser_worker,
                name="douyin-qr-login",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=35):
            raise RuntimeError("抖音登录页打开超时，请检查服务器网络")
        snapshot = self.check_status()
        if snapshot.get("status") == "failed":
            raise RuntimeError(str(snapshot.get("message") or "抖音二维码生成失败"))
        return snapshot

    def check_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _save_cookies(self, cookies: list[dict[str, Any]]) -> bool:
        useful = [
            item for item in cookies
            if isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and str(item.get("value") or "").strip()
            and "douyin.com" in str(item.get("domain") or "")
        ]
        if not useful:
            return False
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cookie_file.with_suffix(self.cookie_file.suffix + ".tmp")
        temp.write_text(
            json.dumps(useful, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(self.cookie_file)
        return True

    def _browser_worker(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._set_status(
                status="failed",
                message="当前镜像缺少 Chromium 扫码组件，请更新 Docker 镜像",
            )
            self._ready.set()
            return

        browser = None
        try:
            with sync_playwright() as playwright:
                configured_executable = os.environ.get(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
                    "",
                ).strip()
                executable = (
                    configured_executable
                    or shutil.which("chromium")
                    or shutil.which("chromium-browser")
                    or shutil.which("google-chrome")
                    or ""
                )
                launch_options: dict[str, Any] = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                }
                if executable and Path(executable).is_file():
                    launch_options["executable_path"] = executable
                browser = playwright.chromium.launch(**launch_options)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="zh-CN",
                )
                page = context.new_page()
                page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                qr_selector = "#animate_qrcode_container img"
                try:
                    page.locator(qr_selector).wait_for(state="visible", timeout=12000)
                except Exception:
                    for selector in (
                        "text=登录",
                        "button:has-text('登录')",
                        "[data-e2e='login-button']",
                    ):
                        try:
                            page.locator(selector).first.click(timeout=2000)
                            break
                        except Exception:
                            continue
                    page.locator(qr_selector).wait_for(state="visible", timeout=12000)

                qr_bytes = page.locator(qr_selector).screenshot(type="png")
                self._set_status(
                    status="pending",
                    message="二维码已生成，请使用抖音 App 扫码并确认",
                    image_base64=base64.b64encode(qr_bytes).decode("ascii"),
                    mime_type="image/png",
                )
                self._ready.set()

                deadline = self.created_at + self.timeout_seconds
                while time.time() < deadline:
                    cookies = context.cookies()
                    cookie_map = {
                        str(item.get("name") or ""): str(item.get("value") or "")
                        for item in cookies
                    }
                    local_login = ""
                    try:
                        local_login = str(
                            page.evaluate(
                                "() => window.localStorage.getItem('HasUserLogin') || ''"
                            )
                        )
                    except Exception:
                        pass
                    if cookie_map.get("LOGIN_STATUS") == "1" or local_login == "1":
                        saved = self._save_cookies(cookies)
                        self._set_status(
                            status="done" if saved else "failed",
                            message=(
                                "抖音登录成功，Cookie 已保存"
                                if saved
                                else "已确认登录，但没有读取到有效 Cookie"
                            ),
                            cookies_saved=saved,
                        )
                        return
                    time.sleep(1)
                self._set_status(status="timeout", message="二维码已过期，请重新获取")
        except Exception as exc:
            logger.exception("抖音二维码登录失败")
            self._set_status(status="failed", message=f"抖音二维码登录失败：{exc}")
            self._ready.set()
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
