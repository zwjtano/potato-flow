"""Danmaku XML danmaku parsing, ASS rendering and FFmpeg burn-in helpers."""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - production runs on Linux/macOS
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - production runs on Windows
    msvcrt = None


_BURN_THREAD_LOCK = threading.Lock()
_ENCODER_PROBE_LOCK = threading.Lock()
_ENCODER_PROBE_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_CPU_ENCODER_PROBE_SIZE = "128x128"
_HARDWARE_ENCODER_PROBE_SIZE = "640x360"
_DEFAULT_RENDER_BLOCKLIST = {"合成大西瓜"}


def _background_subprocess_kwargs() -> dict[str, Any]:
    """Keep FFmpeg and hardware probes out of the Windows desktop."""
    if os.name != "nt":
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    """Keep short-lived probes from flashing a console on Windows."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


ENCODER_PROFILES: dict[str, dict[str, Any]] = {
    "cpu": {
        "label": "CPU（libx264）",
        "ffmpeg_encoder": "libx264",
        "preset": "medium",
        "quality_name": "CRF",
        "quality": 20,
        "presets": ("veryfast", "faster", "fast", "medium", "slow", "slower"),
    },
    "nvidia": {
        "label": "NVIDIA（NVENC）",
        "ffmpeg_encoder": "h264_nvenc",
        "preset": "p5",
        "quality_name": "CQ",
        "quality": 20,
        "presets": ("p1", "p2", "p3", "p4", "p5", "p6", "p7"),
    },
    "intel": {
        "label": "Intel（QSV）",
        "ffmpeg_encoder": "h264_qsv",
        "preset": "medium",
        "quality_name": "global_quality",
        "quality": 20,
        "presets": ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"),
    },
    "amd": {
        "label": "AMD（AMF）",
        "ffmpeg_encoder": "h264_amf",
        "preset": "balanced",
        "quality_name": "QVBR",
        "quality": 20,
        "presets": ("speed", "balanced", "quality"),
    },
}


def _encoder_video_args(backend: str, preset: str, quality: int) -> list[str]:
    profile = ENCODER_PROFILES.get(backend, ENCODER_PROFILES["cpu"])
    selected_preset = str(preset or profile["preset"])
    if selected_preset not in profile["presets"]:
        selected_preset = str(profile["preset"])
    value = str(max(0, min(51, int(quality))))
    if backend == "nvidia":
        return ["-c:v", "h264_nvenc", "-preset", selected_preset, "-cq", value, "-b:v", "0"]
    if backend == "intel":
        return ["-c:v", "h264_qsv", "-preset", selected_preset, "-global_quality", value]
    if backend == "amd":
        return [
            "-c:v", "h264_amf", "-quality", selected_preset, "-rc", "qvbr",
            "-qvbr_quality_level", value,
        ]
    return ["-c:v", "libx264", "-preset", selected_preset, "-crf", value]


def _nvidia_devices() -> list[dict[str, str]]:
    """Return NVIDIA model and driver data when nvidia-smi is available."""
    executable = shutil.which("nvidia-smi")
    if not executable and os.name == "nt":
        windows_dir = Path(os.environ.get("WINDIR") or r"C:\Windows")
        candidates = [
            windows_dir / "System32" / "nvidia-smi.exe",
            Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
            / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        ]
        executable = next((str(path) for path in candidates if path.is_file()), None)
    if not executable:
        return []
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            **_hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    devices = []
    for line in str(result.stdout or "").splitlines():
        name, separator, driver = line.partition(",")
        name = name.strip()
        if name:
            devices.append({"name": name, "driver": driver.strip() if separator else ""})
    return devices


def _cpu_device() -> dict[str, Any]:
    """Return a user-facing CPU identity without requiring optional packages."""
    name = ""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0] or "").strip()
        except (OSError, ImportError):
            pass
    if not name and platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
                **_hidden_subprocess_kwargs(),
            )
            if result.returncode == 0:
                name = str(result.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if not name and Path("/proc/cpuinfo").is_file():
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^(?:model name|hardware)\s*:\s*(.+)$", cpuinfo, re.MULTILINE | re.IGNORECASE)
            if match:
                name = match.group(1).strip()
        except OSError:
            pass
    if not name:
        name = str(platform.processor() or "").strip()
    name = re.sub(r"\s+", " ", name).strip() or "未识别型号的 CPU"
    return {"name": name, "logical_cores": int(os.cpu_count() or 0)}


def _windows_graphics_devices() -> list[dict[str, str]]:
    """Enumerate Windows display adapters through the built-in CIM provider."""
    if os.name != "nt":
        return []
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return []
    script = (
        "@(Get-CimInstance Win32_VideoController | "
        "Select-Object Name,DriverVersion,PNPDeviceID) | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
            **_hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not str(result.stdout or "").strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return []
    rows = payload if isinstance(payload, list) else [payload]
    devices = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        lowered = f"{name} {row.get('PNPDeviceID') or ''}".lower()
        if "nvidia" in lowered:
            backend = "nvidia"
        elif "amd" in lowered or "radeon" in lowered or "advanced micro devices" in lowered:
            backend = "amd"
        elif "intel" in lowered:
            backend = "intel"
        else:
            backend = "unknown"
        if name:
            devices.append({
                "name": name,
                "driver": str(row.get("DriverVersion") or "").strip(),
                "backend": backend,
            })
    return devices


def _graphics_devices() -> list[dict[str, str]]:
    devices = _windows_graphics_devices()
    for device in _nvidia_devices():
        normalized = device["name"].casefold()
        existing = next(
            (item for item in devices if item["name"].casefold() == normalized),
            None,
        )
        if existing:
            existing["backend"] = "nvidia"
            existing["driver"] = device.get("driver") or existing.get("driver", "")
        else:
            devices.append({**device, "backend": "nvidia"})
    return devices


def _resolve_encoder_for_devices(ffmpeg: str, requested: str) -> str:
    """Map stale hardware selections to the encoder supported by this machine."""
    requested = str(requested or "auto").strip().lower()
    if requested == "cpu":
        return requested
    devices = _graphics_devices()
    if requested == "auto" or not any(item.get("backend") == requested for item in devices):
        recommendation = probe_encoding_capabilities(ffmpeg, preferred="auto").get("recommendation", {})
        return str(recommendation.get("id") or "cpu")
    return requested


def _probe_failure_reason(backend: str, error: str, devices: list[dict[str, str]]) -> str:
    lowered = str(error or "").lower()
    if backend == "nvidia" and devices:
        names = "、".join(device["name"] for device in devices)
        if "minimum" in lowered or "dimension" in lowered or "width" in lowered or "height" in lowered:
            return f"已识别 {names}，但测试画面尺寸被当前驱动拒绝"
        if "driver does not support" in lowered or "required nvenc api" in lowered:
            return f"已识别 {names}，但 NVIDIA 驱动不支持当前 FFmpeg 所需的 NVENC API"
        return f"已识别 {names}，但 NVENC 实际编码测试失败"
    if devices:
        names = "、".join(device["name"] for device in devices)
        return f"已识别 {names}，但硬件编码实际测试失败"
    if "unknown encoder" in lowered:
        return "当前 FFmpeg 未包含该硬件编码器"
    if "cannot load" in lowered or "not found" in lowered:
        return "显卡驱动或硬件编码运行库未就绪"
    return "实际编码测试未通过"


def probe_encoding_capabilities(
    ffmpeg: str = "ffmpeg",
    *,
    preferred: str = "auto",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Probe H.264 encoders with a tiny real encode and return a safe recommendation."""
    preferred = str(preferred or "auto").strip().lower()
    if preferred not in {"auto", *ENCODER_PROFILES}:
        preferred = "auto"
    cache_key = (str(ffmpeg), preferred)
    now = time.monotonic()
    with _ENCODER_PROBE_LOCK:
        cached = _ENCODER_PROBE_CACHE.get(cache_key)
        if not force_refresh and cached and now - cached[0] < 300:
            return dict(cached[1])

    cpu_device = _cpu_device()
    graphics_devices = _graphics_devices()
    capabilities: list[dict[str, Any]] = []
    for backend, profile in ENCODER_PROFILES.items():
        # Several hardware encoders reject tiny synthetic frames even when
        # ordinary recordings encode correctly. This previously made a valid
        # RTX 2060 appear unavailable because NVIDIA was probed at 128x128.
        probe_size = (
            _CPU_ENCODER_PROBE_SIZE
            if backend == "cpu"
            else _HARDWARE_ENCODER_PROBE_SIZE
        )
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            # Hardware encoders can reject tiny synthetic inputs even though
            # real recordings encode correctly. Use a normal 16:9 frame for
            # NVENC/QSV/AMF while keeping the CPU probe cheap.
            "-i", f"color=c=black:s={probe_size}:d=0.12", "-frames:v", "1",
            *_encoder_video_args(backend, str(profile["preset"]), int(profile["quality"])),
            "-an", "-f", "null", "-",
        ]
        available = False
        error = ""
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=20,
                **_hidden_subprocess_kwargs(),
            )
            available = result.returncode == 0
            if not available:
                error = str(result.stderr or result.stdout or "").strip()[-800:]
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
        devices = (
            [cpu_device]
            if backend == "cpu"
            else [device for device in graphics_devices if device.get("backend") == backend]
        )
        label = str(profile["label"])
        if devices:
            suffix = {
                "cpu": "CPU / libx264",
                "nvidia": "NVENC",
                "intel": "QSV",
                "amd": "AMF",
            }[backend]
            if backend == "cpu" and cpu_device.get("logical_cores"):
                suffix += f"，{cpu_device['logical_cores']} 线程"
            elif devices[0].get("driver"):
                suffix += f"，驱动 {devices[0]['driver']}"
            extra = f" 等 {len(devices)} 张" if len(devices) > 1 else ""
            label = f"{devices[0]['name']}{extra}（{suffix}）"
        capabilities.append({
            "id": backend,
            "label": label,
            "available": available,
            "ffmpeg_encoder": profile["ffmpeg_encoder"],
            "preset": profile["preset"],
            "quality_name": profile["quality_name"],
            "quality": profile["quality"],
            "error": error,
            "reason": "实际编码测试通过" if available else _probe_failure_reason(backend, error, devices),
            "devices": devices,
            "probe_size": probe_size,
        })

    available_ids = {item["id"] for item in capabilities if item["available"]}
    if preferred != "auto" and preferred in available_ids:
        selected = preferred
        reason = "已验证当前配置指定的编码器可用"
    else:
        selected = next(
            (backend for backend in ("nvidia", "intel", "amd", "cpu") if backend in available_ids),
            "cpu",
        )
        reason = "按实际 FFmpeg 编码测试选择，CPU 始终作为保底"
    recommendation = next(
        (dict(item) for item in capabilities if item["id"] == selected),
        {"id": "cpu", **ENCODER_PROFILES["cpu"]},
    )
    recommendation["reason"] = reason
    payload = {
        "preferred": preferred,
        "hardware": {"cpu": cpu_device, "gpus": graphics_devices},
        "capabilities": capabilities,
        "recommendation": recommendation,
        "cached_seconds": 300,
    }
    with _ENCODER_PROBE_LOCK:
        _ENCODER_PROBE_CACHE[cache_key] = (now, payload)
    return dict(payload)


@contextmanager
def danmaku_burn_slot(
    status_callback: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Serialize FFmpeg burn-in across threads and bridge processes."""
    configured = str(os.environ.get("POTATO_DANMAKU_BURN_LOCK") or "").strip()
    lock_path = Path(configured) if configured else Path(tempfile.gettempdir()) / "potato-flow-danmaku-burn.lock"
    lock_path = lock_path.expanduser().resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    def report(status: str) -> None:
        if status_callback:
            try:
                status_callback(status)
            except Exception:
                pass

    report("queued")
    with _BURN_THREAD_LOCK:
        with lock_path.open("a+b") as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:
                lock_handle.seek(0, os.SEEK_END)
                if lock_handle.tell() == 0:
                    lock_handle.write(b"\0")
                    lock_handle.flush()
                while True:
                    try:
                        lock_handle.seek(0)
                        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.2)
            try:
                report("burning")
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True)
class DanmakuComment:
    time: float
    text: str
    color: int = 0xFFFFFF
    mode: int = 1


def parse_danmaku_xml(path: Path) -> list[DanmakuComment]:
    """Parse Bilibili-compatible `<d p="...">text</d>` entries from recorder XML."""
    comments: list[DanmakuComment] = []
    root = ET.parse(path).getroot()
    for elem in root.iter("d"):
        fields = str(elem.attrib.get("p", "")).split(",")
        text = re.sub(r"\s+", " ", "".join(elem.itertext())).strip()
        if not text or not fields:
            continue
        try:
            timestamp = max(0.0, float(fields[0]))
        except (TypeError, ValueError):
            continue
        try:
            mode = int(fields[1]) if len(fields) > 1 else 1
        except (TypeError, ValueError):
            mode = 1
        try:
            color = int(fields[3]) if len(fields) > 3 else 0xFFFFFF
        except (TypeError, ValueError):
            color = 0xFFFFFF
        comments.append(DanmakuComment(timestamp, text[:200], color & 0xFFFFFF, mode))
    comments.sort(key=lambda item: item.time)
    return comments


def inspect_danmaku_xml(
    path: Path,
    comments: list[DanmakuComment] | None = None,
) -> dict[str, Any]:
    """Return auditable entry and timeline diagnostics for one XML sidecar."""
    parsed = comments if comments is not None else parse_danmaku_xml(path)
    root = ET.parse(path).getroot()
    xml_entries = sum(1 for _ in root.iter("d"))
    timeline = [comment.time for comment in parsed]
    first_second = min(timeline) if timeline else None
    last_second = max(timeline) if timeline else None
    return {
        "danmaku_xml_entries": xml_entries,
        "danmaku_count": len(parsed),
        "danmaku_invalid_count": max(0, xml_entries - len(parsed)),
        "danmaku_first_second": first_second,
        "danmaku_last_second": last_second,
        "danmaku_timeline_span_seconds": (
            max(0.0, float(last_second) - float(first_second))
            if first_second is not None and last_second is not None
            else 0.0
        ),
    }


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(seconds * 100)))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\uFF5B").replace("}", "\uFF5D").replace("\n", " ")


def _ass_bgr(color: int) -> str:
    red = (color >> 16) & 0xFF
    green = (color >> 8) & 0xFF
    blue = color & 0xFF
    return f"{blue:02X}{green:02X}{red:02X}"


def _estimated_text_width(text: str, font_size: int) -> int:
    """Conservatively estimate rendered width for mixed CJK/Latin danmaku."""
    units = 0.0
    for char in text:
        if char.isspace():
            units += 0.34
        elif unicodedata.east_asian_width(char) in {"W", "F", "A"}:
            units += 1.0
        else:
            units += 0.62
    # Include a small allowance for the ASS outline and font metric variance.
    return max(font_size, int(math.ceil(units * font_size)) + 4)


def build_ass(
    comments: list[DanmakuComment],
    output: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    font_name: str = "Noto Sans CJK SC",
    font_size: int = 42,
    duration: float = 9.0,
    opacity: float = 0.92,
) -> Path:
    """Render comments into ASS without allowing occupied lanes to overlap."""
    width = max(320, int(width))
    height = max(240, int(height))
    font_size = max(16, int(font_size))
    duration = max(3.0, float(duration))
    alpha = max(0, min(255, int(round((1.0 - max(0.0, min(1.0, opacity))) * 255))))
    primary = f"&H{alpha:02X}FFFFFF"
    outline = "&H80000000"
    # Leave enough breathing room for the glyph outline and keep gameplay UI
    # visible.  The old 42px/52px layout produced 14 tightly packed lanes at
    # 1080p, which looked overlapped even when the lane math was technically
    # collision-free.
    lane_height = max(font_size + 12, int(math.ceil(font_size * 1.4)))
    lane_count = max(1, int((height * 0.85) // lane_height))
    # Each entry is ``(kind, start, end, estimated_width)``.  Scrolling
    # comments may safely share a lane before the previous comment ends once
    # both their leading and trailing edges can no longer catch each other.
    # Fixed comments reserve the whole lane for their complete lifetime.
    lane_state: list[tuple[str, float, float, int] | None] = [None] * lane_count

    def scrolling_lane_available(
        state: tuple[str, float, float, int] | None,
        start: float,
        estimated_width: int,
    ) -> bool:
        if state is None:
            return True
        kind, previous_start, previous_end, previous_width = state
        if start >= previous_end:
            return True
        if kind != "scroll":
            return False
        delay = max(0.0, start - previous_start)
        previous_entry = duration * previous_width / (width + previous_width)
        current_entry = duration * estimated_width / (width + estimated_width)
        return delay >= max(previous_entry, current_entry)

    def first_free_fixed_lane(start: float, *, reverse: bool = False) -> int | None:
        indices = range(lane_count - 1, -1, -1) if reverse else range(lane_count)
        return next(
            (
                lane
                for lane in indices
                if lane_state[lane] is None or start >= lane_state[lane][2]
            ),
            None,
        )

    header = f"""[Script Info]
Title: 简体中文弹幕
Original Script: PotatoFlow
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Danmaku,{font_name},{font_size},{primary},{primary},{outline},&H00000000,0,0,0,0,100,100,0,0,1,1.6,0,7,0,0,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = [header]
    for comment in comments:
        if re.sub(r"\s+", "", comment.text).casefold() in _DEFAULT_RENDER_BLOCKLIST:
            continue
        text = _escape_ass_text(comment.text)
        color_tag = f"\\c&H{_ass_bgr(comment.color)}&" if comment.color != 0xFFFFFF else ""
        if comment.mode == 5:  # fixed top
            start = comment.time
            end = start + min(duration, 5.0)
            lane = first_free_fixed_lane(start)
            if lane is None:
                continue
            lane_state[lane] = ("fixed", start, end, 0)
            y = 10 + lane * lane_height
            override = f"{{\\an8\\pos({width // 2},{y}){color_tag}}}"
        elif comment.mode == 4:  # fixed bottom
            start = comment.time
            end = start + min(duration, 5.0)
            lane = first_free_fixed_lane(start, reverse=True)
            if lane is None:
                continue
            lane_state[lane] = ("fixed", start, end, 0)
            y = 10 + (lane + 1) * lane_height
            override = f"{{\\an2\\pos({width // 2},{y}){color_tag}}}"
        else:
            start = comment.time
            estimated_width = _estimated_text_width(comment.text, font_size)
            end = start + duration
            available_lanes = [
                index
                for index, state in enumerate(lane_state)
                if scrolling_lane_available(state, start, estimated_width)
            ]
            if not available_lanes:
                continue
            # Spread comments vertically by preferring the least recently used
            # safe lane. Picking the first safe lane bunches sparse comments at
            # the top even though the rest of the screen is available.
            lane = min(
                available_lanes,
                key=lambda index: (
                    lane_state[index][2] if lane_state[index] is not None else -1.0,
                    index,
                ),
            )
            lane_state[lane] = ("scroll", start, end, estimated_width)
            y = 10 + lane * lane_height
            override = f"{{\\an7\\move({width},{y},{-estimated_width},{y}){color_tag}}}"
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Danmaku,,0,0,0,,{override}{text}\n"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(lines), encoding="utf-8-sig")
    return output


def probe_video_size(video: Path, ffprobe: str = "ffprobe") -> tuple[int, int]:
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(video),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            **_hidden_subprocess_kwargs(),
        )
        match = re.search(r"(\d+)x(\d+)", result.stdout)
        if result.returncode == 0 and match:
            return int(match.group(1)), int(match.group(2))
    except (OSError, subprocess.SubprocessError):
        pass
    return 1920, 1080


def _filter_path(path: Path) -> str:
    # Escaping required by FFmpeg's filter parser (in addition to argv handling).
    value = str(path.resolve()).replace("\\", "\\\\")
    for char in (":", "'", "[", "]", ","):
        value = value.replace(char, f"\\{char}")
    return value


def burn_ass(
    video: Path,
    ass_path: Path,
    output: Path,
    *,
    ffmpeg: str = "ffmpeg",
    fonts_dir: Path | None = None,
    preset: str = "medium",
    crf: int = 20,
    encoder: str = "cpu",
    queue_status_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    with danmaku_burn_slot(queue_status_callback):
        output.parent.mkdir(parents=True, exist_ok=True)
        video_filter = f"subtitles=filename='{_filter_path(ass_path)}'"
        if fonts_dir and fonts_dir.is_dir():
            video_filter += f":fontsdir='{_filter_path(fonts_dir)}'"
        selected_encoder = _resolve_encoder_for_devices(ffmpeg, str(encoder or "auto"))
        if selected_encoder not in ENCODER_PROFILES:
            selected_encoder = "cpu"
        profile = ENCODER_PROFILES[selected_encoder]
        selected_preset = str(preset or profile["preset"])
        if selected_preset not in profile["presets"]:
            selected_preset = str(profile["preset"])
        base_command = [
            ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(video),
            "-vf", video_filter,
        ]
        duration_seconds = 0.0
        ffprobe = "ffprobe"
        ffmpeg_path = Path(str(ffmpeg)).expanduser()
        sibling_ffprobe = ffmpeg_path.with_name("ffprobe")
        if ffmpeg_path.parent != Path(".") and sibling_ffprobe.is_file():
            ffprobe = str(sibling_ffprobe)
        try:
            probe = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(video),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                **_hidden_subprocess_kwargs(),
            )
            if probe.returncode == 0:
                duration_seconds = max(0.0, float(probe.stdout.strip() or 0))
        except (OSError, ValueError, subprocess.SubprocessError):
            duration_seconds = 0.0

        def encode(video_args: list[str], audio_args: list[str]) -> tuple[int, str]:
            command = base_command + video_args + audio_args + [
                "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
                str(output),
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                # FFmpeg can emit enough subtitle warnings to fill stderr's
                # pipe while the parent is reading progress from stdout. Merge
                # both streams so every line is drained as it arrives.
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                **_background_subprocess_kwargs(),
            )
            progress: dict[str, str] = {}
            diagnostics: deque[str] = deque(maxlen=200)
            if process.stdout is not None:
                for raw_line in process.stdout:
                    key, separator, value = raw_line.strip().partition("=")
                    if not separator:
                        if raw_line.strip():
                            diagnostics.append(raw_line.rstrip())
                        continue
                    progress[key] = value
                    if key != "progress" or not progress_callback:
                        continue
                    processed_seconds = 0.0
                    try:
                        processed_seconds = float(progress.get("out_time_us") or 0) / 1_000_000
                    except (TypeError, ValueError):
                        pass
                    speed_text = str(progress.get("speed") or "0").rstrip("x")
                    try:
                        encode_speed = max(0.0, float(speed_text))
                    except (TypeError, ValueError):
                        encode_speed = 0.0
                    percent = (
                        min(99.9, max(0.0, processed_seconds / duration_seconds * 100))
                        if duration_seconds > 0
                        else 0.0
                    )
                    eta_seconds = (
                        max(0.0, duration_seconds - processed_seconds) / encode_speed
                        if duration_seconds > 0 and encode_speed > 0
                        else None
                    )
                    try:
                        progress_callback({
                            "percent": percent,
                            "processed_seconds": processed_seconds,
                            "duration_seconds": duration_seconds,
                            "encode_speed": encode_speed,
                            "eta_seconds": eta_seconds,
                            "encoder_requested": str(encoder or "cpu"),
                            "encoder_used": selected_encoder,
                            "ffmpeg_encoder": profile["ffmpeg_encoder"],
                            "preset": selected_preset,
                            "quality_name": profile["quality_name"],
                            "quality": max(0, min(51, int(crf))),
                        })
                    except Exception:
                        pass
                    progress = {}
            # Test doubles and unusual subprocess wrappers may still expose a
            # separate stderr stream. Drain it without changing real FFmpeg's
            # single-stream behavior.
            if process.stderr is not None:
                diagnostics.extend(process.stderr.read().splitlines())
            return process.wait(), "\n".join(diagnostics)

        def encode_with_audio_retry(video_args: list[str]) -> tuple[int, str]:
            returncode, stderr = encode(video_args, ["-c:a", "copy"])
            if returncode == 0:
                return returncode, stderr
            # Opus and a few live codecs cannot be stream-copied into MP4. Retry
            # with AAC while keeping the expensive video encode settings intact.
            output.unlink(missing_ok=True)
            return encode(video_args, ["-c:a", "aac", "-b:a", "192k"])

        video_args = _encoder_video_args(selected_encoder, selected_preset, crf)
        returncode, stderr = encode_with_audio_retry(video_args)
        if returncode != 0 and selected_encoder != "cpu":
            hardware_error = stderr.strip()[-1200:]
            output.unlink(missing_ok=True)
            if progress_callback:
                progress_callback({
                    "encoder_fallback": True,
                    "warning": "硬件编码失败，本次任务已自动回退到 CPU libx264 / medium / CRF 20",
                    "encoder_fallback_from": selected_encoder,
                    "encoder_fallback_to": "cpu",
                    "encoder_fallback_error": hardware_error,
                    "encoder_used": "cpu",
                    "ffmpeg_encoder": "libx264",
                    "preset": "medium",
                    "quality_name": "CRF",
                    "quality": 20,
                })
            selected_encoder = "cpu"
            profile = ENCODER_PROFILES["cpu"]
            selected_preset = "medium"
            returncode, stderr = encode_with_audio_retry(
                _encoder_video_args("cpu", "medium", 20)
            )
        if returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
            detail = stderr.strip()[-2000:]
            raise RuntimeError(f"FFmpeg 烧录 ASS 失败: {detail}")
        if progress_callback:
            progress_callback({
                "percent": 100.0,
                "processed_seconds": duration_seconds,
                "duration_seconds": duration_seconds,
                "encode_speed": 0.0,
                "eta_seconds": 0.0,
            })
        return output


def deduplicate_summary_comments(
    comments: list[DanmakuComment],
    max_repeats: int = 2,
) -> list[DanmakuComment]:
    """Keep every distinct useful comment while bounding identical spam."""
    max_repeats = max(1, min(10, int(max_repeats)))
    unique: list[DanmakuComment] = []
    seen: dict[str, int] = {}
    for item in comments:
        normalized = re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", item.text.lower())
        if len(normalized) < 2:
            continue
        count = seen.get(normalized, 0)
        seen[normalized] = count + 1
        if count < max_repeats:
            unique.append(item)
    return unique


def batch_summary_comments(
    comments: list[DanmakuComment],
    batch_size: int = 600,
) -> list[list[DanmakuComment]]:
    """Split all deduplicated comments into chronological model-sized batches."""
    batch_size = max(100, min(1200, int(batch_size)))
    unique = deduplicate_summary_comments(comments)
    return [
        unique[index:index + batch_size]
        for index in range(0, len(unique), batch_size)
    ]


def select_summary_comments(comments: list[DanmakuComment], limit: int = 800) -> list[DanmakuComment]:
    """Deduplicate spam while retaining both chronology and concrete evidence."""
    limit = max(1, min(2000, int(limit)))
    unique = deduplicate_summary_comments(comments)
    if len(unique) <= limit:
        return unique

    # A single fixed-stride pick can skip the one concrete line that explains a
    # nearby reaction burst (for example "20秒买活").  Split the full
    # recording into chronological slices and retain both the slice boundary and
    # its most informative line.  This keeps whole-recording coverage without
    # sacrificing rare numeric or action-specific evidence needed for grounding.
    slot_count = max(1, limit // 2)
    selected_indexes: set[int] = set()

    def information_score(item: DanmakuComment) -> tuple[int, int]:
        text = str(item.text or "").strip()
        normalized = re.sub(
            r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+",
            "",
            text.lower(),
        )
        # Precise counts, scores and durations are easy to lose because they are
        # often much shorter than surrounding reactions, yet they are the facts
        # a title or chapter label must not infer.
        numeric_bonus = 48 if re.search(r"\d", normalized) else 0
        diversity = len(set(normalized))
        repetition_penalty = (
            40
            if len(normalized) >= 8 and diversity / max(1, len(normalized)) < 0.35
            else 0
        )
        return (
            min(len(normalized), 80)
            + numeric_bonus
            + min(diversity, 20)
            - repetition_penalty,
            -len(text),
        )

    for slot in range(slot_count):
        start = int(math.floor(slot * len(unique) / slot_count))
        end = int(math.floor((slot + 1) * len(unique) / slot_count))
        end = max(start + 1, min(len(unique), end))
        selected_indexes.add(start)
        richest = max(range(start, end), key=lambda index: information_score(unique[index]))
        selected_indexes.add(richest)

    if len(selected_indexes) < limit:
        for index in range(len(unique)):
            selected_indexes.add(index)
            if len(selected_indexes) >= limit:
                break
    return [unique[index] for index in sorted(selected_indexes)]


def format_comments_for_ai(comments: list[DanmakuComment]) -> str:
    def timestamp(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    return "\n".join(f"[{timestamp(item.time)}] {item.text}" for item in comments)
