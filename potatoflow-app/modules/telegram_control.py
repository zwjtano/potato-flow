from __future__ import annotations

import html
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
from .network_proxy import build_common_proxy_url


logger = logging.getLogger("telegram_control")

CONTROL_CONFIG_KEYS = (
    "TELEGRAM_CONTROL_ENABLED",
    "TELEGRAM_CONTROL_ADMIN_USER_IDS",
    "NOTIFY_TELEGRAM_BOT_TOKEN",
    "NOTIFY_TELEGRAM_CHAT_ID",
    "NETWORK_PROXY_URL",
    "NETWORK_PROXY_USERNAME",
    "NETWORK_PROXY_PASSWORD",
)
CONFIRM_TTL_SECONDS = 120
_COPYABLE_ID_PATTERN = re.compile(
    r"(?i)(?:\b(?:user\s+|chat\s+)?(?:id|pid|bvid))\s*[:：]\s*"
    r"(?P<value>[A-Za-z0-9][A-Za-z0-9._:-]*)"
)
_COPYABLE_COMMAND_PATTERN = re.compile(
    r"(?m)/(?:start|record|stop|delete|task|retry|pause|delete_task)\s+"
    r"(?P<value>[A-Za-z0-9][A-Za-z0-9._:-]*)"
)
_COPYABLE_NUMBER_PATTERN = re.compile(r"(?<![\w])(?P<value>-?\d{5,})(?![\w])")
ACTIVE_TASK_STATUSES = {"pending", "processing", "video_uploaded"}
ABNORMAL_TASK_STATUSES = {"failed", "paused"}


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


def _telegram_copyable_html(value: Any) -> tuple[str, bool]:
    """Render ID-like values as Telegram code entities that clients can copy."""
    text = str(value or "")
    spans: set[tuple[int, int]] = set()
    for pattern in (
        _COPYABLE_ID_PATTERN,
        _COPYABLE_COMMAND_PATTERN,
        _COPYABLE_NUMBER_PATTERN,
    ):
        for match in pattern.finditer(text):
            token = match.group("value")
            if pattern is _COPYABLE_COMMAND_PATTERN and not any(
                character.isdigit() for character in token
            ):
                continue
            spans.add(match.span("value"))
    if not spans:
        return text, False

    output: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        output.append(html.escape(text[cursor:start], quote=False))
        output.append(f"<code>{html.escape(text[start:end], quote=False)}</code>")
        cursor = end
    output.append(html.escape(text[cursor:], quote=False))
    return "".join(output), True


@dataclass
class PendingConfirmation:
    action: str
    user_id: str
    chat_id: str
    expires_at: float
    target_id: str = ""
    target_name: str = ""
    value: str = ""


@dataclass
class PendingInput:
    command: str
    token: str
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
        self._pending_confirmations: dict[str, PendingConfirmation] = {}
        self._pending_inputs: dict[tuple[str, str], PendingInput] = {}
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
        try:
            return build_common_proxy_url(self.config)
        except ValueError:
            return ""

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
        try:
            build_common_proxy_url(self.config)
        except ValueError:
            errors.append("有效的通用代理地址")
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
            self._pending_confirmations.clear()
            self._pending_inputs.clear()
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
        rendered_text, has_copyable_values = _telegram_copyable_html(str(text)[:3800])
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": rendered_text,
            "disable_web_page_preview": True,
        }
        if has_copyable_values:
            payload["parse_mode"] = "HTML"
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._api("sendMessage", payload)

    def _edit_message(
        self,
        chat_id: str,
        message_id: Any,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        rendered_text, has_copyable_values = _telegram_copyable_html(str(text)[:3800])
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": rendered_text,
            "disable_web_page_preview": True,
        }
        if has_copyable_values:
            payload["parse_mode"] = "HTML"
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._api("editMessageText", payload)

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
                        {"command": "start", "description": "打开 PotatoFlow 控制台"},
                        {"command": "record", "description": "开始检测/录制指定直播间"},
                        {"command": "stop", "description": "安全停止指定直播间"},
                        {"command": "delete", "description": "删除直播间（二次确认）"},
                        {"command": "menu", "description": "打开按钮控制台"},
                        {"command": "running", "description": "查看进行中的任务"},
                        {"command": "errors", "description": "查看异常任务"},
                        {"command": "ai", "description": "打开 AI 编辑工作台"},
                        {"command": "tasks", "description": "查看最近录播任务"},
                        {"command": "task", "description": "查看任务详情与上传进度"},
                        {"command": "retry", "description": "重试失败或暂停的任务"},
                        {"command": "pause", "description": "暂停正在投稿的任务"},
                        {"command": "delete_task", "description": "删除任务记录（保留文件）"},
                        {"command": "files", "description": "查看最近录播文件"},
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
            pending = self._take_pending_input(chat_id, user_id)
            if pending:
                try:
                    self._dispatch_command(chat_id, user_id, pending.command, text)
                except (RecorderConfigError, TelegramControlError, ValueError) as exc:
                    self._send_message(chat_id, f"操作失败：{exc}")
                except Exception:
                    logger.exception(
                        "Telegram 引导输入处理失败：user_id=%s command=%s",
                        user_id,
                        pending.command,
                    )
                    self._send_message(chat_id, "操作失败，请查看 PotatoFlow 服务日志。")
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
        if command.startswith("/roomvalue:"):
            _, reference, field = command.split(":", 2)
            self._set_room_setting_value(chat_id, reference, field, argument)
            return
        prompts = {
            "/add": "请直接发送完整直播间链接。",
            "/record": "请发送直播间编号或名称。",
            "/stop": "请发送要停止的直播间编号或名称。",
            "/delete": "请发送要删除的直播间编号或名称。",
            "/task": "请发送任务编号或任务 ID。",
            "/retry": "请发送要重试的任务编号或任务 ID。",
            "/pause": "请发送要暂停的任务编号或任务 ID。",
            "/delete_task": "请发送要删除记录的任务编号或任务 ID。",
        }
        if command in prompts and not argument:
            self._request_command_input(chat_id, user_id, command, prompts[command])
            return
        if command in {"/menu", "/start"} and not argument:
            text, markup = self._dashboard_page()
            self._send_message(chat_id, text, reply_markup=markup)
        elif command == "/help":
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
        elif command == "/running":
            text, markup = self._task_list_page("active")
            self._send_message(chat_id, text, reply_markup=markup)
        elif command in {"/errors", "/failed"}:
            text, markup = self._task_list_page("abnormal")
            self._send_message(chat_id, text, reply_markup=markup)
        elif command == "/ai":
            text, markup = self._ai_editor_page()
            self._send_message(chat_id, text, reply_markup=markup)
        elif command == "/task":
            self._send_message(chat_id, self._task_text(argument))
        elif command == "/retry":
            self._retry_task(chat_id, argument)
        elif command == "/pause":
            self._pause_task(chat_id, argument)
        elif command == "/delete_task":
            self._request_task_delete(chat_id, user_id, argument)
        elif command == "/files":
            self._send_message(chat_id, self._files_text())
        else:
            self._send_message(chat_id, "无法识别该命令。\n\n" + self._help_text())

    @staticmethod
    def _help_text() -> str:
        return (
            "PotatoFlow 远程控制\n\n"
            "/menu  打开按钮控制台\n"
            "/rooms  查看直播间编号与状态\n"
            "/add <直播间链接>  添加直播间\n"
            "/record <编号/名称>  开始检测/录制\n"
            "/stop <编号/名称>  安全停止并收尾\n"
            "/delete <编号/名称>  删除直播间（二次确认）\n"
            "\n任务处理\n"
            "/running  查看进行中的任务\n"
            "/errors  查看失败与暂停任务\n"
            "/ai  打开 AI 编辑工作台\n"
            "/tasks  查看最近任务及编号\n"
            "/task <编号/任务ID>  查看详情、进度和错误\n"
            "/retry <编号/任务ID>  重试失败或暂停任务\n"
            "/pause <编号/任务ID>  暂停等待中/上传中的任务\n"
            "/delete_task <编号/任务ID>  删除任务记录并保留文件\n"
            "\n文件\n"
            "/files  查看最近录播文件\n"
            "/help  查看本帮助\n\n"
            "直播间可用列表编号、完整房间号或唯一名称选择。"
        )

    def _rooms(self) -> list[dict[str, Any]]:
        return list(self.manager.rooms_with_status())

    @staticmethod
    def _nav_keyboard(*rows: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in rows
            ]
        }

    def _dashboard_page(self) -> tuple[str, dict[str, Any]]:
        rooms = self._rooms()
        jobs = list(self.manager.pipeline_jobs(None))
        recording = sum(
            1 for room in rooms if (room.get("runtime") or {}).get("recording")
        )
        active = sum(
            1 for job in jobs if str(job.get("status") or "") in ACTIVE_TASK_STATUSES
        )
        abnormal = sum(
            1 for job in jobs if str(job.get("status") or "") in ABNORMAL_TASK_STATUSES
        )
        text = (
            "🥔 PotatoFlow 控制台\n\n"
            f"直播间：{len(rooms)} 个 · 🔴 正在录制 {recording} 个\n"
            f"进行中任务：{active} 个\n"
            f"异常任务：{abnormal} 个"
        )
        return text, self._nav_keyboard(
            [("🎥 直播间", "nav:rooms"), ("⏳ 进行中", "nav:active")],
            [("⚠️ 异常任务", "nav:abnormal"), ("🧾 最近任务", "nav:tasks")],
            [("✨ AI 编辑", "nav:ai")],
            [("🔄 刷新", "nav:home")],
        )

    def _rooms_page(self) -> tuple[str, dict[str, Any]]:
        rooms = self._rooms()
        text = self._rooms_text()
        rows: list[list[tuple[str, str]]] = []
        for index, room in enumerate(rooms[:8], 1):
            runtime = room.get("runtime") or {}
            enabled = str(runtime.get("state") or "") not in {"paused", "stopped"}
            action = "停止" if enabled else "启动"
            rows.append([
                (f"⚙️ {index}. {room.get('name') or '直播间'}", f"roomsettings:{index}"),
                (action, f"roomtoggle:{index}:{0 if enabled else 1}"),
                ("删除", f"roomdelete:{index}"),
            ])
        rows.extend([
            [("➕ 添加直播间", "roomadd:prompt")],
            [("🔄 刷新", "nav:rooms"), ("🏠 首页", "nav:home")],
        ])
        return text, self._nav_keyboard(*rows)

    def _room_settings_page(self, reference: str) -> tuple[str, dict[str, Any]]:
        room = self._resolve_room(reference)
        rooms = self._rooms()
        index = rooms.index(room) + 1
        segment_enabled = bool(room.get("segment_enabled", True))
        multipart = bool(room.get("multipart_enabled", False))
        record_only = bool(room.get("record_only", False))
        burn_in = bool(room.get("danmaku_burn_in", False))
        schedule = bool(room.get("recording_schedule_enabled", False))
        quality = str(room.get("recording_quality") or "source")
        text = (
            f"⚙️ 直播间设置 · {room.get('name') or '直播间'}\n\n"
            f"录制质量：{quality}\n"
            f"分段录制：{'开启' if segment_enabled else '关闭'}\n"
            f"分段时长：{int(room.get('segment_minutes') or 60)} 分钟\n"
            f"合并分P：{'开启' if multipart else '关闭'}\n"
            f"投稿模式：{'仅录制' if record_only else '录制并投稿'}\n"
            f"弹幕烧录：{'开启' if burn_in else '关闭'}\n"
            f"定时录制：{'开启' if schedule else '关闭'}\n"
            f"时间范围：{room.get('recording_schedule_start') or '00:00'}–"
            f"{room.get('recording_schedule_end') or '23:59'}\n\n"
            f"B站账号：{room.get('bilibili_account_id') or '默认账号'}\n"
            f"B站合集：{room.get('bilibili_collection_id') or '不加入'}\n"
            f"继承全局 ASS：{'是' if room.get('danmaku_settings_inherit', True) else '否'}\n"
            f"ASS 参数：{room.get('danmaku_duration_seconds', 10)} 秒 · "
            f"字号 {room.get('danmaku_font_size', 42)} · "
            f"透明度 {room.get('danmaku_opacity', 0.92)}\n"
            f"ASS 编码：{room.get('danmaku_encoder', 'cpu')} · "
            f"{room.get('danmaku_encode_preset', 'medium')} · "
            f"质量 {room.get('danmaku_encode_crf', 20)}\n\n"
            "点击按钮会立即保存；录制中修改可能在当前分段安全收尾后应用。"
        )
        return text, self._nav_keyboard(
            [("切换画质", f"roomsetting:{index}:quality"),
             ("切换分段", f"roomsetting:{index}:segment")],
            [("调整分段时长", f"roomsetting:{index}:minutes"),
             ("切换分P", f"roomsetting:{index}:multipart")],
            [("切换投稿模式", f"roomsetting:{index}:recordonly"),
             ("切换弹幕烧录", f"roomsetting:{index}:burn")],
            [("切换定时录制", f"roomsetting:{index}:schedule")],
            [("开始时间", f"roominput:{index}:schedule_start"),
             ("结束时间", f"roominput:{index}:schedule_end")],
            [("B站投稿账号", f"roomaccount:{index}"),
             ("B站合集", f"roominput:{index}:collection")],
            [("继承全局 ASS", f"roomsetting:{index}:inherit"),
             ("ASS 飘屏秒数", f"roominput:{index}:duration")],
            [("ASS 字号", f"roominput:{index}:font_size"),
             ("ASS 透明度", f"roominput:{index}:opacity")],
            [("ASS 编码器", f"roomsetting:{index}:encoder"),
             ("编码预设", f"roomsetting:{index}:preset")],
            [("ASS 质量值", f"roominput:{index}:encode_quality")],
            [("🔄 刷新", f"roomsettings:{index}"), ("⬅️ 直播间", "nav:rooms")],
            [("🏠 首页", "nav:home")],
        )

    @staticmethod
    def _room_setting_values(room: dict[str, Any]) -> dict[str, Any]:
        return {
            "segment_enabled": bool(room.get("segment_enabled", True)),
            "segment_minutes": int(room.get("segment_minutes") or 60),
            "multipart_enabled": bool(room.get("multipart_enabled", False)),
            "record_only": bool(room.get("record_only", False)),
            "danmaku_burn_in": bool(room.get("danmaku_burn_in", False)),
            "danmaku_settings_inherit": bool(room.get("danmaku_settings_inherit", True)),
            "danmaku_duration_seconds": room.get("danmaku_duration_seconds", 10),
            "danmaku_font_size": room.get("danmaku_font_size", 42),
            "danmaku_opacity": room.get("danmaku_opacity", 0.92),
            "danmaku_encoder": room.get("danmaku_encoder", "cpu"),
            "danmaku_encode_preset": room.get("danmaku_encode_preset", "medium"),
            "danmaku_encode_quality": room.get("danmaku_encode_crf", 20),
            "recording_quality": str(room.get("recording_quality") or "source"),
            "bilibili_account_id": room.get("bilibili_account_id", ""),
            "bilibili_collection_id": room.get("bilibili_collection_id", ""),
            "recording_schedule_enabled": bool(room.get("recording_schedule_enabled", False)),
            "recording_schedule_start": room.get("recording_schedule_start", "00:00"),
            "recording_schedule_end": room.get("recording_schedule_end", "23:59"),
        }

    def _toggle_room_setting(self, reference: str, field: str) -> tuple[str, dict[str, Any]]:
        room = self._resolve_room(reference)
        quality_order = ("source", "2160p", "1080p", "720p")
        values = self._room_setting_values(room)
        current_quality = str(values["recording_quality"])
        quality_index = quality_order.index(current_quality) if current_quality in quality_order else 0
        if field == "quality":
            values["recording_quality"] = quality_order[(quality_index + 1) % len(quality_order)]
        elif field == "segment":
            values["segment_enabled"] = not values["segment_enabled"]
        elif field == "minutes":
            durations = (30, 60, 90, 120, 180)
            current = values["segment_minutes"]
            values["segment_minutes"] = next(
                (duration for duration in durations if duration > current), durations[0]
            )
        elif field == "multipart":
            values["multipart_enabled"] = not values["multipart_enabled"]
        elif field == "recordonly":
            values["record_only"] = not values["record_only"]
        elif field == "burn":
            values["danmaku_burn_in"] = not values["danmaku_burn_in"]
        elif field == "schedule":
            values["recording_schedule_enabled"] = not values["recording_schedule_enabled"]
        elif field == "inherit":
            values["danmaku_settings_inherit"] = not values["danmaku_settings_inherit"]
        elif field == "encoder":
            choices = ("auto", "cpu", "nvidia", "intel", "amd")
            current = str(values["danmaku_encoder"])
            index = choices.index(current) if current in choices else 0
            values["danmaku_encoder"] = choices[(index + 1) % len(choices)]
        elif field == "preset":
            choices = (
                "speed", "balanced", "quality", "p1", "p2", "p3", "p4",
                "p5", "p6", "p7", "veryfast", "faster", "fast", "medium",
                "slow", "slower",
            )
            current = str(values["danmaku_encode_preset"])
            index = choices.index(current) if current in choices else 0
            values["danmaku_encode_preset"] = choices[(index + 1) % len(choices)]
        else:
            raise RecorderConfigError("无法识别直播间设置")
        self.manager.save_room_recording_settings(str(room.get("id") or ""), **values)
        return self._room_settings_page(reference)

    def _request_room_setting_input(
        self, chat_id: str, user_id: str, reference: str, field: str
    ) -> None:
        prompts = {
            "schedule_start": "请发送开始时间，例如 08:30。",
            "schedule_end": "请发送结束时间，例如 23:59。",
            "collection": "请发送合集 ID；发送 0 表示清空。",
            "duration": "请发送 ASS 飘屏秒数（1–30）。",
            "font_size": "请发送 ASS 字号（12–120）。",
            "opacity": "请发送 ASS 透明度（0.10–1.00）。",
            "encode_quality": "请发送 ASS 编码质量值（0–51）。",
        }
        prompt = prompts.get(field)
        if not prompt:
            raise RecorderConfigError("无法识别直播间设置")
        token = secrets.token_urlsafe(8)
        with self._pending_lock:
            self._purge_pending_confirmations()
            self._pending_inputs[(chat_id, user_id)] = PendingInput(
                command=f"/roomvalue:{reference}:{field}",
                token=token,
                expires_at=time.time() + CONFIRM_TTL_SECONDS,
            )
        self._send_message(
            chat_id,
            prompt + "\n两分钟内有效。",
            reply_markup={"inline_keyboard": [[
                {"text": "取消输入", "callback_data": f"inputcancel:{token}"},
            ]]},
        )

    def _set_room_setting_value(
        self, chat_id: str, reference: str, field: str, raw_value: str
    ) -> None:
        room = self._resolve_room(reference)
        values = self._room_setting_values(room)
        value = str(raw_value or "").strip()
        if field in {"schedule_start", "schedule_end"}:
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise RecorderConfigError("时间格式必须是 HH:MM，例如 08:30")
            values[f"recording_{field}"] = value
        elif field == "collection":
            values["bilibili_collection_id"] = "" if value in {"", "0"} else value
        elif field == "duration":
            values["danmaku_duration_seconds"] = value
        elif field == "font_size":
            values["danmaku_font_size"] = value
        elif field == "opacity":
            values["danmaku_opacity"] = value
        elif field == "encode_quality":
            values["danmaku_encode_quality"] = value
        else:
            raise RecorderConfigError("无法识别直播间设置")
        self.manager.save_room_recording_settings(str(room.get("id") or ""), **values)
        text, markup = self._room_settings_page(reference)
        self._send_message(chat_id, "✅ 设置已保存。\n\n" + text, reply_markup=markup)

    def _room_account_page(self, reference: str) -> tuple[str, dict[str, Any]]:
        room = self._resolve_room(reference)
        accounts = list(self.manager.bilibili_archive_accounts())
        rooms = self._rooms()
        room_index = rooms.index(room) + 1
        rows: list[list[tuple[str, str]]] = [[
            ("使用默认账号", f"roomaccountset:{room_index}:default")
        ]]
        lines = [f"B站投稿账号 · {room.get('name') or '直播间'}"]
        for index, account in enumerate(accounts[:8], 1):
            lines.append(
                f"{index}. {account.get('name') or 'B站账号'} · UID {account.get('uid') or '—'}"
            )
            rows.append([(
                f"选择 {account.get('name') or index}",
                f"roomaccountset:{room_index}:{index}",
            )])
        rows.append([("⬅️ 返回设置", f"roomsettings:{room_index}")])
        return "\n".join(lines), self._nav_keyboard(*rows)

    def _set_room_account(self, reference: str, account_reference: str) -> tuple[str, dict[str, Any]]:
        room = self._resolve_room(reference)
        accounts = list(self.manager.bilibili_archive_accounts())
        account_id = ""
        if account_reference != "default":
            if not account_reference.isdigit() or not 1 <= int(account_reference) <= len(accounts):
                raise RecorderConfigError("没有找到该 B站账号")
            account_id = str(accounts[int(account_reference) - 1].get("id") or "")
        values = self._room_setting_values(room)
        values["bilibili_account_id"] = account_id
        self.manager.save_room_recording_settings(str(room.get("id") or ""), **values)
        return self._room_settings_page(reference)

    def _task_list_page(self, kind: str) -> tuple[str, dict[str, Any]]:
        all_jobs = list(self.manager.pipeline_jobs(None))
        if kind == "active":
            jobs = [
                job for job in all_jobs
                if str(job.get("status") or "") in ACTIVE_TASK_STATUSES
            ]
            heading = "⏳ 进行中的任务"
        elif kind == "abnormal":
            jobs = [
                job for job in all_jobs
                if str(job.get("status") or "") in ABNORMAL_TASK_STATUSES
            ]
            heading = "⚠️ 异常任务"
        else:
            jobs = all_jobs[:10]
            heading = "🧾 最近任务"
        lines = [f"{heading}（{len(jobs)}）"]
        rows: list[list[tuple[str, str]]] = []
        if not jobs:
            lines.append("\n目前没有符合条件的任务。")
        stage_labels = {
            "ai": "AI 简介", "cover": "封面", "burn": "烧录字幕",
            "upload": "投稿", "collection": "加入合集", "verify": "校验",
        }
        for index, job in enumerate(jobs[:8], 1):
            completed = int(job.get("completed_stages") or 0)
            total = int(job.get("total_stages") or 0)
            percent = completed / total * 100 if total else 0
            stage = str(job.get("active_stage") or job.get("failed_stage") or "等待")
            status = str(job.get("status") or "未知")
            lines.append(
                f"\n{index}. {job.get('room_name') or '直播间'} · {status}\n"
                f"{job.get('title') or job.get('video_name') or self._job_display_id(job)}\n"
                f"阶段：{stage_labels.get(stage, stage)} · 总进度 {completed}/{total} ({percent:.0f}%)"
            )
            progress = job.get("upload_progress")
            if isinstance(progress, dict) and float(progress.get("total_bytes") or 0) > 0:
                uploaded = float(progress.get("uploaded_bytes") or 0)
                total_bytes = float(progress.get("total_bytes") or 0)
                lines.append(f"上传：{uploaded / total_bytes * 100:.1f}%")
            rows.append([(
                f"查看 {self._job_display_id(job)}",
                f"taskview:{self._job_callback_id(job)}",
            )])
        refresh = {"active": "nav:active", "abnormal": "nav:abnormal"}.get(
            kind, "nav:tasks"
        )
        rows.append([("🔄 刷新", refresh), ("🏠 首页", "nav:home")])
        return "\n".join(lines)[:3800], self._nav_keyboard(*rows)

    def _task_detail_page(self, reference: str) -> tuple[str, dict[str, Any]]:
        job = self._resolve_task(reference)
        display_id = self._job_display_id(job)
        callback_id = self._job_callback_id(job)
        status = str(job.get("status") or "")
        rows: list[list[tuple[str, str]]] = [
            [("🔄 刷新进度", f"taskview:{callback_id}")],
        ]
        actions: list[tuple[str, str]] = []
        if job.get("retryable"):
            actions.append(("🔁 重试", f"taskretry:{callback_id}"))
        if job.get("pausable"):
            actions.append(("⏸ 暂停", f"taskpause:{callback_id}"))
        if status not in ACTIVE_TASK_STATUSES:
            actions.append(("🗑 删除记录", f"taskdelete:{callback_id}"))
        if actions:
            rows.append(actions)
        rows.append([("⬅️ 任务列表", "nav:tasks"), ("🏠 首页", "nav:home")])
        return self._task_text(display_id), self._nav_keyboard(*rows)

    def _engine_page(self) -> tuple[str, dict[str, Any]]:
        status = self.manager.status()
        text = (
            "⚙️ 录制引擎\n\n"
            f"状态：{'🟢 运行中' if status.get('running') else '⚫ 已停止'}\n"
            f"进程 PID：{status.get('pid') or '—'}"
        )
        action = (
            [("⛔ 停止引擎", "enginestop:request")]
            if status.get("running") else [("▶️ 启动引擎", "enginestart:now")]
        )
        return text, self._nav_keyboard(
            action,
            [("🔄 刷新", "nav:engine"), ("🏠 首页", "nav:home")],
        )

    def _ai_editor_page(self) -> tuple[str, dict[str, Any]]:
        jobs = list(self.manager.pipeline_jobs(None))
        editable = [
            job for job in jobs
            if job.get("title") or job.get("description") or job.get("bvid")
        ][:8]
        lines = ["✨ AI 编辑工作台", "", "可查看并重新生成标题、简介、标签与封面。"]
        rows: list[list[tuple[str, str]]] = []
        if not editable:
            lines.append("\n目前没有可编辑的录播任务。")
        for index, job in enumerate(editable, 1):
            title = self._clean_detail(
                job.get("title") or job.get("video_name") or "未生成标题", 90
            )
            lines.append(
                f"\n{index}. {job.get('room_name') or '直播间'} · "
                f"{job.get('status') or '未知'}\n{title}"
            )
            button_title = self._clean_detail(title, 28)
            rows.append([(
                f"编辑：{button_title}",
                f"aiview:{self._job_callback_id(job)}",
            )])
        rows.append([("🔄 刷新", "nav:ai"), ("🏠 首页", "nav:home")])
        return "\n".join(lines)[:3800], self._nav_keyboard(*rows)

    def _ai_detail_page(self, reference: str) -> tuple[str, dict[str, Any]]:
        job = self._resolve_task(reference)
        callback_id = self._job_callback_id(job)
        description = self._clean_detail(job.get("description"), 900) or "尚未生成"
        tags = "、".join(str(tag) for tag in (job.get("tags") or [])) or "尚未生成"
        text = (
            "✨ AI 编辑\n\n"
            f"状态：{job.get('status') or '未知'}\n\n"
            f"标题\n{self._clean_detail(job.get('title'), 180) or '尚未生成'}\n\n"
            f"简介\n{description}\n\n"
            f"标签\n{tags}"
        )
        return text[:3800], self._nav_keyboard(
            [("生成标题", f"airegen:title:{callback_id}"),
             ("生成简介", f"airegen:description:{callback_id}")],
            [("生成标签", f"airegen:tags:{callback_id}"),
             ("重新生成全部", f"airegen:all:{callback_id}")],
            [("生成 16:9 封面", f"airegen:cover_16x9:{callback_id}"),
             ("生成 4:3 封面", f"airegen:cover_4x3:{callback_id}")],
            [("🔄 刷新", f"aiview:{callback_id}"), ("⬅️ AI 工作台", "nav:ai")],
            [("🏠 首页", "nav:home")],
        )

    def _notifications_page(self) -> tuple[str, dict[str, Any]]:
        enabled = _as_bool(self.config.get("NOTIFY_ENABLED"))
        telegram = _as_bool(self.config.get("NOTIFY_TELEGRAM_ENABLED"))
        event_keys = (
            "NOTIFY_EVENT_TASK_ADDED", "NOTIFY_EVENT_TASK_COMPLETED",
            "NOTIFY_EVENT_TASK_FAILED", "NOTIFY_EVENT_RECORDING_STARTED",
            "NOTIFY_EVENT_RECORDING_STOPPED", "NOTIFY_EVENT_COOKIE_INVALID",
        )
        enabled_events = sum(_as_bool(self.config.get(key, True)) for key in event_keys)
        text = (
            "🔔 通知与安全\n\n"
            f"通知总开关：{'开启' if enabled else '关闭'}\n"
            f"Telegram 通知：{'开启' if telegram else '关闭'}\n"
            f"已启用事件：{enabled_events}/{len(event_keys)}\n\n"
            "安全策略\n"
            f"• 管理员白名单：{len(self.admin_user_ids)} 人\n"
            f"• 固定目标会话：{'已配置' if self.target_chat_id else '未配置'}\n"
            "• 停止引擎、删除直播间和删除任务均需二次确认\n"
            f"• 确认按钮有效期：{CONFIRM_TTL_SECONDS} 秒\n\n"
            "通知开关请在 PotatoFlow 设置页修改。"
        )
        return text, self._nav_keyboard(
            [("🔄 刷新", "nav:notifications"), ("🏠 首页", "nav:home")],
        )

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
        pending = PendingConfirmation(
            action="room_delete",
            user_id=user_id,
            chat_id=chat_id,
            expires_at=time.time() + CONFIRM_TTL_SECONDS,
            target_id=str(room.get("id") or ""),
            target_name=str(room.get("name") or "直播间"),
        )
        with self._pending_lock:
            self._pending_confirmations[token] = pending
            self._purge_pending_confirmations()
        self._send_message(
            chat_id,
            f"确定删除直播间“{pending.target_name}”吗？\n"
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

    def _purge_pending_confirmations(self) -> None:
        now = time.time()
        expired = [
            token
            for token, pending in self._pending_confirmations.items()
            if pending.expires_at < now
        ]
        for token in expired:
            self._pending_confirmations.pop(token, None)
        expired_inputs = [
            key for key, pending in self._pending_inputs.items()
            if pending.expires_at < now
        ]
        for key in expired_inputs:
            self._pending_inputs.pop(key, None)

    def _request_command_input(
        self,
        chat_id: str,
        user_id: str,
        command: str,
        prompt: str,
    ) -> None:
        token = secrets.token_urlsafe(8)
        with self._pending_lock:
            self._purge_pending_confirmations()
            self._pending_inputs[(chat_id, user_id)] = PendingInput(
                command=command,
                token=token,
                expires_at=time.time() + CONFIRM_TTL_SECONDS,
            )
        self._send_message(
            chat_id,
            f"{prompt}\n两分钟内有效；下一条文字将作为 {command} 的参数。",
            reply_markup={"inline_keyboard": [[
                {"text": "取消输入", "callback_data": f"inputcancel:{token}"},
            ]]},
        )

    def _take_pending_input(self, chat_id: str, user_id: str) -> PendingInput | None:
        with self._pending_lock:
            self._purge_pending_confirmations()
            return self._pending_inputs.pop((chat_id, user_id), None)

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
        if self._handle_navigation_callback(
            callback_id, chat_id, user_id, message, data
        ):
            return
        action, _, token = data.partition(":")
        with self._pending_lock:
            self._purge_pending_confirmations()
            pending = self._pending_confirmations.pop(token, None)
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
        if action.endswith("_cancel"):
            result_text = "已取消操作。"
        elif action != pending.action:
            result_text = "确认操作不匹配，未执行任何更改。"
        elif action == "room_delete":
            state = self.manager.delete_room_and_reload(pending.target_id)
            if state == "missing":
                result_text = f"“{pending.target_name}”已经不存在。"
            else:
                result_text = f"✅ 已删除直播间“{pending.target_name}”。\n已有录播文件未删除。"
            logger.info(
                "Telegram 控制删除直播间：user_id=%s room_id=%s state=%s",
                user_id,
                pending.target_id,
                state,
            )
        elif action == "task_delete":
            self.manager.delete_pipeline_job(pending.target_id, delete_files=False)
            result_text = (
                f"✅ 已删除任务记录“{pending.target_name}”。\n"
                "原始录播、字幕和封面文件均已保留。"
            )
            logger.info(
                "Telegram 控制删除任务记录：user_id=%s task_id=%s",
                user_id,
                pending.target_id[:12],
            )
        elif action == "engine_stop":
            self.manager.stop()
            result_text = "✅ 录制引擎已停止，正在录制的文件已安全收尾。"
            logger.info("Telegram 控制停止录制引擎：user_id=%s", user_id)
        elif action == "ai_regen":
            fields = {
                "title": {"title"},
                "description": {"description"},
                "tags": {"tags"},
                "all": {"title", "description", "tags"},
                "cover_16x9": {"cover_16x9"},
                "cover_4x3": {"cover_4x3"},
            }.get(pending.value)
            if not fields:
                raise RecorderConfigError("无法识别 AI 生成项目")
            self._api(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": "AI 生成已开始"},
            )
            message_id = message.get("message_id")
            if message_id:
                try:
                    self._edit_message(
                        chat_id,
                        message_id,
                        f"⏳ 正在让 AI 重新生成{pending.target_name}……\n完成后会发送通知。",
                    )
                except TelegramControlError:
                    logger.warning("Telegram AI 编辑进度消息更新失败")
            threading.Thread(
                target=self._run_ai_regeneration,
                args=(chat_id, user_id, pending, fields),
                name=f"potato-telegram-ai-{pending.target_id[:8]}",
                daemon=True,
            ).start()
            return
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

    def _run_ai_regeneration(
        self,
        chat_id: str,
        user_id: str,
        pending: PendingConfirmation,
        fields: set[str],
    ) -> None:
        try:
            self.manager.regenerate_published_metadata(pending.target_id, fields)
            self._send_message(
                chat_id,
                f"✅ AI 生成完成：{pending.target_name}。\n"
                f"任务 ID：{pending.target_id[:12]}\n发送 /ai 检查结果。",
            )
            logger.info(
                "Telegram AI 编辑完成：user_id=%s task_id=%s fields=%s",
                user_id,
                pending.target_id[:12],
                sorted(fields),
            )
        except Exception as exc:
            logger.exception(
                "Telegram AI 编辑失败：user_id=%s task_id=%s",
                user_id,
                pending.target_id[:12],
            )
            try:
                self._send_message(
                    chat_id,
                    f"❌ AI 生成失败：{self._clean_detail(exc, 300)}",
                )
            except TelegramControlError:
                logger.warning("Telegram AI 编辑失败通知发送失败")

    def _handle_navigation_callback(
        self,
        callback_id: str,
        chat_id: str,
        user_id: str,
        message: dict[str, Any],
        data: str,
    ) -> bool:
        prefixes = (
            "nav:", "noop:", "roomadd:", "roomtoggle:", "roomdelete:", "roomsettings:",
            "roomsetting:", "roominput:", "roomaccount:", "roomaccountset:", "taskview:",
            "taskretry:", "taskpause:", "taskdelete:",
            "aiview:", "airegen:", "inputcancel:",
        )
        if not data.startswith(prefixes):
            return False
        message_id = message.get("message_id")

        def answer(text: str = "已刷新", *, alert: bool = False) -> None:
            if callback_id:
                self._api("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": text[:200],
                    "show_alert": alert,
                })

        def render(page: tuple[str, dict[str, Any]]) -> None:
            text, markup = page
            if message_id:
                try:
                    self._edit_message(chat_id, message_id, text, reply_markup=markup)
                    return
                except TelegramControlError as exc:
                    if "message is not modified" in str(exc).lower():
                        return
                    raise
            self._send_message(chat_id, text, reply_markup=markup)

        if data.startswith("noop:"):
            answer("请选择右侧操作")
            return True
        if data.startswith("inputcancel:"):
            token = data.partition(":")[2]
            key = (chat_id, user_id)
            with self._pending_lock:
                self._purge_pending_confirmations()
                pending = self._pending_inputs.get(key)
                if pending and secrets.compare_digest(pending.token, token):
                    self._pending_inputs.pop(key, None)
                    cancelled = True
                else:
                    cancelled = False
            answer("已取消输入" if cancelled else "输入请求已失效", alert=not cancelled)
            if cancelled and message_id:
                self._edit_message(chat_id, message_id, "已取消输入。")
            return True
        if data.startswith("nav:"):
            page_name = data.partition(":")[2]
            if page_name == "home":
                page = self._dashboard_page()
            elif page_name == "rooms":
                page = self._rooms_page()
            elif page_name in {"active", "abnormal", "tasks"}:
                page = self._task_list_page(page_name)
            elif page_name == "ai":
                page = self._ai_editor_page()
            else:
                answer("页面不存在", alert=True)
                return True
            render(page)
            answer()
            return True
        if data.startswith("roomadd:"):
            self._request_command_input(
                chat_id, user_id, "/add", "请直接发送完整直播间链接。"
            )
            answer("请发送直播间链接")
            return True
        if data.startswith("roomtoggle:"):
            _, reference, enabled_text = data.split(":", 2)
            room = self._resolve_room(reference)
            self.manager.set_room_recording(
                str(room.get("id") or ""), enabled_text == "1"
            )
            render(self._rooms_page())
            answer("直播间状态已更新")
            return True
        if data.startswith("roomsettings:"):
            render(self._room_settings_page(data.partition(":")[2]))
            answer()
            return True
        if data.startswith("roomsetting:"):
            _, reference, field = data.split(":", 2)
            render(self._toggle_room_setting(reference, field))
            answer("设置已保存")
            return True
        if data.startswith("roominput:"):
            _, reference, field = data.split(":", 2)
            self._request_room_setting_input(chat_id, user_id, reference, field)
            answer("请发送设置值")
            return True
        if data.startswith("roomaccountset:"):
            _, reference, account_reference = data.split(":", 2)
            render(self._set_room_account(reference, account_reference))
            answer("投稿账号已保存")
            return True
        if data.startswith("roomaccount:"):
            render(self._room_account_page(data.partition(":")[2]))
            answer()
            return True
        if data.startswith("roomdelete:"):
            reference = data.partition(":")[2]
            self._request_room_delete(chat_id, user_id, reference)
            answer("请二次确认")
            return True
        if data.startswith("taskview:"):
            render(self._task_detail_page(data.partition(":")[2]))
            answer()
            return True
        if data.startswith("taskretry:"):
            job = self._resolve_task(data.partition(":")[2])
            self.manager.retry_pipeline_job(str(job.get("id") or ""))
            render(self._task_detail_page(self._job_display_id(job)))
            answer("已开始重试")
            return True
        if data.startswith("taskpause:"):
            job = self._resolve_task(data.partition(":")[2])
            self.manager.pause_pipeline_job(str(job.get("id") or ""))
            render(self._task_detail_page(self._job_display_id(job)))
            answer("任务已暂停")
            return True
        if data.startswith("taskdelete:"):
            self._request_task_delete(chat_id, user_id, data.partition(":")[2])
            answer("请二次确认")
            return True
        if data.startswith("aiview:"):
            render(self._ai_detail_page(data.partition(":")[2]))
            answer()
            return True
        if data.startswith("airegen:"):
            _, field, reference = data.split(":", 2)
            self._request_ai_regenerate(chat_id, user_id, reference, field)
            answer("请二次确认")
            return True
        return False

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
        for index, job in enumerate(jobs[:10], 1):
            status = str(job.get("status") or "unknown")
            active = str(job.get("active_stage") or job.get("failed_stage") or "")
            detail = f" · {active}" if active else ""
            lines.append(
                f"{index}. {status_icons.get(status, '•')} "
                f"{job.get('room_name') or '直播间'} · {status}{detail}\n"
                f"   {job.get('title') or job.get('video_name') or self._job_display_id(job)}\n"
                f"   ID: {self._job_display_id(job)}"
            )
        lines.append("\n发送 /task <编号> 查看详情，例如：/task 1")
        return "\n".join(lines)[:4096]

    def _resolve_task(self, reference: str) -> dict[str, Any]:
        value = str(reference or "").strip()
        if not value:
            raise RecorderConfigError("请提供任务编号，例如 /task 1")
        if re.fullmatch(r"[0-9a-fA-F]{64}", value):
            exact_job = self.manager.pipeline_job(value.lower())
            if exact_job:
                return exact_job
        jobs = list(self.manager.pipeline_jobs(None))
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(jobs):
                return jobs[index - 1]
        display_matches = [
            job
            for job in jobs
            if self._job_display_id(job).lower() == value.lower()
        ]
        if len(display_matches) == 1:
            return display_matches[0]
        id_matches = [
            job
            for job in jobs
            if (
                str(job.get("id") or "").lower().startswith(value.lower())
                or str(job.get("short_id") or "").lower().startswith(value.lower())
            )
        ]
        if len(id_matches) == 1 and len(value) >= 6:
            return id_matches[0]
        title_matches = [
            job
            for job in jobs
            if value.lower()
            in str(job.get("title") or job.get("video_name") or "").lower()
        ]
        if len(title_matches) == 1:
            return title_matches[0]
        raise RecorderConfigError("没有找到唯一任务，请先发送 /tasks 查看编号")

    @staticmethod
    def _job_display_id(job: dict[str, Any]) -> str:
        return str(
            job.get("display_id")
            or job.get("short_id")
            or str(job.get("id") or "")[:12]
        )

    @staticmethod
    def _clean_detail(value: Any, limit: int = 700) -> str:
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]+", " ", str(value or "")).strip()
        return text[:limit] + ("…" if len(text) > limit else "")

    def _task_text(self, reference: str) -> str:
        job = self._resolve_task(reference)
        status_labels = {
            "completed": "已完成",
            "processing": "处理中",
            "failed": "失败",
            "paused": "已暂停",
            "pending": "等待中",
            "dry_run": "试运行",
        }
        stage_labels = {
            "detect": "识别",
            "record": "录制",
            "ass": "弹幕字幕",
            "ai": "AI 简介",
            "cover": "封面",
            "remux": "封装",
            "verify": "校验",
            "cleanup": "清理",
            "upload": "投稿",
            "collection": "加入合集",
        }
        stage_icons = {
            "completed": "✅",
            "skipped": "↪️",
            "running": "⏳",
            "queued": "🕓",
            "failed": "❌",
            "paused": "⏸",
            "pending": "•",
        }
        lines = [
            str(job.get("title") or job.get("video_name") or "录播任务"),
            "",
            f"任务 ID：{self._job_display_id(job)}",
            f"直播间：{job.get('room_name') or '未匹配'}",
            f"状态：{status_labels.get(str(job.get('status')), str(job.get('status') or '未知'))}",
            f"总进度：{int(job.get('completed_stages') or 0)}/{int(job.get('total_stages') or 0)}",
        ]
        upload_progress = job.get("upload_progress")
        if isinstance(upload_progress, dict):
            uploaded = float(upload_progress.get("uploaded_bytes") or 0)
            total = float(upload_progress.get("total_bytes") or 0)
            speed = float(
                upload_progress.get("speed_bytes_per_second")
                or upload_progress.get("speed_bytes_per_sec")
                or 0
            )
            percent = uploaded / total * 100 if total > 0 else 0
            lines.append(
                f"上传：{percent:.2f}% · {_human_bytes(uploaded)} / {_human_bytes(total)}"
            )
            if speed > 0:
                lines.append(f"速度：{_human_bytes(speed)}/s")
            eta = upload_progress.get("eta_seconds")
            if eta is not None:
                lines.append(f"预计剩余：{max(0, int(float(eta)))} 秒")
        queue_position = job.get("upload_queue_position")
        if queue_position:
            lines.append(f"投稿队列：第 {queue_position} 位")
        stages = list(job.get("stages") or [])
        if stages:
            lines.append("\n处理步骤")
            for stage in stages:
                key = str(stage.get("key") or "")
                state = str(stage.get("status") or "pending")
                lines.append(
                    f"{stage_icons.get(state, '•')} {stage_labels.get(key, key)}：{state}"
                )
        error = self._clean_detail(job.get("error"))
        if not error:
            failed_stage = next(
                (stage for stage in stages if stage.get("status") == "failed"),
                {},
            )
            error = self._clean_detail(failed_stage.get("error"))
        if error:
            lines.extend(["\n错误", error])
        if job.get("bvid"):
            lines.append(f"\nBVID：{job.get('bvid')}")
        available = []
        if job.get("retryable"):
            available.append(f"/retry {self._job_display_id(job)}")
        if job.get("pausable"):
            available.append(f"/pause {self._job_display_id(job)}")
        if str(job.get("status")) not in {"processing", "video_uploaded"}:
            available.append(f"/delete_task {self._job_display_id(job)}")
        if available:
            lines.extend(["\n可用操作", "\n".join(available)])
        return "\n".join(lines)[:4096]

    def _retry_task(self, chat_id: str, reference: str) -> None:
        job = self._resolve_task(reference)
        self.manager.retry_pipeline_job(str(job.get("id") or ""))
        self._send_message(
            chat_id,
            f"✅ 已开始重试任务 {self._job_display_id(job)}。\n"
            "已有 AI 简介和封面会直接复用，不会重复生成。",
        )

    def _pause_task(self, chat_id: str, reference: str) -> None:
        job = self._resolve_task(reference)
        self.manager.pause_pipeline_job(str(job.get("id") or ""))
        self._send_message(
            chat_id,
            f"✅ 已暂停任务 {self._job_display_id(job)}。\n源文件和处理产物均已保留。",
        )

    def _request_task_delete(self, chat_id: str, user_id: str, reference: str) -> None:
        job = self._resolve_task(reference)
        token = secrets.token_urlsafe(8)
        pending = PendingConfirmation(
            action="task_delete",
            user_id=user_id,
            chat_id=chat_id,
            expires_at=time.time() + CONFIRM_TTL_SECONDS,
            target_id=str(job.get("id") or ""),
            target_name=str(
                job.get("title")
                or job.get("video_name")
                or self._job_display_id(job)
                or "录播任务"
            ),
        )
        with self._pending_lock:
            self._pending_confirmations[token] = pending
            self._purge_pending_confirmations()
        self._send_message(
            chat_id,
            f"确定删除任务记录“{pending.target_name}”吗？\n"
            "若任务仍在处理会先停止；原始录播、字幕和封面文件都会保留，"
            "确认按钮 2 分钟内有效。",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "确认删除记录", "callback_data": f"task_delete:{token}"},
                    {"text": "取消", "callback_data": f"task_cancel:{token}"},
                ]]
            },
        )

    @staticmethod
    def _job_callback_id(job: dict[str, Any]) -> str:
        """Keep Telegram callback_data safely below its 64-byte limit."""
        return str(job.get("short_id") or job.get("id") or "")[:12]

    def _request_ai_regenerate(
        self,
        chat_id: str,
        user_id: str,
        reference: str,
        field: str,
    ) -> None:
        job = self._resolve_task(reference)
        labels = {
            "title": "标题", "description": "简介", "tags": "标签",
            "all": "标题、简介和标签", "cover_16x9": "16:9 封面",
            "cover_4x3": "4:3 封面",
        }
        label = labels.get(field)
        if not label:
            raise RecorderConfigError("无法识别 AI 生成项目")
        token = secrets.token_urlsafe(8)
        pending = PendingConfirmation(
            action="ai_regen",
            user_id=user_id,
            chat_id=chat_id,
            expires_at=time.time() + CONFIRM_TTL_SECONDS,
            target_id=str(job.get("id") or ""),
            target_name=label,
            value=field,
        )
        with self._pending_lock:
            self._pending_confirmations[token] = pending
            self._purge_pending_confirmations()
        self._send_message(
            chat_id,
            f"确定让 AI 重新生成{label}吗？\n"
            "现有内容会在生成成功后更新，确认按钮 2 分钟内有效。",
            reply_markup={"inline_keyboard": [[
                {"text": f"确认生成{label}", "callback_data": f"ai_regen:{token}"},
                {"text": "取消", "callback_data": f"ai_cancel:{token}"},
            ]]},
        )

    def _files_text(self) -> str:
        payload = self.manager.recording_files(limit=12)
        files = list(payload.get("files") or [])
        if not files:
            return "录播目录中还没有视频、XML 弹幕或 ASS 字幕文件。"
        type_icons = {"video": "🎬", "xml": "💬", "ass": "🔤"}
        lines = [
            f"最近录播文件（共 {int(payload.get('total_files') or len(files))} 个，"
            f"{_human_bytes(payload.get('total_size_bytes') or 0)}）"
        ]
        for index, item in enumerate(files, 1):
            lock = f" · 🔒{item.get('lock_reason')}" if item.get("locked") else ""
            lines.append(
                f"{index}. {type_icons.get(str(item.get('type')), '📄')} "
                f"{item.get('name') or '未命名'}\n"
                f"   {_human_bytes(item.get('size_bytes') or 0)}{lock}"
            )
        return "\n".join(lines)[:4096]

    def _engine_command(self, chat_id: str, user_id: str, argument: str) -> None:
        action = str(argument or "").strip().lower()
        if not action:
            status = self.manager.status()
            self._send_message(
                chat_id,
                "录制引擎\n\n"
                f"状态：{'运行中' if status.get('running') else '已停止'}\n"
                f"进程 PID：{status.get('pid') or '—'}\n\n"
                "使用 /engine start 启动，/engine stop 安全停止。",
            )
            return
        if action == "start":
            status = self.manager.start()
            self._send_message(
                chat_id,
                f"✅ 录制引擎已启动，PID：{status.get('pid') or '—'}。",
            )
            return
        if action != "stop":
            raise RecorderConfigError("用法：/engine、/engine start 或 /engine stop")
        status = self.manager.status()
        if not status.get("running"):
            self._send_message(chat_id, "录制引擎已经处于停止状态。")
            return
        active = sum(
            1 for room in self._rooms() if (room.get("runtime") or {}).get("recording")
        )
        token = secrets.token_urlsafe(8)
        pending = PendingConfirmation(
            action="engine_stop",
            user_id=user_id,
            chat_id=chat_id,
            expires_at=time.time() + CONFIRM_TTL_SECONDS,
        )
        with self._pending_lock:
            self._pending_confirmations[token] = pending
            self._purge_pending_confirmations()
        warning = (
            f"当前有 {active} 个直播间正在录制，将安全停止并收尾文件。"
            if active
            else "引擎停止后将不再自动检测开播。"
        )
        self._send_message(
            chat_id,
            f"确定停止整个录制引擎吗？\n{warning}\n确认按钮 2 分钟内有效。",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "确认停止引擎", "callback_data": f"engine_stop:{token}"},
                    {"text": "取消", "callback_data": f"engine_cancel:{token}"},
                ]]
            },
        )

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
