#!/usr/bin/env python3
"""Versioned scene policies for text-model requests.

The registry intentionally stores orchestration metadata rather than full
prompts.  Business modules keep owning their output contracts, while this file
gives each stable contract a version, cache eligibility, model route and output
budget.  Changing a prompt contract must bump its version so cached results are
invalidated without deleting the cache database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    cacheable: bool = False
    model_config_key: str = ""
    max_output_tokens: int | None = None
    required_keys: tuple[str, ...] = ()
    required_any_keys: tuple[str, ...] = ()
    list_keys: tuple[str, ...] = ()
    result_kind: str = "object"


_PROMPT_SPECS = (
    PromptSpec(
        "recording_danmaku_summary",
        "2026-08-24.2",
        cacheable=True,
        model_config_key="OPENAI_METADATA_MODEL_NAME",
        max_output_tokens=1200,
        required_any_keys=("description", "timeline"),
    ),
    PromptSpec(
        "recording_danmaku_summary_batch",
        "2026-08-24.2",
        cacheable=True,
        model_config_key="OPENAI_METADATA_MODEL_NAME",
        max_output_tokens=1200,
        required_any_keys=("description", "timeline"),
    ),
    PromptSpec(
        "ai_enhancer_tags",
        "2026-08-24.1",
        cacheable=True,
        model_config_key="OPENAI_CLASSIFICATION_MODEL_NAME",
        max_output_tokens=160,
        required_keys=("tags",),
        list_keys=("tags",),
    ),
    PromptSpec(
        "ai_enhancer_content_profile",
        "2026-08-24.1",
        cacheable=True,
        model_config_key="OPENAI_CLASSIFICATION_MODEL_NAME",
        max_output_tokens=320,
        required_keys=("domain",),
    ),
    PromptSpec(
        "ai_enhancer_partition_selection",
        "2026-08-24.1",
        cacheable=True,
        model_config_key="OPENAI_CLASSIFICATION_MODEL_NAME",
        max_output_tokens=320,
        result_kind="partition_selection",
    ),
    PromptSpec(
        "ai_enhancer_metadata_translate",
        "2026-08-24.1",
        cacheable=True,
        model_config_key="OPENAI_TRANSLATION_MODEL_NAME",
        required_any_keys=("title", "description"),
    ),
    PromptSpec(
        "subtitle_translate_batch",
        "2026-08-24.1",
        cacheable=True,
        model_config_key="SUBTITLE_OPENAI_MODEL_NAME",
        max_output_tokens=4096,
        required_keys=("translations",),
        list_keys=("translations",),
    ),
    PromptSpec(
        "subtitle_translate_batch_strict",
        "2026-08-24.1",
        cacheable=True,
        model_config_key="SUBTITLE_OPENAI_MODEL_NAME",
        max_output_tokens=4096,
        required_keys=("translations",),
        list_keys=("translations",),
    ),
    PromptSpec(
        "subtitle_qc",
        "2026-08-24.1",
        cacheable=True,
        model_config_key="SUBTITLE_QC_MODEL_NAME",
        max_output_tokens=120,
        required_any_keys=("passed", "pass"),
    ),
)
_PROMPT_SPEC_BY_NAME = {spec.name: spec for spec in _PROMPT_SPECS}


def canonical_scene_name(scene_name: str) -> str:
    """Collapse transport fallbacks without merging creative retry scenes."""
    scene = str(scene_name or "").strip()
    for suffix in ("_fallback_plain_json", "_retry_json"):
        if scene.endswith(suffix):
            return scene[: -len(suffix)]
    return scene


def get_prompt_spec(scene_name: str) -> PromptSpec:
    canonical = canonical_scene_name(scene_name)
    return _PROMPT_SPEC_BY_NAME.get(
        canonical,
        PromptSpec(canonical or "unregistered", "unversioned"),
    )


def is_valid_cache_result(spec: PromptSpec, result: Any) -> bool:
    """Apply the minimum stable output contract before a result enters the cache."""
    if not isinstance(result, dict) or not result:
        return False
    if any(key not in result for key in spec.required_keys):
        return False
    if spec.required_any_keys and not any(key in result for key in spec.required_any_keys):
        return False
    if any(not isinstance(result.get(key), list) for key in spec.list_keys):
        return False
    if spec.result_kind == "partition_selection":
        if str(result.get("id") or "").strip():
            return True
        return any(
            isinstance(value, dict) and str(value.get("id") or "").strip()
            for value in result.values()
        )
    return True


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {str(key): item for key, item in parsed.items()} if isinstance(parsed, dict) else {}


def resolve_scene_request(
    config: Mapping[str, Any] | None,
    *,
    scene_name: str,
    model_name: str,
    max_tokens: int | None,
) -> tuple[str, int | None, PromptSpec]:
    """Return the routed model and bounded output budget for one scene."""
    cfg = config or {}
    spec = get_prompt_spec(scene_name)
    model_overrides = _mapping(cfg.get("OPENAI_SCENE_MODEL_OVERRIDES"))
    routed_model = str(
        model_overrides.get(spec.name)
        or model_overrides.get(scene_name)
        or (cfg.get(spec.model_config_key) if spec.model_config_key else "")
        or model_name
        or cfg.get("OPENAI_MODEL_NAME")
        or ""
    ).strip()

    budget_overrides = _mapping(cfg.get("OPENAI_SCENE_TOKEN_BUDGETS"))
    configured_budget = budget_overrides.get(spec.name, budget_overrides.get(scene_name))
    try:
        budget = int(configured_budget) if configured_budget not in (None, "") else None
    except (TypeError, ValueError):
        budget = None
    if budget is not None:
        budget = max(16, min(budget, 128_000))
    elif spec.max_output_tokens is not None:
        budget = spec.max_output_tokens

    if max_tokens is None:
        resolved_tokens = budget
    elif budget is None:
        resolved_tokens = max_tokens
    else:
        resolved_tokens = min(max_tokens, budget)
    return routed_model, resolved_tokens, spec
