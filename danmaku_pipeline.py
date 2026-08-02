"""Danmaku XML danmaku parsing, ASS rendering and FFmpeg burn-in helpers."""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
import threading
import time
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


_BURN_THREAD_LOCK = threading.Lock()
_ENCODER_PROBE_LOCK = threading.Lock()
_ENCODER_PROBE_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

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
        "quality_name": "CQP",
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
            "-c:v", "h264_amf", "-quality", selected_preset, "-rc", "cqp",
            "-qp_i", value, "-qp_p", value,
        ]
    return ["-c:v", "libx264", "-preset", selected_preset, "-crf", value]


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

    capabilities: list[dict[str, Any]] = []
    for backend, profile in ENCODER_PROFILES.items():
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=black:s=64x64:d=0.12", "-frames:v", "1",
            *_encoder_video_args(backend, str(profile["preset"]), int(profile["quality"])),
            "-an", "-f", "null", "-",
        ]
        available = False
        error = ""
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            available = result.returncode == 0
            if not available:
                error = str(result.stderr or result.stdout or "").strip()[-800:]
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
        capabilities.append({
            "id": backend,
            "label": profile["label"],
            "available": available,
            "ffmpeg_encoder": profile["ffmpeg_encoder"],
            "preset": profile["preset"],
            "quality_name": profile["quality_name"],
            "quality": profile["quality"],
            "error": error,
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
            try:
                report("burning")
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


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
    """Render scrolling/top/bottom comments into an ASS file with simple lane allocation."""
    width = max(320, int(width))
    height = max(240, int(height))
    font_size = max(16, int(font_size))
    duration = max(3.0, float(duration))
    alpha = max(0, min(255, int(round((1.0 - max(0.0, min(1.0, opacity))) * 255))))
    primary = f"&H{alpha:02X}FFFFFF"
    outline = "&H80000000"
    lane_height = max(font_size + 8, int(font_size * 1.25))
    lane_count = max(1, int((height * 0.72) // lane_height))
    lane_free = [0.0] * lane_count
    top_index = 0
    bottom_index = 0

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
        text = _escape_ass_text(comment.text)
        color_tag = f"\\c&H{_ass_bgr(comment.color)}&" if comment.color != 0xFFFFFF else ""
        if comment.mode == 5:  # fixed top
            y = 20 + (top_index % max(1, lane_count // 3)) * lane_height
            top_index += 1
            start = comment.time
            end = start + min(duration, 5.0)
            override = f"{{\\an8\\pos({width // 2},{y}){color_tag}}}"
        elif comment.mode == 4:  # fixed bottom
            y = height - 20 - (bottom_index % max(1, lane_count // 3)) * lane_height
            bottom_index += 1
            start = comment.time
            end = start + min(duration, 5.0)
            override = f"{{\\an2\\pos({width // 2},{y}){color_tag}}}"
        else:
            lane = min(range(lane_count), key=lambda idx: lane_free[idx])
            start = comment.time
            estimated_width = max(font_size, int(len(comment.text) * font_size * 0.72))
            end = start + duration
            lane_free[lane] = start + min(duration * 0.45, 4.0)
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
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
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
        selected_encoder = str(encoder or "cpu").strip().lower()
        if selected_encoder == "auto":
            selected_encoder = str(
                probe_encoding_capabilities(ffmpeg).get("recommendation", {}).get("id") or "cpu"
            )
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


def select_summary_comments(comments: list[DanmakuComment], limit: int = 400) -> list[DanmakuComment]:
    """Deduplicate spam while retaining both chronology and concrete evidence."""
    limit = max(1, min(2000, int(limit)))
    unique: list[DanmakuComment] = []
    seen: dict[str, int] = {}
    for item in comments:
        normalized = re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", item.text.lower())
        if len(normalized) < 2:
            continue
        count = seen.get(normalized, 0)
        seen[normalized] = count + 1
        if count < 2:
            unique.append(item)
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
