#!/usr/bin/env python3
"""Image provider profiles for capability-aware cover generation failover."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping


PROFILE_SLOTS = tuple(range(1, 5))


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _parse_scenes(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value or "*").replace("\n", ",").split(",")
    return tuple(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


@dataclass(frozen=True)
class ImageProviderProfile:
    profile_id: str
    adapter: str
    api_key: str
    base_url: str
    model_name: str
    scenes: tuple[str, ...]
    priority: int
    supports_edit: bool = True
    legacy: bool = False

    @property
    def health_key(self) -> str:
        key_digest = hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()
        raw = f"image\0{self.profile_id}\0{self.adapter}\0{self.base_url}\0{self.model_name}\0{key_digest}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def supports_scene(self, scene_name: str) -> bool:
        return not scene_name or not self.scenes or "*" in self.scenes or scene_name in self.scenes

    def materialize_config(self, root: Mapping[str, Any]) -> dict[str, Any]:
        config = dict(root or {})
        config.update({
            "OPENAI_API_KEY": self.api_key,
            "OPENAI_BASE_URL": self.base_url,
            "OPENAI_MODEL_NAME": self.model_name,
            "OPENAI_PROVIDER_ROUTING_DISABLED": True,
            "OPENAI_ACTIVE_PROVIDER_PROFILE": self.profile_id,
        })
        return config


def resolve_image_provider_profiles(
    config: Mapping[str, Any] | None,
    *,
    scene_name: str,
) -> tuple[ImageProviderProfile, ...]:
    root = config or {}
    profiles: list[ImageProviderProfile] = []
    configured_count = 0
    for slot in PROFILE_SLOTS:
        prefix = f"IMAGE_PROVIDER_{slot}_"
        if not _coerce_bool(root.get(f"{prefix}ENABLED", False)):
            continue
        api_key = str(root.get(f"{prefix}API_KEY") or "").strip()
        if not api_key:
            continue
        configured_count += 1
        adapter = str(root.get(f"{prefix}ADAPTER") or "openai-compatible").strip().casefold()
        if adapter != "openai-compatible":
            continue
        profile = ImageProviderProfile(
            profile_id=str(root.get(f"{prefix}ID") or f"image-provider-{slot}").strip(),
            adapter=adapter,
            api_key=api_key,
            base_url=str(root.get(f"{prefix}BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/"),
            model_name=str(root.get(f"{prefix}MODEL_NAME") or "gpt-image-2").strip(),
            scenes=_parse_scenes(root.get(f"{prefix}SCENES", "*")),
            priority=slot,
            supports_edit=_coerce_bool(root.get(f"{prefix}SUPPORTS_EDIT", True)),
        )
        if profile.supports_scene(scene_name):
            profiles.append(profile)

    legacy_key = str(root.get("OPENAI_IMAGE_API_KEY") or root.get("OPENAI_API_KEY") or "").strip()
    legacy_fallback = _coerce_bool(root.get("IMAGE_PROVIDER_LEGACY_FALLBACK_ENABLED", True))
    if configured_count == 0 or (profiles and legacy_fallback):
        if legacy_key:
            profiles.append(ImageProviderProfile(
                profile_id="legacy-image",
                adapter="openai-compatible",
                api_key=legacy_key,
                base_url=str(root.get("OPENAI_IMAGE_BASE_URL") or root.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/"),
                model_name=str(root.get("OPENAI_IMAGE_MODEL_NAME") or "gpt-image-2").strip(),
                scenes=("*",),
                priority=999,
                legacy=True,
            ))
    return tuple(profiles)
