from __future__ import annotations

import logging
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .live_recorder_manager import RecorderConfigError, recordings_dir


logger = logging.getLogger("telegram_control")

CONTROL_CONFIG_KEYS = (
    "TELEGRAM_CONTROL_ENABLED",
    "TELEGRAM_CONTROL_ADMIN_USER_IDS",
    "NOTIFY_TELEGRAM_BOT_TOKEN",
    "NOTIFY_TELEGRAM_CHAT_ID",
    "NOTIFY_TELEGRAM_PROXY_URL",
)
CONFIRM_TTL_SECONDS = 120


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"true", "1", "on", "yes"}


def _split_ids(value: Any) -> set[str]:
    return {
        item
        for item in re.split(r"[\s,;，；]+", str(value or "").strip())
        if re.fullmatch(r"-?\d+", item)
    }


def _human_bytes(value: int | float) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


@dataclass
class PendingRoomDelete:
    room_id: str
    room_name: str
    user_id: str
    chat_id: str
    expires_at: float


class TelegramControlError(RuntimeError):
    pass


class TelegramControlService:
    """Telegram long-polling controller with explicit user and chat allowlists."""

    def __init__(
        self,
        manager: Any,
        config: dict[str, Any] | None = None,
        *,
        request_session: Any = requests,
    ) -> None:
        self.manager = manager
        self.config = dict(config or {})
        self.request_session = request_session
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._offset: int | None = None
        self._pending_deletes: dict[str, PendingRoomDelete] = {}
        self._pending_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return _as_bool(self.config.get("TELEGRAM_CONTROL_ENABLED"))

    @property
    def token(self) -> str:
        return str(self.config.get("NOTIFY_TELEGRAM_BOT_TOKEN") or "").strip()

    @property
    def target_chat_id(self) -> str:
        return str(self.config.get("NOTIFY_TELEGRAM_CHAT_ID") or "").strip()

    @property
    def admin_user_ids(self) -> set[str]:
        return _split_ids(self.config.get("TELEGRAM_CONTROL_ADMIN_USER_IDS"))

    @property
    def proxy_url(self) -> str:
        return str(self.config.get("NOTIFY_TELEGRAM_PROXY_URL") or "").strip()

    def validation_errors(self) -> list[str]:
        if not self.enabled:
            return []
        errors = []
        if not self.token:
            errors.append("Bot Token")
        if not self.target_chat_id:
            errors.append("Chat ID")
        if not self.admin_user_ids:
            errors.append("管理员 User ID")
        if self.proxy_url and not re.match(r"^https?://", self.proxy_url, re.IGNORECASE):
            errors.append("有效的 HTTP/HTTPS 代理地址")
        return errors

    def start(self) -> bool:
        if not self.enabled:
            return False
        errors = self.validation_errors()
        if errors:
            raise TelegramControlError("Telegram 控制配置不完整：" + "、".join(errors))
        if self._thread is not None and self._thread.is_alive():
            return True
        stop_event = threading.Event()
        self._stop_event = stop_event
        self._thread = threading.Thread(
            target=self._run,
            args=(stop_event,),
            name="potato-telegram-control",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=7)
        self._thread = None

    def reconfigure(self, config: dict[str, Any] | None) -> None:
        updated = dict(config or {})
        old_signature = tuple(str(self.config.get(key) or "") for key in CONTROL_CONFIG_KEYS)
        new_signature = tuple(str(updated.get(key) or "") for key in CONTROL_CONFIG_KEYS)
        self.config = updated
        if old_signature == new_signature:
            if self.enabled and (self._thread is None or not self._thread.is_alive()):
                self.start()
            return
        self.stop()
        self._offset = None
        with self._pending_lock:
            self._pending_deletes.clear()
        if self.enabled:
            self.start()

    def _request_options(self) -> dict[str, Any]:
        if not self.proxy_url:
            return {}
        return {
            "proxies": {
                "http": self.proxy_url,
                "https": self.proxy_url,
            }
        }

    def _api(self, method: str, payload: dict[str, Any], *, timeout: int = 15) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            response = self.request_session.post(
                url,
                json=payload,
                timeout=timeout,
                **self._request_options(),
            )
            data = response.json()
        except requests.RequestException as exc:
            raise TelegramControlError(
                f"Telegram 请求失败：{type(exc).__name__}"
            ) from exc
        except ValueError as exc:
            raise TelegramControlError("Telegram 返回了无效响应") from exc
        if not 200 <= int(response.status_code) < 300 or not bool(data.get("ok")):
            description = str(data.get("description") or f"HTTP {response.status_code}")
            raise TelegramControlError(description.replace(self.token, "[redacted]"))
        return data.get("result")

    def _send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": str(text)[:4096],
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._api("sendMessage", payload)

    def _discard_backlog(self) -> None:
        updates = self._api(
            "getUpdates",
            {
                "timeout": 0,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        if isinstance(updates, list) and updates:
            self._offset = max(int(item.get("update_id") or 0) for item in updates) + 1

    def _run(self, stop_event: threading.Event) -> None:
        try:
            self._discard_backlog()
            self._api(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "rooms", "description": "查看直播间与录制状态"},
                        {"command": "add", "description": "添加直播间链接"},
                        {"command": "start", "description": "开始检测/录制指定直播间"},
                        {"command": "stop", "description": "安全停止指定直播间"},
                        {"command": "delete", "description": "删除直播间（二次确认）"},
                        {"command": "tasks", "description": "查看最近录播任务"},
                        {"command": "status", "description": "查看服务与磁盘状态"},
                        {"command": "help", "description": "查看命令帮助"},
                    ]
                },
            )
            logger.info("Telegram 机器人控制已启动")
        except TelegramControlError as exc:
            logger.warning("Telegram 机器人控制初始化失败：%s", exc)

        while not stop_event.is_set():
            try:
                payload: dict[str, Any] = {
                    "timeout": 3,
                    "allowed_updates": ["message", "callback_query"],
                }
                if self._offset is not None:
                    payload["offset"] = self._offset
                updates = self._api("getUpdates", payload, timeout=6)
                for update in updates if isinstance(updates, list) else []:
                    self._offset = max(
                        int(update.get("update_id") or 0) + 1,
                        self._offset or 0,
                    )
                    self.process_update(update)
            except TelegramControlError as exc:
                if not stop_event.is_set():
                    logger.warning("Telegram 控制轮询失败：%s", exc)
                    stop_event.wait(5)
            except Exception:
                logger.exception("Telegram 控制处理出现未预期异常")
                stop_event.wait(3)

    def _authorized(self, chat_id: str, user_id: str) -> bool:
        return (
            bool(chat_id)
            and chat_id == self.target_chat_id
            and bool(user_id)
            and user_id in self.admin_user_ids
        )

    @staticmethod
    def _message_context(message: dict[str, Any]) -> tuple[str, str, str]:
        chat_id = str((message.get("chat") or {}).get("id") or "")
        user_id = str((message.get("from") or {}).get("id") or "")
        text = str(message.get("text") or "").strip()
        return chat_id, user_id, text

    def process_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            try:
                self._handle_callback(callback)
            except (RecorderConfigError, TelegramControlError, ValueError) as exc:
                callback_id = str(callback.get("id") or "")
                if callback_id:
                    try:
                        self._api(
                            "answerCallbackQuery",
                            {
                                "callback_query_id": callback_id,
                                "text": f"操作失败：{exc}"[:200],
                                "show_alert": True,
                            },
                        )
                    except TelegramControlError:
                        pass
            except Exception:
                logger.exception("Telegram 确认按钮处理失败")
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat_id, user_id, text = self._message_context(message)
        if chat_id != self.target_chat_id:
            logger.warning(
                "Telegram 控制拒绝非目标会话：chat_id=%s user_id=%s",
                chat_id,
                user_id,
            )
            return
        if user_id not in self.admin_user_ids:
            logger.warning("Telegram 控制拒绝未授权用户：user_id=%s", user_id)
            self._send_message(chat_id, f"无权操作。当前 User ID：{user_id}")
            return
        if not text.startswith("/"):
            return

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()
        logger.info(
            "Telegram 控制命令：user_id=%s command=%s",
            user_id,
            command,
        )
        try:
            self._dispatch_command(chat_id, user_id, command, argument)
        except (RecorderConfigError, TelegramControlError, ValueError) as exc:
            self._send_message(chat_id, f"操作失败：{exc}")
        except Exception:
            logger.exception(
                "Telegram 控制命令失败：user_id=%s command=%s",
                user_id,
                command,
            )
            self._send_message(chat_id, "操作失败，请查看 PotatoFlow 服务日志。")

    def _dispatch_command(
        self,
        chat_id: str,
        user_id: str,
        command: str,
        argument: str,
    ) -> None:
        if command in {"/help", "/start"} and not argument:
            self._send_message(chat_id, self._help_text())
        elif command == "/rooms":
            self._send_message(chat_id, self._rooms_text())
        elif command == "/add":
            self._add_room(chat_id, argument)
        elif command == "/delete":
            self._request_room_delete(chat_id, user_id, argument)
        elif command in {"/start", "/record"}:
            self._set_room_recording(chat_id, argument, True)
        elif command == "/stop":
            self._set_room_recording(chat_id, argument, False)
        elif command == "/tasks":
            self._send_message(chat_id, self._tasks_text())
        elif command in {"/status", "/disk"}:
            self._send_message(chat_id, self._status_text())
        else:
            self._send_message(chat_id, "无法识别该命令。\n\n" + self._help_text())

    @staticmethod
    def _help_text() -> str:
        return (
            "PotatoFlow 远程控制\n\n"
            "/rooms  查看直播间编号与状态\n"
            "/add <直播间链接>  添加直播间\n"
            "/start <编号>  开始检测/录制\n"
            "/stop <编号>  安全停止并收尾\n"
            "/delete <编号>  删除直播间（二次确认）\n"
            "/tasks  查看最近录播任务\n"
            "/status  查看服务与磁盘状态\n"
            "/help  查看本帮助"
        )

    def _rooms(self) -> list[dict[str, Any]]:
        return list(self.manager.rooms_with_status())

    def _rooms_text(self) -> str:
        rooms = self._rooms()
        if not rooms:
            return "目前没有直播间。\n使用 /add <直播间链接> 添加。"
        state_icons = {
            "recording": "🔴",
            "checking": "🔎",
            "offline": "⚪",
            "paused": "⏸",
            "stopped": "⏹",
            "unknown": "❔",
        }
        lines = [f"直播间（{len(rooms)}）"]
        for index, room in enumerate(rooms, 1):
            runtime = room.get("runtime") or {}
            state = str(runtime.get("state") or "unknown")
            mode = "仅录制" if room.get("record_only") else "录制并投稿"
            lines.append(
                f"{index}. {state_icons.get(state, '❔')} "
                f"{room.get('name') or '直播间'} · {runtime.get('label') or state} · {mode}"
            )
        lines.append("\n使用编号操作，例如：/stop 1")
        return "\n".join(lines)

    def _resolve_room(self, reference: str) -> dict[str, Any]:
        value = str(reference or "").strip()
        if not value:
            raise RecorderConfigError("请提供直播间编号，例如 /stop 1")
        rooms = self._rooms()
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(rooms):
                return rooms[index - 1]
        exact = next((room for room in rooms if str(room.get("id") or "") == value), None)
        if exact:
            return exact
        name_matches = [
            room for room in rooms
            if value.lower() in str(room.get("name") or "").lower()
        ]
        if len(name_matches) == 1:
            return name_matches[0]
        raise RecorderConfigError("没有找到该直播间，请先发送 /rooms 查看编号")

    def _add_room(self, chat_id: str, url: str) -> None:
        if not re.match(r"^https?://", str(url or "").strip(), re.IGNORECASE):
            raise RecorderConfigError("请发送完整直播间链接，例如 /add https://www.douyu.com/9999")
        room, reload_state = self.manager.add_room_from_url_and_reload(url.strip())
        state_labels = {
            "reloaded": "录制引擎已重载",
            "pending": "当前录制结束后自动重载",
            "saved": "配置已保存",
        }
        self._send_message(
            chat_id,
            f"✅ 已添加：{room.get('name') or '直播间'}\n"
            f"{room.get('url') or url}\n"
            f"{state_labels.get(reload_state, reload_state)}",
        )

    def _set_room_recording(self, chat_id: str, reference: str, enabled: bool) -> None:
        room = self._resolve_room(reference)
        updated = self.manager.set_room_recording(str(room.get("id") or ""), enabled)
        action = "开始检测，开播后自动录制" if enabled else "正在安全停止并收尾文件"
        self._send_message(chat_id, f"✅ {updated.get('name') or '直播间'}：{action}")

    def _request_room_delete(self, chat_id: str, user_id: str, reference: str) -> None:
        room = self._resolve_room(reference)
        runtime = room.get("runtime") or {}
        if runtime.get("recording"):
            raise RecorderConfigError("该直播间正在录制，请先 /stop 并等待安全收尾")
        token = secrets.token_urlsafe(8)
        pending = PendingRoomDelete(
            room_id=str(room.get("id") or ""),
            room_name=str(room.get("name") or "直播间"),
            user_id=user_id,
            chat_id=chat_id,
            expires_at=time.time() + CONFIRM_TTL_SECONDS,
        )
        with self._pending_lock:
            self._pending_deletes[token] = pending
            self._purge_pending_deletes()
        self._send_message(
            chat_id,
            f"确定删除直播间“{pending.room_name}”吗？\n"
            "不会删除已有录播文件，确认按钮 2 分钟内有效。",
            reply_markup={
                "inline_keyboard": [[
                    {
                        "text": "确认删除",
                        "callback_data": f"room_delete:{token}",
                    },
                    {
                        "text": "取消",
                        "callback_data": f"room_cancel:{token}",
                    },
                ]]
            },
        )

    def _purge_pending_deletes(self) -> None:
        now = time.time()
        expired = [
            token
            for token, pending in self._pending_deletes.items()
            if pending.expires_at < now
        ]
        for token in expired:
            self._pending_deletes.pop(token, None)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        user_id = str((callback.get("from") or {}).get("id") or "")
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        data = str(callback.get("data") or "")
        if not self._authorized(chat_id, user_id):
            if callback_id:
                self._api(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_id,
                        "text": "无权操作",
                        "show_alert": True,
                    },
                )
            return
        action, _, token = data.partition(":")
        with self._pending_lock:
            self._purge_pending_deletes()
            pending = self._pending_deletes.pop(token, None)
        if (
            pending is None
            or pending.user_id != user_id
            or pending.chat_id != chat_id
        ):
            self._api(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": "确认已失效，请重新发送命令",
                    "show_alert": True,
                },
            )
            return
        if action == "room_cancel":
            result_text = f"已取消删除“{pending.room_name}”。"
        elif action == "room_delete":
            state = self.manager.delete_room_and_reload(pending.room_id)
            if state == "missing":
                result_text = f"“{pending.room_name}”已经不存在。"
            else:
                result_text = f"✅ 已删除直播间“{pending.room_name}”。\n已有录播文件未删除。"
            logger.info(
                "Telegram 控制删除直播间：user_id=%s room_id=%s state=%s",
                user_id,
                pending.room_id,
                state,
            )
        else:
            result_text = "无法识别该确认操作。"
        self._api(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": "操作完成"},
        )
        message_id = message.get("message_id")
        if message_id:
            try:
                self._api(
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": result_text,
                    },
                )
                return
            except TelegramControlError:
                pass
        self._send_message(chat_id, result_text)

    def _tasks_text(self) -> str:
        jobs = list(self.manager.pipeline_jobs(10))
        if not jobs:
            return "目前没有录播处理任务。"
        status_icons = {
            "completed": "✅",
            "processing": "⏳",
            "failed": "❌",
            "paused": "⏸",
            "pending": "🕓",
            "dry_run": "🧪",
        }
        lines = ["最近录播任务"]
        for job in jobs[:10]:
            status = str(job.get("status") or "unknown")
            active = str(job.get("active_stage") or job.get("failed_stage") or "")
            detail = f" · {active}" if active else ""
            lines.append(
                f"{status_icons.get(status, '•')} "
                f"{job.get('room_name') or '直播间'} · {status}{detail}\n"
                f"   {job.get('title') or job.get('video_name') or job.get('short_id')}"
            )
        return "\n".join(lines)[:4096]

    def _status_text(self) -> str:
        status = self.manager.status()
        rooms = self._rooms()
        active = sum(1 for room in rooms if (room.get("runtime") or {}).get("recording"))
        root = Path(recordings_dir())
        disk_root = root
        while not disk_root.exists() and disk_root != disk_root.parent:
            disk_root = disk_root.parent
        usage = shutil.disk_usage(disk_root)
        return (
            "PotatoFlow 状态\n\n"
            f"录制引擎：{'运行中' if status.get('running') else '已停止'}\n"
            f"直播间：{len(rooms)} 个，正在录制 {active} 个\n"
            f"录播目录：{root}\n"
            f"磁盘：已用 {_human_bytes(usage.used)} / "
            f"{_human_bytes(usage.total)}，可用 {_human_bytes(usage.free)}"
        )


_GLOBAL_CONTROL_LOCK = threading.Lock()
_GLOBAL_CONTROL: TelegramControlService | None = None


def configure_global_telegram_control(
    manager: Any,
    config: dict[str, Any] | None,
) -> TelegramControlService:
    global _GLOBAL_CONTROL
    with _GLOBAL_CONTROL_LOCK:
        if _GLOBAL_CONTROL is None:
            _GLOBAL_CONTROL = TelegramControlService(manager, config)
            if _GLOBAL_CONTROL.enabled:
                _GLOBAL_CONTROL.start()
        else:
            _GLOBAL_CONTROL.manager = manager
            _GLOBAL_CONTROL.reconfigure(config)
        return _GLOBAL_CONTROL


def shutdown_global_telegram_control() -> None:
    global _GLOBAL_CONTROL
    with _GLOBAL_CONTROL_LOCK:
        if _GLOBAL_CONTROL is not None:
            _GLOBAL_CONTROL.stop()
        _GLOBAL_CONTROL = None
