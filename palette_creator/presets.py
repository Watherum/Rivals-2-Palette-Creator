"""Save and load rule sets as JSON."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .colorspace import hex_to_lab, lab_to_hex
from .config import PRESETS_DIR
from .recolor import MatchMode, RecolorRule, Region

FORMAT_VERSION = 1

# Rules are stored as hex rather than raw LAB so a preset file is readable and
# hand-editable; the LAB round trip is lossless enough at 8-bit precision.
_FIELDS = (
    "tolerance",
    "feather",
    "lightness_amount",
    "chroma_amount",
    "hue_amount",
    "opacity",
    "smooth_edges",
    "contiguous",
    "match_all",
    "enabled",
    "lock_to_cluster",
    "name",
)


def rule_to_dict(rule: RecolorRule) -> dict:
    data = {
        "source": lab_to_hex(rule.source_lab),
        "target": lab_to_hex(rule.target_lab),
        "mode": rule.mode.value,
    }
    for field in _FIELDS:
        data[field] = getattr(rule, field)
    if rule.l_weight is not None:
        data["l_weight"] = rule.l_weight
    if rule.c_weight is not None:
        data["c_weight"] = rule.c_weight
    if rule.seed_uv is not None:
        data["seed_uv"] = list(rule.seed_uv)
    if rule.cluster_id is not None:
        data["cluster_id"] = rule.cluster_id
    if rule.region is not None:
        # Normalized coords, so a preset drawn against a 1024px render still
        # lands correctly on a differently-sized one.
        data["region"] = {
            "kind": rule.region.kind,
            "points": [list(p) for p in rule.region.points],
        }
    return data


def rule_from_dict(data: dict) -> RecolorRule:
    rule = RecolorRule(
        source_lab=hex_to_lab(data["source"]),
        target_lab=hex_to_lab(data["target"]),
        mode=MatchMode(data.get("mode", "material")),
    )
    for field in _FIELDS:
        if field in data:
            setattr(rule, field, data[field])
    rule.l_weight = data.get("l_weight")
    rule.c_weight = data.get("c_weight")
    seed = data.get("seed_uv")
    rule.seed_uv = tuple(seed) if seed else None
    rule.cluster_id = data.get("cluster_id")
    region = data.get("region")
    if region:
        rule.region = Region(
            kind=region.get("kind", "rect"),
            points=[tuple(p) for p in region.get("points", [])],
        )
    return rule


def save_preset(path: Path | str, rules, *, source: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": FORMAT_VERSION,
        "source_render": source,
        "rules": [rule_to_dict(r) for r in rules],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_preset(path: Path | str) -> list[RecolorRule]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != FORMAT_VERSION:
        raise ValueError(
            f"unsupported preset format {data.get('format')!r} "
            f"(this build reads version {FORMAT_VERSION})"
        )
    return [rule_from_dict(d) for d in data.get("rules", [])]


def list_presets(directory: Path | str = PRESETS_DIR) -> list[Path]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))
