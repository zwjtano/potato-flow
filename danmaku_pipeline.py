"""Biliup XML danmaku parsing, ASS rendering and FFmpeg burn-in helpers."""

from __future__ import annotations

import math
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DanmakuComment:
    time: float
    text: str
    color: int = 0xFFFFFF
    mode: int = 1


def parse_biliup_xml(path: Path) -> list[DanmakuComment]:
    """Parse Bilibili-compatible `<d p="...">text</d>` entries from biliup XML."""
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
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    video_filter = f"subtitles=filename='{_filter_path(ass_path)}'"
    if fonts_dir and fonts_dir.is_dir():
        video_filter += f":fontsdir='{_filter_path(fonts_dir)}'"
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(video),
        "-vf", video_filter, "-c:v", "libx264",
        "-preset", str(preset), "-crf", str(max(0, min(51, int(crf)))),
        "-c:a", "copy", "-movflags", "+faststart", str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        # Opus and a few live codecs cannot be stream-copied into MP4. Retry
        # with AAC while keeping the expensive video encode settings intact.
        command = command[:-5] + [
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        detail = completed.stderr.strip()[-2000:]
        raise RuntimeError(f"FFmpeg 烧录 ASS 失败: {detail}")
    return output


def select_summary_comments(comments: list[DanmakuComment], limit: int = 400) -> list[DanmakuComment]:
    """Deduplicate spam and sample across the whole recording for an LLM summary."""
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
    step = len(unique) / limit
    return [unique[min(len(unique) - 1, int(math.floor(index * step)))] for index in range(limit)]


def format_comments_for_ai(comments: list[DanmakuComment]) -> str:
    return "\n".join(f"[{int(item.time // 60):02d}:{int(item.time % 60):02d}] {item.text}" for item in comments)
