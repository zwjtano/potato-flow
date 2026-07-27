from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import requests

from .models import NotificationMessage


CHANNEL_WECOM = "wecom"
CHANNEL_SERVERCHAN = "serverchan"
CHANNEL_MESSAGE_PUSHER = "message_pusher"
CHANNEL_TELEGRAM = "telegram"

ALL_CHANNELS = (
    CHANNEL_WECOM,
    CHANNEL_SERVERCHAN,
    CHANNEL_MESSAGE_PUSHER,
    CHANNEL_TELEGRAM,
)

CHANNEL_LABELS = {
    CHANNEL_WECOM: "企业微信",
    CHANNEL_SERVERCHAN: "Server酱",
    CHANNEL_MESSAGE_PUSHER: "message-pusher",
    CHANNEL_TELEGRAM: "Telegram",
}

CHANNEL_ENABLE_KEY_MAP = {
    CHANNEL_WECOM: "NOTIFY_WECOM_ENABLED",
    CHANNEL_SERVERCHAN: "NOTIFY_SERVERCHAN_ENABLED",
    CHANNEL_MESSAGE_PUSHER: "NOTIFY_MESSAGE_PUSHER_ENABLED",
    CHANNEL_TELEGRAM: "NOTIFY_TELEGRAM_ENABLED",
}

CHANNEL_REQUIRED_CONFIG_MAP = {
    CHANNEL_WECOM: ("NOTIFY_WECOM_WEBHOOK_URL",),
    CHANNEL_SERVERCHAN: ("NOTIFY_SERVERCHAN_SENDKEY",),
    CHANNEL_MESSAGE_PUSHER: (
        "NOTIFY_MESSAGE_PUSHER_SERVER",
        "NOTIFY_MESSAGE_PUSHER_USERNAME",
        "NOTIFY_MESSAGE_PUSHER_TOKEN",
    ),
    CHANNEL_TELEGRAM: (
        "NOTIFY_TELEGRAM_BOT_TOKEN",
        "NOTIFY_TELEGRAM_CHAT_ID",
    ),
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("true", "1", "on", "yes")


def iter_enabled_channel_ids(config: dict[str, Any] | None) -> list[str]:
    normalized = dict(config or {})
    result = []
    for channel_id in ALL_CHANNELS:
        if _as_bool(normalized.get(CHANNEL_ENABLE_KEY_MAP[channel_id], False)):
            result.append(channel_id)
    return result


def validate_channel_config_fields(channel_id: str, config: dict[str, Any] | None) -> list[str]:
    normalized = dict(config or {})
    missing = []
    for key in CHANNEL_REQUIRED_CONFIG_MAP.get(channel_id, ()):
        if not str(normalized.get(key) or "").strip():
            missing.append(key)
    return missing


class NotificationSendError(RuntimeError):
    pass


@dataclass
class Notifier:
    channel_id: str
    label: str

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        return validate_channel_config_fields(self.channel_id, config)

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        raise NotImplementedError

    @staticmethod
    def _raise_for_http_error(response) -> None:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NotificationSendError(str(exc)) from exc


class WeComNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(channel_id=CHANNEL_WECOM, label=CHANNEL_LABELS[CHANNEL_WECOM])

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        webhook = str(config.get("NOTIFY_WECOM_WEBHOOK_URL") or "").strip()
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": message.markdown,
            },
        }
        try:
            response = requests.post(webhook, json=payload, timeout=10)
            self._raise_for_http_error(response)
            data = response.json()
            if int(data.get("errcode", -1)) == 0:
                return
            raise NotificationSendError(str(data.get("errmsg") or "企业微信 Markdown 推送失败"))
        except NotificationSendError:
            raise
        except Exception as original_exc:
            fallback_text = f"{message.title}\n{message.summary}"
            fallback = {
                "msgtype": "text",
                "text": {
                    "content": fallback_text,
                },
            }
            try:
                response = requests.post(webhook, json=fallback, timeout=10)
                self._raise_for_http_error(response)
                data = response.json()
                if int(data.get("errcode", -1)) != 0:
                    raise NotificationSendError(str(data.get("errmsg") or "企业微信推送失败")) from original_exc
            except NotificationSendError:
                raise
            except Exception as fallback_exc:
                raise NotificationSendError(f"企业微信推送失败(含回退): {fallback_exc}") from original_exc


class ServerChanNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(channel_id=CHANNEL_SERVERCHAN, label=CHANNEL_LABELS[CHANNEL_SERVERCHAN])

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        sendkey = str(config.get("NOTIFY_SERVERCHAN_SENDKEY") or "").strip()
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        response = requests.post(
            url,
            data={
                "title": message.title,
                "desp": message.markdown,
            },
            timeout=10,
        )
        self._raise_for_http_error(response)
        data = response.json()
        if int(data.get("code", -1)) != 0:
            raise NotificationSendError(str(data.get("message") or "Server酱推送失败"))


class MessagePusherNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(channel_id=CHANNEL_MESSAGE_PUSHER, label=CHANNEL_LABELS[CHANNEL_MESSAGE_PUSHER])

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        server = str(config.get("NOTIFY_MESSAGE_PUSHER_SERVER") or "").strip().rstrip("/")
        username = str(config.get("NOTIFY_MESSAGE_PUSHER_USERNAME") or "").strip()
        token = str(config.get("NOTIFY_MESSAGE_PUSHER_TOKEN") or "").strip()
        channel = str(config.get("NOTIFY_MESSAGE_PUSHER_CHANNEL") or "").strip()
        payload = {
            "title": message.title,
            "description": message.summary,
            "content": message.markdown,
            "token": token,
        }
        if channel:
            payload["channel"] = channel
        response = requests.post(
            f"{server}/push/{username}",
            json=payload,
            timeout=10,
        )
        self._raise_for_http_error(response)
        data = response.json()
        if not bool(data.get("success")):
            raise NotificationSendError(str(data.get("message") or "message-pusher 推送失败"))


def _telegram_plain_text(message: NotificationMessage) -> str:
    markdown = str(message.markdown or "").strip()
    markdown = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", markdown)
    lines = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith(">"):
            line = line[1:].lstrip()
        line = line.replace("**", "").replace("`", "")
        lines.append(line)
    body = "\n".join(lines).strip()
    text = f"{message.title}\n\n{body}" if body else f"{message.title}\n\n{message.summary}"
    return text[:4096]


class TelegramNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(channel_id=CHANNEL_TELEGRAM, label=CHANNEL_LABELS[CHANNEL_TELEGRAM])

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        token = str(config.get("NOTIFY_TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = str(config.get("NOTIFY_TELEGRAM_CHAT_ID") or "").strip()
        proxy_url = str(config.get("NOTIFY_TELEGRAM_PROXY_URL") or "").strip()
        if proxy_url and not re.match(r"^https?://", proxy_url, flags=re.IGNORECASE):
            raise NotificationSendError("Telegram 代理地址仅支持 http:// 或 https://")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        request_options: dict[str, Any] = {}
        if proxy_url:
            request_options["proxies"] = {
                "http": proxy_url,
                "https": proxy_url,
            }
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": _telegram_plain_text(message),
                    "disable_web_page_preview": True,
                },
                timeout=10,
                **request_options,
            )
        except requests.RequestException as exc:
            # Telegram URL includes the bot token, so never persist the raw exception text.
            raise NotificationSendError(
                f"Telegram 请求失败：{type(exc).__name__}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            if not 200 <= int(response.status_code) < 300:
                raise NotificationSendError(f"Telegram HTTP {response.status_code}") from exc
            raise NotificationSendError("Telegram 返回了无效响应") from exc
        description = str(data.get("description") or "").replace(token, "[redacted]")
        if not 200 <= int(response.status_code) < 300:
            raise NotificationSendError(description or f"Telegram HTTP {response.status_code}")
        if not bool(data.get("ok")):
            raise NotificationSendError(description or "Telegram 推送失败")


def build_notifier_registry() -> dict[str, Notifier]:
    return {
        CHANNEL_WECOM: WeComNotifier(),
        CHANNEL_SERVERCHAN: ServerChanNotifier(),
        CHANNEL_MESSAGE_PUSHER: MessagePusherNotifier(),
        CHANNEL_TELEGRAM: TelegramNotifier(),
    }
