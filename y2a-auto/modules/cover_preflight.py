"""Bilibili cover normalization and validation."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


BILIBILI_COVER_SIZE = (1146, 717)
BILIBILI_COVER43_SIZE = (1600, 1200)
BILIBILI_COVER_MAX_BYTES = 5 * 1024 * 1024
_JPEG_QUALITY_STEPS = (92, 88, 84, 80, 76, 72, 68, 64, 60)


class CoverPreflightError(ValueError):
    """Raised when a cover cannot be prepared safely for Bilibili."""


def _rgb_image(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _validate_prepared_jpeg(
    path: str,
    *,
    target_size: tuple[int, int],
    max_bytes: int,
) -> dict:
    try:
        file_size = os.path.getsize(path)
    except OSError as exc:
        raise CoverPreflightError(f"无法读取转换后的封面: {exc}") from exc
    if file_size <= 0:
        raise CoverPreflightError("转换后的封面为空文件")
    if file_size > max_bytes:
        raise CoverPreflightError(
            f"转换后的封面大小 {file_size / 1024 / 1024:.2f}MB，超过 {max_bytes / 1024 / 1024:.0f}MB"
        )

    try:
        with open(path, "rb") as handle:
            if handle.read(3) != b"\xff\xd8\xff":
                raise CoverPreflightError("转换后的封面不是真实 JPEG 文件")
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            image_format = str(image.format or "").upper()
            mode = image.mode
    except CoverPreflightError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise CoverPreflightError(f"转换后的封面无法解码: {exc}") from exc

    if image_format != "JPEG":
        raise CoverPreflightError(f"转换后的封面格式异常: {image_format or '未知'}")
    if (width, height) != target_size:
        raise CoverPreflightError(
            f"转换后的封面尺寸异常: {width}×{height}，应为 {target_size[0]}×{target_size[1]}"
        )
    if mode != "RGB":
        raise CoverPreflightError(f"转换后的封面色彩模式异常: {mode}，应为 RGB")

    expected_ratio = target_size[0] / target_size[1]
    actual_ratio = width / height
    if abs(actual_ratio - expected_ratio) > 0.001:
        raise CoverPreflightError(
            f"转换后的封面比例异常: {actual_ratio:.4f}，应为 {expected_ratio:.4f}"
        )

    return {
        "path": path,
        "format": image_format,
        "width": width,
        "height": height,
        "ratio": actual_ratio,
        "size_bytes": file_size,
    }


def prepare_bilibili_cover(
    source_path: str,
    *,
    target_size: tuple[int, int] = BILIBILI_COVER_SIZE,
    output_path: str | None = None,
    max_bytes: int = BILIBILI_COVER_MAX_BYTES,
) -> dict:
    """Create a deterministic RGB JPEG and validate the final upload artifact."""
    source = Path(str(source_path or "")).expanduser()
    if not source.is_file():
        raise CoverPreflightError(f"封面文件不存在: {source}")
    if target_size[0] <= 0 or target_size[1] <= 0:
        raise CoverPreflightError(f"封面目标尺寸无效: {target_size}")

    destination = Path(output_path) if output_path else source.with_name(
        "upload_cover.jpg" if target_size == BILIBILI_COVER_SIZE else "upload_cover43.jpg"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")

    try:
        with Image.open(source) as original:
            source_format = str(original.format or "").upper()
            source_size = original.size
            original.seek(0)
            oriented = ImageOps.exif_transpose(original)
            rgb = _rgb_image(oriented)
            prepared = ImageOps.fit(
                rgb,
                target_size,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            for quality in _JPEG_QUALITY_STEPS:
                prepared.save(
                    temporary,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                    subsampling="4:2:0",
                )
                if temporary.stat().st_size <= max_bytes:
                    break
            else:
                raise CoverPreflightError(
                    f"封面压缩后仍超过 {max_bytes / 1024 / 1024:.0f}MB"
                )
        os.replace(temporary, destination)
    except CoverPreflightError:
        if temporary.exists():
            temporary.unlink()
        raise
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        if temporary.exists():
            temporary.unlink()
        raise CoverPreflightError(f"封面预检失败，图片无法转换: {exc}") from exc

    result = _validate_prepared_jpeg(
        str(destination),
        target_size=target_size,
        max_bytes=max_bytes,
    )
    result.update(
        {
            "source_format": source_format,
            "source_width": source_size[0],
            "source_height": source_size[1],
        }
    )
    return result
