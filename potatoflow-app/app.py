#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import io
import json
import logging
import mimetypes
import re
import shutil
import secrets
import subprocess
import sys
import time
import uuid
import threading

from datetime import datetime, timedelta
from pathlib import Path
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file, session, Response, stream_with_context
from functools import wraps
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash, safe_join
from modules.youtube_handler import extract_video_urls_from_playlist
from modules.utils import get_app_subdir
from modules.config_manager import (
    RECORDING_AI_PROMPT_CONFIG_KEYS,
    RECORDING_AI_PROMPT_MAX_LENGTH,
    load_config,
    reset_specific_config,
    update_config,
)
from modules.upload_line_manager import (
    load_probe_state as load_upload_probe_state,
    probe_and_select as probe_upload_lines,
    select_upload_line as save_upload_line,
)
from modules.whisper_languages import WHISPER_LANGUAGE_LIST
from modules.task_manager import add_task, start_task, pause_task, abandon_task, get_task, get_tasks_paginated, get_tasks_by_status, get_all_tasks, update_task, delete_task, force_upload_task, TASK_STATES, clear_all_tasks, retry_failed_tasks, register_task_updates_listener, unregister_task_updates_listener, resolve_cookie_file_path
from modules.task_manager import (
    PIPELINE_STAGE_DOWNLOAD_VIDEO,
    PIPELINE_STAGE_FETCH_INFO,
    PIPELINE_STAGE_COVER_PRECHECK,
    PIPELINE_STAGE_COVER_UPLOAD,
    PIPELINE_STAGE_GENERATE_TAGS,
    PIPELINE_STAGE_MODERATE_CONTENT,
    PIPELINE_STAGE_RECOMMEND_PARTITION,
    PIPELINE_STAGE_TRANSLATE_CONTENT,
    PIPELINE_STAGE_TRANSLATE_SUBTITLE,
    PIPELINE_STAGE_UPLOAD,
    _get_completed_stages,
)
from modules.task_lifecycle import (
    can_automatically_cleanup_youtube_download,
    youtube_task_capabilities,
)
from modules.bilibili_auth import BilibiliQrLoginSession
from queue import Empty
from modules.youtube_monitor import youtube_monitor
from modules.live_recorder_manager import (
    RecorderConfigError,
    live_recorder_manager,
    recordings_dir,
    validate_recordings_dir,
)
from modules.speech_pipeline_settings import (
    SPEECH_PIPELINE_CHECKBOXES,
    SPEECH_PIPELINE_FLOAT_FIELDS,
    SPEECH_PIPELINE_INT_FIELDS,
)
from modules.cookiecloud import (
    CookieCloudError,
    sync_cookiecloud_to_youtube_file,
    test_cookiecloud_youtube_sync,
)
from modules.notifications import (
    CHANNEL_LABELS,
    CHANNEL_MESSAGE_PUSHER,
    CHANNEL_SERVERCHAN,
    CHANNEL_TELEGRAM,
    CHANNEL_WECOM,
    EVENT_LOGIN_LOCKED,
    EVENT_LOGIN_SUCCESS,
    EVENT_QR_LOGIN_FAILED,
    EVENT_QR_LOGIN_SUCCESS,
    NotificationEvent,
    emit_notification_event,
    get_global_notification_service,
    iter_enabled_channel_ids,
    validate_channel_config_fields,
)
from modules.telegram_control import (
    configure_global_telegram_control,
    shutdown_global_telegram_control,
)
from apscheduler.schedulers.background import BackgroundScheduler
from version import __author__, __version__
from modules.runtime_info import build_runtime_info
from modules.task_queue_view import (
    build_queue_summary,
    filter_recording_jobs,
    filter_queue_items,
    normalize_queue_filter,
    normalize_recording_time_filter,
    normalize_recording_type_filter,
    normalize_source_filter,
    paginate_items,
    recording_queue_bucket,
    recording_room_options,
    youtube_queue_bucket,
)
from modules.bilibili_accounts import (
    DEFAULT_ACCOUNT_CONFIG_KEY,
    LEGACY_ACCOUNT_ID,
    account_cookie_destination,
    create_account_record,
    default_account_id,
    normalize_accounts,
    resolve_account,
    serialize_custom_accounts,
)

app = Flask(__name__)
_desktop_server = None
app.secret_key = os.urandom(24)  # 用于flash消息

_CSRF_SESSION_KEY = '_potatoflow_csrf_token'
_SECRET_FORM_SENTINEL = '__POTATOFLOW_SECRET_PRESENT__'
_SENSITIVE_SETTING_FIELDS = {
    'ALIYUN_ACCESS_KEY_ID',
    'ALIYUN_ACCESS_KEY_SECRET',
    'COOKIECLOUD_PASSWORD',
    'NETWORK_PROXY_PASSWORD',
    'NOTIFY_MESSAGE_PUSHER_TOKEN',
    'NOTIFY_SERVERCHAN_SENDKEY',
    'NOTIFY_TELEGRAM_BOT_TOKEN',
    'NOTIFY_WECOM_WEBHOOK_URL',
    'OPENAI_API_KEY',
    'OPENAI_IMAGE_API_KEY',
    'SUBTITLE_OPENAI_API_KEY',
    'SUBTITLE_QC_API_KEY',
    'WHISPER_API_KEY',
    'VOXTRAL_API_KEY',
    'AI_SEGMENTATION_API_KEY',
    'YOUTUBE_API_KEY',
    'STEAM_WEB_API_KEY',
}


def _get_csrf_token() -> str:
    token = str(session.get(_CSRF_SESSION_KEY) or '')
    if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token


def _secret_form_value(config: dict, key: str) -> str:
    return _SECRET_FORM_SENTINEL if str(config.get(key) or '').strip() else ''


def _admin_avatar_file() -> Path:
    return Path(get_app_subdir('admin')) / 'avatar.png'


@app.context_processor
def inject_app_settings():
    app_settings = app.config.get('POTATOFLOW_SETTINGS', {})
    if not isinstance(app_settings, dict):
        app_settings = {}
    runtime_info = build_runtime_info(
        __version__,
        Path(__file__).resolve().with_name('version.py'),
    )
    admin_avatar_available = _admin_avatar_file().is_file()
    return {
        'now': datetime.now(),  # 每次请求动态获取当前时间
        'app_settings': app_settings,
        'app_version': __version__,
        'app_author': __author__,
        'app_runtime': runtime_info,
        'csrf_token': _get_csrf_token(),
        'secret_field_value': lambda key: _secret_form_value(app_settings, str(key)),
        'show_logout_in_nav': bool(
            app_settings.get('password_protection_enabled') and session.get('logged_in')
        ),
        'current_admin_username': str(
            session.get('admin_username')
            or app_settings.get('admin_username')
            or 'admin'
        ),
        'admin_avatar_url': url_for('admin_avatar') if admin_avatar_available else '',
    }


@app.before_request
def protect_state_changing_requests():
    if request.method in {'GET', 'HEAD', 'OPTIONS', 'TRACE'}:
        return None
    config = load_config()
    if not config.get('password_protection_enabled'):
        return None
    expected = str(session.get(_CSRF_SESSION_KEY) or '')
    supplied = str(
        request.headers.get('X-CSRF-Token')
        or request.form.get('_csrf_token')
        or ''
    )
    if expected and supplied and secrets.compare_digest(expected, supplied):
        return None
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': False,
            'error': '安全令牌无效或已过期，请刷新页面后重试。',
        }), 400
    flash('安全令牌无效或已过期，请刷新页面后重试。', 'danger')
    return redirect(request.referrer or url_for('login'))


@app.after_request
def attach_app_version(response):
    """Expose one authoritative version and prevent stale rendered pages."""
    response.headers['X-PotatoFlow-Version'] = __version__
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault(
        'Permissions-Policy',
        'camera=(), geolocation=(), microphone=()',
    )
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@app.route('/api/version')
def app_version():
    runtime = build_runtime_info(
        __version__,
        Path(__file__).resolve().with_name('version.py'),
    )
    return jsonify({
        'name': 'PotatoFlow',
        'version': __version__,
        'author': __author__,
        'runtime': runtime,
    })


@app.route('/healthz')
def healthz():
    """Public liveness probe without filesystem, credential, or task details."""
    return jsonify({
        'status': 'ok',
        'version': __version__,
        'application_version': __version__,
        'runtime_mode': os.environ.get('POTATOFLOW_RUNTIME_MODE', 'source'),
        'architecture': os.environ.get('PROCESSOR_ARCHITECTURE') or os.environ.get('PROCESSOR_ARCHITEW6432') or '',
        'recorder_core_version': '1.2.2',
        'desktop_instance': os.environ.get('POTATOFLOW_DESKTOP_INSTANCE_ID', ''),
    })


def _desktop_request_authorized() -> bool:
    token = str(os.environ.get('POTATOFLOW_DESKTOP_TOKEN') or '')
    supplied = str(request.headers.get('X-PotatoFlow-Desktop-Token') or '')
    return bool(token and secrets.compare_digest(token, supplied) and request.remote_addr in {'127.0.0.1', '::1'})


def _is_windows_desktop_mode() -> bool:
    """Keep Windows-only controls out of Docker and Linux deployments."""
    enabled = str(os.environ.get('POTATOFLOW_DESKTOP_MODE') or '').strip().lower()
    return sys.platform == 'win32' and enabled in {'1', 'true', 'yes'}


@app.route('/api/desktop/status')
def desktop_status():
    if not _desktop_request_authorized():
        return jsonify({'error': 'forbidden'}), 403
    rooms = live_recorder_manager.rooms_with_status()
    return jsonify({
        'desktop_instance': os.environ.get('POTATOFLOW_DESKTOP_INSTANCE_ID', ''),
        'recording': any(bool(item.get('runtime', {}).get('recording')) for item in rooms),
        'rooms': [
            str(item.get('name') or '')
            for item in rooms
            if bool(item.get('runtime', {}).get('recording'))
        ],
    })


@app.route('/api/desktop/shutdown', methods=['POST'])
def desktop_shutdown():
    if not _desktop_request_authorized():
        return jsonify({'error': 'forbidden'}), 403
    server = _desktop_server
    if server is None:
        return jsonify({'ok': False}), 503
    threading.Thread(target=server.shutdown, daemon=True, name='desktop-shutdown').start()
    return jsonify({'ok': True})


ALLOWED_COVER_EXTENSIONS = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.webp': 'image/webp',
}

# bilibili二维码登录会话（内存）
_BILIBILI_QR_SESSIONS = {}
_BILIBILI_QR_SESSION_LOCK = threading.Lock()
_BILIBILI_QR_SESSION_TTL_SECONDS = 300
# 登录安全状态存储
def _get_security_state_path():
    try:
        db_dir = get_app_subdir('db')
    except Exception:
        # 回退到当前目录下的db
        db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db')
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, 'security_state.json')

def _load_security_state():
    path = _get_security_state_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 兼容缺失字段
                if not isinstance(data, dict):
                    data = {}
        else:
            data = {}
    except Exception:
        data = {}
    # 默认值
    return {
        'failed_attempts': int(data.get('failed_attempts', 0) or 0),
        'locked_until': float(data.get('locked_until', 0) or 0.0),
        'last_attempt': float(data.get('last_attempt', 0) or 0.0),
    }

def _save_security_state(state):
    try:
        path = _get_security_state_path()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _describe_youtube_api_status(status_code: str) -> str:
    messages = {
        'direct_ready': 'YouTube API 初始化成功，当前为直连模式',
        'proxy_ready': 'YouTube API 初始化成功，通用代理已启用',
        'missing_api_key': 'YouTube API 密钥未配置，请先在设置页完成接入。',
        'init_failed': 'YouTube监控 API 初始化失败，请检查 API 密钥、代理配置与网络连通性。',
    }
    return messages.get(status_code, 'YouTube监控 API 状态未知，请检查设置。')


def _build_startup_config_log_summary(config: dict | None) -> dict:
    normalized = dict(config or {})

    return {
        'feature_flags': {
            'AUTO_MODE_ENABLED': bool(normalized.get('AUTO_MODE_ENABLED', False)),
            'NOTIFY_ENABLED': bool(normalized.get('NOTIFY_ENABLED', False)),
            'password_protection_enabled': bool(normalized.get('password_protection_enabled', False)),
            'CONTENT_MODERATION_ENABLED': bool(normalized.get('CONTENT_MODERATION_ENABLED', False)),
            'YOUTUBE_PROXY_ENABLED': bool(normalized.get('YOUTUBE_PROXY_ENABLED', False)),
            'YOUTUBE_API_PROXY_ENABLED': bool(normalized.get('YOUTUBE_API_PROXY_ENABLED', False)),
            'COOKIECLOUD_ENABLED': bool(normalized.get('COOKIECLOUD_ENABLED', False)),
            'SUBTITLE_TRANSLATION_ENABLED': bool(normalized.get('SUBTITLE_TRANSLATION_ENABLED', False)),
            'SPEECH_RECOGNITION_ENABLED': bool(normalized.get('SPEECH_RECOGNITION_ENABLED', False)),
        },
        'config_keys_total': len(normalized),
    }


def _sync_notification_service(config: dict | None = None):
    effective_config = dict(config or load_config())
    try:
        get_global_notification_service(effective_config)
        logger.info("通知服务配置已同步")
    except Exception as e:
        logger.warning(f"同步通知服务配置失败: {e}")
    try:
        configure_global_telegram_control(live_recorder_manager, effective_config)
        logger.info("Telegram 机器人控制配置已同步")
    except Exception as e:
        logger.warning(f"同步 Telegram 机器人控制失败: {e}")


def _append_notification_config_warnings(messages: list, config: dict | None):
    effective_config = dict(config or {})
    if effective_config.get('NOTIFY_ENABLED'):
        for channel_id in iter_enabled_channel_ids(effective_config):
            missing_fields = validate_channel_config_fields(channel_id, effective_config)
            if missing_fields:
                readable_fields = '、'.join(missing_fields)
                channel_label = CHANNEL_LABELS.get(channel_id, channel_id)
                _append_settings_message(
                    messages,
                    'warning',
                    f'已启用 {channel_label} 通知，但缺少配置：{readable_fields}。该渠道会暂时跳过发送。'
                )
    if _coerce_checkbox_value(effective_config.get('TELEGRAM_CONTROL_ENABLED')):
        missing_control_fields = []
        if not str(effective_config.get('NOTIFY_TELEGRAM_BOT_TOKEN') or '').strip():
            missing_control_fields.append('Bot Token')
        if not str(effective_config.get('NOTIFY_TELEGRAM_CHAT_ID') or '').strip():
            missing_control_fields.append('Chat ID')
        if not str(effective_config.get('TELEGRAM_CONTROL_ADMIN_USER_IDS') or '').strip():
            missing_control_fields.append('管理员 User ID')
        if missing_control_fields:
            _append_settings_message(
                messages,
                'warning',
                '已启用 Telegram 远程控制，但缺少配置：'
                + '、'.join(missing_control_fields)
                + '。控制服务不会启动。',
            )


def _coerce_checkbox_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or '').strip().lower() in ('true', '1', 'on', 'yes', 'y')


def _merge_cookiecloud_runtime_settings(payload: dict | None, base_config: dict | None = None) -> dict:
    effective_config = dict(base_config or load_config())
    incoming = dict(payload) if isinstance(payload, dict) else {}
    secret_form_sentinel = '__POTATOFLOW_SECRET_PRESENT__'

    bool_fields = {'COOKIECLOUD_ENABLED', 'COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT'}
    text_fields = {
        'COOKIECLOUD_SERVER_URL',
        'COOKIECLOUD_UUID',
        'COOKIECLOUD_PASSWORD',
        'COOKIECLOUD_CRYPTO_TYPE',
        'YOUTUBE_COOKIES_PATH',
    }

    for key in bool_fields:
        if key in incoming:
            effective_config[key] = _coerce_checkbox_value(incoming.get(key))

    for key in text_fields:
        if key in incoming:
            value = str(incoming.get(key) or '').strip()
            if key == 'COOKIECLOUD_PASSWORD' and value == secret_form_sentinel:
                continue
            if key == 'COOKIECLOUD_PASSWORD' and not value:
                continue
            effective_config[key] = value

    return effective_config


def _cookiecloud_operation_error_message(action: str, retry_later: bool = False) -> str:
    action_key = str(action or '').strip().lower()
    if action_key == 'test':
        return 'CookieCloud 连接测试失败，请稍后重试。' if retry_later else 'CookieCloud 连接测试失败，请检查配置后重试。'
    if action_key == 'sync':
        return 'CookieCloud 立即拉取失败，请稍后重试。' if retry_later else 'CookieCloud 立即拉取失败，请检查配置后重试。'
    return 'CookieCloud 操作失败，请稍后重试。' if retry_later else 'CookieCloud 操作失败，请检查配置后重试。'


def _remember_cookiecloud_sync_result(success: bool, message: str):
    status = 'success' if success else 'error'
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        update_config({
            'COOKIECLOUD_LAST_SYNC_AT': timestamp,
            'COOKIECLOUD_LAST_SYNC_STATUS': status,
            'COOKIECLOUD_LAST_SYNC_MESSAGE': str(message or '').strip(),
        })
    except Exception as e:
        logger.warning(f'记录 CookieCloud 最近同步状态失败: {e}')
    return timestamp


def _get_request_ip_address() -> str:
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return str(request.remote_addr or 'unknown').strip() or 'unknown'


def _emit_login_event(event_type: str, payload: dict):
    emit_notification_event(NotificationEvent(event_type=event_type, payload=payload))


def _cleanup_bilibili_qr_sessions():
    now_ts = time.time()
    with _BILIBILI_QR_SESSION_LOCK:
        stale_ids = []
        for sid, item in _BILIBILI_QR_SESSIONS.items():
            created_at = float(item.get('created_at', 0) or 0)
            if now_ts - created_at > _BILIBILI_QR_SESSION_TTL_SECONDS:
                stale_ids.append(sid)
        for sid in stale_ids:
            _BILIBILI_QR_SESSIONS.pop(sid, None)


def _create_bilibili_qr_session(**metadata):
    _cleanup_bilibili_qr_sessions()
    session_id = str(uuid.uuid4())
    session_obj = BilibiliQrLoginSession()
    with _BILIBILI_QR_SESSION_LOCK:
        _BILIBILI_QR_SESSIONS[session_id] = {
            'created_at': time.time(),
            'session': session_obj,
            'success_notified': False,
            'failure_notified': False,
            **metadata,
        }
    return session_id, session_obj


def _get_bilibili_qr_session_item(session_id: str):
    if not session_id:
        return None
    _cleanup_bilibili_qr_sessions()
    with _BILIBILI_QR_SESSION_LOCK:
        item = _BILIBILI_QR_SESSIONS.get(session_id)
    return item


def _get_bilibili_qr_session(session_id: str):
    item = _get_bilibili_qr_session_item(session_id)
    return item.get('session') if item else None


def _mark_qr_notification_sent(session_store: dict, lock: threading.Lock, session_id: str, success: bool) -> bool:
    flag_name = 'success_notified' if success else 'failure_notified'
    with lock:
        item = session_store.get(session_id)
        if not item or item.get(flag_name):
            return False
        item[flag_name] = True
        return True


def _emit_qr_login_event_once(
    session_store: dict,
    lock: threading.Lock,
    session_id: str,
    platform: str,
    status_data: dict,
):
    status = str((status_data or {}).get('status') or '').strip().lower()
    if status not in ('done', 'failed'):
        return
    is_success = status == 'done'
    if not _mark_qr_notification_sent(session_store, lock, session_id, is_success):
        return
    _emit_login_event(
        EVENT_QR_LOGIN_SUCCESS if is_success else EVENT_QR_LOGIN_FAILED,
        {
            'platform': platform,
            'message': str((status_data or {}).get('message') or ('Cookies 已保存' if is_success else '登录失败')).strip(),
        }
    )


_SETTINGS_SAVE_OPERATIONS = {}
_SETTINGS_SAVE_LOCK = threading.Lock()
_SETTINGS_SAVE_TTL_SECONDS = 600
_MONITOR_RUN_OPERATIONS = {}
_MONITOR_RUN_LOCK = threading.Lock()
_MONITOR_RUN_TTL_SECONDS = 600


def _new_settings_save_state(operation_id: str) -> dict:
    now_ts = time.time()
    return {
        'operation_id': operation_id,
        'stage': 'saving_config',
        'message': '正在准备保存设置',
        'detail': '正在提交保存任务，请稍候。',
        'percent': None,
        'downloaded_bytes': None,
        'total_bytes': None,
        'done': False,
        'level': 'info',
        'success': None,
        'messages': [],
        'created_at': now_ts,
        'updated_at': now_ts,
        'expires_at': None,
    }


def _cleanup_settings_save_operations():
    now_ts = time.time()
    with _SETTINGS_SAVE_LOCK:
        stale_ids = []
        for operation_id, state in _SETTINGS_SAVE_OPERATIONS.items():
            expires_at = state.get('expires_at')
            if expires_at and now_ts >= float(expires_at):
                stale_ids.append(operation_id)
        for operation_id in stale_ids:
            _SETTINGS_SAVE_OPERATIONS.pop(operation_id, None)


def _update_settings_save_progress(operation_id: str, **fields) -> dict:
    _cleanup_settings_save_operations()
    with _SETTINGS_SAVE_LOCK:
        state = dict(_SETTINGS_SAVE_OPERATIONS.get(operation_id) or _new_settings_save_state(operation_id))
        state.update(fields)
        state['updated_at'] = time.time()
        if state.get('done'):
            state['expires_at'] = state['updated_at'] + _SETTINGS_SAVE_TTL_SECONDS
        _SETTINGS_SAVE_OPERATIONS[operation_id] = state
        return dict(state)


def _get_settings_save_progress(operation_id: str):
    _cleanup_settings_save_operations()
    with _SETTINGS_SAVE_LOCK:
        state = _SETTINGS_SAVE_OPERATIONS.get(operation_id)
        return dict(state) if state else None


def _new_monitor_run_state(operation_id: str, config_id: int) -> dict:
    now_ts = time.time()
    return {
        'operation_id': operation_id,
        'config_id': config_id,
        'message': '监控任务已创建',
        'detail': '正在后台执行 YouTube 监控，请稍候。',
        'done': False,
        'level': 'info',
        'success': None,
        'created_at': now_ts,
        'updated_at': now_ts,
        'expires_at': None,
    }


def _cleanup_monitor_run_operations():
    now_ts = time.time()
    with _MONITOR_RUN_LOCK:
        stale_ids = []
        for operation_id, state in _MONITOR_RUN_OPERATIONS.items():
            expires_at = state.get('expires_at')
            if expires_at and now_ts >= float(expires_at):
                stale_ids.append(operation_id)
        for operation_id in stale_ids:
            _MONITOR_RUN_OPERATIONS.pop(operation_id, None)


def _update_monitor_run_progress(operation_id: str, config_id: int, **fields) -> dict:
    _cleanup_monitor_run_operations()
    with _MONITOR_RUN_LOCK:
        state = dict(_MONITOR_RUN_OPERATIONS.get(operation_id) or _new_monitor_run_state(operation_id, config_id))
        state.update(fields)
        state['config_id'] = config_id
        state['updated_at'] = time.time()
        if state.get('done'):
            state['expires_at'] = state['updated_at'] + _MONITOR_RUN_TTL_SECONDS
        _MONITOR_RUN_OPERATIONS[operation_id] = state
        return dict(state)


def _get_monitor_run_progress(operation_id: str):
    _cleanup_monitor_run_operations()
    with _MONITOR_RUN_LOCK:
        state = _MONITOR_RUN_OPERATIONS.get(operation_id)
        return dict(state) if state else None


def _finalize_monitor_run_operation(operation_id: str, config_id: int, success: bool, message: str, detail: str = ''):
    _update_monitor_run_progress(
        operation_id,
        config_id,
        message=message,
        detail=detail or message,
        done=True,
        level='success' if success else 'error',
        success=success,
    )


def _run_monitor_operation(operation_id: str, config_id: int):
    try:
        success, message = youtube_monitor.run_monitor(config_id)
    except Exception as exc:
        logger.exception("后台执行 YouTube 监控失败，配置ID: %s", config_id)
        success = False
        message = f"监控失败: {exc}"

    detail = '监控记录已更新，可刷新页面查看最新结果。' if success else message
    _finalize_monitor_run_operation(operation_id, config_id, success, message, detail)


def _start_monitor_run_operation(config_id: int):
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        return None, None, "监控配置不存在"

    _cleanup_monitor_run_operations()
    with _MONITOR_RUN_LOCK:
        existing = next(
            (
                state for state in _MONITOR_RUN_OPERATIONS.values()
                if int(state.get('config_id') or 0) == int(config_id)
                and not state.get('done', False)
            ),
            None,
        )
        if existing:
            return existing['operation_id'], config, None
        operation_id = str(uuid.uuid4())
        state = _new_monitor_run_state(operation_id, config_id)
        state.update({
            'message': f"已启动监控任务：{config['name']}",
            'detail': '正在后台执行 YouTube 监控，请稍候。',
            'done': False,
            'level': 'info',
            'success': None,
        })
        _MONITOR_RUN_OPERATIONS[operation_id] = state

    monitor_thread = threading.Thread(
        target=_run_monitor_operation,
        args=(operation_id, config_id),
        daemon=True,
        name=f'youtube-monitor-run-{config_id}-{operation_id[:8]}'
    )
    monitor_thread.start()
    return operation_id, config, None


def _append_settings_message(messages: list, category: str, text: str):
    clean_text = str(text or '').strip()
    if not clean_text:
        return
    messages.append({'category': category, 'text': clean_text})


def _get_task_dir_real(task_id: str) -> str:
    downloads_dir_real = os.path.realpath(get_app_subdir('downloads'))
    try:
        normalized_task_id = str(uuid.UUID(str(task_id or '').strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError('非法任务目录') from exc

    safe_task_dir = safe_join(downloads_dir_real, normalized_task_id)
    if not safe_task_dir:
        raise ValueError('非法任务目录')
    task_dir_real = os.path.realpath(safe_task_dir)
    if os.path.commonpath([downloads_dir_real, task_dir_real]) != downloads_dir_real:
        raise ValueError('非法任务目录')
    return task_dir_real


def _safe_join_task_dir(task_dir_real: str, *parts: str) -> str | None:
    try:
        safe_path = safe_join(task_dir_real, *[str(part) for part in parts])
        if not safe_path:
            return None
        file_real = os.path.realpath(safe_path)
        if os.path.commonpath([task_dir_real, file_real]) != task_dir_real:
            return None
        return file_real
    except (ValueError, OSError):
        return None


def _get_cover_file_info(path: str):
    ext = os.path.splitext(str(path or ''))[1].lower()
    return ext, ALLOWED_COVER_EXTENSIONS.get(ext)


def _validate_cover_upload(file_storage):
    if not file_storage or not getattr(file_storage, 'filename', ''):
        raise ValueError('请选择要上传的封面图片')

    ext, _ = _get_cover_file_info(file_storage.filename)
    if ext not in ALLOWED_COVER_EXTENSIONS:
        raise ValueError('仅支持 JPG、JPEG、PNG、WEBP 格式的封面图片')

    current_pos = file_storage.stream.tell()
    try:
        with Image.open(file_storage.stream) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f'上传文件不是有效图片: {exc}') from exc
    finally:
        file_storage.stream.seek(current_pos)

    return ext


def _find_original_cover_backup(task_dir_real: str):
    for ext in ALLOWED_COVER_EXTENSIONS:
        candidate = _safe_join_task_dir(task_dir_real, f'original_cover{ext}')
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _get_current_cover_path(task: dict, task_dir_real: str):
    cover_path = str(task.get('cover_path_local') or '').strip()
    if cover_path:
        candidate = _safe_join_task_dir(task_dir_real, os.path.basename(cover_path))
        if candidate and os.path.exists(candidate):
            return candidate

    for prefix in ('custom_cover', 'cover', 'thumbnail'):
        for ext in ALLOWED_COVER_EXTENSIONS:
            candidate = _safe_join_task_dir(task_dir_real, f'{prefix}{ext}')
            if candidate and os.path.exists(candidate):
                return candidate

    if os.path.isdir(task_dir_real):
        with os.scandir(task_dir_real) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                if entry.name.lower().startswith('original_cover.'):
                    continue
                if entry.name.lower().endswith(tuple(ALLOWED_COVER_EXTENSIONS.keys())):
                    candidate = _safe_join_task_dir(task_dir_real, entry.name)
                    if candidate and os.path.exists(candidate):
                        return candidate

    return ''


def _task_cover_available(task: dict) -> bool:
    """Return whether the task has a cover that the cover route can serve."""
    try:
        task_dir_real = _get_task_dir_real(task.get('id'))
    except (AttributeError, TypeError, ValueError, OSError):
        return False
    return bool(_get_current_cover_path(task, task_dir_real))


def _replace_task_cover(task: dict, uploaded_file):
    task_id = str(task.get('id') or '').strip()
    if not task_id:
        raise ValueError('任务不存在')

    task_dir_real = _get_task_dir_real(task_id)
    os.makedirs(task_dir_real, exist_ok=True)

    current_cover_path = _get_current_cover_path(task, task_dir_real)

    ext = _validate_cover_upload(uploaded_file)
    original_backup = _find_original_cover_backup(task_dir_real)

    if not original_backup and current_cover_path:
        current_ext, _ = _get_cover_file_info(current_cover_path)
        if current_ext not in ALLOWED_COVER_EXTENSIONS:
            raise ValueError('当前原始封面格式不受支持，无法创建恢复备份')
        original_backup = _safe_join_task_dir(task_dir_real, f'original_cover{current_ext}')
        if not original_backup:
            raise ValueError('无法创建原始封面备份路径')
        shutil.copy2(current_cover_path, original_backup)

    staged_custom_covers = []
    for existing_ext in ALLOWED_COVER_EXTENSIONS:
        custom_candidate = _safe_join_task_dir(task_dir_real, f'custom_cover{existing_ext}')
        if custom_candidate and os.path.exists(custom_candidate):
            staged = f"{custom_candidate}.replacing-{uuid.uuid4().hex}"
            os.replace(custom_candidate, staged)
            staged_custom_covers.append((custom_candidate, staged))

    new_cover_path = _safe_join_task_dir(task_dir_real, f'custom_cover{ext}')
    if not new_cover_path:
        raise ValueError('无法创建封面保存路径')
    try:
        uploaded_file.save(new_cover_path)
        if not update_task(task_id, cover_path_local=new_cover_path, silent=True):
            raise RuntimeError('封面文件已写入，但任务记录更新失败')
    except Exception:
        try:
            if os.path.exists(new_cover_path):
                os.remove(new_cover_path)
        finally:
            for original, staged in reversed(staged_custom_covers):
                if os.path.exists(staged) and not os.path.exists(original):
                    os.replace(staged, original)
        raise
    for _original, staged in staged_custom_covers:
        try:
            os.remove(staged)
        except OSError as exc:
            logger.warning("旧自定义封面暂存文件清理失败 %s: %s", staged, exc)
    return new_cover_path


def _restore_task_cover(task: dict):
    task_id = str(task.get('id') or '').strip()
    if not task_id:
        raise ValueError('任务不存在')

    task_dir_real = _get_task_dir_real(task_id)
    if not os.path.isdir(task_dir_real):
        raise ValueError('任务目录不存在，无法恢复原封面')

    original_backup = _find_original_cover_backup(task_dir_real)
    if not original_backup:
        raise ValueError('未找到原始封面备份，无法恢复')

    if not update_task(task_id, cover_path_local=original_backup, silent=True):
        raise RuntimeError('恢复封面时任务记录更新失败')
    return original_backup


def _is_ajax_request() -> bool:
    requested_with = request.headers.get('X-Requested-With', '')
    accept_header = request.headers.get('Accept', '')
    return requested_with == 'XMLHttpRequest' or 'application/json' in accept_header


def _extract_settings_uploads(files_storage) -> dict:
    uploads = {}
    for field_name in (
        'youtube_cookies_file',
        'bilibili_cookies_file',
        'douyin_cookies_file',
        'admin_avatar_file',
    ):
        file_storage = files_storage.get(field_name)
        if not file_storage or not getattr(file_storage, 'filename', ''):
            continue
        uploads[field_name] = {
            'filename': file_storage.filename,
            'content': file_storage.read()
        }
    return uploads


def _persist_settings_uploads(form_data: dict, uploads: dict):
    cookies_dir = get_app_subdir('cookies')
    os.makedirs(cookies_dir, exist_ok=True)

    file_specs = {
        'youtube_cookies_file': ('yt_cookies.txt', 'YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt', 'YouTube'),
        'bilibili_cookies_file': ('bili_cookies.json', 'BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json', 'Bilibili'),
        'douyin_cookies_file': ('douyin_cookies.txt', 'DOUYIN_COOKIES_PATH', 'cookies/douyin_cookies.txt', '抖音'),
    }

    for field_name, payload in uploads.items():
        if field_name == 'admin_avatar_file':
            content = payload.get('content') or b''
            if not content or len(content) > 5 * 1024 * 1024:
                raise ValueError('管理员头像不能为空且不能超过 5 MB')
            try:
                with Image.open(io.BytesIO(content)) as source_image:
                    if str(source_image.format or '').upper() not in {'JPEG', 'PNG', 'WEBP'}:
                        raise ValueError('管理员头像只支持 JPG、PNG 或 WebP')
                    avatar = ImageOps.fit(
                        source_image.convert('RGBA'),
                        (512, 512),
                        method=Image.Resampling.LANCZOS,
                    )
                    target_path = _admin_avatar_file()
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary_path = target_path.with_suffix('.tmp.png')
                    avatar.save(temporary_path, format='PNG', optimize=True)
                    os.replace(temporary_path, target_path)
            except (UnidentifiedImageError, OSError) as exc:
                raise ValueError('管理员头像文件无效，请重新选择图片') from exc
            form_data['admin_avatar_path'] = 'admin/avatar.png'
            logger.info('管理员头像已更新')
            continue
        spec = file_specs.get(field_name)
        if not spec or not payload.get('filename'):
            continue
        save_name, config_key, relative_path, service_name = spec
        target_path = os.path.join(cookies_dir, save_name)
        content = payload.get('content') or b''
        if field_name == 'douyin_cookies_file':
            from modules.douyin_auth import (
                missing_douyin_cookie_names,
                normalize_douyin_cookie,
            )

            normalized_cookie = normalize_douyin_cookie(content)
            missing_cookie_names = missing_douyin_cookie_names(content)
            if missing_cookie_names:
                raise ValueError(
                    '抖音 Cookie 缺少录制引擎要求的字段：'
                    f"{', '.join(missing_cookie_names)}。"
                    '请使用 Get cookies.txt LOCALLY 在 douyin.com 登录后重新导出。'
                )
            content = (normalized_cookie + '\n').encode('utf-8')
        with open(target_path, 'wb') as target_file:
            target_file.write(content)
        form_data[config_key] = relative_path
        logger.info(f"{service_name} cookies文件已上传、转换并保存到: {target_path}")


def _build_settings_progress_reporter(operation_id: str | None):
    if not operation_id:
        return None

    def _report(payload: dict):
        _update_settings_save_progress(
            operation_id,
            stage=payload.get('stage', 'saving_config'),
            message=payload.get('message', ''),
            detail=payload.get('detail', ''),
            percent=payload.get('percent'),
            downloaded_bytes=payload.get('downloaded_bytes'),
            total_bytes=payload.get('total_bytes'),
            level=payload.get('level', 'info')
        )

    return _report


def _apply_missing_checkbox_defaults(
    form_data: dict,
    checkbox_names,
    scoped_fields=None,
) -> None:
    """Only clear unchecked boxes that belong to the submitted settings scope."""
    submitted_scope = set(scoped_fields or ())
    is_scoped = scoped_fields is not None
    for checkbox in checkbox_names:
        if checkbox in form_data:
            continue
        if is_scoped and checkbox not in submitted_scope:
            continue
        form_data[checkbox] = 'off'


def _perform_settings_save(form_data: dict, uploads: dict, operation_id: str | None = None) -> dict:
    form_data = dict(form_data or {})
    uploads = uploads or {}
    messages = []
    settings_scope = str(form_data.pop('settings_scope', '') or '').strip()
    raw_scope_fields = str(form_data.pop('settings_scope_fields', '') or '')
    scoped_fields = {
        field.strip()
        for field in raw_scope_fields.split(',')
        if field.strip()
    }
    current_config = load_config()
    previous_recordings_path = str(current_config.get('RECORDINGS_PATH') or 'docker-data/recordings').strip()
    progress_reporter = _build_settings_progress_reporter(operation_id)

    def report(stage: str, message: str, detail: str = '', percent=None, level: str = 'info', downloaded_bytes=None, total_bytes=None):
        if not progress_reporter:
            return
        progress_reporter({
            'stage': stage,
            'message': message,
            'detail': detail,
            'percent': percent,
            'downloaded_bytes': downloaded_bytes,
            'total_bytes': total_bytes,
            'level': level,
        })

    try:
        report('saving_config', '正在保存配置', '正在校验并写入设置。')
        form_data.pop('save_operation_id', None)
        if not _is_windows_desktop_mode():
            form_data.pop('DESKTOP_ALLOW_LAN', None)
            form_data.pop('DESKTOP_START_WITH_WINDOWS', None)

        new_password = form_data.get('new_password')
        confirm_password = form_data.get('confirm_password')
        if 'admin_username' in form_data:
            normalized_admin_username = _normalize_admin_username(
                form_data.get('admin_username')
            )
            if normalized_admin_username:
                form_data['admin_username'] = normalized_admin_username
            else:
                form_data.pop('admin_username', None)
                _append_settings_message(
                    messages,
                    'danger',
                    '管理员用户名需为 2 到 32 位，只能包含文字、数字、点、横线、下划线或 @。',
                )
        if new_password:
            if new_password == confirm_password:
                form_data['password'] = generate_password_hash(new_password)
            else:
                _append_settings_message(messages, 'danger', '新密码两次输入不一致，密码未更新。')

        form_data.pop('new_password', None)
        form_data.pop('confirm_password', None)
        for field in _SENSITIVE_SETTING_FIELDS:
            if form_data.get(field) == _SECRET_FORM_SENTINEL:
                form_data.pop(field, None)

        checkboxes = [
            'AUTO_MODE_ENABLED', 'TRANSLATE_TITLE', 'TRANSLATE_DESCRIPTION',
            'UPLOAD_APPEND_REPOST_NOTICE',
            'GENERATE_TAGS', 'YOUTUBE_UPLOADER_AS_FIRST_TAG', 'RECOMMEND_PARTITION',
            'RECOMMEND_PARTITION_WITH_COVER', 'AI_GENERATE_RECORDING_COVER',
            'DOUYU_STATS_ENABLED', 'DOUYU_STATS_APPEND_DESCRIPTION',
            'DOUYU_STATS_COVER_CONTEXT_ENABLED',
            'CONTENT_MODERATION_ENABLED',
            'OPENAI_THINKING_ENABLED', 'SUBTITLE_OPENAI_THINKING_ENABLED', 'SUBTITLE_QC_THINKING_ENABLED',
            'LOG_CLEANUP_ENABLED', 'SUBTITLE_TRANSLATION_ENABLED', 'SUBTITLE_EMBED_IN_VIDEO',
            'SUBTITLE_KEEP_ORIGINAL', 'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED',
            'YOUTUBE_PROXY_ENABLED', 'YOUTUBE_API_PROXY_ENABLED', 'password_protection_enabled',
            'SPEECH_RECOGNITION_ENABLED',
            'VAD_ENABLED',
            'SUBTITLE_NORMALIZE_PUNCTUATION', 'SUBTITLE_FILTER_FILLER_WORDS',
            'SUBTITLE_TIME_OFFSET_ENABLED', 'SUBTITLE_MIN_CUE_DURATION_ENABLED',
            'SUBTITLE_MERGE_GAP_ENABLED', 'SUBTITLE_MIN_TEXT_LENGTH_ENABLED',
            'SUBTITLE_MAX_LINE_LENGTH_ENABLED', 'SUBTITLE_MAX_LINES_ENABLED',
            'SUBTITLE_QC_ENABLED',
            'FFMPEG_AUTO_DOWNLOAD', 'WHISPER_TRANSLATE',
            'VOXTRAL_DIARIZE',
            'NOTIFY_ENABLED',
            'NOTIFY_EVENT_TASK_ADDED',
            'NOTIFY_EVENT_TASK_COMPLETED',
            'NOTIFY_EVENT_TASK_FAILED',
            'NOTIFY_EVENT_LOGIN_SUCCESS',
            'NOTIFY_EVENT_LOGIN_LOCKED',
            'NOTIFY_EVENT_QR_LOGIN_SUCCESS',
            'NOTIFY_EVENT_QR_LOGIN_FAILED',
            'NOTIFY_EVENT_RECORDING_STARTED',
            'NOTIFY_EVENT_RECORDING_STOPPED',
            'NOTIFY_EVENT_COOKIE_INVALID',
            'NOTIFY_WECOM_ENABLED',
            'NOTIFY_SERVERCHAN_ENABLED',
            'NOTIFY_MESSAGE_PUSHER_ENABLED',
            'NOTIFY_TELEGRAM_ENABLED',
            'TELEGRAM_CONTROL_ENABLED',
            'COOKIECLOUD_ENABLED',
            'COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT',
        ]
        if _is_windows_desktop_mode():
            checkboxes.extend(('DESKTOP_ALLOW_LAN', 'DESKTOP_START_WITH_WINDOWS'))
        for checkbox in SPEECH_PIPELINE_CHECKBOXES:
            if checkbox not in checkboxes:
                checkboxes.append(checkbox)
        _apply_missing_checkbox_defaults(
            form_data,
            checkboxes,
            scoped_fields if settings_scope else None,
        )

        numeric_fields = [
            'MAX_CONCURRENT_TASKS', 'MAX_CONCURRENT_UPLOADS', 'LOG_CLEANUP_HOURS',
            'LOG_CLEANUP_INTERVAL', 'SUBTITLE_BATCH_SIZE', 'SUBTITLE_MAX_RETRIES',
            'SUBTITLE_RETRY_DELAY', 'SUBTITLE_MAX_WORKERS', 'YOUTUBE_DOWNLOAD_THREADS',
            'YOUTUBE_DOWNLOAD_MAX_HEIGHT',
            'LOGIN_MAX_FAILED_ATTEMPTS', 'LOGIN_LOCKOUT_MINUTES', 'LOGIN_SESSION_TIMEOUT_MINUTES',
            'VAD_SILERO_MIN_SPEECH_MS',
            'VAD_SILERO_MIN_SILENCE_MS', 'VAD_SILERO_MAX_SPEECH_S',
            'VAD_SILERO_SPEECH_PAD_MS', 'VAD_MAX_SEGMENT_S',
            'SUBTITLE_QC_SAMPLE_MAX_ITEMS', 'SUBTITLE_QC_MAX_CHARS',
            'SUBTITLE_MIN_TEXT_LENGTH',
            'WHISPER_MAX_WORKERS', 'WHISPER_MAX_RETRIES'
        ]
        for field in SPEECH_PIPELINE_INT_FIELDS:
            if field not in numeric_fields:
                numeric_fields.append(field)
        for field in numeric_fields:
            if field in form_data:
                try:
                    original_value = form_data[field]
                    normalized_value = int(original_value)
                    if field == 'LOGIN_SESSION_TIMEOUT_MINUTES':
                        normalized_value = max(1, normalized_value)
                    form_data[field] = str(normalized_value)
                except (ValueError, TypeError) as e:
                    logger.debug(f"整数转换失败 - field: {field}, value: {form_data[field]}, error: {e}")
                    defaults = {
                        'MAX_CONCURRENT_TASKS': 2,
                        'MAX_CONCURRENT_UPLOADS': 1,
                        'LOG_CLEANUP_HOURS': 168,
                        'LOG_CLEANUP_INTERVAL': 24,
                        'SUBTITLE_BATCH_SIZE': 5,
                        'SUBTITLE_MAX_RETRIES': 3,
                        'SUBTITLE_RETRY_DELAY': 5,
                        'SUBTITLE_MAX_WORKERS': 2,
                        'YOUTUBE_DOWNLOAD_THREADS': 4,
                        'YOUTUBE_DOWNLOAD_MAX_HEIGHT': 1080,
                        'LOGIN_MAX_FAILED_ATTEMPTS': 5,
                        'LOGIN_LOCKOUT_MINUTES': 15,
                        'LOGIN_SESSION_TIMEOUT_MINUTES': 30,
                        'VAD_SILERO_MIN_SPEECH_MS': 300,
                        'VAD_SILERO_MIN_SILENCE_MS': 320,
                        'VAD_SILERO_MAX_SPEECH_S': 120,
                        'VAD_SILERO_SPEECH_PAD_MS': 120,
                        'VAD_MAX_SEGMENT_S': 15,
                        'SUBTITLE_QC_SAMPLE_MAX_ITEMS': 80,
                        'SUBTITLE_QC_MAX_CHARS': 9000
                    }
                    defaults.update(SPEECH_PIPELINE_INT_FIELDS)
                    form_data[field] = str(defaults.get(field, 1))
                    logger.debug(f"整数字段使用默认值 - field: {field}, value: {form_data[field]}")

        float_fields = [
            'VAD_SILERO_THRESHOLD',
            'SUBTITLE_TIME_OFFSET_S', 'SUBTITLE_MIN_CUE_DURATION_S', 'SUBTITLE_MERGE_GAP_S',
            'SUBTITLE_QC_THRESHOLD',
            'WHISPER_RETRY_DELAY_S', 'AUDIO_CHUNK_WINDOW_S', 'AUDIO_CHUNK_OVERLAP_S',
            'VAD_MERGE_GAP_S', 'VAD_MIN_SEGMENT_S', 'VAD_MAX_SEGMENT_S_FOR_SPLIT'
        ]
        for field in SPEECH_PIPELINE_FLOAT_FIELDS:
            if field not in float_fields:
                float_fields.append(field)
        for field in float_fields:
            if field in form_data:
                try:
                    original_value = form_data[field]
                    if str(original_value).strip() == '':
                        raise ValueError('empty string')
                    form_data[field] = str(float(original_value))
                except (ValueError, TypeError) as e:
                    logger.debug(f"浮点数转换失败 - field: {field}, value: {form_data[field]}, error: {e}")
                    float_defaults = {
                        'VAD_SILERO_THRESHOLD': 0.55,
                        'SUBTITLE_TIME_OFFSET_S': 0.0,
                        'SUBTITLE_MIN_CUE_DURATION_S': 0.6,
                        'SUBTITLE_MERGE_GAP_S': 0.3,
                        'SUBTITLE_QC_THRESHOLD': 0.35,
                        'WHISPER_RETRY_DELAY_S': 2.0,
                        'AUDIO_CHUNK_WINDOW_S': 15.0,
                        'AUDIO_CHUNK_OVERLAP_S': 0.4,
                        'VAD_MERGE_GAP_S': 0.35,
                        'VAD_MIN_SEGMENT_S': 0.8,
                        'VAD_MAX_SEGMENT_S_FOR_SPLIT': 15.0,
                    }
                    float_defaults.update(SPEECH_PIPELINE_FLOAT_FIELDS)
                    form_data[field] = str(float_defaults.get(field, 0.0))
                    logger.debug(f"浮点字段使用默认值 - field: {field}, value: {form_data[field]}")

        if 'SUBTITLE_FONT_NAME' in form_data:
            form_data['SUBTITLE_FONT_NAME'] = str(form_data['SUBTITLE_FONT_NAME']).strip()

        for config_key in RECORDING_AI_PROMPT_CONFIG_KEYS.values():
            if config_key not in form_data:
                continue
            prompt_text = str(form_data.get(config_key) or "").strip()
            if len(prompt_text) > RECORDING_AI_PROMPT_MAX_LENGTH:
                raise RecorderConfigError(
                    f"录播 AI 提示词每项不能超过 {RECORDING_AI_PROMPT_MAX_LENGTH} 字"
                )
            form_data[config_key] = prompt_text

        danmaku_ranges = {
            'DANMAKU_DURATION_SECONDS': (float, 1, 30, '弹幕飘屏时间'),
            'DANMAKU_FONT_SIZE': (int, 12, 120, '弹幕字号'),
            'DANMAKU_OPACITY': (float, 0.1, 1.0, '弹幕透明度'),
            'DANMAKU_ENCODE_QUALITY': (int, 0, 51, '编码质量值'),
        }
        for field, (converter, lower, upper, label) in danmaku_ranges.items():
            if field not in form_data:
                continue
            try:
                value = converter(form_data[field])
            except (TypeError, ValueError) as exc:
                raise RecorderConfigError(f'{label}必须是数字') from exc
            if not lower <= value <= upper:
                raise RecorderConfigError(f'{label}必须在 {lower} 到 {upper} 之间')
            form_data[field] = value

        if 'RECORDINGS_PATH' in form_data:
            requested_recordings_path = str(form_data.get('RECORDINGS_PATH') or 'docker-data/recordings').strip()
            validate_recordings_dir(requested_recordings_path)
            form_data['RECORDINGS_PATH'] = requested_recordings_path or 'docker-data/recordings'

        _persist_settings_uploads(form_data, uploads)
        updated_config = update_config(form_data)
        if any(field.startswith('DANMAKU_') for field in form_data):
            try:
                live_recorder_manager.sync_configs()
            except RecorderConfigError as exc:
                _append_settings_message(messages, 'warning', f'ASS 设置已保存，但录制配置同步失败：{exc}')
        recording_prompt_settings_changed = bool(
            (
                set(RECORDING_AI_PROMPT_CONFIG_KEYS.values())
            )
            & set(form_data)
        )
        if recording_prompt_settings_changed:
            try:
                # Rebuild only generated configuration. The current recorder
                # process does not need to restart; subsequent tasks read the
                # updated bridge configuration.
                live_recorder_manager.sync_configs()
            except RecorderConfigError as exc:
                _append_settings_message(
                    messages,
                    'warning',
                    f'录播 AI 提示词已保存，但桥接配置同步失败：{exc}',
                )
        if {'DESKTOP_START_WITH_WINDOWS', 'DESKTOP_ALLOW_LAN'} & set(form_data):
            try:
                from modules.desktop_runtime import sync_windows_startup
                sync_windows_startup(bool(updated_config.get('DESKTOP_START_WITH_WINDOWS')))
            except Exception as exc:
                logger.warning("Windows 开机启动设置同步失败: %s", exc)
        recordings_path_changed = (
            str(updated_config.get('RECORDINGS_PATH') or 'docker-data/recordings').strip()
            != previous_recordings_path
        )
        if recordings_path_changed:
            try:
                reload_state = live_recorder_manager.refresh_credentials()
                effective_path = recordings_dir()
                if reload_state == 'pending':
                    _append_settings_message(
                        messages,
                        'success',
                        f'录播目录已改为 {effective_path}；当前录制结束后自动切换。',
                    )
                else:
                    _append_settings_message(
                        messages,
                        'success',
                        f'录播目录已改为 {effective_path}。',
                    )
            except RecorderConfigError as exc:
                _append_settings_message(
                    messages,
                    'warning',
                    f'录播目录已保存，但录制 worker 未能自动重载：{exc}',
                )
        douyu_pipeline_settings_changed = any(
            field in form_data
            for field in (
                'DOUYU_STATS_ENABLED',
                'DOUYU_STATS_APPEND_DESCRIPTION',
                'DOUYU_STATS_COVER_CONTEXT_ENABLED',
            )
        )
        if douyu_pipeline_settings_changed and not recordings_path_changed:
            try:
                live_recorder_manager.refresh_credentials()
            except RecorderConfigError as exc:
                logger.warning("斗鱼直播数据设置已保存，但录播配置重载失败: %s", exc)
                _append_settings_message(
                    messages,
                    'warning',
                    f'斗鱼直播数据设置已保存，但录制配置未能立即重载：{exc}',
                )
        if (
            'DOUYIN_COOKIES_PATH' in form_data
            or 'douyin_cookies_file' in uploads
            or 'BILIBILI_COOKIES_PATH' in form_data
            or 'bilibili_cookies_file' in uploads
        ):
            try:
                live_recorder_manager.refresh_credentials()
            except RecorderConfigError as exc:
                logger.warning("平台 Cookie 已保存，但录制配置重载失败: %s", exc)
                _append_settings_message(
                    messages,
                    'warning',
                    f'平台 Cookie 已保存，但录制 worker 未能自动重载：{exc}',
                )

        try:
            from modules.task_manager import get_global_task_processor
            configure_app(app, updated_config)
            get_global_task_processor(updated_config)
            logger.info("配置已更新并同步到任务处理器")
        except Exception as e:
            logger.warning(f"同步任务处理器配置失败: {e}")

        _sync_notification_service(updated_config)
        _append_notification_config_warnings(messages, updated_config)

        try:
            need_ffmpeg = False
            if str(updated_config.get('SPEECH_RECOGNITION_ENABLED', False)).lower() in ['true', '1', 'on']:
                need_ffmpeg = True
            if str(updated_config.get('SUBTITLE_EMBED_IN_VIDEO', False)).lower() in ['true', '1', 'on']:
                need_ffmpeg = True

            if need_ffmpeg:
                from modules.ffmpeg_manager import get_windows_ffmpeg_manual_setup_message
                from modules.youtube_handler import get_ffmpeg_path
                report('checking_ffmpeg', '正在检查 FFmpeg', '已启用依赖 FFmpeg 的功能，正在检查本地环境。')
                ff_path = get_ffmpeg_path(
                    logger=logger,
                    force_refresh=True,
                    progress_callback=progress_reporter
                )
                if ff_path and os.path.exists(ff_path):
                    logger.info(f"FFmpeg 已就绪: {ff_path}")
                    report('completed', 'FFmpeg 已就绪', ff_path, percent=100.0, level='success')
                else:
                    warning_msg = get_windows_ffmpeg_manual_setup_message()
                    logger.warning(warning_msg)
                    _append_settings_message(messages, 'warning', warning_msg)
                    report('warning', 'FFmpeg 未就绪', warning_msg, level='warning')
            else:
                report('completed', '配置已保存', '当前设置不需要额外下载 FFmpeg。', percent=100.0, level='success')
        except Exception as e:
            from modules.ffmpeg_manager import get_windows_ffmpeg_manual_setup_message
            warning_msg = f'检查内置 FFmpeg 状态失败，请查看服务日志。{get_windows_ffmpeg_manual_setup_message()}'
            logger.warning("检查内置 FFmpeg 状态失败: %s", e)
            _append_settings_message(messages, 'warning', warning_msg)
            report('warning', 'FFmpeg 检查失败', warning_msg, level='warning')

        api_key = str(updated_config.get('YOUTUBE_API_KEY') or '').strip()
        api_ready, api_status = youtube_monitor.reload_api_client(updated_config)
        if api_key:
            if api_ready:
                youtube_monitor.start_all_schedules()
                if api_status == 'proxy_ready':
                    logger.info("YouTube监控 API 已重建并同步到监控系统，独立代理已启用")
                else:
                    logger.info("YouTube监控 API 已重建并同步到监控系统，当前为直连模式")
            else:
                youtube_monitor.stop_all_schedules()
                if api_status == 'missing_api_key':
                    warning_msg = 'YouTube API 密钥未配置，请先在设置页完成接入。'
                else:
                    warning_msg = 'YouTube监控 API 初始化失败，请检查 API 密钥、代理配置与网络连通性。'
                logger.warning(warning_msg)
                _append_settings_message(messages, 'warning', warning_msg)
        else:
            youtube_monitor.stop_all_schedules()
            logger.info("YouTube API密钥未配置，已跳过监控系统初始化")

        _append_settings_message(messages, 'success', '配置已成功保存')
        final_level = 'warning' if any(msg['category'] in ('warning', 'danger') for msg in messages) else 'success'
        final_stage = 'warning' if final_level == 'warning' else 'completed'
        final_message = '配置已保存，但有提醒需要处理。' if final_level == 'warning' else '配置已成功保存'
        final_detail = next((msg['text'] for msg in messages if msg['category'] in ('warning', 'danger')), '设置已生效。')
        return {
            'success': True,
            'messages': messages,
            'updated_config': updated_config,
            'final_stage': final_stage,
            'final_message': final_message,
            'final_detail': final_detail,
            'final_level': final_level,
        }
    except RecorderConfigError as e:
        logger.warning("保存设置校验失败: %s", e)
        public_message = str(e)
        _append_settings_message(messages, 'danger', public_message)
        return {
            'success': False,
            'messages': messages,
            'updated_config': None,
            'final_stage': 'failed',
            'final_message': '保存设置失败',
            'final_detail': public_message,
            'final_level': 'error',
        }
    except Exception as e:
        logger.exception("保存设置失败: %s", e)
        public_message = '保存设置失败，请查看服务日志。'
        _append_settings_message(messages, 'danger', public_message)
        return {
            'success': False,
            'messages': messages,
            'updated_config': None,
            'final_stage': 'failed',
            'final_message': '保存设置失败',
            'final_detail': public_message,
            'final_level': 'error',
        }


def _finalize_settings_save_operation(operation_id: str, result: dict):
    current_state = _get_settings_save_progress(operation_id) or _new_settings_save_state(operation_id)
    percent = current_state.get('percent')
    if result.get('success') and percent is None:
        percent = 100.0

    _update_settings_save_progress(
        operation_id,
        stage=result.get('final_stage', 'completed'),
        message=result.get('final_message', ''),
        detail=result.get('final_detail', ''),
        percent=percent,
        done=True,
        level=result.get('final_level', 'success'),
        success=result.get('success'),
        messages=result.get('messages', []),
    )


def _run_settings_save_operation(operation_id: str, form_data: dict, uploads: dict):
    result = _perform_settings_save(form_data, uploads, operation_id=operation_id)
    _finalize_settings_save_operation(operation_id, result)


# 登录验证装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        config = load_config()
        if config.get('password_protection_enabled'):
            if 'logged_in' not in session:
                flash('请先登录以访问此页面。', 'info')
                return redirect(url_for('login', next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function


def _verify_login_password(stored_password: str, submitted_password: str) -> tuple[bool, bool]:
    """Return (matched, legacy_plaintext) without leaking comparison timing."""
    stored = str(stored_password or '')
    submitted = str(submitted_password or '')
    if not stored or not submitted:
        return False, False
    if stored.startswith(('scrypt:', 'pbkdf2:')):
        try:
            return check_password_hash(stored, submitted), False
        except (TypeError, ValueError):
            return False, False
    return secrets.compare_digest(stored, submitted), True


def _normalize_admin_username(value) -> str:
    username = str(value or '').strip()
    if not re.fullmatch(r'[\w.@+-]{2,32}', username, flags=re.UNICODE):
        return ''
    return username


@app.route('/live-recording')
@login_required
def live_recording():
    rooms = live_recorder_manager.rooms_with_status()
    for room in rooms:
        try:
            _, reference_kind = live_recorder_manager.room_cover_reference(
                str(room.get('id') or '')
            )
        except RecorderConfigError:
            reference_kind = 'avatar'
        room['cover_reference_kind'] = reference_kind
        room['cover_reference_is_custom'] = reference_kind == 'custom'
    requested_room_uid = request.args.get('room', '').strip()
    selected_room = next(
        (
            room for room in rooms
            if requested_room_uid
            and str(room.get('uid') or '') == requested_room_uid
        ),
        None,
    )
    if requested_room_uid and selected_room is None:
        first_room_uid = str((rooms[0] if rooms else {}).get('uid') or '')
        return redirect(
            url_for('live_recording', **({'room': first_room_uid} if first_room_uid else {}))
        )
    selected_room_id = str(
        (selected_room or (rooms[0] if rooms else {})).get('id') or ''
    )
    recording_files = live_recorder_manager.recording_files(limit=500).get("files", [])
    config = load_config()
    return render_template(
        'live_recording.html',
        rooms=rooms,
        recording_files=recording_files,
        recorder_status=live_recorder_manager.status(),
        recorder_log=live_recorder_manager.tail_log(),
        recording_prompt_defaults=live_recorder_manager.recording_prompt_defaults(),
        recording_prompt_inherited=live_recorder_manager.effective_recording_prompt_defaults(config),
        selected_room_id=selected_room_id,
        bilibili_accounts=normalize_accounts(config),
        bilibili_default_account_id=default_account_id(config),
        danmaku_defaults={
            'duration': config.get('DANMAKU_DURATION_SECONDS', 10),
            'font_size': config.get('DANMAKU_FONT_SIZE', 42),
            'opacity': config.get('DANMAKU_OPACITY', 0.92),
            'encoder': config.get('DANMAKU_ENCODER', 'auto'),
            'preset': config.get('DANMAKU_ENCODE_PRESET', 'medium'),
            'quality': config.get('DANMAKU_ENCODE_QUALITY', 20),
        },
    )


def _live_recording_room_query(room_id: str) -> str:
    room = next(
        (
            item for item in live_recorder_manager.list_rooms()
            if str(item.get('id') or '') == str(room_id or '')
        ),
        None,
    )
    return live_recorder_manager.room_uid(room) if room else str(room_id or '')


@app.route('/live-recording/status')
@login_required
def live_recording_status():
    return jsonify(live_recorder_manager.live_status_payload())


@app.route('/api/encoding-capabilities')
@login_required
def encoding_capabilities():
    purpose = str(request.args.get('purpose') or 'danmaku').strip().lower()
    if purpose != 'danmaku':
        return jsonify({'error': '不支持的编码检测用途'}), 400
    try:
        project_root = Path(__file__).resolve().parent.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        from danmaku_pipeline import probe_encoding_capabilities
        from modules.youtube_handler import get_ffmpeg_path

        config = load_config()
        ffmpeg = get_ffmpeg_path(logger=logger) or 'ffmpeg'
        result = probe_encoding_capabilities(
            ffmpeg,
            preferred=str(config.get('VIDEO_ENCODER') or 'auto'),
            force_refresh=_coerce_checkbox_value(request.args.get('refresh', 'off')),
        )
        result['purpose'] = purpose
        result['ffmpeg'] = str(ffmpeg)
        result['configured_encoder'] = str(config.get('DANMAKU_ENCODER') or 'cpu')
        return jsonify(result)
    except Exception as exc:
        logger.exception("检测弹幕烧录编码器失败: %s", exc)
        return jsonify({'error': '编码器检测失败，已保留 CPU 安全方案'}), 500


@app.route('/live-recording/jobs')
@login_required
def live_recording_jobs():
    room_id = request.args.get('room_id', '').strip() or None
    return jsonify({'jobs': live_recorder_manager.pipeline_jobs(50, room_id=room_id)})


@app.route('/live-recording/jobs/<fingerprint>')
@login_required
def live_recording_job(fingerprint):
    job = live_recorder_manager.pipeline_job(fingerprint)
    if not job:
        return jsonify({'error': '没有找到该录播任务'}), 404
    job['log'] = live_recorder_manager.pipeline_log(fingerprint)
    return jsonify(job)


@app.route('/live-recording/jobs/<fingerprint>/cover')
@login_required
def live_recording_job_cover(fingerprint):
    try:
        path = live_recorder_manager.pipeline_cover(fingerprint, "16x9")
        return send_file(path, conditional=True)
    except RecorderConfigError as exc:
        return jsonify({'error': str(exc)}), 404


@app.route('/live-recording/jobs/<fingerprint>/cover/<variant>')
@login_required
def live_recording_job_cover_variant(fingerprint, variant):
    try:
        path = live_recorder_manager.pipeline_cover(fingerprint, variant)
        return send_file(path, conditional=True)
    except RecorderConfigError as exc:
        return jsonify({'error': str(exc)}), 404


@app.route('/live-recording/jobs/<fingerprint>/retry', methods=['POST'])
@login_required
def live_recording_job_retry(fingerprint):
    try:
        started = live_recorder_manager.retry_pipeline_job(fingerprint)
        if not started:
            return jsonify({
                'ok': False,
                'error': '任务状态已变化，可能已由其他请求开始重试。',
            }), 409
        return jsonify({'ok': True, 'message': '已开始重试，进度会自动刷新。'}), 202
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except OSError as exc:
        logger.error("启动录播任务重试进程失败：%s", exc)
        return jsonify({
            'ok': False,
            'error': '重试进程启动失败，任务已恢复为可重试状态。',
        }), 503


@app.route('/live-recording/jobs/<fingerprint>/pause', methods=['POST'])
@login_required
def live_recording_job_pause(fingerprint):
    try:
        paused = live_recorder_manager.pause_pipeline_job(fingerprint)
        if not paused:
            return jsonify({
                'ok': False,
                'error': '任务状态已变化，未能暂停。',
            }), 409
        return jsonify({
            'ok': True,
            'message': '录播任务已暂停，源文件和处理产物均已保留。',
        })
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/live-recording/jobs/<fingerprint>/delete', methods=['POST'])
@login_required
def live_recording_job_delete(fingerprint):
    delete_files = request.form.get('delete_files', 'false').lower() in ('true', 'yes', '1', 'on')
    try:
        result = live_recorder_manager.delete_pipeline_job(
            fingerprint,
            delete_files=delete_files,
        )
        cleanup_pending = result.get('cleanup_pending') or []
        flash(
            f"录播任务已删除"
            + (f"，同时删除 {result['deleted_file_count']} 个关联文件" if delete_files else "")
            + (f"；另有 {len(cleanup_pending)} 项暂存文件待系统清理" if cleanup_pending else ""),
            'warning' if cleanup_pending else 'success',
        )
    except RecorderConfigError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('tasks'))


@app.route('/live-recording/jobs/<fingerprint>/review-hold', methods=['POST'])
@login_required
def live_recording_job_review_hold(fingerprint):
    try:
        result = live_recorder_manager.request_pipeline_ai_review(fingerprint)
        return jsonify({
            'ok': True,
            **result,
            'review_url': url_for(
                'live_recording_job_review', fingerprint=fingerprint
            ),
            'message': (
                '已直接介入并暂停后续流程，可以重新编辑 AI 标题和简介。'
                if result.get('paused')
                else '稿件已经发布，可以在预览后同步新的标题和简介。'
            ),
        })
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/live-recording/jobs/<fingerprint>/review', methods=['GET', 'POST'])
@login_required
def live_recording_job_review(fingerprint):
    job = live_recorder_manager.pipeline_job(fingerprint)
    if not job:
        flash('没有找到该录播任务。', 'danger')
        return redirect(url_for('manual_review'))

    if request.method == 'POST':
        try:
            action = request.form.get('action', 'save').strip().lower()
            tags_submitted = 'tags_json' in request.form
            try:
                tags = json.loads(
                    request.form.get('tags_json', '[]')
                    if tags_submitted
                    else json.dumps(job.get('tags', []), ensure_ascii=False)
                )
            except json.JSONDecodeError:
                tags = []
            if not isinstance(tags, list):
                tags = []
            live_recorder_manager.save_pipeline_review(
                fingerprint,
                title=(
                    request.form.get('title', '')
                    if 'title' in request.form
                    else job.get('title', '')
                ),
                description=(
                    request.form.get('description', '')
                    if 'description' in request.form
                    else job.get('description', '')
                ),
                tags=tags if tags_submitted else job.get('tags', []),
                partition_id=(
                    request.form.get('partition_id', '')
                    if 'partition_id' in request.form
                    else job.get('partition_id', '')
                ),
                cover_file=request.files.get('cover_file'),
                cover43_file=request.files.get('cover43_file'),
            )
            published = bool(job.get('bvid'))
            regenerate_fields = {
                'regenerate_title': {'title'},
                'regenerate_description': {'description'},
                'regenerate_tags': {'tags'},
                'regenerate_cover_16x9': {'cover_16x9'},
                'regenerate_cover_4x3': {'cover_4x3'},
                'regenerate_all': {'title', 'description', 'tags'},
            }
            if action in regenerate_fields:
                regenerated = live_recorder_manager.regenerate_published_metadata(
                    fingerprint,
                    regenerate_fields[action],
                )
                field_names = {
                    'title': '标题',
                    'description': '简介',
                    'tags': '标签',
                    'cover_16x9': '16:9 封面',
                    'cover_4x3': '4:3 封面',
                }
                selected_names = '、'.join(
                    field_names[field]
                    for field in (
                        'title', 'description', 'tags', 'cover_16x9', 'cover_4x3'
                    )
                    if field in regenerate_fields[action]
                )
                cover_errors = regenerated.get('ai_cover_regeneration_errors') or []
                if cover_errors:
                    flash(
                        'AI 封面已按成功结果分别更新；生成失败的尺寸保留上一版：'
                        + '；'.join(str(error) for error in cover_errors),
                        'warning',
                    )
                else:
                    destination = '同步到 B站' if published else '继续封面和投稿流程'
                    sequence = '已先生成简介，再基于新简介生成标题。' if action == 'regenerate_all' else ''
                    flash(
                        f'AI 已重新生成{selected_names}。{sequence}'
                        f'请预览后再确认{destination}。',
                        'success',
                    )
                return redirect(url_for('live_recording_job_review', fingerprint=fingerprint))
            if action in {'apply_to_bilibili', 'apply_to_bilibili_and_comment'}:
                live_recorder_manager.update_published_metadata(fingerprint)
                if action == 'apply_to_bilibili_and_comment':
                    try:
                        comment_result = (
                            live_recorder_manager.sync_published_description_comment(
                                fingerprint
                            )
                        )
                        comment_action = (
                            '已创建并置顶'
                            if comment_result.get('action') == 'created'
                            else '已更新并重新置顶'
                        )
                        flash(
                            '稿件信息已同步到 B站，简介评论'
                            f'{comment_action}；视频与分P未改动。',
                            'success',
                        )
                    except RecorderConfigError as exc:
                        flash(
                            f'稿件信息已同步，但置顶评论同步失败：{exc}',
                            'warning',
                        )
                else:
                    flash('标题、简介、标签、分区和两种封面已同步到 B站，视频与分P未改动。', 'success')
                return redirect(url_for('live_recording_job_review', fingerprint=fingerprint))
            if action in {'save_and_continue', 'save_and_retry'}:
                live_recorder_manager.continue_pipeline_ai_review(fingerprint)
                flash('人工修改已确认，现在才开始生成封面并继续投稿。', 'success')
                return redirect(url_for('live_recording', job=fingerprint))
            flash('人工修改已保存。', 'success')
            return redirect(url_for('live_recording_job_review', fingerprint=fingerprint))
        except RecorderConfigError as exc:
            flash(str(exc), 'danger')

    job = live_recorder_manager.pipeline_job(fingerprint) or job
    return render_template(
        'recording_review_edit.html',
        job=job,
        bilibili_id_mapping=_build_bilibili_partition_mapping(),
    )


@app.route('/bilibili-archives')
@login_required
def bilibili_archives():
    accounts = live_recorder_manager.bilibili_archive_accounts()
    requested_account = str(request.args.get('account_id') or '').strip()
    account_ids = {str(account.get('id') or '') for account in accounts}
    selected_account_id = (
        requested_account if requested_account in account_ids
        else (str(accounts[0].get('id') or '') if accounts else '')
    )
    allowed_statuses = {'all', 'pubed', 'is_pubing', 'not_pubed'}
    selected_status = str(request.args.get('status') or 'pubed').strip()
    if selected_status not in allowed_statuses:
        selected_status = 'pubed'
    try:
        page = max(1, int(request.args.get('page') or 1))
    except (TypeError, ValueError):
        page = 1
    archives = {'archives': [], 'page': page, 'page_size': 20, 'total': 0}
    archive_error = ''
    message_overview = {'categories': {}}
    message_overview_error = ''
    if selected_account_id:
        try:
            archives = live_recorder_manager.bilibili_archives(
                selected_account_id,
                page=page,
                status=selected_status,
            )
        except RecorderConfigError as exc:
            archive_error = str(exc)
        try:
            message_overview = live_recorder_manager.bilibili_message_overview(
                selected_account_id,
            )
        except RecorderConfigError as exc:
            message_overview_error = str(exc)
    selected_bvid = str(request.args.get('bvid') or '').strip()
    selected_archive = None
    archive_comments = {'comments': [], 'page': 1, 'has_more': False}
    archive_comments_error = ''
    if selected_account_id and selected_bvid:
        try:
            selected_archive = live_recorder_manager.bilibili_archive_detail(
                selected_account_id,
                selected_bvid,
            )
        except RecorderConfigError as exc:
            archive_error = str(exc)
        if selected_archive:
            try:
                archive_comments = live_recorder_manager.bilibili_archive_comments(
                    selected_account_id,
                    selected_bvid,
                    aid=selected_archive.get('aid'),
                )
            except RecorderConfigError as exc:
                archive_comments_error = str(exc)
    return render_template(
        'bilibili_archives.html',
        accounts=accounts,
        selected_account_id=selected_account_id,
        selected_status=selected_status,
        archives=archives,
        archive_error=archive_error,
        selected_archive=selected_archive,
        replacement_videos=live_recorder_manager.burned_replacement_videos(200),
        replacement_jobs=live_recorder_manager.archive_replacement_jobs(30),
        bilibili_id_mapping=_build_bilibili_partition_mapping(),
        archive_comments=archive_comments,
        archive_comments_error=archive_comments_error,
        message_overview=message_overview,
        message_overview_error=message_overview_error,
    )


@app.route('/bilibili-archives/update', methods=['POST'])
@login_required
def bilibili_archive_update():
    account_id = str(request.form.get('account_id') or '').strip()
    bvid = str(request.form.get('bvid') or '').strip()
    tags = [
        tag.strip()
        for tag in str(request.form.get('tags') or '').replace('，', ',').split(',')
        if tag.strip()
    ]
    sync_pinned_comment = str(
        request.form.get('sync_pinned_comment') or ''
    ) == '1'
    metadata_updated = False
    try:
        live_recorder_manager.update_bilibili_archive_metadata(
            account_id=account_id,
            bvid=bvid,
            title=str(request.form.get('title') or '').strip(),
            description=str(request.form.get('description') or ''),
            tags=tags,
            partition_id=str(request.form.get('partition_id') or '').strip(),
        )
        metadata_updated = True
        if sync_pinned_comment:
            comment_result = (
                live_recorder_manager.sync_bilibili_archive_description_comment(
                    account_id=account_id,
                    bvid=bvid,
                    description=str(request.form.get('description') or ''),
                )
            )
            comment_action = (
                '已创建并置顶' if comment_result.get('action') == 'created'
                else '已更新并重新置顶'
            )
            flash(
                f'{bvid} 的稿件信息已同步，简介评论{comment_action}。',
                'success',
            )
        else:
            flash(f'{bvid} 的标题、简介、标签和分区已提交到 B站，视频与分P未改动。', 'success')
    except RecorderConfigError as exc:
        if metadata_updated:
            flash(f'稿件信息已更新，但置顶评论同步失败：{exc}', 'warning')
        else:
            flash(str(exc), 'danger')
    return redirect(url_for(
        'bilibili_archives',
        account_id=account_id,
        bvid=bvid,
    ))


@app.route('/bilibili-archives/reply', methods=['POST'])
@login_required
def bilibili_archive_reply():
    account_id = str(request.form.get('account_id') or '').strip()
    bvid = str(request.form.get('bvid') or '').strip()
    if str(request.form.get('confirm_reply') or '') != '1':
        flash('请先确认本次回复会立即发布到 B站。', 'danger')
    else:
        try:
            live_recorder_manager.reply_to_bilibili_archive_comment(
                account_id=account_id,
                bvid=bvid,
                root_rpid=str(request.form.get('root_rpid') or '').strip(),
                parent_rpid=str(request.form.get('parent_rpid') or '').strip(),
                message=str(request.form.get('message') or ''),
            )
            flash('评论回复已发布到 B站。', 'success')
        except RecorderConfigError as exc:
            flash(str(exc), 'danger')
    return redirect(url_for(
        'bilibili_archives',
        account_id=account_id,
        bvid=bvid,
    ))


@app.route('/bilibili-archives/delete', methods=['POST'])
@login_required
def bilibili_archive_delete():
    account_id = str(request.form.get('account_id') or '').strip()
    bvid = str(request.form.get('bvid') or '').strip()
    confirmation_bvid = str(
        request.form.get('confirmation_bvid') or ''
    ).strip()
    if not bvid or confirmation_bvid.lower() != bvid.lower():
        flash('删除确认失败：请输入完整且一致的 BVID。', 'danger')
        return redirect(url_for(
            'bilibili_archives',
            account_id=account_id,
            bvid=bvid,
        ))
    try:
        live_recorder_manager.delete_bilibili_archive(
            account_id=account_id,
            bvid=bvid,
        )
        flash(f'{bvid} 已从 B站永久删除。', 'success')
    except RecorderConfigError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for(
            'bilibili_archives',
            account_id=account_id,
            bvid=bvid,
        ))
    return redirect(url_for(
        'bilibili_archives',
        account_id=account_id,
    ))


@app.route('/bilibili-archives/replace', methods=['POST'])
@login_required
def bilibili_archive_replace():
    account_id = str(request.form.get('account_id') or '').strip()
    bvid = str(request.form.get('bvid') or '').strip()
    try:
        result = live_recorder_manager.start_archive_source_replacement(
            account_id=account_id,
            bvid=bvid,
            page_number=request.form.get('page_number'),
            file_id=str(request.form.get('file_id') or '').strip(),
            confirmation_bvid=str(request.form.get('confirmation_bvid') or '').strip(),
        )
        flash(
            f"{result['bvid']} 的 P{result['page_number']} 已进入换源队列，"
            f"视频：{result['video_name']}。",
            'success',
        )
    except RecorderConfigError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for(
        'bilibili_archives',
        account_id=account_id,
        bvid=bvid,
    ))


@app.route('/bilibili-archives/replacements')
@login_required
def bilibili_archive_replacements():
    public_jobs = []
    for job in live_recorder_manager.archive_replacement_jobs(30):
        public_jobs.append({
            key: job.get(key)
            for key in (
                'id', 'account_id', 'account_name', 'bvid', 'page_number',
                'video_name', 'status', 'progress', 'error', 'created_at',
                'updated_at', 'completed_at',
            )
        })
    return jsonify({
        'ok': True,
        'jobs': public_jobs,
    })


@app.route('/live-recording/files')
@login_required
def live_recording_files():
    return jsonify(live_recorder_manager.recording_files())


@app.route('/live-recording/files/open-folder', methods=['POST'])
@login_required
def live_recording_files_open_folder():
    if request.remote_addr not in {'127.0.0.1', '::1'}:
        return jsonify({
            'ok': False,
            'error': '打开文件夹仅支持在 PotatoFlow 所在电脑本机操作。',
        }), 403

    payload = request.get_json(silent=True) or {}
    file_id = str(payload.get('file_id') or '').strip()

    try:
        if file_id:
            file_path, _ = live_recorder_manager.recording_file(file_id)
            path = file_path.parent
        else:
            path = validate_recordings_dir(load_config().get('RECORDINGS_PATH', 'docker-data/recordings'))
        if sys.platform == 'win32':
            os.startfile(str(path))
        elif sys.platform == 'darwin':
            subprocess.Popen(
                ['open', str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
            subprocess.Popen(
                ['xdg-open', str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            return jsonify({
                'ok': False,
                'error': f'当前服务器没有可用的桌面文件管理器。录播目录：{path}',
            }), 409
    except (OSError, RecorderConfigError) as exc:
        return jsonify({'ok': False, 'error': f'无法打开录播文件夹：{exc}'}), 500

    message = '已打开文件所在文件夹。' if file_id else '已在系统文件管理器中打开录播文件夹。'
    return jsonify({'ok': True, 'message': message, 'path': str(path)})


@app.route('/live-recording/files/<file_id>/download')
@login_required
def live_recording_file_download(file_id):
    try:
        path, _ = live_recorder_manager.recording_file(file_id)
        return send_file(path, as_attachment=True, download_name=path.name, conditional=True)
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404


@app.route('/live-recording/files/<file_id>/cover')
@login_required
def live_recording_file_cover(file_id):
    try:
        path = live_recorder_manager.recording_cover(file_id)
        return send_file(path, conditional=True, max_age=300)
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404


@app.route('/live-recording/files/<file_id>/delete', methods=['POST'])
@login_required
def live_recording_file_delete(file_id):
    try:
        deleted = live_recorder_manager.delete_recording_file(file_id)
        return jsonify({'ok': True, 'message': '文件已删除。', 'file': deleted})
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/live-recording/files/batch-delete', methods=['POST'])
@login_required
def live_recording_files_batch_delete():
    payload = request.get_json(silent=True) or {}
    try:
        result = live_recorder_manager.delete_recording_files(payload.get('file_ids'))
        return jsonify({
            'ok': result['failed_count'] == 0,
            'message': f"已删除 {result['deleted_count']} 个文件。",
            **result,
        })
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/live-recording/rooms', methods=['POST'])
@login_required
def live_recording_save_room():
    try:
        room, reload_state = live_recorder_manager.add_room_from_url_and_reload(
            request.form.get('url', ''),
            segment_enabled=_coerce_checkbox_value(
                request.form.get('segment_enabled', 'off')
            ),
            segment_minutes=request.form.get('segment_minutes', '60'),
            multipart_enabled=_coerce_checkbox_value(
                request.form.get('multipart_enabled', 'off')
            ),
            record_only=_coerce_checkbox_value(
                request.form.get('record_only', 'off')
            ),
            danmaku_burn_in=_coerce_checkbox_value(
                request.form.get('danmaku_burn_in', 'off')
            ),
            recording_quality=request.form.get('recording_quality', 'source'),
            bilibili_account_id=request.form.get('bilibili_account_id', ''),
            bilibili_collection_id=request.form.get('bilibili_collection_id', ''),
        )
        room_name = str(room.get('name') or '直播间')
        if reload_state == 'reloaded':
            flash(f'已识别“{room_name}”，录制 worker 已自动重载。', 'success')
        elif reload_state == 'pending':
            flash(f'已识别“{room_name}”；当前录制结束后会自动重载 worker。', 'success')
        else:
            flash(f'已识别并添加“{room_name}”；录制与上传配置已同步。', 'success')
    except RecorderConfigError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('live_recording'))


@app.route('/live-recording/rooms/resolve', methods=['POST'])
@login_required
def live_recording_resolve_room():
    payload = request.get_json(silent=True) or request.form
    try:
        room = live_recorder_manager.resolve_room(str(payload.get('url') or ''))
        return jsonify({'ok': True, 'room': room})
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/live-recording/rooms/search', methods=['POST'])
@login_required
def live_recording_search_rooms():
    payload = request.get_json(silent=True) or request.form
    try:
        result = live_recorder_manager.search_rooms_with_diagnostics(
            str(payload.get('query') or '')
        )
        return jsonify({'ok': True, **result})
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/live-recording/rooms/<room_id>/delete', methods=['POST'])
@login_required
def live_recording_delete_room(room_id):
    try:
        state = live_recorder_manager.delete_room_and_reload(room_id)
        if state == 'reloaded':
            flash('直播间已删除，录制 worker 已自动重载。', 'success')
        elif state == 'pending':
            flash('直播间已删除；其他录制结束后会自动重载 worker。', 'success')
        elif state == 'stopped':
            flash('最后一个直播间已删除，录制引擎已停止。', 'success')
        elif state == 'deleted':
            flash('直播间已删除。', 'success')
        else:
            flash('没有找到该直播间。', 'warning')
    except RecorderConfigError as exc:
        flash(str(exc), 'warning')
    except (OSError, SystemError, subprocess.SubprocessError):
        app.logger.exception('删除直播间时发生系统错误：room_id=%s', room_id)
        flash('删除直播间时录制进程清理失败，请重试；已有录播文件和上传任务未删除。', 'danger')
    except Exception as exc:
        app.logger.exception('删除直播间时发生未预期错误：room_id=%s', room_id)
        detail = str(exc).strip() or type(exc).__name__
        flash(f'删除直播间失败：{detail}', 'danger')
    return redirect(url_for('live_recording'))


@app.route('/live-recording/rooms/<room_id>/prompts', methods=['POST'])
@login_required
def live_recording_room_prompts(room_id):
    try:
        reference_file = request.files.get('cover_reference_file')
        reference_suffix = ''
        if reference_file and str(getattr(reference_file, 'filename', '') or '').strip():
            reference_suffix = _validate_cover_upload(reference_file)
        room = live_recorder_manager.save_room_prompts(
            room_id,
            title_prompt=request.form.get('ai_title_prompt', ''),
            description_prompt=request.form.get('ai_description_prompt', ''),
            cover_prompt=request.form.get('ai_cover_prompt', ''),
            reaction_delay_seconds=request.form.get(
                'ai_danmaku_reaction_delay_seconds', '8'
            ),
            cover_reference_file=reference_file,
            cover_reference_suffix=reference_suffix,
            restore_cover_reference=(
                request.form.get('reference_action', '') == 'restore'
            ),
        )
        flash(
            f"“{room.get('name') or '直播间'}”的 AI 投稿设置已保存；"
            "新生成的封面会使用当前人物底稿。",
            'success',
        )
    except (RecorderConfigError, ValueError) as exc:
        flash(str(exc), 'danger')
    return redirect(
        url_for('live_recording', room=_live_recording_room_query(room_id))
    )


@app.route('/live-recording/rooms/<room_id>/recording-settings', methods=['POST'])
@login_required
def live_recording_room_recording_settings(room_id):
    try:
        room, reload_state = live_recorder_manager.save_room_recording_settings(
            room_id,
            segment_enabled=_coerce_checkbox_value(
                request.form.get('segment_enabled', 'off')
            ),
            segment_minutes=request.form.get('segment_minutes', '60'),
            multipart_enabled=_coerce_checkbox_value(
                request.form.get('multipart_enabled', 'off')
            ),
            record_only=_coerce_checkbox_value(
                request.form.get('record_only', 'off')
            ),
            danmaku_burn_in=_coerce_checkbox_value(
                request.form.get('danmaku_burn_in', 'off')
            ),
            danmaku_settings_inherit=_coerce_checkbox_value(
                request.form.get('danmaku_settings_inherit', 'off')
            ),
            danmaku_duration_seconds=request.form.get('danmaku_duration_seconds', '10'),
            danmaku_font_size=request.form.get('danmaku_font_size', '42'),
            danmaku_opacity=request.form.get('danmaku_opacity', '0.92'),
            danmaku_encoder=request.form.get('danmaku_encoder', 'cpu'),
            danmaku_encode_preset=request.form.get('danmaku_encode_preset', 'medium'),
            danmaku_encode_quality=request.form.get('danmaku_encode_quality', '20'),
            recording_quality=request.form.get('recording_quality', 'source'),
            bilibili_account_id=request.form.get('bilibili_account_id', ''),
            bilibili_collection_id=request.form.get('bilibili_collection_id', ''),
            recording_schedule_enabled=_coerce_checkbox_value(
                request.form.get('recording_schedule_enabled', 'off')
            ),
            recording_schedule_start=request.form.get(
                'recording_schedule_start', '00:00'
            ),
            recording_schedule_end=request.form.get(
                'recording_schedule_end', '23:59'
            ),
        )
        room_name = str(room.get('name') or '直播间')
        if reload_state == 'pending':
            flash(
                f'“{room_name}”的录制设置已保存；当前分段会安全收尾，'
                '随后自动重载并应用新设置。',
                'success',
            )
        elif reload_state == 'queued':
            flash(
                f'“{room_name}”的录制设置已保存；录制引擎空闲后自动应用。',
                'success',
            )
        else:
            flash(f'“{room_name}”的录制与分段设置已保存。', 'success')
    except RecorderConfigError as exc:
        flash(str(exc), 'danger')
    return redirect(
        url_for('live_recording', room=_live_recording_room_query(room_id))
    )


@app.route('/live-recording/rooms/<room_id>/cover-reference')
@login_required
def live_recording_room_cover_reference(room_id):
    try:
        path, kind = live_recorder_manager.room_cover_reference(room_id)
        if path is None or kind == 'avatar':
            raise RecorderConfigError("这个直播间当前使用直播间头像")
        return send_file(path, conditional=True)
    except RecorderConfigError as exc:
        return jsonify({'error': str(exc)}), 404


@app.route('/live-recording/rooms/<room_id>/recording', methods=['POST'])
@login_required
def live_recording_room_control(room_id):
    payload = request.get_json(silent=True) or request.form
    action = str(payload.get('action') or '').strip().lower()
    if action not in {'start', 'stop'}:
        return jsonify({'ok': False, 'error': '录制操作无效'}), 400
    try:
        room = live_recorder_manager.set_room_recording(room_id, action == 'start')
        message = '已开始检测直播，开播后立即录制。' if action == 'start' else '正在安全停止录制并收尾视频与弹幕文件。'
        return jsonify({'ok': True, 'message': message, 'room': room}), 202
    except RecorderConfigError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/live-recording/start', methods=['POST'])
@login_required
def live_recording_start():
    try:
        live_recorder_manager.start()
        flash('录制引擎已启动。', 'success')
    except RecorderConfigError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('live_recording'))


@app.route('/live-recording/stop', methods=['POST'])
@login_required
def live_recording_stop():
    live_recorder_manager.stop()
    flash('录制引擎已停止。', 'success')
    return redirect(url_for('live_recording'))

# 确保日志目录存在
log_dir = get_app_subdir('logs')
os.makedirs(log_dir, exist_ok=True)

# 配置日志
log_file = os.path.join(log_dir, 'app.log')
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 文件处理器
file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=10, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.WARNING)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.WARNING)
# 确保Windows控制台编码正确
if os.name == 'nt':
    import sys
    import codecs
    
    # 强制设置stdout和stderr为UTF-8编码
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')  # type: ignore
        except Exception:
            pass
        
    if hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stderr.reconfigure(encoding='utf-8')  # type: ignore
        except Exception:
            pass
    
    # 设置环境变量
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 为控制台处理器设置编码
    try:
        console_handler.setStream(codecs.getwriter('utf-8')(sys.stdout.buffer))  # type: ignore
    except Exception:
        pass

# 配置根日志记录器
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# 强制设置所有日志记录器的默认编码为UTF-8
try:
    logging.getLogger().handlers[0].encoding = 'utf-8'  # type: ignore
except Exception:
    pass

try:
    if len(logging.getLogger().handlers) > 1:
        logging.getLogger().handlers[1].encoding = 'utf-8'  # type: ignore
except Exception:
    pass

# 配置应用日志记录器
logger = logging.getLogger('PotatoFlow')
logger.setLevel(logging.WARNING)

# 模板辅助函数
def task_status_display(status):
    """将任务状态代码转换为显示文本"""
    status_map = {
        TASK_STATES['PENDING']: '等待处理',
        TASK_STATES['DOWNLOADING']: '下载中',
        TASK_STATES['DOWNLOADED']: '下载完成',
        TASK_STATES['TRANSLATING_SUBTITLE']: '翻译字幕中',
    TASK_STATES['ASR_TRANSCRIBING']: '语音转写中',
        TASK_STATES['ENCODING_VIDEO']: '转码视频中',
        TASK_STATES['TRANSLATING']: '翻译中',
        TASK_STATES['TAGGING']: '生成标签中',
        TASK_STATES['PARTITIONING']: '推荐分区中',
        TASK_STATES['MODERATING']: '内容审核中',
        TASK_STATES['AWAITING_REVIEW']: '等待人工审核',
        TASK_STATES['READY_FOR_UPLOAD']: '准备上传',
        TASK_STATES['UPLOADING']: '上传中',
        TASK_STATES['PAUSED']: '已暂停',
        TASK_STATES['COMPLETED']: '已完成',
        TASK_STATES['FAILED']: '失败',
        'fetching_info': '采集信息中',
        'info_fetched': '信息已采集',
    }
    return status_map.get(status, status)

def task_status_color(status):
    """将任务状态代码转换为显示颜色"""
    color_map = {
        TASK_STATES['PENDING']: 'secondary',
        TASK_STATES['DOWNLOADING']: 'info',
        TASK_STATES['DOWNLOADED']: 'info',
        TASK_STATES['TRANSLATING_SUBTITLE']: 'info',
    TASK_STATES['ASR_TRANSCRIBING']: 'info',
        TASK_STATES['ENCODING_VIDEO']: 'info',
        TASK_STATES['TRANSLATING']: 'info',
        TASK_STATES['TAGGING']: 'info',
        TASK_STATES['PARTITIONING']: 'info',
        TASK_STATES['MODERATING']: 'info',
        TASK_STATES['AWAITING_REVIEW']: 'warning',
        TASK_STATES['READY_FOR_UPLOAD']: 'primary',
        TASK_STATES['UPLOADING']: 'primary',
        TASK_STATES['PAUSED']: 'secondary',
        TASK_STATES['COMPLETED']: 'success',
        TASK_STATES['FAILED']: 'danger'
    }
    return color_map.get(status, 'secondary')


@app.context_processor
def inject_task_lifecycle_helpers():
    return {'task_capabilities': youtube_task_capabilities}

def _get_bilibili_zone_data():
    from modules.bilibili_zones import get_zone_list_sub
    return get_zone_list_sub()


def _build_bilibili_partition_mapping():
    id_mapping = []
    zone_data = _get_bilibili_zone_data()
    for parent in zone_data:
        if not isinstance(parent, dict):
            continue
        parent_tid = parent.get('tid')
        parent_name = parent.get('name')
        if parent_tid in (None, 0, '0') or not parent_name:
            continue
        id_mapping.append({
            'category': parent_name,
            'partitions': [{
                'id': str(parent_tid),
                'name': parent_name,
                'sub_partitions': [
                    {
                        'id': str(sub.get('tid')),
                        'name': sub.get('name'),
                    }
                    for sub in (parent.get('sub') or [])
                    if isinstance(sub, dict) and sub.get('tid') not in (None, 0, '0') and sub.get('name')
                ]
            }]
        })
    return id_mapping


def _build_bilibili_partition_name_map():
    """Return a flat partition-id to display-name map for dynamic task details."""
    names = {}
    for parent in _get_bilibili_zone_data():
        if not isinstance(parent, dict):
            continue
        for partition in (parent, *(parent.get('sub') or [])):
            if not isinstance(partition, dict):
                continue
            partition_id = partition.get('tid')
            partition_name = str(partition.get('name') or '').strip()
            if partition_id not in (None, 0, '0') and partition_name:
                names[str(partition_id)] = partition_name
    return names


def get_partition_name(partition_id, upload_target='bilibili'):
    """根据 Bilibili 分区 ID 获取名称。"""
    if not partition_id:
        return None

    pid = str(partition_id)
    try:
        zone_data = _get_bilibili_zone_data()
        for parent in zone_data:
            if str(parent.get('tid')) == pid:
                return parent.get('name')
            for sub in parent.get('sub', []) or []:
                if str(sub.get('tid')) == pid:
                    return sub.get('name')
    except Exception as e:
        logger.error(f"获取bilibili分区名称时出错: {str(e)}")
    return None

def parse_json(json_str):
    """将JSON字符串解析为Python对象"""
    if not json_str:
        return {}  # 返回空字典
    
    try:
        return json.loads(json_str)
    except Exception as e:
        logger.error(f"解析JSON时出错: {str(e)}")
        return {} # 返回空字典

def parse_youtube_duration(duration_str):
    """解析YouTube ISO 8601时长格式为秒数"""
    import re
    
    if not duration_str:
        return 0
    
    # PT1H30M45S -> 1小时30分45秒
    pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
    match = re.match(pattern, duration_str)
    
    if not match:
        return 0
    
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    
    return hours * 3600 + minutes * 60 + seconds

# 注册模板过滤器
app.jinja_env.filters['parse_youtube_duration'] = parse_youtube_duration

ALIYUN_LABEL_MAP = {
    "pornographic_adult": "疑似色情内容",
    "sexual_terms": "疑似性健康内容",
    "sexual_suggestive": "疑似低俗内容",
    "political_figure": "疑似政治人物",
    "political_entity": "疑似政治实体",
    "political_n": "疑似敏感政治内容",
    "political_p": "疑似涉政禁宣人物",
    "political_a": "涉政专项升级保障",
    "violent_extremist": "疑似极端组织",
    "violent_incidents": "疑似极端主义内容",
    "violent_weapons": "疑似武器弹药",
    "contraband_drug": "疑似毒品相关",
    "contraband_gambling": "疑似赌博相关",
    "contraband_act": "疑似违禁行为",
    "contraband_entity": "疑似违禁工具",
    "inappropriate_discrimination": "疑似偏见歧视内容",
    "inappropriate_ethics": "疑似不良价值观内容",
    "inappropriate_profanity": "疑似攻击辱骂内容",
    "inappropriate_oral": "疑似低俗口头语内容",
    "inappropriate_superstition": "疑似封建迷信内容",
    "inappropriate_nonsense": "疑似无意义灌水内容",
    "pt_to_sites": "疑似站外引流",
    "pt_by_recruitment": "疑似网赚兼职广告",
    "pt_to_contact": "疑似引流广告号",
    "religion_b": "疑似涉及佛教",
    "religion_t": "疑似涉及道教",
    "religion_c": "疑似涉及基督教",
    "religion_i": "疑似涉及伊斯兰教",
    "religion_h": "疑似涉及印度教",
    "customized": "命中自定义词库",
    "nonLabel": "内容正常", # 通常表示无风险
    "normal": "内容正常" # 另一种表示无风险的标签
    # 可以根据需要添加更多映射
}

def get_aliyun_label_chinese(label_value):
    """获取阿里云审核标签的中文含义"""
    return ALIYUN_LABEL_MAP.get(label_value, label_value) # 如果找不到映射，返回原始标签

# 注册模板辅助函数
app.jinja_env.globals.update(
    task_status_display=task_status_display,
    task_status_color=task_status_color,
    get_partition_name=get_partition_name,
    parse_json=parse_json,
    get_aliyun_label_chinese=get_aliyun_label_chinese # 添加新的辅助函数
)

@app.route('/login', methods=['GET', 'POST'])
def login():
    config = load_config()
    # 如果管理员账号登录未启用，或已登录，则重定向到首页
    if not config.get('password_protection_enabled'):
        return redirect(url_for('index'))
    if 'logged_in' in session:
        return redirect(url_for('index'))

    # 读取登录安全状态
    sec = _load_security_state()
    now_ts = time.time()
    # 检查是否处于锁定期
    if sec.get('locked_until', 0) and now_ts < sec['locked_until']:
        remaining = int(sec['locked_until'] - now_ts)
        minutes = remaining // 60
        seconds = remaining % 60
        flash(f'登录已被临时锁定，请 {minutes} 分 {seconds} 秒后重试。', 'danger')
        return render_template('login.html')

    if request.method == 'POST':
        username = _normalize_admin_username(request.form.get('username'))
        password = request.form.get('password')
        stored_username = _normalize_admin_username(
            config.get('admin_username') or 'admin'
        ) or 'admin'
        stored_password = config.get('password')

        # 检查是否已完成管理员账号设置
        if not stored_password:
            flash('系统尚未设置管理员密码，请先在设置页面完成管理员账号设置。', 'danger')
            return render_template('login.html')

        password_matches, legacy_plaintext = _verify_login_password(stored_password, password)
        username_matches = bool(username) and secrets.compare_digest(
            stored_username,
            username,
        )
        if username_matches and password_matches:
            if legacy_plaintext:
                try:
                    update_config({'password': generate_password_hash(str(password))})
                    logger.info('登录密码已自动迁移为安全哈希')
                except Exception:
                    logger.exception('迁移旧版登录密码失败')
            session['logged_in'] = True
            session['admin_username'] = stored_username
            session.permanent = True  # session持久化
            # 登录成功，重置失败计数与锁定
            sec.update({'failed_attempts': 0, 'locked_until': 0, 'last_attempt': now_ts})
            _save_security_state(sec)
            _emit_login_event(
                EVENT_LOGIN_SUCCESS,
                {
                    'ip_address': _get_request_ip_address(),
                    'username': stored_username,
                }
            )
            flash('登录成功', 'success')
            return redirect(url_for('index'))
        else:
            # 密码错误，更新失败计数
            max_attempts = int(config.get('LOGIN_MAX_FAILED_ATTEMPTS', 5) or 5)
            lock_minutes = int(config.get('LOGIN_LOCKOUT_MINUTES', 15) or 15)
            failed = int(sec.get('failed_attempts', 0) or 0) + 1
            sec['failed_attempts'] = failed
            sec['last_attempt'] = now_ts
            # 达到阈值则锁定
            if failed >= max_attempts:
                sec['locked_until'] = now_ts + lock_minutes * 60
                _save_security_state(sec)
                _emit_login_event(
                    EVENT_LOGIN_LOCKED,
                    {
                        'ip_address': _get_request_ip_address(),
                        'failed_attempts': failed,
                        'max_attempts': max_attempts,
                        'lock_minutes': lock_minutes,
                    }
                )
                flash(f'账号或密码错误次数过多（{failed}/{max_attempts}），已锁定 {lock_minutes} 分钟。', 'danger')
            else:
                _save_security_state(sec)
                remain = max_attempts - failed
                flash(f'账号或密码错误。还可尝试 {remain} 次后将被锁定。', 'danger')
    
    return render_template('login.html')


@app.route('/admin/avatar')
def admin_avatar():
    avatar_path = _admin_avatar_file()
    if not avatar_path.is_file():
        return '', 404
    return send_file(avatar_path, mimetype='image/png', conditional=True, max_age=300)

@app.route('/logout')
def logout():
    session.clear()
    flash('您已成功退出。', 'info')
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    """首页"""
    logger.info("访问首页")
    # 统计信息用于仪表盘
    try:
        from modules.task_manager import get_db_connection
        config = load_config()
        conn = get_db_connection()
        cur = conn.cursor()

        # 本地时间的今日起止
        now_local = datetime.now()
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        fmt = "%Y-%m-%d %H:%M:%S"
        start_str = today_start.strftime(fmt)
        end_str = tomorrow_start.strftime(fmt)

        # 各类计数
        cur.execute("SELECT COUNT(*) FROM tasks")
        total_tasks = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN (?, ?)",
            (TASK_STATES['AWAITING_REVIEW'], TASK_STATES['FAILED'])
        )
        awaiting_review = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['FAILED'],))
        failed_total = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['PENDING'],))
        pending_total = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['READY_FOR_UPLOAD'],))
        ready_total = cur.fetchone()[0] or 0

        # 进行中的状态集合
        processing_states = (
            'fetching_info', 'info_fetched',
            TASK_STATES['TRANSLATING'], TASK_STATES['TAGGING'], TASK_STATES['PARTITIONING'],
            TASK_STATES['MODERATING'], TASK_STATES['DOWNLOADING'], TASK_STATES['DOWNLOADED'],
            TASK_STATES['ASR_TRANSCRIBING'], TASK_STATES['TRANSLATING_SUBTITLE'],
            TASK_STATES['ENCODING_VIDEO'], TASK_STATES['UPLOADING']
        )
        placeholders = ",".join(["?"] * len(processing_states))
        cur.execute(f"SELECT COUNT(*) FROM tasks WHERE status IN ({placeholders})", processing_states)
        in_progress = cur.fetchone()[0] or 0

        # 今日完成/失败/新增任务
        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ? AND updated_at < ?",
            (TASK_STATES['COMPLETED'], start_str, end_str)
        )
        completed_today = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ? AND updated_at < ?",
            (TASK_STATES['FAILED'], start_str, end_str)
        )
        failed_today = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND created_at < ?",
            (start_str, end_str)
        )
        created_today = cur.fetchone()[0] or 0

        # 最近任务（按更新时间倒序）
        cur.execute(
            """SELECT id, display_id, video_title_translated, video_title_original,
                      status, updated_at, bilibili_upload_response
               FROM tasks
               ORDER BY updated_at DESC
               LIMIT 10"""
        )
        rows = cur.fetchall()
        recent_tasks = []
        for r in rows:
            upload_id = None
            try:
                resp = json.loads(r[6]) if r[6] else None
                if isinstance(resp, dict):
                    upload_id = resp.get('bvid') or resp.get('aid')
            except Exception:
                upload_id = None
            recent_tasks.append({
                'id': r[0],
                'display_id': r[1] or r[0],
                'title': r[2] or r[3] or '未获取标题',
                'status': r[4],
                'updated_at': r[5],
                'upload_target': 'bilibili',
                'upload_id': upload_id,
                'source': 'youtube',
            })

        conn.close()

        monitor_configs = youtube_monitor.get_monitor_configs()
        youtube_summary = {
            'monitor_total': len(monitor_configs),
            'monitor_enabled': sum(bool(config.get('enabled')) for config in monitor_configs),
            'queued': pending_total + ready_total,
            'processing': in_progress,
            'review': awaiting_review,
            'failed': failed_total,
            'completed_today': completed_today,
        }

        recording_jobs = live_recorder_manager.pipeline_jobs(100)
        recording_rooms = live_recorder_manager.rooms_with_status()
        recorder_status = live_recorder_manager.status()
        recording_status_map = {
            'completed': TASK_STATES['COMPLETED'],
            'failed': TASK_STATES['FAILED'],
            'dry_run': TASK_STATES['READY_FOR_UPLOAD'],
            'processing': TASK_STATES['UPLOADING'],
            'video_uploaded': TASK_STATES['UPLOADING'],
        }

        def recording_local_time(value):
            try:
                parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                return parsed
            except (TypeError, ValueError):
                return datetime.min

        recording_today = [
            job for job in recording_jobs
            if today_start <= recording_local_time(job.get('created_at')) < tomorrow_start
        ]
        recording_updated_today = [
            job for job in recording_jobs
            if today_start <= recording_local_time(job.get('updated_at')) < tomorrow_start
        ]
        recording_failed = sum(job.get('status') == 'failed' for job in recording_jobs)
        recording_processing = sum(
            job.get('status') in {'processing', 'video_uploaded'} for job in recording_jobs
        )
        recording_completed_today = sum(
            job.get('status') == 'completed' for job in recording_updated_today
        )
        recording_summary = {
            'room_total': len(recording_rooms),
            'room_enabled': sum(bool(room.get('enabled', True)) for room in recording_rooms),
            'recording_now': sum(
                bool((room.get('runtime') or {}).get('recording'))
                for room in recording_rooms
            ),
            'engine_running': bool(recorder_status.get('running')),
            'processing': recording_processing,
            'review': recording_failed,
            'failed': recording_failed,
            'completed_today': recording_completed_today,
        }
        total_tasks += len(recording_jobs)
        awaiting_review += sum(job.get('status') == 'failed' for job in recording_jobs)
        failed_total += recording_failed
        ready_total += sum(job.get('status') == 'dry_run' for job in recording_jobs)
        in_progress += recording_processing
        completed_today += recording_completed_today
        failed_today += sum(job.get('status') == 'failed' for job in recording_updated_today)
        created_today += len(recording_today)
        recent_tasks.extend({
            'id': job['id'],
            'display_id': job.get('display_id') or job['id'],
            'title': job.get('title') or job.get('video_name') or '直播录播',
            'status': recording_status_map.get(job.get('status'), TASK_STATES['PENDING']),
            'updated_at': job.get('updated_at'),
            'upload_target': 'bilibili',
            'upload_id': job.get('bvid') or None,
            'source': 'recording',
            '_sort_time': recording_local_time(job.get('updated_at')),
        } for job in recording_jobs[:10])
        for task in recent_tasks:
            task.setdefault('_sort_time', recording_local_time(task.get('updated_at')))
        recent_tasks = sorted(
            recent_tasks,
            key=lambda task: task['_sort_time'],
            reverse=True,
        )[:10]

        stats = {
            'total_tasks': total_tasks,
            'awaiting_review': awaiting_review,
            'failed_total': failed_total,
            'pending_total': pending_total,
            'ready_total': ready_total,
            'in_progress': in_progress,
            'completed_today': completed_today,
            'failed_today': failed_today,
            'created_today': created_today
        }
    except Exception as e:
        logger.warning(f"首页统计失败: {e}")
        stats = {
            'total_tasks': 0,
            'awaiting_review': 0,
            'failed_total': 0,
            'pending_total': 0,
            'ready_total': 0,
            'in_progress': 0,
            'completed_today': 0,
            'failed_today': 0,
            'created_today': 0
        }
        recent_tasks = []
        youtube_summary = {
            'monitor_total': 0,
            'monitor_enabled': 0,
            'queued': 0,
            'processing': 0,
            'review': 0,
            'failed': 0,
            'completed_today': 0,
        }
        recording_summary = {
            'room_total': 0,
            'room_enabled': 0,
            'recording_now': 0,
            'engine_running': False,
            'processing': 0,
            'review': 0,
            'failed': 0,
            'completed_today': 0,
        }
        config = load_config()

    return render_template(
        'index.html',
        stats=stats,
        recent_tasks=recent_tasks,
        youtube_summary=youtube_summary,
        recording_summary=recording_summary,
    )

@app.route('/tasks')
@login_required
def tasks():
    """任务列表页面"""
    logger.info("访问任务列表页面")
    
    # 获取分页参数
    page = request.args.get('page', 1, type=int)
    per_page = 20  # 每页显示20条记录
    queue_filter = normalize_queue_filter(request.args.get('status'))
    source_filter = normalize_source_filter(request.args.get('source'))
    recording_page = request.args.get('recording_page', 1, type=int)
    recording_keyword = str(request.args.get('recording_q') or '').strip()[:120]
    recording_room = str(request.args.get('recording_room') or 'all').strip()[:160]
    recording_type = normalize_recording_type_filter(request.args.get('recording_type'))
    recording_time = normalize_recording_time_filter(request.args.get('recording_time'))

    config = load_config()
    all_youtube_tasks = get_all_tasks()
    for task in all_youtube_tasks:
        _decorate_youtube_task_for_view(task, config)
    all_recording_jobs = live_recorder_manager.pipeline_jobs(None)
    queue_summary = build_queue_summary(all_youtube_tasks, all_recording_jobs)
    room_options = recording_room_options(all_recording_jobs)
    allowed_rooms = {'all', *(item['value'] for item in room_options)}
    if recording_room not in allowed_rooms:
        recording_room = 'all'

    youtube_tasks = (
        filter_queue_items(all_youtube_tasks, queue_filter, youtube_queue_bucket)
        if source_filter in {'all', 'youtube'}
        else []
    )
    status_filtered_recording_jobs = (
        filter_queue_items(all_recording_jobs, queue_filter, recording_queue_bucket)
        if source_filter in {'all', 'recording'}
        else []
    )
    filtered_recording_jobs = filter_recording_jobs(
        status_filtered_recording_jobs,
        keyword=recording_keyword,
        room=recording_room,
        task_type=recording_type,
        time_range=recording_time,
    )
    recording_pagination = paginate_items(
        filtered_recording_jobs,
        page=recording_page,
        per_page=per_page,
    )
    recording_jobs = recording_pagination['tasks']
    pagination_data = paginate_items(youtube_tasks, page=page, per_page=per_page)
    return render_template('tasks.html', 
                         tasks=pagination_data['tasks'],
                         recording_jobs=recording_jobs,
                         pagination=pagination_data,
                         config=config,
                         queue_summary=queue_summary,
                         queue_filter=queue_filter,
                         source_filter=source_filter,
                         recording_keyword=recording_keyword,
                         recording_room=recording_room,
                         recording_type=recording_type,
                         recording_time=recording_time,
                         recording_room_options=room_options,
                         recording_pagination=recording_pagination,
                         bilibili_partition_names=_build_bilibili_partition_name_map(),
                         bilibili_accounts=normalize_accounts(config),
                         bilibili_default_account_id=default_account_id(config))


def _youtube_pipeline_stages(config: dict) -> list[str]:
    """Return the stages that are visible for a YouTube/manual task."""
    stages = [PIPELINE_STAGE_FETCH_INFO]
    if _coerce_checkbox_value(config.get('TRANSLATE_TITLE', True)) or _coerce_checkbox_value(config.get('TRANSLATE_DESCRIPTION', True)):
        stages.append(PIPELINE_STAGE_TRANSLATE_CONTENT)
    if _coerce_checkbox_value(config.get('GENERATE_TAGS', True)):
        stages.append(PIPELINE_STAGE_GENERATE_TAGS)
    if _coerce_checkbox_value(config.get('RECOMMEND_PARTITION', False)):
        stages.append(PIPELINE_STAGE_RECOMMEND_PARTITION)
    if _coerce_checkbox_value(config.get('CONTENT_MODERATION_ENABLED', False)):
        stages.append(PIPELINE_STAGE_MODERATE_CONTENT)
    stages.append(PIPELINE_STAGE_DOWNLOAD_VIDEO)
    if _coerce_checkbox_value(config.get('SUBTITLE_TRANSLATION_ENABLED', False)) or _coerce_checkbox_value(config.get('SUBTITLE_EMBED_IN_VIDEO', True)):
        stages.append(PIPELINE_STAGE_TRANSLATE_SUBTITLE)
    stages.extend((PIPELINE_STAGE_COVER_PRECHECK, PIPELINE_STAGE_COVER_UPLOAD))
    stages.append(PIPELINE_STAGE_UPLOAD)
    return stages


def _decorate_youtube_task_for_view(task: dict, config: dict) -> dict:
    """Attach account and compact pipeline presentation fields to a task."""
    account = resolve_account(config, task.get('bilibili_account_id'))
    task['bilibili_account_name'] = account['name']
    task['bilibili_account_uid'] = account.get('bilibili_uid', '')
    task['bilibili_account_avatar_url'] = account.get('avatar_url', '')

    visible_stages = _youtube_pipeline_stages(config)
    completed_stages = _get_completed_stages(task)
    completed_count = len(set(visible_stages) & set(completed_stages))
    total_stages = max(1, len(visible_stages))
    if task.get('status') == TASK_STATES['COMPLETED']:
        completed_count = total_stages

    task['completed_stages'] = completed_count
    task['total_stages'] = total_stages
    task['progress_percent'] = min(100, (completed_count * 100) // total_stages)
    task['cover_available'] = _task_cover_available(task)
    try:
        checkpoint = json.loads(task.get('pipeline_checkpoint') or '{}')
    except (TypeError, ValueError):
        checkpoint = {}
    raw_stage_status = checkpoint.get('stage_status', {}) if isinstance(checkpoint, dict) else {}
    if not isinstance(raw_stage_status, dict):
        raw_stage_status = {}
    cover_stage_status = []
    for key, label in (
        (PIPELINE_STAGE_COVER_PRECHECK, '封面预检'),
        (PIPELINE_STAGE_COVER_UPLOAD, '封面上传'),
    ):
        stage = raw_stage_status.get(key, {})
        if not isinstance(stage, dict):
            stage = {}
        status = str(stage.get('status') or '')
        if not status:
            status = 'completed' if task.get('status') == TASK_STATES['COMPLETED'] else 'pending'
        cover_stage_status.append({
            'key': key,
            'label': label,
            'status': status,
            'message': str(stage.get('message') or ''),
            'details': stage.get('details') if isinstance(stage.get('details'), dict) else {},
        })
    task['cover_stage_status'] = cover_stage_status
    task['progress_label'] = {
        TASK_STATES['COMPLETED']: '全部处理完成',
        TASK_STATES['FAILED']: '处理失败',
        TASK_STATES['AWAITING_REVIEW']: '等待人工审核',
        TASK_STATES['READY_FOR_UPLOAD']: '等待上传',
        TASK_STATES['PAUSED']: '已暂停',
    }.get(task.get('status'), task_status_display(task.get('status')))
    active_cover_stage = next(
        (
            stage for stage in cover_stage_status
            if stage['status'] in {'running', 'failed'}
        ),
        None,
    )
    if active_cover_stage:
        task['progress_label'] = (
            f"{active_cover_stage['label']}失败"
            if active_cover_stage['status'] == 'failed'
            else f"正在{active_cover_stage['label']}"
        )
    return task


YOUTUBE_PIPELINE_STAGE_META = {
    PIPELINE_STAGE_FETCH_INFO: ('读取视频信息', '读取标题、简介、封面和视频元数据', 'bi-card-text'),
    PIPELINE_STAGE_TRANSLATE_CONTENT: ('翻译标题与简介', '生成适合投稿的中文标题和简介', 'bi-translate'),
    PIPELINE_STAGE_GENERATE_TAGS: ('生成投稿标签', '根据视频内容生成 B站投稿标签', 'bi-tags'),
    PIPELINE_STAGE_RECOMMEND_PARTITION: ('推荐投稿分区', '分析内容并选择合适的 B站分区', 'bi-diagram-3'),
    PIPELINE_STAGE_MODERATE_CONTENT: ('检查投稿内容', '检查标题、简介和标签是否需要人工确认', 'bi-shield-check'),
    PIPELINE_STAGE_DOWNLOAD_VIDEO: ('下载视频文件', '下载并检查待投稿的视频文件', 'bi-download'),
    PIPELINE_STAGE_TRANSLATE_SUBTITLE: ('处理字幕与视频', '翻译字幕，并按配置完成字幕嵌入或视频处理', 'bi-badge-cc'),
    PIPELINE_STAGE_COVER_PRECHECK: ('封面预检', '检查封面尺寸、格式和上传可用性', 'bi-image'),
    PIPELINE_STAGE_COVER_UPLOAD: ('封面上传', '上传并确认 B站投稿封面', 'bi-cloud-arrow-up'),
    PIPELINE_STAGE_UPLOAD: ('投稿到 B站', '上传视频与投稿信息，并取得稿件 BV 号', 'bi-send-check'),
}


def _youtube_pipeline_stage_details(task: dict, config: dict) -> list[dict]:
    """Build the authenticated task-detail timeline used by the tasks page."""
    visible_stages = _youtube_pipeline_stages(config)
    completed_stages = _get_completed_stages(task)
    try:
        checkpoint = json.loads(task.get('pipeline_checkpoint') or '{}')
    except (TypeError, ValueError):
        checkpoint = {}
    raw_statuses = checkpoint.get('stage_status', {}) if isinstance(checkpoint, dict) else {}
    if not isinstance(raw_statuses, dict):
        raw_statuses = {}

    active_stage_by_status = {
        'fetching_info': PIPELINE_STAGE_FETCH_INFO,
        'translating': PIPELINE_STAGE_TRANSLATE_CONTENT,
        'tagging': PIPELINE_STAGE_GENERATE_TAGS,
        'partitioning': PIPELINE_STAGE_RECOMMEND_PARTITION,
        'moderating': PIPELINE_STAGE_MODERATE_CONTENT,
        'downloading': PIPELINE_STAGE_DOWNLOAD_VIDEO,
        'asr_transcribing': PIPELINE_STAGE_TRANSLATE_SUBTITLE,
        'translating_subtitle': PIPELINE_STAGE_TRANSLATE_SUBTITLE,
        'encoding_video': PIPELINE_STAGE_TRANSLATE_SUBTITLE,
        'uploading': PIPELINE_STAGE_UPLOAD,
    }
    active_stage = active_stage_by_status.get(str(task.get('status') or ''))
    stages = []
    for key in visible_stages:
        raw = raw_statuses.get(key, {})
        raw = raw if isinstance(raw, dict) else {}
        status = str(raw.get('status') or '')
        if not status:
            if task.get('status') == TASK_STATES['COMPLETED'] or key in completed_stages:
                status = 'completed'
            elif key == active_stage:
                status = 'running'
            else:
                status = 'pending'
        label, description, icon = YOUTUBE_PIPELINE_STAGE_META[key]
        stages.append({
            'key': key,
            'label': label,
            'description': description,
            'icon': icon,
            'status': status,
            'message': str(raw.get('message') or ''),
            'details': raw.get('details') if isinstance(raw.get('details'), dict) else {},
            'updated_at': raw.get('updated_at'),
        })
    return stages


def _render_task_fragments(task: dict, config: dict | None = None) -> dict:
    if config is None:
        config = load_config()
    task = _decorate_youtube_task_for_view(dict(task), config)

    return {
        'task_id': task.get('id'),
        'row_html': render_template('partials/task_row.html', task=task, config=config),
        'card_html': render_template('partials/task_card.html', task=task, config=config),
    }


@app.route('/tasks/<task_id>/fragment')
@login_required
def task_fragment(task_id):
    """返回单个任务的桌面/移动端片段 HTML。"""
    task = get_task(task_id)
    if not task:
        return jsonify({'success': False, 'message': '任务不存在'}), 404

    config = load_config()
    return jsonify({
        'success': True,
        **_render_task_fragments(task, config)
    })


@app.route('/tasks/<task_id>/detail')
@login_required
def task_detail(task_id):
    """Return the standard task pipeline in the same shape as recording details."""
    task = get_task(task_id)
    if not task:
        return jsonify({'success': False, 'message': '任务不存在'}), 404
    config = load_config()
    decorated = _decorate_youtube_task_for_view(dict(task), config)
    return jsonify({
        'success': True,
        'task': {
            'id': decorated.get('id'),
            'display_id': decorated.get('display_id'),
            'title': decorated.get('video_title_translated') or decorated.get('video_title_original') or '未获取标题',
            'status': decorated.get('status'),
            'status_label': task_status_display(decorated.get('status')),
            'progress_label': decorated.get('progress_label'),
            'progress_percent': decorated.get('progress_percent'),
            'completed_stages': decorated.get('completed_stages'),
            'total_stages': decorated.get('total_stages'),
            'created_at': decorated.get('created_at'),
            'updated_at': decorated.get('updated_at'),
            'error': decorated.get('error_message') or '',
            'stages': _youtube_pipeline_stage_details(decorated, config),
        },
    })


def _missing_upload_partition_labels(task, config):
    recommend_enabled = str(config.get('RECOMMEND_PARTITION', False)).strip().lower() in ('true', '1', 'on', 'yes')
    missing = []
    fixed_bili_pid = str(config.get('FIXED_PARTITION_ID_BILIBILI', '') or '').strip()
    bili_partition = str(
        task.get('selected_partition_id_bilibili')
        or task.get('recommended_partition_id_bilibili')
        or task.get('selected_partition_id')
        or task.get('recommended_partition_id')
        or ''
    ).strip()
    if not fixed_bili_pid and not bili_partition and not recommend_enabled:
        missing.append('bilibili 分区')

    return missing


def _start_background_force_upload(task_id, config, platform_name):
    logger.info(f"开始后台强制上传任务 {task_id} 到{platform_name}")

    def background_force_upload():
        try:
            success = force_upload_task(task_id, config)
            if success:
                logger.info(f"任务 {task_id} 后台强制上传成功")
            else:
                logger.error(f"任务 {task_id} 后台强制上传失败")
        except Exception as e:
            logger.error(f"任务 {task_id} 后台强制上传出错: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())

    try:
        upload_thread = threading.Thread(target=background_force_upload, daemon=True)
        upload_thread.start()
    except RuntimeError as exc:
        logger.error("无法启动任务 %s 的后台强制上传线程：%s", task_id, exc)
        return False
    return True
    
@app.route('/tasks/stream')
@login_required
def tasks_event_stream():
    """Server-Sent Events stream for realtime task updates."""

    def generate():
        listener = register_task_updates_listener()
        try:
            yield 'data: {"type":"welcome"}\n\n'
            while True:
                try:
                    event = listener.get(timeout=10)  # 减少心跳间隔到 10 秒
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except Empty:
                    yield 'data: {"type":"heartbeat"}\n\n'
        except GeneratorExit:
            pass
        finally:
            unregister_task_updates_listener(listener)

    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    response.headers['Transfer-Encoding'] = 'chunked'
    return response

@app.route('/manual_review')
@login_required
def manual_review():
    """人工审核列表页面"""
    logger.info("访问人工审核列表页面")
    review_tasks = get_tasks_by_status(TASK_STATES['AWAITING_REVIEW'])
    failed_tasks = get_tasks_by_status(TASK_STATES['FAILED'])
    known_task_ids = {task.get('id') for task in review_tasks}
    review_tasks.extend(task for task in failed_tasks if task.get('id') not in known_task_ids)
    review_tasks.sort(key=lambda task: str(task.get('updated_at') or ''), reverse=True)
    recording_review_jobs = live_recorder_manager.pipeline_jobs(
        100,
        statuses={'failed'},
    )
    
    # 封面图片现在直接从downloads目录提供
    
    return render_template(
        'manual_review.html',
        tasks=review_tasks,
        recording_jobs=recording_review_jobs,
    )

@app.route('/tasks/<task_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_task(task_id):
    """任务编辑页面"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if request.method == 'POST':
        action = request.form.get('action', 'save_metadata').strip().lower()
        redirect_target = url_for('edit_task', task_id=task_id)

        if action == 'replace_cover':
            try:
                cover_file = request.files.get('cover_file')
                _replace_task_cover(task, cover_file)
                flash('任务封面已更新。', 'success')
            except Exception as e:
                logger.warning(f"替换任务 {task_id} 封面失败: {e}")
                flash(f'更换封面失败: {e}', 'danger')
            return redirect(redirect_target)

        if action == 'restore_cover':
            try:
                _restore_task_cover(task)
                flash('已恢复原始封面。', 'success')
            except Exception as e:
                logger.warning(f"恢复任务 {task_id} 原始封面失败: {e}")
                flash(f'恢复原封面失败: {e}', 'danger')
            return redirect(redirect_target)

        # 处理表单提交
        video_title = request.form.get('video_title_translated', '')
        description = request.form.get('description_translated', '')
        legacy_partition_id = request.form.get('selected_partition_id', '')
        partition_id_bilibili = request.form.get('selected_partition_id_bilibili', '')
        tags_json = request.form.get('tags_json', '[]')

        partition_id_bilibili = partition_id_bilibili or legacy_partition_id
        # 更新任务信息
        update_data = {
            'video_title_translated': video_title,
            'description_translated': description,
            'selected_partition_id_bilibili': partition_id_bilibili,
            'tags_generated': tags_json,
            'error_message': None,
        }

        # 只有在安全状态下才允许设置为可上传状态，避免与正在处理的任务产生竞态条件
        safe_states_to_make_uploadable = [
            TASK_STATES['DOWNLOADED'],        # 已下载，可以上传
            TASK_STATES['MODERATING'],        # 审核中，可以手动干预
            TASK_STATES['AWAITING_REVIEW'],   # 等待人工审核
            TASK_STATES['FAILED'],            # 失败状态，可以重试
            TASK_STATES['UPLOADING']          # 允许重置卡住的上传状态
        ]
        
        if task['status'] in safe_states_to_make_uploadable:
            update_data['status'] = TASK_STATES['READY_FOR_UPLOAD']
        
        try:
            # 确保silent参数是布尔类型
            final_update_data = update_data.copy()
            silent_param = False  # 默认值
            
            if 'silent' in final_update_data:
                if isinstance(final_update_data['silent'], str):
                    silent_param = final_update_data['silent'].lower() in ('true', 'yes', '1', 'on')
                elif isinstance(final_update_data['silent'], bool):
                    silent_param = final_update_data['silent']
                # 从final_update_data中移除silent，避免重复传递
                final_update_data.pop('silent')
            
            update_succeeded = update_task(
                task_id,
                silent=silent_param,
                **final_update_data,
            )
        except Exception as e:
            logger.warning(f"update_task调用失败: {e}")
            update_succeeded = False
        if not update_succeeded:
            flash('任务信息保存失败，未执行后续操作，请重试。', 'danger')
            return redirect(redirect_target)
        logger.info(f"任务 {task_id} 信息已更新")
        updated_task = get_task(task_id)
        if action == 'force_upload':
            config = load_config()
            platform_name = 'bilibili'
            missing_partitions = _missing_upload_partition_labels(updated_task or task, config)
            if missing_partitions:
                flash(f'请先选择{ "、".join(missing_partitions) }，或开启分区推荐后再继续上传。', 'danger')
                return redirect(redirect_target)

            if not _start_background_force_upload(task_id, config, platform_name):
                flash('任务信息已保存，但后台上传线程启动失败，请重试。', 'danger')
                return redirect(redirect_target)
            flash(f'已保存当前修改，并启动强制上传到{platform_name}，正在后台处理...', 'info')
            return redirect(url_for('manual_review'))

        if updated_task and updated_task['status'] == TASK_STATES['READY_FOR_UPLOAD']:
            flash('任务已保存，当前可单独执行上传。', 'success')
        else:
            flash('任务已保存。', 'success')

        return redirect(redirect_target)
    
    # GET请求，显示编辑页面
    # 封面图片现在直接从downloads目录提供
    bilibili_id_mapping = _build_bilibili_partition_mapping()
    
    # 准备标签字符串
    tags_string = ""
    if task.get('tags_generated'):
        try:
            tags = json.loads(task['tags_generated'])
            tags_string = ", ".join(tags)
        except Exception as e:
            logger.error(f"解析标签JSON失败: {str(e)}")
    
    # 获取当前配置
    config = load_config()
    can_upload = task['status'] in [
        TASK_STATES['COMPLETED'],
        TASK_STATES['PENDING'],
        TASK_STATES['READY_FOR_UPLOAD'],
        TASK_STATES['AWAITING_REVIEW']
    ]
    has_original_cover_backup = False
    has_cover_preview = False
    is_custom_cover_active = False
    current_cover_filename = ''
    try:
        task_dir_real = _get_task_dir_real(task_id)
        has_original_cover_backup = bool(os.path.isdir(task_dir_real) and _find_original_cover_backup(task_dir_real))
        active_cover_path = _get_current_cover_path(task, task_dir_real) if os.path.isdir(task_dir_real) else ''
        has_cover_preview = bool(active_cover_path)
        current_cover_filename = os.path.basename(active_cover_path) if active_cover_path else ''
        is_custom_cover_active = current_cover_filename.startswith('custom_cover.')
    except Exception:
        has_original_cover_backup = False
        has_cover_preview = bool(task.get('cover_path_local'))
        is_custom_cover_active = False
        current_cover_filename = os.path.basename(str(task.get('cover_path_local') or ''))
    
    return render_template(
        'edit_task.html', 
        task=task, 
        id_mapping=bilibili_id_mapping,
        bilibili_id_mapping=bilibili_id_mapping,
        tags_string=tags_string,
        config=config,
        upload_target='bilibili',
        can_upload=can_upload,
        has_cover_preview=has_cover_preview,
        has_original_cover_backup=has_original_cover_backup,
        is_custom_cover_active=is_custom_cover_active,
        current_cover_filename=current_cover_filename
    )

@app.route('/tasks/<task_id>/cover')
@login_required
def get_task_cover(task_id):
    """获取任务封面图片"""
    task = get_task(task_id)
    
    if not task:
        # 返回默认图片或404
        return '', 404
    
    try:
        task_dir_real = _get_task_dir_real(task_id)
    except (ValueError, OSError):
        return '', 404

    cover_path = _get_current_cover_path(task, task_dir_real)
    if cover_path and os.path.exists(cover_path):
        mime_type, _ = mimetypes.guess_type(cover_path)
        return send_file(cover_path, mimetype=mime_type)
    
    # 没有找到封面
    return '', 404

@app.route('/tasks/<task_id>/review')
@login_required
def review_task(task_id):
    """重定向到任务编辑页面"""
    return redirect(url_for('edit_task', task_id=task_id))

@app.route('/tasks/add', methods=['POST'])
@login_required
def add_task_route():
    """添加新任务，支持播放列表批量添加"""
    youtube_url = request.form.get('youtube_url')
    upload_target = 'bilibili'
    
    if not youtube_url:
        flash('YouTube URL不能为空', 'danger')
        return redirect(url_for('tasks'))

    config = load_config()
    selected_account = resolve_account(
        config,
        request.form.get('bilibili_account_id', ''),
    )
    # 判断是否为播放列表URL
    if 'youtube.com/playlist' in youtube_url or 'youtu.be/playlist' in youtube_url:
        # 提取所有视频URL
        cookies_path = config.get('YOUTUBE_COOKIES_PATH')
        video_urls = extract_video_urls_from_playlist(youtube_url, cookies_path)
        if not video_urls:
            flash('未能提取到播放列表中的视频', 'danger')
            return redirect(url_for('tasks'))
        added_count = 0
        for url in video_urls:
            task_id = add_task(
                url,
                upload_target=upload_target,
                bilibili_account_id=selected_account['id'],
            )
            if task_id:
                added_count += 1
        flash(f'已批量添加 {added_count} 个视频任务（来自播放列表）', 'success')
        return redirect(url_for('tasks'))
    else:
        task_id = add_task(
            youtube_url,
            upload_target=upload_target,
            bilibili_account_id=selected_account['id'],
        )
        if task_id:
            if config.get('AUTO_MODE_ENABLED', False):
                logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
                if start_task(task_id, config):
                    flash(f'任务已添加并开始处理: {youtube_url}', 'success')
                else:
                    flash(
                        f'任务已添加，但未能立即启动，将保留在队列中: {youtube_url}',
                        'warning',
                    )
            else:
                flash(f'任务已添加: {youtube_url}', 'success')
        else:
            flash(f'添加任务失败: {youtube_url}', 'danger')
        return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/start', methods=['POST'])
@login_required
def start_task_route(task_id):
    """开始处理任务"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if task['status'] not in [
        TASK_STATES['PENDING'],
        TASK_STATES['FAILED'],
        TASK_STATES['PAUSED'],
    ]:
        flash(f'当前任务状态为 {task_status_display(task["status"])}，不能启动', 'warning')
        return redirect(url_for('tasks'))
    
    # 获取当前配置
    config = load_config()
    
    # 启动任务处理
    success = start_task(task_id, config)
    
    if success:
        # 检查是否是自动模式
        if config.get('AUTO_MODE_ENABLED', False):
            flash('任务已启动，自动模式将会自动完成下载、处理和上传', 'info')
            
            # 使用传统页面刷新方式
        else:
            flash('任务处理已启动', 'success')
    else:
        flash('启动任务处理失败', 'danger')
    
    return redirect(url_for('tasks'))


@app.route('/tasks/<task_id>/pause', methods=['POST'])
@login_required
def pause_task_route(task_id):
    """Cooperatively pause a task without deleting its files."""
    task = get_task(task_id)
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))

    if pause_task(task_id):
        flash('任务已暂停，文件已保留，可稍后继续', 'success')
    else:
        latest = get_task(task_id)
        if latest and latest.get('status') == TASK_STATES['PAUSED']:
            flash('任务正在停止，请稍后刷新确认', 'warning')
        else:
            flash(f'当前任务状态为 {task_status_display(task["status"])}，不能暂停', 'warning')
    return redirect(url_for('tasks'))


@app.route('/tasks/<task_id>/delete', methods=['POST'])
@login_required
def delete_task_route(task_id):
    """删除任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    success = delete_task(task_id, delete_files)
    
    if success:
        flash('任务已删除', 'success')
    else:
        flash('任务尚未安全停止，未删除记录或文件，请稍后重试', 'warning')
    
    return redirect(url_for('tasks'))


@app.route('/tasks/clear_all', methods=['POST'])
@login_required
def clear_all_tasks_route():
    """清空所有任务（可选择同时删除任务文件）"""
    try:
        delete_files = request.form.get('delete_files', 'true').lower() in ['true', '1', 'on']
        success = clear_all_tasks(delete_files=delete_files)
        if success:
            flash('所有任务已清空', 'success')
        else:
            flash('清空任务失败，请查看日志', 'danger')
    except Exception as e:
        logger.error(f"清空所有任务失败: {e}")
        flash(f'清空任务失败: {e}', 'danger')
    return redirect(url_for('tasks'))


@app.route('/tasks/retry_failed', methods=['POST'])
@login_required
def retry_failed_tasks_route():
    """重新调度所有失败的任务（从任务管理器调用）"""
    try:
        # 加载最新配置
        cfg = load_config()
        result = retry_failed_tasks(cfg)
        if isinstance(result, dict):
            scheduled = result.get('scheduled', 0)
            total = result.get('total', 0)
            flash(f'已重新调度 {scheduled}/{total} 个失败任务', 'success')
        else:
            flash('重新调度失败，请查看日志', 'danger')
    except Exception as e:
        logger.error(f"重试失败任务失败: {e}")
        flash(f'重试失败任务失败: {e}', 'danger')
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/force_upload', methods=['POST'])
@login_required
def force_upload_task_route(task_id):
    """强制上传任务"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('manual_review'))
    
    # 获取当前配置
    config = load_config()
    platform_name = 'bilibili'
    missing_partitions = _missing_upload_partition_labels(task, config)
    if missing_partitions:
        flash(f'请先选择{ "、".join(missing_partitions) }，或开启分区推荐后再继续上传。', 'danger')
        return redirect(url_for('edit_task', task_id=task_id))
    
    # 启动后台强制上传
    if _start_background_force_upload(task_id, config, platform_name):
        flash(f'已启动强制上传到{platform_name}，正在后台处理...', 'info')
    else:
        flash('后台上传线程启动失败，请重试。', 'danger')

    return redirect(url_for('manual_review'))

@app.route('/tasks/reset_stuck', methods=['POST'])
@login_required
def reset_stuck_tasks_route():
    """重置卡住的任务"""
    from modules.task_manager import reset_stuck_tasks
    
    try:
        reset_count = reset_stuck_tasks()
        if reset_count > 0:
            flash(f'已重置 {reset_count} 个卡住的任务', 'success')
        else:
            flash('没有发现卡住的任务', 'info')
    except Exception as e:
        logger.error(f"重置卡住任务失败: {str(e)}")
        flash('重置卡住任务失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/abandon', methods=['POST'])
@login_required
def abandon_task_route(task_id):
    """放弃任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')

    if abandon_task(task_id, delete_files=delete_files):
        flash('任务已废弃' + ('，相关文件已删除' if delete_files else '，文件已保留'), 'success')
    else:
        flash('任务尚未安全停止，未废弃也未删除文件，请稍后重试', 'warning')
    return redirect(url_for('tasks'))

# 系统健康检查辅助函数

def check_docker_volumes():
    """检查Docker挂载卷状态"""
    volumes = {}
    app_root = os.path.dirname(os.path.abspath(__file__))
    
    volume_paths = [
        ('config', 'config'),
        ('db', 'db'),
        ('downloads', 'downloads'),
        ('logs', 'logs'),
        ('cookies', 'cookies'),
        ('temp', 'temp')
    ]
    
    for name, path in volume_paths:
        full_path = os.path.join(app_root, path)
        volumes[name] = {
            'path': full_path,
            'exists': os.path.exists(full_path),
            'is_mount': os.path.ismount(full_path),
            'writable': os.access(full_path, os.W_OK) if os.path.exists(full_path) else False,
            'size_mb': get_directory_size(full_path) if os.path.exists(full_path) else 0
        }
    
    return volumes

def get_directory_size(path):
    """获取目录大小(MB)"""
    try:
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    total_size += os.path.getsize(fp)
        return round(total_size / 1024 / 1024, 2)
    except Exception as e:
        logger.warning(f"获取目录大小失败 {path}: {e}")
        return 0

def _public_health_check_error_message(component: str = '检查项') -> str:
    return f'{component}检查失败，请查看服务日志。'

def get_database_info():
    """获取数据库文件信息"""
    try:
        from modules.task_manager import get_db_path
        db_path = get_db_path()
        
        if os.path.exists(db_path):
            stat_info = os.stat(db_path)
            return {
                'path': db_path,
                'size': stat_info.st_size,
                'writable': os.access(db_path, os.W_OK),
                'last_modified': stat_info.st_mtime
            }
        else:
            return {
                'path': db_path,
                'size': 0,
                'writable': False,
                'last_modified': None
            }
    except Exception as e:
        logger.warning("获取数据库文件信息失败: %s", e)
        return {
            'path': 'unknown',
            'size': 0,
            'writable': False,
            'error': _public_health_check_error_message('数据库')
        }

def get_database_debug_info():
    """获取数据库调试信息"""
    try:
        from modules.task_manager import get_db_path
        db_path = get_db_path()
        
        debug_info = {
            'db_path': db_path,
            'db_exists': os.path.exists(db_path),
            'db_dir': os.path.dirname(db_path),
            'db_dir_exists': os.path.exists(os.path.dirname(db_path)),
            'db_dir_writable': os.access(os.path.dirname(db_path), os.W_OK) if os.path.exists(os.path.dirname(db_path)) else False,
            'current_user': os.environ.get('USER', 'unknown'),
            'current_uid': os.getuid() if hasattr(os, 'getuid') else 'unknown',  # type: ignore
            'current_gid': os.getgid() if hasattr(os, 'getgid') else 'unknown'   # type: ignore
        }
        
        if os.path.exists(db_path):
            stat_info = os.stat(db_path)
            debug_info.update({
                'db_size': stat_info.st_size,
                'db_mode': oct(stat_info.st_mode)[-3:],
                'db_uid': stat_info.st_uid,
                'db_gid': stat_info.st_gid
            })
        
        return debug_info
    except Exception as e:
        logger.warning("获取数据库调试信息失败: %s", e)
        return {'error': _public_health_check_error_message('数据库')}

def get_file_info(file_path):
    """获取文件详细信息"""
    try:
        info = {
            'exists': os.path.exists(file_path),
            'size': 0,
            'readable': False,
            'last_modified': None
        }
        
        if info['exists']:
            stat_info = os.stat(file_path)
            info.update({
                'size': stat_info.st_size,
                'readable': os.access(file_path, os.R_OK),
                'last_modified': stat_info.st_mtime
            })
        
        return info
    except Exception as e:
        logger.warning("获取文件信息失败: %s", e)
        return {
            'exists': False,
            'size': 0,
            'readable': False,
            'last_modified': None,
            'error': _public_health_check_error_message('文件')
        }

def get_path_debug_info(file_path):
    """获取路径调试信息"""
    try:
        debug_info = {
            'path': file_path,
            'dirname': os.path.dirname(file_path),
            'basename': os.path.basename(file_path),
            'dirname_exists': os.path.exists(os.path.dirname(file_path)),
            'dirname_readable': os.access(os.path.dirname(file_path), os.R_OK) if os.path.exists(os.path.dirname(file_path)) else False,
            'dirname_writable': os.access(os.path.dirname(file_path), os.W_OK) if os.path.exists(os.path.dirname(file_path)) else False
        }
        
        # 列出目录内容
        if debug_info['dirname_exists'] and debug_info['dirname_readable']:
            try:
                debug_info['directory_contents'] = os.listdir(os.path.dirname(file_path))
            except:
                debug_info['directory_contents'] = 'permission_denied'
        
        return debug_info
    except Exception as e:
        logger.warning("获取路径调试信息失败: %s", e)
        return {'error': _public_health_check_error_message('路径')}


def _desktop_onboarding_path() -> Path:
    return Path(get_app_subdir('state')) / 'onboarding.json'


@app.route('/onboarding')
@login_required
def desktop_onboarding():
    data_root = Path(os.environ.get('POTATOFLOW_DATA_DIR') or get_app_subdir('state')).resolve()
    paths = {
        'data': str(data_root),
        'recordings': str(recordings_dir()),
        'exports': str(Path(os.environ.get('POTATOFLOW_EXPORTS_DIR') or data_root / 'exports').resolve()),
    }
    return render_template('onboarding.html', paths=paths)


@app.route('/api/onboarding/status')
@login_required
def desktop_onboarding_status():
    path = _desktop_onboarding_path()
    try:
        state = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    except (OSError, ValueError):
        state = {}
    return jsonify({'completed': bool(state.get('completed')), 'version': state.get('version', '')})


@app.route('/api/onboarding/complete', methods=['POST'])
@login_required
def desktop_onboarding_complete():
    payload = request.get_json(silent=True) or {}
    encoder = str(payload.get('encoder') or 'auto').lower()
    if encoder not in {'auto', 'cpu', 'amd', 'nvidia', 'intel'}:
        return jsonify({'success': False, 'error': '无效的编码器'}), 400
    updates = {
        'RECORDINGS_PATH': str(recordings_dir()),
        'DANMAKU_ENCODER': encoder,
    }
    update_config(updates)
    state = {'completed': True, 'version': __version__, 'completed_at': datetime.now().isoformat()}
    path = _desktop_onboarding_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'success': True, 'redirect': url_for('index')})


@app.route('/diagnostics')
@login_required
def desktop_diagnostics():
    from modules.desktop_runtime import component_diagnostics, resolve_windows_runtime

    layout = resolve_windows_runtime(sys.executable, Path(__file__).resolve().parent)
    # The launcher is authoritative in frozen mode; source mode uses the active paths.
    data_root = Path(os.environ.get('POTATOFLOW_DATA_DIR') or layout.data_root).resolve()
    recordings_root = Path(os.environ.get('POTATOFLOW_RECORDINGS_DIR') or recordings_dir()).resolve()
    usage_target = recordings_root
    while not usage_target.exists() and usage_target.parent != usage_target:
        usage_target = usage_target.parent
    usage = shutil.disk_usage(usage_target)
    diagnostics = {
        'mode': os.environ.get('POTATOFLOW_RUNTIME_MODE', 'source'),
        'architecture': os.environ.get('PROCESSOR_ARCHITECTURE', ''),
        'version': __version__,
        'data_root': str(data_root),
        'recordings_root': str(recordings_root),
        'recordings_writable': os.access(recordings_root if recordings_root.exists() else usage_target, os.W_OK),
        'disk_free_gb': round(usage.free / 1024 ** 3, 1),
        'components': component_diagnostics(layout),
    }
    return render_template('diagnostics.html', diagnostics=diagnostics)


@app.route('/api/runtime-diagnostics')
@login_required
def desktop_runtime_diagnostics_api():
    from modules.desktop_runtime import component_diagnostics, resolve_windows_runtime

    layout = resolve_windows_runtime(sys.executable, Path(__file__).resolve().parent)
    components = component_diagnostics(layout)
    webview2 = False
    if os.name == 'nt':
        try:
            import winreg
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' if root == winreg.HKEY_LOCAL_MACHINE else r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
                    with winreg.OpenKey(root, key):
                        webview2 = True
                        break
                except OSError:
                    continue
        except ImportError:
            pass
    components.append({'name': 'WebView2 Runtime', 'exists': webview2, 'path': 'Windows Runtime', 'sha256': ''})
    return jsonify({'components': components, 'mode': os.environ.get('POTATOFLOW_RUNTIME_MODE', 'source')})

@app.route('/system_health')
@login_required
def system_health():
    """系统健康检查 - 增强Docker环境兼容性"""
    from modules.task_manager import get_db_connection, validate_cookies, resolve_cookie_file_path
    import sqlite3
    import os
    import platform
    import sys
    
    # 检测运行环境
    is_docker = os.path.exists('/.dockerenv') or os.environ.get('CONTAINER') == 'docker'
    
    health_status = {
        'runtime': build_runtime_info(
            __version__,
            Path(__file__).resolve().with_name('version.py'),
        ),
        'environment': {
            'platform': platform.system(),
            'python_version': sys.version.split()[0],
            'is_docker': is_docker,
            'user': os.environ.get('USER', 'unknown'),
            'working_directory': os.getcwd()
        },
        'database': {'status': 'unknown', 'message': ''},
        'youtube_cookies': {'status': 'unknown', 'message': ''},
        'bilibili_cookies': {'status': 'unknown', 'message': ''},
        'stuck_tasks': {'count': 0, 'tasks': []},
        'recent_errors': [],
        'docker_volumes': {}
    }
    
    # Docker环境特殊检查
    if is_docker:
        health_status['docker_volumes'] = check_docker_volumes()
    
    # 检查数据库
    try:
        logger.info("开始数据库健康检查...")
        conn = get_db_connection()
        
        # 测试基本连接
        cursor = conn.execute('SELECT COUNT(*) FROM tasks')
        task_count = cursor.fetchone()[0]
        
        # 检查数据库文件权限和位置
        db_info = get_database_info()
        
        health_status['database'] = {
            'status': 'ok',
            'message': f'数据库正常，共有 {task_count} 个任务',
            'location': db_info['path'],
            'size_mb': round(db_info['size'] / 1024 / 1024, 2),
            'writable': db_info['writable']
        }
        
        # 检查卡住的任务
        stuck_cursor = conn.execute('''
            SELECT id, status, created_at, updated_at, error_message
            FROM tasks 
            WHERE status IN ('processing', 'downloading', 'uploading', 'fetching_info', 'translating')
            AND datetime(updated_at) < datetime('now', '-30 minutes')
        ''')
        stuck_tasks = stuck_cursor.fetchall()
        health_status['stuck_tasks'] = {
            'count': len(stuck_tasks),
            'tasks': [{'id': t[0][:8] + '...', 'status': t[1], 'updated': t[3]} for t in stuck_tasks]
        }
        
        # 检查最近的错误
        error_cursor = conn.execute('''
            SELECT id, error_message, updated_at
            FROM tasks 
            WHERE status = 'failed' AND error_message IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 5
        ''')
        error_tasks = error_cursor.fetchall()
        health_status['recent_errors'] = [
            {'id': t[0][:8] + '...', 'error': t[1][:100] + '...' if len(t[1]) > 100 else t[1], 'time': t[2]}
            for t in error_tasks
        ]
        
        conn.close()
        logger.info("数据库健康检查完成")
    except Exception:
        logger.exception("数据库健康检查失败")
        health_status['database'] = {
            'status': 'error',
            'message': _public_health_check_error_message('数据库'),
            'details': get_database_debug_info()
        }
    

    # 检查cookies - 使用更健壮的路径处理
    try:
        logger.info("开始cookies健康检查...")
        config = load_config()
        
        # YouTube cookies
        yt_cookies_path = config.get('YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt')
        if yt_cookies_path:
            # 如果是相对路径，转换为绝对路径
            yt_cookies_path = resolve_cookie_file_path(
                path_value=yt_cookies_path,
                default_relative_path='cookies/yt_cookies.txt',
                service_name='YouTube',
                logger_obj=logger,
                allow_json_txt_fallback=False
            )
            
            try:
                logger.debug(f"检查YouTube cookies文件: {yt_cookies_path}")
                is_valid, message = validate_cookies(yt_cookies_path, "YouTube")
                
                # 获取文件详细信息
                file_info = get_file_info(yt_cookies_path)
                
                health_status['youtube_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': yt_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception:
                logger.exception("YouTube cookies检查异常")
                health_status['youtube_cookies'] = {
                    'status': 'error',
                    'message': _public_health_check_error_message('YouTube Cookies'),
                    'path': yt_cookies_path,
                    'debug_info': get_path_debug_info(yt_cookies_path)
                }
        else:
            health_status['youtube_cookies'] = {
                'status': 'warning',
                'message': '未配置YouTube cookies路径'
            }
        

        # Bilibili cookies
        bili_cookies_path = config.get('BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json')
        if bili_cookies_path:
            bili_cookies_path = resolve_cookie_file_path(
                path_value=bili_cookies_path,
                default_relative_path='cookies/bili_cookies.json',
                service_name='Bilibili',
                logger_obj=logger,
                allow_json_txt_fallback=False
            )

            try:
                logger.debug(f"检查Bilibili cookies文件: {bili_cookies_path}")
                is_valid, message = validate_cookies(bili_cookies_path, "Bilibili")
                file_info = get_file_info(bili_cookies_path)
                health_status['bilibili_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': bili_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception:
                logger.exception("Bilibili cookies检查异常")
                health_status['bilibili_cookies'] = {
                    'status': 'error',
                    'message': _public_health_check_error_message('Bilibili Cookies'),
                    'path': bili_cookies_path,
                    'debug_info': get_path_debug_info(bili_cookies_path)
                }
        else:
            health_status['bilibili_cookies'] = {
                'status': 'warning',
                'message': '未配置Bilibili cookies路径'
            }
        
        logger.info("cookies健康检查完成")
            
    except Exception:
        logger.exception("检查cookies时发生错误")
        health_status['youtube_cookies'] = {
            'status': 'error',
            'message': _public_health_check_error_message('YouTube Cookies'),
            'debug_info': _public_health_check_error_message('Cookies')
        }
        health_status['bilibili_cookies'] = {
            'status': 'error',
            'message': _public_health_check_error_message('Bilibili Cookies'),
            'debug_info': _public_health_check_error_message('Cookies')
        }
    
    return jsonify(health_status)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """设置页面，用于管理配置"""
    if request.method == 'POST':
        config = load_config()
        form_data = request.form.to_dict()
        uploads = _extract_settings_uploads(request.files)
        operation_id = str(form_data.get('save_operation_id') or uuid.uuid4())
        enable_password_protection = str(form_data.get('password_protection_enabled', '')).lower() in ['true', '1', 'on']
        submitted_new_password = str(form_data.get('new_password') or '')
        submitted_confirm_password = str(form_data.get('confirm_password') or '')
        submitted_admin_username = _normalize_admin_username(
            form_data.get('admin_username') or config.get('admin_username') or 'admin'
        )
        has_effective_password = (
            (submitted_new_password and submitted_new_password == submitted_confirm_password)
            or bool(config.get('password'))
        )

        # 首次启用管理员账号登录时，需要立即把当前会话标记为已登录，
        # 否则前端接下来轮询 /settings/save-progress/... 会被 login_required 重定向到登录页，
        # 导致保存进度卡住或请求解析失败。
        if enable_password_protection and submitted_admin_username and has_effective_password:
            session['logged_in'] = True
            session['admin_username'] = submitted_admin_username
            session.permanent = True
        # 关闭管理员账号登录时不立即清除会话，避免中断当前保存流程的进度轮询。
        # 会话将在用户主动退出或session过期时自然失效。

        if _is_ajax_request():
            _update_settings_save_progress(
                operation_id,
                stage='saving_config',
                message='正在准备保存设置',
                detail='保存任务已创建，正在后台执行。',
                percent=None,
                done=False,
                level='info',
                success=None,
                messages=[]
            )
            save_thread = threading.Thread(
                target=_run_settings_save_operation,
                args=(operation_id, form_data, uploads),
                daemon=True,
                name=f'settings-save-{operation_id[:8]}'
            )
            save_thread.start()
            return jsonify({
                'success': True,
                'messages': [],
                'operation_id': operation_id
            })

        result = _perform_settings_save(form_data, uploads)
        for item in result.get('messages', []):
            flash(item.get('text', ''), item.get('category', 'info'))
        return redirect(url_for('settings'))
    
    # GET请求，显示设置页面
    config = load_config()
    try:
        from modules.bilibili_auth import get_account_identity

        legacy_account = resolve_account(config, LEGACY_ACCOUNT_ID)
        legacy_cookie = resolve_cookie_file_path(
            legacy_account.get('cookies_path'),
            'cookies/bili_cookies.json',
            allow_json_txt_fallback=False,
        )
        if legacy_cookie and os.path.isfile(legacy_cookie):
            identity = get_account_identity(legacy_cookie)
            identity_changes = {
                'BILIBILI_ACCOUNT_NAME': identity.get('name', ''),
                'BILIBILI_ACCOUNT_UID': identity.get('uid', ''),
                'BILIBILI_ACCOUNT_AVATAR_URL': identity.get('avatar_url', ''),
            }
            if any(
                str(config.get(key) or '') != str(value or '')
                for key, value in identity_changes.items()
            ):
                config = update_config(identity_changes)
    except Exception as exc:
        logger.debug("读取默认 B站账号名称失败，继续显示 Cookie 内 UID: %s", exc)
    bilibili_partition_mapping = _build_bilibili_partition_mapping()
    try:
        from modules.prompt_manager import get_builtin_prompt_previews
        builtin_prompts = get_builtin_prompt_previews()
    except Exception as exc:
        logger.debug("获取内置 Prompt 预览失败，将不显示预览: %s", exc)
        builtin_prompts = {}
    return render_template(
        'settings.html',
        config=config,
        bilibili_accounts=normalize_accounts(config),
        bilibili_default_account_id=default_account_id(config),
        whisper_languages=WHISPER_LANGUAGE_LIST,
        bilibili_partition_mapping=bilibili_partition_mapping,
        builtin_prompts=builtin_prompts,
        recording_prompt_defaults=live_recorder_manager.recording_prompt_defaults(),
        recordings_path=str(recordings_dir()),
        windows_desktop_mode=_is_windows_desktop_mode(),
    )


@app.route('/settings/bilibili-accounts', methods=['POST'])
@login_required
def add_bilibili_account():
    from modules.task_manager import validate_cookies
    from modules.bilibili_auth import get_account_identity

    upload = request.files.get('bilibili_account_cookie_file')
    try:
        if not upload or not str(upload.filename or '').strip():
            raise ValueError('请选择账号 Cookies 文件')
        config = load_config()
        account = create_account_record(
            '',
            upload.filename,
        )
        destination = account_cookie_destination(account)
        temporary = destination.with_name(f".{destination.name}.upload")
        upload.save(temporary)
        valid, error = validate_cookies(str(temporary), f"Bilibili（{account['name']}）")
        if not valid:
            temporary.unlink(missing_ok=True)
            raise ValueError(f'Cookies 文件无效：{error}')
        try:
            identity = get_account_identity(str(temporary))
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise ValueError(f'无法读取 B站真实昵称，请确认 Cookie 有效：{exc}') from exc
        if not identity.get('name') or not identity.get('uid'):
            temporary.unlink(missing_ok=True)
            raise ValueError('B站账号信息缺少真实昵称或 UID，请重新登录后导出 Cookie')
        account.update({
            'name': identity['name'],
            'bilibili_name': identity['name'],
            'bilibili_uid': identity['uid'],
            'avatar_url': identity.get('avatar_url', ''),
        })
        temporary.replace(destination)

        custom_accounts = serialize_custom_accounts(normalize_accounts(config))
        custom_accounts.append(account)
        changes = {'BILIBILI_ACCOUNTS': custom_accounts}
        if _coerce_checkbox_value(request.form.get('make_default', 'off')):
            changes[DEFAULT_ACCOUNT_CONFIG_KEY] = account['id']
        update_config(changes)
        try:
            live_recorder_manager.refresh_credentials()
        except RecorderConfigError as exc:
            logger.warning("B站账号池已保存，但录制配置重载失败: %s", exc)
        flash(f"已添加 B站投稿账号“{account['name']}”。", 'success')
    except (OSError, ValueError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('settings') + '#vtab-accounts')


@app.route('/settings/bilibili-accounts/<account_id>/cookies', methods=['POST'])
@login_required
def update_bilibili_account_cookies(account_id):
    """Replace one account's Cookie and refresh its public identity."""
    from modules.task_manager import validate_cookies
    from modules.bilibili_auth import get_account_identity

    upload = request.files.get('bilibili_account_cookie_file')
    try:
        config = load_config()
        account = next(
            (item for item in normalize_accounts(config) if item['id'] == account_id),
            None,
        )
        if not account:
            raise ValueError('没有找到该 B站投稿账号')
        if not upload or not str(upload.filename or '').strip():
            raise ValueError('请选择账号 Cookies 文件')

        if account_id == LEGACY_ACCOUNT_ID:
            destination = Path(resolve_cookie_file_path(
                path_value=account['cookies_path'],
                default_relative_path='cookies/bili_cookies.json',
                service_name='Bilibili',
                logger_obj=logger,
                allow_json_txt_fallback=False,
            ))
        else:
            destination = account_cookie_destination(account)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.upload")
        upload.save(temporary)
        valid, error = validate_cookies(str(temporary), f"Bilibili（{account['name']}）")
        if not valid:
            temporary.unlink(missing_ok=True)
            raise ValueError(f'Cookies 文件无效：{error}')
        try:
            identity = get_account_identity(str(temporary))
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise ValueError(f'无法读取 B站真实昵称，请确认 Cookie 有效：{exc}') from exc
        if not identity.get('name') or not identity.get('uid'):
            temporary.unlink(missing_ok=True)
            raise ValueError('B站账号信息缺少真实昵称或 UID，请重新登录')
        temporary.replace(destination)

        if account_id == LEGACY_ACCOUNT_ID:
            update_config({
                'BILIBILI_ACCOUNT_NAME': identity['name'],
                'BILIBILI_ACCOUNT_UID': identity['uid'],
                'BILIBILI_ACCOUNT_AVATAR_URL': identity.get('avatar_url', ''),
            })
        else:
            custom_accounts = serialize_custom_accounts(normalize_accounts(config))
            for item in custom_accounts:
                if item['id'] == account_id:
                    item.update({
                        'name': identity['name'],
                        'bilibili_name': identity['name'],
                        'bilibili_uid': identity['uid'],
                        'avatar_url': identity.get('avatar_url', ''),
                    })
                    break
            update_config({'BILIBILI_ACCOUNTS': custom_accounts})
        try:
            live_recorder_manager.refresh_credentials()
        except RecorderConfigError as exc:
            logger.warning("B站账号 Cookie 已更新，但录制配置重载失败: %s", exc)
        flash(f"已更新 B站账号“{identity['name']}”。", 'success')
    except (OSError, ValueError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('settings') + '#vtab-accounts')


@app.route('/settings/bilibili-accounts/<account_id>/default', methods=['POST'])
@login_required
def set_default_bilibili_account(account_id):
    config = load_config()
    account = next(
        (item for item in normalize_accounts(config) if item['id'] == account_id),
        None,
    )
    if not account:
        flash('没有找到该 B站投稿账号。', 'danger')
    else:
        update_config({DEFAULT_ACCOUNT_CONFIG_KEY: account['id']})
        try:
            live_recorder_manager.refresh_credentials()
        except RecorderConfigError as exc:
            logger.warning("默认 B站账号已保存，但录制配置重载失败: %s", exc)
        flash(f"已将“{account['name']}”设为默认投稿账号。", 'success')
    return redirect(url_for('settings') + '#vtab-accounts')


@app.route('/settings/bilibili-accounts/<account_id>/delete', methods=['POST'])
@login_required
def delete_bilibili_account(account_id):
    if account_id == LEGACY_ACCOUNT_ID:
        flash('默认兼容账号不能删除，可上传新 Cookies 覆盖。', 'danger')
        return redirect(url_for('settings') + '#vtab-accounts')
    config = load_config()
    accounts = normalize_accounts(config)
    account = next((item for item in accounts if item['id'] == account_id), None)
    if not account:
        flash('没有找到该 B站投稿账号。', 'danger')
        return redirect(url_for('settings') + '#vtab-accounts')
    custom_accounts = [
        item for item in serialize_custom_accounts(accounts)
        if item['id'] != account_id
    ]
    changes = {'BILIBILI_ACCOUNTS': custom_accounts}
    if default_account_id(config) == account_id:
        changes[DEFAULT_ACCOUNT_CONFIG_KEY] = LEGACY_ACCOUNT_ID
    update_config(changes)
    try:
        live_recorder_manager.refresh_credentials()
    except RecorderConfigError as exc:
        logger.warning("B站账号已删除，但录制配置重载失败: %s", exc)
    try:
        account_cookie_destination(account).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除 B站账号 Cookie 文件失败: %s", exc)
    flash(f"已删除 B站投稿账号“{account['name']}”。", 'success')
    return redirect(url_for('settings') + '#vtab-accounts')


@app.route('/settings/bilibili/upload-lines', methods=['GET'])
@login_required
def settings_bilibili_upload_lines():
    """Return the server-wide cached upload-line state."""
    return jsonify({"success": True, **load_upload_probe_state()})


@app.route('/settings/bilibili/upload-lines/probe', methods=['POST'])
@login_required
def settings_probe_bilibili_upload_lines():
    """Run the explicit 10 MiB probe and cache the fastest supported line."""
    try:
        state = probe_upload_lines()
        return jsonify({
            "success": True,
            "message": f"测速完成，已选择 {state.get('selected_line') or '可用线路'}。",
            **state,
        })
    except Exception as exc:
        logger.exception("投稿线路测速失败")
        return jsonify({
            "success": False,
            "message": f"投稿线路测速失败：{str(exc).splitlines()[0][:240]}",
        }), 502


@app.route('/settings/bilibili/upload-lines/select', methods=['POST'])
@login_required
def settings_select_bilibili_upload_line():
    """Select one cached and supported line for all submissions."""
    payload = request.get_json(silent=True) or {}
    try:
        state = save_upload_line(payload.get("line"))
        return jsonify({
            "success": True,
            "message": f"全局投稿线路已改为 {state.get('selected_line')}。",
            **state,
        })
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception:
        logger.exception("保存投稿线路失败")
        return jsonify({"success": False, "message": "投稿线路保存失败，请查看服务日志。"}), 500


@app.route('/settings/recordings/directories', methods=['GET'])
@login_required
def settings_recording_directories():
    """Browse server-side directories for the recording path picker."""
    requested = str(request.args.get('path') or '').strip()
    try:
        current = recordings_dir(requested or None)
    except RecorderConfigError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400

    missing_path = None
    if not current.exists():
        missing_path = str(current)
        candidate = current.parent
        while not candidate.exists() and candidate.parent != candidate:
            candidate = candidate.parent
        current = candidate
    if not current.is_dir():
        return jsonify({'success': False, 'message': f'这不是文件夹：{current}'}), 400

    directories = []
    try:
        for child in current.iterdir():
            try:
                if child.is_dir():
                    directories.append({
                        'name': child.name,
                        'path': str(child.resolve(strict=False)),
                        'readable': os.access(child, os.R_OK | os.X_OK),
                        'writable': os.access(child, os.W_OK),
                    })
            except OSError:
                continue
    except OSError as exc:
        return jsonify({'success': False, 'message': f'无法读取目录：{exc}'}), 403
    directories.sort(key=lambda item: item['name'].casefold())

    quick_paths = []
    seen = set()
    candidates = [
        ('当前录播目录', recordings_dir()),
        ('项目目录', recordings_dir().parent),
        ('容器录播挂载', Path('/data/recordings')),
        ('数据盘 /vol1', Path('/vol1')),
        ('挂载盘 /mnt', Path('/mnt')),
        ('媒体目录 /media', Path('/media')),
        ('用户目录', Path.home()),
    ]
    for label, candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            key = str(resolved)
            if key in seen or not resolved.is_dir():
                continue
            seen.add(key)
            quick_paths.append({'label': label, 'path': key})
        except OSError:
            continue

    parent = current.parent if current.parent != current else None
    return jsonify({
        'success': True,
        'current': str(current.resolve(strict=False)),
        'parent': str(parent.resolve(strict=False)) if parent else None,
        'directories': directories,
        'quick_paths': quick_paths,
        'writable': os.access(current, os.W_OK),
        'notice': f'原目录不存在：{missing_path}，已打开最近的可用上级目录。' if missing_path else '',
    })


@app.route('/settings/save-progress/<operation_id>', methods=['GET'])
@login_required
def settings_save_progress(operation_id):
    progress = _get_settings_save_progress(operation_id)
    if not progress:
        return jsonify({
            'found': False,
            'stage': None,
            'message': '',
            'detail': '',
            'percent': None,
            'downloaded_bytes': None,
            'total_bytes': None,
            'done': True,
            'level': 'error',
            'success': False,
            'messages': []
        })

    return jsonify({
        'found': True,
        'stage': progress.get('stage'),
        'message': progress.get('message'),
        'detail': progress.get('detail'),
        'percent': progress.get('percent'),
        'downloaded_bytes': progress.get('downloaded_bytes'),
        'total_bytes': progress.get('total_bytes'),
        'done': progress.get('done', False),
        'level': progress.get('level', 'info'),
        'success': progress.get('success'),
        'messages': progress.get('messages', [])
    })


@app.route('/settings/notifications/test', methods=['POST'])
@login_required
def settings_test_notification():
    if request.is_json:
        data = request.get_json(silent=True) or {}
        channel = str(data.get('channel') or '').strip()
    else:
        channel = str(request.form.get('channel') or '').strip()

    if channel not in (CHANNEL_WECOM, CHANNEL_SERVERCHAN, CHANNEL_MESSAGE_PUSHER, CHANNEL_TELEGRAM):
        return jsonify({'success': False, 'message': '不支持的通知渠道'}), 400

    try:
        config = load_config()
        _sync_notification_service(config)
        service = get_global_notification_service(config)
        service.send_test_message(channel)
        return jsonify({
            'success': True,
            'message': f'{CHANNEL_LABELS.get(channel, channel)} 测试消息已发送'
        })
    except ValueError:
        logger.warning("测试通知发送失败，渠道=%s", channel, exc_info=True)
        return jsonify({
            'success': False,
            'message': f'{CHANNEL_LABELS.get(channel, channel)} 配置不完整，请检查后重试'
        }), 400
    except Exception:
        logger.exception("测试通知发送失败，渠道=%s", channel)
        return jsonify({'success': False, 'message': '测试通知发送失败，请稍后重试'}), 500


@app.route('/settings/cookiecloud/test', methods=['POST'])
@login_required
def settings_test_cookiecloud():
    payload = request.get_json(silent=True) or {}
    effective_config = _merge_cookiecloud_runtime_settings(payload)

    try:
        result = test_cookiecloud_youtube_sync(effective_config)
        message = (
            f"CookieCloud 连接成功，已解析 {result['cookie_count']} 条 YouTube/Google Cookies，"
            f"当前使用 {result['crypto_type_used']} 算法。"
        )
        updated_at = _remember_cookiecloud_sync_result(True, message)
        return jsonify({
            'success': True,
            'message': message,
            'cookie_count': result['cookie_count'],
            'crypto_type_used': result['crypto_type_used'],
            'updated_at': updated_at,
            'status': 'success',
        })
    except CookieCloudError as exc:
        message = _cookiecloud_operation_error_message('test')
        updated_at = _remember_cookiecloud_sync_result(False, message)
        logger.warning('CookieCloud 连接测试失败（%s）: %s', type(exc).__name__, exc)
        return jsonify({
            'success': False,
            'message': message,
            'updated_at': updated_at,
            'status': 'error',
        }), 400
    except Exception:
        message = _cookiecloud_operation_error_message('test', retry_later=True)
        updated_at = _remember_cookiecloud_sync_result(False, message)
        logger.exception('CookieCloud 连接测试失败')
        return jsonify({
            'success': False,
            'message': message,
            'updated_at': updated_at,
            'status': 'error',
        }), 500


@app.route('/settings/cookiecloud/sync', methods=['POST'])
@login_required
def settings_sync_cookiecloud():
    payload = request.get_json(silent=True) or {}
    effective_config = _merge_cookiecloud_runtime_settings(payload)

    try:
        result = sync_cookiecloud_to_youtube_file(effective_config)
        message = (
            f"CookieCloud 已成功写入 {result['cookie_count']} 条 YouTube/Google Cookies 到 "
            f"{result['output_path_display']}。"
        )
        updated_at = _remember_cookiecloud_sync_result(True, message)
        return jsonify({
            'success': True,
            'message': message,
            'cookie_count': result['cookie_count'],
            'crypto_type_used': result['crypto_type_used'],
            'output_path_display': result['output_path_display'],
            'updated_at': updated_at,
            'status': 'success',
        })
    except CookieCloudError as exc:
        message = _cookiecloud_operation_error_message('sync')
        updated_at = _remember_cookiecloud_sync_result(False, message)
        logger.warning('CookieCloud 立即拉取失败（%s）: %s', type(exc).__name__, exc)
        return jsonify({
            'success': False,
            'message': message,
            'updated_at': updated_at,
            'status': 'error',
        }), 400
    except Exception:
        message = _cookiecloud_operation_error_message('sync', retry_later=True)
        updated_at = _remember_cookiecloud_sync_result(False, message)
        logger.exception('CookieCloud 立即拉取失败')
        return jsonify({
            'success': False,
            'message': message,
            'updated_at': updated_at,
            'status': 'error',
        }), 500


@app.route('/settings/bilibili/qrcode/start', methods=['POST'])
@login_required
def bilibili_qrcode_start():
    """发起 bilibili 二维码登录并返回二维码图片。"""
    config = load_config()
    payload = request.get_json(silent=True) or {}
    requested_account_id = str(payload.get('account_id') or '').strip()
    accounts = normalize_accounts(config)
    account = next(
        (item for item in accounts if item['id'] == requested_account_id),
        None,
    )
    is_new_account = account is None
    if is_new_account:
        account = create_account_record('', 'qrcode.json')
    if account['id'] == LEGACY_ACCOUNT_ID:
        cookie_path = resolve_cookie_file_path(
            path_value=account['cookies_path'],
            default_relative_path='cookies/bili_cookies.json',
            service_name='Bilibili',
            logger_obj=logger,
            allow_json_txt_fallback=False,
        )
    else:
        cookie_path = str(account_cookie_destination(account))

    try:
        session_id, qr_session = _create_bilibili_qr_session(
            account=dict(account),
            cookie_path=cookie_path,
            is_new_account=is_new_account,
        )
        qr_data = qr_session.generate()
        return jsonify({
            'success': True,
            'session_id': session_id,
            'image_base64': qr_data.get('image_base64', ''),
            'mime_type': qr_data.get('mime_type', 'image/png'),
            'expires_in': _BILIBILI_QR_SESSION_TTL_SECONDS,
            'cookie_path': cookie_path,
        })
    except Exception as e:
        logger.error(f"发起 bilibili 二维码登录失败: {e}")
        return jsonify({'success': False, 'message': '二维码登录失败，请稍后重试'}), 500

@app.route('/settings/bilibili/qrcode/status/<session_id>', methods=['GET'])
@login_required
def bilibili_qrcode_status(session_id):
    """轮询 bilibili 二维码登录状态。"""
    session_item = _get_bilibili_qr_session_item(session_id)
    qr_session = session_item.get('session') if session_item else None
    if not session_item or not qr_session:
        return jsonify({'success': False, 'message': '二维码会话不存在或已过期'}), 404

    cookie_path = resolve_cookie_file_path(
        path_value=str(session_item.get('cookie_path') or ''),
        default_relative_path='cookies/bili_cookies.json',
        service_name='Bilibili',
        logger_obj=logger,
        allow_json_txt_fallback=False,
    )
    account = dict(session_item.get('account') or {})
    is_new_account = bool(session_item.get('is_new_account'))

    try:
        status_data = qr_session.check_status(cookie_file=cookie_path)
        _emit_qr_login_event_once(
            _BILIBILI_QR_SESSIONS,
            _BILIBILI_QR_SESSION_LOCK,
            session_id,
            'bilibili',
            status_data,
        )
        status = status_data.get('status')
        if status == 'done' and status_data.get('cookies_saved'):
            from modules.bilibili_auth import get_account_identity

            try:
                identity = get_account_identity(cookie_path)
                if not identity.get('name') or not identity.get('uid'):
                    raise ValueError('B站账号信息缺少真实昵称或 UID')
                config = load_config()
                if account.get('id') == LEGACY_ACCOUNT_ID:
                    update_config({
                        'BILIBILI_ACCOUNT_NAME': identity['name'],
                        'BILIBILI_ACCOUNT_UID': identity['uid'],
                        'BILIBILI_ACCOUNT_AVATAR_URL': identity.get('avatar_url', ''),
                    })
                else:
                    account.update({
                        'name': identity['name'],
                        'bilibili_name': identity['name'],
                        'bilibili_uid': identity['uid'],
                        'avatar_url': identity.get('avatar_url', ''),
                    })
                    custom_accounts = serialize_custom_accounts(normalize_accounts(config))
                    existing_index = next(
                        (index for index, item in enumerate(custom_accounts)
                         if item['id'] == account['id']),
                        None,
                    )
                    if existing_index is None:
                        custom_accounts.append(account)
                    else:
                        custom_accounts[existing_index] = account
                    changes = {'BILIBILI_ACCOUNTS': custom_accounts}
                    if is_new_account and len(normalize_accounts(config)) == 1:
                        changes[DEFAULT_ACCOUNT_CONFIG_KEY] = account['id']
                    update_config(changes)
                status_data.update({
                    'account_id': account.get('id'),
                    'account_name': identity['name'],
                    'account_uid': identity['uid'],
                })
            except Exception as exc:
                if is_new_account:
                    try:
                        Path(cookie_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                status_data.update({
                    'status': 'failed',
                    'cookies_saved': False,
                    'message': f'登录成功，但读取真实账号信息失败：{exc}',
                })
                status = 'failed'
            try:
                live_recorder_manager.refresh_credentials()
            except RecorderConfigError as exc:
                logger.warning("Bilibili Cookie 已保存，但录制配置重载失败: %s", exc)
                status_data['recorder_warning'] = f'录制配置未能自动重载：{exc}'
        if status in ('done', 'timeout', 'failed'):
            with _BILIBILI_QR_SESSION_LOCK:
                _BILIBILI_QR_SESSIONS.pop(session_id, None)
        return jsonify({'success': True, **status_data})
    except Exception as e:
        logger.error(f"查询 bilibili 二维码登录状态失败: {e}")
        return jsonify({'success': False, 'message': '查询登录状态失败，请稍后重试'}), 500


@app.route('/settings/reset', methods=['POST'])
@login_required
def reset_settings():
    """重置设置"""
    try:
        data = request.get_json() or {}
        keys = data.get('keys', [])
        
        if keys:
            # 重置指定项
            reset_specific_config(keys)
            flash('当前页面的设置已重置为默认值。', 'success')
        else:
            # 如果未指定keys，则不执行任何操作或返回错误
            # 为了防止误操作全重置，这里要求必须指定keys
            return jsonify({'status': 'error', 'message': '未指定要重置的配置项'}), 400
            
        return jsonify({'status': 'success', 'message': '设置已重置'})
    except Exception as e:
        logger.error(f"重置设置失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '重置设置失败，请稍后重试'}), 500

@app.route('/logs/cleanup', methods=['POST'])
@login_required
def cleanup_logs_route():
    """手动触发日志清理"""
    config = load_config()
    hours = request.form.get('hours', config.get('LOG_CLEANUP_HOURS', 168))
    
    result = cleanup_logs(hours)
    
    if result.get('success'):
        flash(f"日志清理成功，删除了{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"日志清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

@app.route('/maintenance/clear_logs', methods=['POST'])
@login_required
def clear_logs_route():
    """立即清空特定日志文件"""
    result = clear_specific_logs()
    
    if result.get('success'):
        processed_files_str = "、".join(result['processed_files'])
        flash(f"日志清理成功，已处理{result['files_processed']}个文件（{processed_files_str}），释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"日志清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

@app.route('/maintenance/cleanup_downloads', methods=['POST'])
@login_required
def cleanup_downloads_route():
    """手动触发下载内容清理"""
    config = load_config()
    hours = request.form.get('hours', config.get('DOWNLOAD_CLEANUP_HOURS', 72))
    
    result = cleanup_downloads(hours)
    
    if result.get('success'):
        protected = int(result.get('skipped_protected', 0) or 0)
        protected_text = f"，保留了 {protected} 个未完成或可重试任务目录" if protected else ""
        flash(f"下载内容清理成功，删除了{result['dirs_removed']}个目录、{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间{protected_text}", 'success')
    else:
        flash(f"下载内容清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))


def _human_readable_size(num_bytes: float) -> str:
    # Simple helper for human readable file sizes
    if num_bytes is None:
        return '0B'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f}PB"


def _validated_cleanup_hours(hours, *, maximum: int) -> float:
    try:
        value = float(hours)
    except (TypeError, ValueError) as exc:
        raise ValueError("保留时长必须是数字") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"保留时长必须在 1 到 {maximum} 小时之间")
    return value


def cleanup_logs(hours: int):
    """删除logs目录下指定小时之前的日志文件（不包括当前运行日志）"""
    try:
        retention_hours = _validated_cleanup_hours(hours, maximum=8760)
        logs_dir = get_app_subdir('logs')
        if not os.path.exists(logs_dir):
            return {'success': True, 'files_removed': 0, 'bytes_freed': 0, 'bytes_freed_readable': '0B'}

        cutoff = time.time() - retention_hours * 3600
        files_removed = 0
        bytes_freed = 0
        failures = []

        for filename in os.listdir(logs_dir):
            path = os.path.join(logs_dir, filename)
            # skip current top-level app and manager logs when present
            if filename in ('app.log', 'task_manager.log'):
                continue
            try:
                stat = os.stat(path)
                if stat.st_mtime < cutoff:
                    bytes_freed += stat.st_size if stat.st_size else 0
                    if os.path.isfile(path):
                        os.remove(path)
                        files_removed += 1
                    elif os.path.isdir(path):
                        # 统计目录内实际文件数和大小
                        for dirpath, dirnames, dir_filenames in os.walk(path):
                            for df in dir_filenames:
                                try:
                                    bytes_freed += os.path.getsize(os.path.join(dirpath, df))
                                except Exception:
                                    pass
                                files_removed += 1
                        shutil.rmtree(path)
            except Exception as exc:
                failures.append(filename)
                logger.warning("清理日志项失败 %s: %s", filename, exc)

        result = {
            'success': not failures,
            'files_removed': files_removed,
            'bytes_freed': bytes_freed,
            'bytes_freed_readable': _human_readable_size(bytes_freed),
        }
        if failures:
            result['error'] = f"有 {len(failures)} 个日志项未能删除"
        return result
    except Exception as e:
        logger.warning(f"日志清理失败: {e}")
        return {'success': False, 'error': str(e)}


def clear_specific_logs():
    """清空特定日志文件并删除 task_xxx.log 文件"""
    try:
        logs_dir = get_app_subdir('logs')
        processed_files = []
        failed_files = []
        bytes_freed = 0

        # 清空 app.log 和 task_manager.log
        for fname in ('app.log', 'task_manager.log'):
            fpath = os.path.join(logs_dir, fname)
            if os.path.exists(fpath):
                try:
                    bytes_freed += os.path.getsize(fpath)
                    open(fpath, 'w', encoding='utf-8').close()
                    processed_files.append(fname)
                except Exception as exc:
                    failed_files.append(fname)
                    logger.warning("清空日志文件失败 %s: %s", fname, exc)

        # 删除所有task_xxx.log文件
        for filename in os.listdir(logs_dir):
            if filename.startswith('task_') and filename.endswith('.log'):
                path = os.path.join(logs_dir, filename)
                try:
                    bytes_freed += os.path.getsize(path) if os.path.exists(path) else 0
                    os.remove(path)
                    processed_files.append(filename)
                except Exception as exc:
                    failed_files.append(filename)
                    logger.warning("删除任务日志失败 %s: %s", filename, exc)

        result = {
            'success': not failed_files,
            'files_processed': len(processed_files),
            'processed_files': processed_files,
            'failed_files': failed_files,
            'bytes_freed': bytes_freed,
            'bytes_freed_readable': _human_readable_size(bytes_freed),
        }
        if failed_files:
            result['error'] = f"有 {len(failed_files)} 个日志文件未能清理"
        return result
    except Exception as e:
        logger.warning(f"清空日志失败: {e}")
        return {'success': False, 'error': str(e)}


def cleanup_downloads(hours: int):
    """清理下载目录中指定hours之前的任务目录"""
    try:
        retention_hours = _validated_cleanup_hours(hours, maximum=17520)
        downloads_dir = get_app_subdir('downloads')
        if not os.path.exists(downloads_dir):
            return {'success': True, 'dirs_removed': 0, 'files_removed': 0, 'bytes_freed': 0, 'bytes_freed_readable': '0B'}

        cutoff = time.time() - retention_hours * 3600
        dirs_removed = 0
        files_removed = 0
        bytes_freed = 0

        tasks_by_id = {
            str(task.get('id')): task
            for task in get_all_tasks()
            if task.get('id')
        }
        skipped_protected = 0
        failures = []

        for entry in os.listdir(downloads_dir):
            path = os.path.join(downloads_dir, entry)
            try:
                if os.path.isdir(path):
                    task = tasks_by_id.get(str(entry))
                    if task is not None and not can_automatically_cleanup_youtube_download(task):
                        skipped_protected += 1
                        continue
                    # check last modification
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        # accumulate size
                        for root, dirs, files in os.walk(path):
                            for f in files:
                                fp = os.path.join(root, f)
                                if os.path.exists(fp):
                                    bytes_freed += os.path.getsize(fp)
                                    files_removed += 1
                        shutil.rmtree(path)
                        dirs_removed += 1
            except Exception as exc:
                failures.append(entry)
                logger.warning("清理下载项失败 %s: %s", entry, exc)

        result = {
            'success': not failures,
            'dirs_removed': dirs_removed,
            'files_removed': files_removed,
            'skipped_protected': skipped_protected,
            'bytes_freed': bytes_freed,
            'bytes_freed_readable': _human_readable_size(bytes_freed),
        }
        if failures:
            result['error'] = f"有 {len(failures)} 个下载目录未能删除"
        return result
    except Exception as e:
        logger.warning(f"下载内容清理失败: {e}")
        return {'success': False, 'error': str(e)}


def configure_app(app, config):
    """为Flask app应用一些基础配置值（如 secret_key、上传限制等）"""
    try:
        # 使用配置中的SECRET_KEY提高会话安全，首次运行时自动生成并持久化
        secret = config.get('SECRET_KEY') if isinstance(config, dict) else None
        if secret:
            app.secret_key = secret
        elif isinstance(config, dict):
            # 若新配置中无 SECRET_KEY 但 app 已有，则复用，避免 session 全部失效
            if app.secret_key:
                config['SECRET_KEY'] = app.secret_key
            else:
                import secrets
                new_secret = secrets.token_hex(32)
                config['SECRET_KEY'] = new_secret
                app.secret_key = new_secret
                try:
                    update_config({'SECRET_KEY': new_secret})
                    logger.info("已自动生成并保存SECRET_KEY")
                except Exception as e:
                    logger.warning(f"保存自动生成的SECRET_KEY失败: {e}")

        max_content = config.get('MAX_CONTENT_LENGTH_MB', None) if isinstance(config, dict) else None
        if max_content:
            try:
                app.config['MAX_CONTENT_LENGTH'] = int(max_content) * 1024 * 1024
            except Exception:
                pass

        timeout_minutes = 30
        if isinstance(config, dict):
            timeout_value = config.get('LOGIN_SESSION_TIMEOUT_MINUTES', 30)
            try:
                timeout_minutes = int(timeout_value)
            except (TypeError, ValueError):
                timeout_minutes = 30
        app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=max(1, timeout_minutes))
        app.config['SESSION_REFRESH_EACH_REQUEST'] = True
        app.config['SESSION_COOKIE_HTTPONLY'] = True
        app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
        app.config['SESSION_COOKIE_SECURE'] = str(
            os.environ.get('POTATO_FLOW_HTTPS', '')
        ).strip().lower() in {'1', 'true', 'yes', 'on'}

        # 允许覆盖的内容
        app.config['POTATOFLOW_SETTINGS'] = config
    except Exception as e:
        logger.warning(f"应用配置失败: {e}")


def auto_start_pending_tasks(config):
    """在启动时尝试自动启动pending状态的任务"""
    try:
        from modules.task_manager import get_global_task_processor, get_tasks_by_status, TASK_STATES
        processor = get_global_task_processor(config)
        if not processor:
            return

        # 循环尝试启动下一个pending任务，直到并发数或没有更多pending
        # 我们设置一个上限避免无限循环
        attempts = 0
        while attempts < 200:
            attempts += 1
            try:
                processor._check_and_start_next_pending_task()
            except Exception:
                break
            # 如果没有pending则退出
            pending = get_tasks_by_status(TASK_STATES['PENDING'])
            if not pending:
                break
            time.sleep(0.05)
    except Exception as e:
        logger.warning(f"自动启动pending任务失败: {e}")


def schedule_log_cleanup():
    """为日志清理创建并启动一个BackgroundScheduler, 返回调度器对象"""
    try:
        config = load_config()
        interval_hours = int(config.get('LOG_CLEANUP_INTERVAL', 24))
        if not config.get('LOG_CLEANUP_ENABLED', False):
            return None

        scheduler = BackgroundScheduler()
        def _job():
            cleanup_logs(int(config.get('LOG_CLEANUP_HOURS', 168)))
        scheduler.add_job(_job, 'interval', hours=interval_hours, id='log_cleanup', replace_existing=True)
        scheduler.start()
        return scheduler
    except Exception as e:
        logger.warning(f"启动日志清理定时任务失败: {e}")
        return None


def schedule_download_cleanup():
    try:
        config = load_config()
        interval_hours = int(config.get('DOWNLOAD_CLEANUP_INTERVAL', 24))
        if not config.get('DOWNLOAD_CLEANUP_ENABLED', False):
            return None

        scheduler = BackgroundScheduler()
        def _job():
            cleanup_downloads(int(config.get('DOWNLOAD_CLEANUP_HOURS', 72)))
        scheduler.add_job(_job, 'interval', hours=interval_hours, id='download_cleanup', replace_existing=True)
        scheduler.start()
        return scheduler
    except Exception as e:
        logger.warning(f"启动下载内容清理定时任务失败: {e}")
        return None


# YouTube监控系统路由
def _build_youtube_monitor_readiness(config):
    """Summarize prerequisites without exposing credentials or proxy details."""
    config = dict(config or {})
    api_key_configured = bool(str(config.get('YOUTUBE_API_KEY') or '').strip())
    api_client_ready = bool(getattr(youtube_monitor, 'youtube', None))
    api_error = str(getattr(youtube_monitor, '_last_api_init_error', '') or '')
    if api_client_ready:
        api_state, api_value, api_detail = 'ready', 'API 已连接', '可以执行频道与关键词监控'
    elif not api_key_configured or api_error == 'missing_api_key':
        api_state, api_value, api_detail = 'attention', 'API Key 未配置', '监控发现功能暂不可用'
    else:
        api_state, api_value, api_detail = 'error', 'API 初始化失败', '请检查密钥、代理与网络连通性'

    cookie_path = resolve_cookie_file_path(
        config.get('YOUTUBE_COOKIES_PATH'),
        'cookies/yt_cookies.txt',
        service_name='YouTube',
        logger_obj=logger,
    )
    cookie_ready = bool(cookie_path and os.path.isfile(cookie_path))
    proxy_enabled = _coerce_checkbox_value(config.get('YOUTUBE_API_PROXY_ENABLED', False))
    proxy_url_ready = bool(str(config.get('NETWORK_PROXY_URL') or '').strip())
    if proxy_enabled and proxy_url_ready:
        proxy_state, proxy_value, proxy_detail = 'ready', '代理模式', '监控 API 将使用通用网络代理'
    elif proxy_enabled:
        proxy_state, proxy_value, proxy_detail = 'error', '代理地址缺失', '已启用代理但尚未配置通用代理地址'
    else:
        proxy_state, proxy_value, proxy_detail = 'ready', '直连模式', '服务器需能直接访问 YouTube Data API'

    items = [
        {
            'key': 'api',
            'label': 'YouTube Data API',
            'icon': 'bi-key',
            'state': api_state,
            'value': api_value,
            'detail': api_detail,
            'url': url_for('settings') + '#vtab-ops',
        },
        {
            'key': 'cookies',
            'label': 'YouTube Cookies',
            'icon': 'bi-cookie',
            'state': 'ready' if cookie_ready else 'attention',
            'value': '下载登录态可用' if cookie_ready else 'Cookies 文件缺失',
            'detail': '发现视频后可稳定下载受限内容' if cookie_ready else '公开内容仍可能可用，建议先补齐',
            'url': url_for('settings') + '#vtab-accounts',
        },
        {
            'key': 'network',
            'label': '网络方式',
            'icon': 'bi-globe2',
            'state': proxy_state,
            'value': proxy_value,
            'detail': proxy_detail,
            'url': url_for('settings') + '#vtab-accounts',
        },
    ]
    return {
        'ready': all(item['state'] == 'ready' for item in items),
        'blocking': any(item['state'] == 'error' for item in items),
        'items': items,
    }


@app.route('/youtube_monitor')
@login_required
def youtube_monitor_index():
    """YouTube监控主页"""
    configs = youtube_monitor.get_monitor_configs()
    app_config = load_config()
    for monitor_config in configs:
        account = resolve_account(
            app_config,
            monitor_config.get('bilibili_account_id'),
        )
        monitor_config['bilibili_account_id'] = account['id']
        monitor_config['bilibili_account_name'] = account['name']
        monitor_config['bilibili_account_uid'] = account.get('bilibili_uid', '')
    history = youtube_monitor.get_monitor_history(limit=50)
    readiness = _build_youtube_monitor_readiness(app_config)
    return render_template(
        'youtube_monitor.html',
        configs=configs,
        history=history,
        readiness=readiness,
    )

@app.route('/youtube_monitor/config', methods=['GET', 'POST'])
@login_required
def youtube_monitor_config():
    """监控配置页面"""
    app_config = load_config()
    bilibili_accounts = normalize_accounts(app_config)
    bilibili_default_account_id = default_account_id(app_config)
    if request.method == 'POST':
        try:
            # 安全的整数转换函数
            def safe_int(value, default=0):
                if not value or value.strip() == '':
                    return default
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            # 获取监控类型和模式
            monitor_type = request.form.get('monitor_type', 'youtube_search')
            channel_mode = request.form.get('channel_mode', 'latest')
            
            config_data = {
                'name': request.form.get('name', '').strip(),
                'enabled': 'enabled' in request.form,
                'monitor_type': monitor_type,
                'channel_mode': channel_mode,
                'region_code': request.form.get('region_code', 'US'),
                'category_id': request.form.get('category_id', '0'),
                'time_period': safe_int(request.form.get('time_period'), 7),
                'max_results': safe_int(request.form.get('max_results'), 10),
                'min_view_count': safe_int(request.form.get('min_view_count'), 0),
                'min_like_count': safe_int(request.form.get('min_like_count'), 0),
                'min_comment_count': safe_int(request.form.get('min_comment_count'), 0),
                'keywords': request.form.get('keywords', ''),
                'exclude_keywords': request.form.get('exclude_keywords', ''),
                'channel_ids': request.form.get('channel_ids', ''),
                'channel_keywords': request.form.get('channel_keywords', ''),
                'exclude_channel_ids': request.form.get('exclude_channel_ids', ''),
                'min_duration': safe_int(request.form.get('min_duration'), 0),
                'max_duration': safe_int(request.form.get('max_duration'), 0),
                'schedule_type': request.form.get('schedule_type', 'manual'),
                'schedule_interval': safe_int(request.form.get('schedule_interval'), 120),
                'order_by': request.form.get('order_by', 'viewCount'),
                'start_date': request.form.get('start_date', ''),
                'end_date': request.form.get('end_date', ''),
                'latest_days': safe_int(request.form.get('latest_days'), 7),
                'latest_max_results': safe_int(request.form.get('latest_max_results'), 20),
                'rate_limit_requests': safe_int(request.form.get('rate_limit_requests'), 20),
                'rate_limit_window': safe_int(request.form.get('rate_limit_window'), 60),
                'auto_add_to_tasks': 'auto_add_to_tasks' in request.form,
                'bilibili_account_id': resolve_account(
                    app_config,
                    request.form.get('bilibili_account_id'),
                )['id'],
                'video_types': ','.join(request.form.getlist('video_types') or ['video','short','live'])
            }
            
            # 验证必填项
            if not config_data['name']:
                flash('配置名称不能为空', 'danger')
                return render_template(
                    'youtube_monitor_config.html',
                    bilibili_accounts=bilibili_accounts,
                    bilibili_default_account_id=bilibili_default_account_id,
                )
            
            config_id = youtube_monitor.create_monitor_config(config_data)
            flash(f'监控配置 "{config_data["name"]}" 创建成功！', 'success')
            return redirect(url_for('youtube_monitor_index'))
            
        except Exception as e:
            flash(f'创建监控配置失败: {str(e)}', 'danger')
    
    return render_template(
        'youtube_monitor_config.html',
        bilibili_accounts=bilibili_accounts,
        bilibili_default_account_id=bilibili_default_account_id,
    )

@app.route('/youtube_monitor/config/<int:config_id>/edit', methods=['GET', 'POST'])
@login_required
def youtube_monitor_config_edit(config_id):
    """编辑监控配置"""
    app_config = load_config()
    bilibili_accounts = normalize_accounts(app_config)
    bilibili_default_account_id = default_account_id(app_config)
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        flash('监控配置不存在', 'danger')
        return redirect(url_for('youtube_monitor_index'))
    
    if request.method == 'POST':
        try:
            # 安全的整数转换函数
            def safe_int(value, default=0):
                if not value or value.strip() == '':
                    return default
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            # 获取监控类型和模式
            monitor_type = request.form.get('monitor_type', 'youtube_search')
            channel_mode = request.form.get('channel_mode', 'latest')
            
            config_data = {
                'name': request.form.get('name', '').strip(),
                'enabled': 'enabled' in request.form,
                'monitor_type': monitor_type,
                'channel_mode': channel_mode,
                'region_code': request.form.get('region_code', 'US'),
                'category_id': request.form.get('category_id', '0'),
                'time_period': safe_int(request.form.get('time_period'), 7),
                'max_results': safe_int(request.form.get('max_results'), 10),
                'min_view_count': safe_int(request.form.get('min_view_count'), 0),
                'min_like_count': safe_int(request.form.get('min_like_count'), 0),
                'min_comment_count': safe_int(request.form.get('min_comment_count'), 0),
                'keywords': request.form.get('keywords', ''),
                'exclude_keywords': request.form.get('exclude_keywords', ''),
                'channel_ids': request.form.get('channel_ids', ''),
                'channel_keywords': request.form.get('channel_keywords', ''),
                'exclude_channel_ids': request.form.get('exclude_channel_ids', ''),
                'min_duration': safe_int(request.form.get('min_duration'), 0),
                'max_duration': safe_int(request.form.get('max_duration'), 0),
                'schedule_type': request.form.get('schedule_type', 'manual'),
                'schedule_interval': safe_int(request.form.get('schedule_interval'), 120),
                'order_by': request.form.get('order_by', 'viewCount'),
                'start_date': request.form.get('start_date', ''),
                'end_date': request.form.get('end_date', ''),
                'latest_days': safe_int(request.form.get('latest_days'), 7),
                'latest_max_results': safe_int(request.form.get('latest_max_results'), 20),
                'rate_limit_requests': safe_int(request.form.get('rate_limit_requests'), 20),
                'rate_limit_window': safe_int(request.form.get('rate_limit_window'), 60),
                'auto_add_to_tasks': 'auto_add_to_tasks' in request.form,
                'bilibili_account_id': resolve_account(
                    app_config,
                    request.form.get('bilibili_account_id'),
                )['id'],
                'video_types': ','.join(request.form.getlist('video_types') or ['video','short','live'])
            }
            
            # 验证必填项
            if not config_data['name']:
                flash('配置名称不能为空', 'danger')
                return render_template(
                    'youtube_monitor_config.html',
                    config=config,
                    is_edit=True,
                    bilibili_accounts=bilibili_accounts,
                    bilibili_default_account_id=bilibili_default_account_id,
                )
            
            youtube_monitor.update_monitor_config(config_id, config_data)
            flash(f'监控配置更新成功！', 'success')
            return redirect(url_for('youtube_monitor_index'))
            
        except Exception as e:
            flash(f'更新监控配置失败: {str(e)}', 'danger')
    
    return render_template(
        'youtube_monitor_config.html',
        config=config,
        is_edit=True,
        bilibili_accounts=bilibili_accounts,
        bilibili_default_account_id=bilibili_default_account_id,
    )

@app.route('/youtube_monitor/config/<int:config_id>/delete', methods=['POST'])
@login_required
def youtube_monitor_config_delete(config_id):
    """删除监控配置"""
    try:
        config = youtube_monitor.get_monitor_config(config_id)
        if config:
            youtube_monitor.delete_monitor_config(config_id)
            flash(f'监控配置 "{config["name"]}" 删除成功！', 'success')
        else:
            flash('监控配置不存在', 'danger')
    except Exception as e:
        flash(f'删除监控配置失败: {str(e)}', 'danger')
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/config/<int:config_id>/run', methods=['POST'])
@login_required
def youtube_monitor_run(config_id):
    """立即执行一次监控任务"""
    operation_id, config, error_message = _start_monitor_run_operation(config_id)
    if error_message:
        if _is_ajax_request():
            return jsonify({'success': False, 'message': error_message}), 404
        flash(error_message, 'danger')
        return redirect(url_for('youtube_monitor_index'))

    if not config:
        fallback_message = '监控配置不存在'
        if _is_ajax_request():
            return jsonify({'success': False, 'message': fallback_message}), 404
        flash(fallback_message, 'danger')
        return redirect(url_for('youtube_monitor_index'))

    started_message = f"监控已在后台开始执行：{config['name']}"
    if _is_ajax_request():
        return jsonify({
            'success': True,
            'message': started_message,
            'operation_id': operation_id,
            'config_id': config_id,
        })

    flash(f'{started_message}，请稍后刷新查看结果。', 'info')
    return redirect(url_for('youtube_monitor_history', config_id=config_id))


@app.route('/youtube_monitor/run-status/<operation_id>', methods=['GET'])
@login_required
def youtube_monitor_run_status(operation_id):
    """查询后台监控任务的执行状态"""
    progress = _get_monitor_run_progress(operation_id)
    if not progress:
        return jsonify({
            'found': False,
            'config_id': None,
            'message': '',
            'detail': '',
            'done': True,
            'level': 'error',
            'success': False,
        })

    return jsonify({
        'found': True,
        'config_id': progress.get('config_id'),
        'message': progress.get('message', ''),
        'detail': progress.get('detail', ''),
        'done': progress.get('done', False),
        'level': progress.get('level', 'info'),
        'success': progress.get('success'),
    })

@app.route('/youtube_monitor/history/<int:config_id>')
@login_required
def youtube_monitor_history(config_id):
    """查看指定监控配置的发现历史"""
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        flash('监控配置不存在', 'danger')
        return redirect(url_for('youtube_monitor_index'))
    
    history = youtube_monitor.get_monitor_history(config_id, limit=200)
    
    # 计算统计数据
    stats = {
        'total_records': len(history),
        'added_to_tasks': 0,
        'avg_views': 0,
        'avg_likes': 0
    }
    
    if history:
        total_views = 0
        total_likes = 0
        
        for record in history:
            if record.get('added_to_tasks'):
                stats['added_to_tasks'] += 1
            total_views += record.get('view_count', 0)
            total_likes += record.get('like_count', 0)
        
        stats['avg_views'] = int(total_views / len(history))
        stats['avg_likes'] = int(total_likes / len(history))
    
    return render_template('youtube_monitor_history.html', history=history, config=config, stats=stats)

@app.route('/youtube_monitor/add_to_tasks', methods=['POST'])
@login_required
def youtube_monitor_add_to_tasks():
    """从监控历史中添加视频到任务列表"""
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    config_id = data.get('config_id')
    if not video_id or not config_id:
        return jsonify({'success': False, 'message': '参数不完整'}), 400
    try:
        config_id_int = int(config_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'config_id 无效'}), 400

    success, message = youtube_monitor.add_video_to_tasks_manually(video_id, config_id_int)

    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'message': message}), 400

@app.route('/youtube_monitor/history/<int:config_id>/clear', methods=['POST'])
@login_required
def youtube_monitor_clear_history(config_id):
    """清空指定监控任务的历史记录"""
    youtube_monitor.clear_monitor_history(config_id)
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/history/clear_all', methods=['POST'])
@login_required
def youtube_monitor_clear_all_history():
    """清空所有历史记录"""
    youtube_monitor.clear_all_monitor_history()
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/restore_configs', methods=['POST'])
@login_required
def youtube_monitor_restore_configs():
    """恢复默认监控配置"""
    youtube_monitor.restore_configs_from_files_manually()
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/config/<int:config_id>/reset_offset', methods=['POST'])
@login_required
def youtube_monitor_reset_offset(config_id):
    """重置频道监控的视频偏移量"""
    youtube_monitor.reset_historical_offset(config_id)
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/api/cookies/sync', methods=['POST'])
@login_required
def sync_cookies():
    """
    接收从浏览器扩展同步过来的Cookie
    """
    try:
        if not request.is_json:
            return jsonify({'error': '请求必须是JSON格式'}), 400
        
        data = request.get_json()
        
        # 验证必要字段
        required_fields = ['source', 'timestamp', 'cookies', 'cookieCount']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'缺少必要字段: {field}'}), 400
        
        # 验证来源
        if data['source'] not in ['userscript', 'extension']:
            return jsonify({'error': '不支持的cookie来源'}), 400
        
        # 验证cookie数据
        cookies_content = data['cookies']
        if not cookies_content or not isinstance(cookies_content, str):
            return jsonify({'error': 'cookie数据无效'}), 400
        
        # 保存cookie到文件
        cookies_dir = get_app_subdir('cookies')
        os.makedirs(cookies_dir, exist_ok=True)
        
        youtube_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
        
        # 写入新的cookie文件
        try:
            with open(youtube_cookies_path, 'w', encoding='utf-8') as f:
                f.write(cookies_content)
            
            # 记录同步信息
            sync_info = {
                'timestamp': data['timestamp'],
                'sync_time': time.time(),
                'cookie_count': data['cookieCount'],
                'user_agent': data.get('userAgent', ''),
                'source_url': data.get('url', ''),
                'file_size': len(cookies_content)
            }
            
            source_name = '浏览器扩展' if data['source'] == 'extension' else '油猴脚本'
            logger.info(f"Cookie同步成功 - 来源: {source_name}, 数量: {data['cookieCount']}, 大小: {len(cookies_content)} bytes")

            return jsonify({
                'success': True,
                'message': 'Cookie同步成功',
                'sync_info': sync_info
            }), 200
            
        except Exception as e:
            logger.error(f"写入cookie文件失败: {str(e)}")
            return jsonify({'error': '保存cookie失败，请稍后重试'}), 500

    except Exception as e:
        logger.error(f"Cookie同步API异常: {str(e)}")
        return jsonify({'error': '服务器内部错误，请稍后重试'}), 500

@app.route('/api/cookies/status', methods=['GET'])
@login_required
def get_cookie_status():
    """
    提供Cookie状态给浏览器扩展
    """
    try:
        cookies_dir = get_app_subdir('cookies')
        youtube_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
        
        status = {
            'youtube_cookies_exists': os.path.exists(youtube_cookies_path),
            'last_modified': None,
            'file_size': 0,
            'line_count': 0
        }
        
        if status['youtube_cookies_exists']:
            stat_info = os.stat(youtube_cookies_path)
            status['last_modified'] = stat_info.st_mtime
            status['file_size'] = stat_info.st_size
            
            # 统计行数
            try:
                with open(youtube_cookies_path, 'r', encoding='utf-8') as f:
                    status['line_count'] = sum(1 for line in f if line.strip() and not line.startswith('#'))
            except Exception as e:
                logger.warning(f"读取cookie文件失败: {str(e)}")
                status['line_count'] = -1
        
        return jsonify(status), 200
        
    except Exception as e:
        logger.error(f"获取cookie状态失败: {str(e)}")
        return jsonify({'error': '获取状态失败，请稍后重试'}), 500

if __name__ == '__main__':
    logger.info("PotatoFlow 启动中...")

    # 加载配置
    config = load_config()
    app.config['POTATOFLOW_SETTINGS'] = config
    logger.info(
        "配置已加载（摘要）: %s",
        json.dumps(_build_startup_config_log_summary(config), ensure_ascii=False)
    )
    _sync_notification_service(config)

    # 初始化全局任务处理器，确保并发控制生效
    from modules.task_manager import get_global_task_processor, shutdown_global_task_processor
    get_global_task_processor(config)
    logger.info("全局任务处理器已初始化")

    # 自动启动所有pending任务（如果启用了自动模式）
    if config.get('AUTO_MODE_ENABLED', False):
        logger.info("自动模式已启用，正在启动所有pending任务...")
        auto_start_pending_tasks(config)

    # 初始化YouTube监控API
    if config.get('YOUTUBE_API_KEY'):
        api_ready, api_status = youtube_monitor.reload_api_client(config)
        if api_ready:
            youtube_monitor.start_all_schedules()
            if api_status == 'proxy_ready':
                logger.info("YouTube监控系统已初始化，独立代理已启用")
            else:
                logger.info("YouTube监控系统已初始化，当前为直连模式")
        else:
            if api_status == 'missing_api_key':
                logger.warning('YouTube API 密钥未配置，请先在设置页完成接入。')
            else:
                logger.warning('YouTube监控 API 初始化失败，请检查 API 密钥、代理配置与网络连通性。')

    # 配置应用
    configure_app(app, config)

    # 录制器是统一服务的内部无端口 worker。Linux systemd/Docker 只需启动
    # 当前 Web 服务不再运行独立录制服务或开放旧端口 19159。
    if os.environ.get('AUTO_START_RECORDER', '1').strip().lower() not in ('0', 'false', 'no'):
        try:
            if live_recorder_manager.list_rooms():
                live_recorder_manager.start()
                logger.info("内部录制 worker 已自动启动（无额外 HTTP 端口）")
        except RecorderConfigError as exc:
            logger.warning("内部录制 worker 自动启动失败: %s", exc)

    # 设置日志清理定时任务
    log_cleanup_scheduler = schedule_log_cleanup()

    # 设置下载内容清理定时任务
    download_cleanup_scheduler = schedule_download_cleanup()

    try:
        port = int(os.environ.get('PORT', 5001))
        logger.info(f"服务启动，监听地址: http://127.0.0.1:{port}")
        # 使用标准Flask运行
        desktop_mode = str(os.environ.get('POTATOFLOW_DESKTOP_MODE') or '').strip().lower() in ('1', 'true', 'yes')
        host = '127.0.0.1' if desktop_mode else '0.0.0.0'
        if desktop_mode:
            from werkzeug.serving import make_server

            _desktop_server = make_server(host, port, app, threaded=True)
            _desktop_server.serve_forever()
        else:
            app.run(host=host, port=port, debug=False)
    except KeyboardInterrupt:
        logger.info("接收到退出信号，服务正在关闭...")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
    finally:
        shutdown_global_telegram_control()
        live_recorder_manager.stop()

        # 关闭全局任务处理器
        shutdown_global_task_processor()

        if log_cleanup_scheduler:
            log_cleanup_scheduler.shutdown()
        if download_cleanup_scheduler:
            download_cleanup_scheduler.shutdown()
        logger.info("服务已关闭")
