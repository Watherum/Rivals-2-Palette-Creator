"""Benchmark the recolor engine against the game's own palettes.

For a given (character, skin), this extracts a palette from the Neutral render,
reads each cluster's true target color out of the official recolor at the same
pixel coordinates, then applies those rules and reports how close the result
lands.

Reference points measured on this asset set:

* 7.7/255 RMS -- what a per-source-color lookup table achieves. That is the
  ceiling for anything driven by color, and it is not reachable here: a LUT gets
  one free parameter per distinct color, while this engine gets a handful per
  cluster.
* The official recolors are not a color LUT at all -- one source RGB maps to
  ~2.8 different targets, because the game recolors by material/UV region. Some
  residual is therefore structural, not a defect.

The number to watch is the trend across refactors, not its absolute value.

    python scripts/bench_recolor.py
    python scripts/bench_recolor.py --pairs Ranno:Abyss Kragg:Default --k 16
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from palette_creator.colorspace import rgb_to_lab
from palette_creator.imagectx import load_context
from palette_creator.library import Library
from palette_creator.palette_extract import assign, extract_palette, seed_for_path
from palette_creator.recolor import MatchMode, RecolorRule, render

DEFAULT_PAIRS = [
    ("Ranno", "Abyss"), ("Ranno", "Default"), ("Clairen", "Default"),
    ("Zetterburn", "Default"), ("Kragg", "Default"), ("Maypul", "Default"),
    ("Orcane", "Default"), ("Wrastor", "Default"), ("Absa", "Default"),
    ("Fleet", "Default"),
]


def build_rules(ctx, official, k: int):
    """One rule per cluster, targeted at that cluster's true color in `official`."""
    result = extract_palette(ctx.full, k=k, seed=seed_for_path(ctx.path))
    ps = ctx.full
    labels = assign(ps.lab, result.centroids)

    target_flat = official.reshape(-1, 4)[ps.idx, :3].astype(np.float32) / 255.0
    target_lab = rgb_to_lab(target_flat)

    rules = []
    for swatch in result.swatches:
        member = labels == swatch.index
        if member.sum() < 32:
            continue
        rules.append(
            RecolorRule(
                source_lab=swatch.midtone_lab,
                target_lab=np.median(target_lab[member], axis=0),
                mode=MatchMode.MATERIAL,
                tolerance=swatch.suggested_tolerance,
            )
        )
    return rules, result


def evaluate(neutral_path: Path, official_path: Path, k: int) -> dict | None:
    from PIL import Image

    ctx = load_context(neutral_path)
    official = np.asarray(Image.open(official_path).convert("RGBA"), dtype=np.uint8)
    if official.shape != ctx.rgba_full.shape:
        return None

    rules, result = build_rules(ctx, official, k)
    if not rules:
        return None

    out = render(ctx, rules, use_proxy=False)

    solid = (ctx.rgba_full[..., 3] > 200) & (official[..., 3] > 200)
    a = out[..., :3][solid].astype(np.float32)
    b = official[..., :3][solid].astype(np.float32)
    baseline = ctx.rgba_full[..., :3][solid].astype(np.float32)

    def rms(x, y):
        return float(np.sqrt(((x - y) ** 2).sum(axis=1).mean()))

    within = [s.spread for s in result.swatches if s.pixel_count > 32]
    return {
        "rules": len(rules),
        "rms": rms(a, b),
        "rms_untouched": rms(baseline, b),
        "spread": float(np.mean(within)) if within else 0.0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Recolor accuracy benchmark.")
    parser.add_argument("--k", type=int, default=12)
    parser.add_argument("--palette", default="Red", help="Official palette to target.")
    parser.add_argument(
        "--pairs", nargs="*", metavar="CHAR:SKIN", help="Override the default pairs."
    )
    args = parser.parse_args(argv)

    pairs = (
        [tuple(p.split(":", 1)) for p in args.pairs] if args.pairs else DEFAULT_PAIRS
    )

    library = Library()
    library.scan()

    print(f"Targeting the official '{args.palette}' palette, k={args.k}\n")
    print(
        f"{'character / skin':<26} {'rules':>5} {'RMS':>8} {'no-op':>8} {'gain':>7} {'spread':>7}"
    )
    print("-" * 68)

    scores, spreads = [], []
    for character, skin in pairs:
        neutral = library.get(character, skin, "Neutral")
        official = library.get(character, skin, args.palette)
        label = f"{character} / {skin}"
        if neutral is None or official is None:
            print(f"{label:<26} {'-- missing --':>41}")
            continue

        r = evaluate(neutral.path, official.path, args.k)
        if r is None:
            print(f"{label:<26} {'-- unusable --':>41}")
            continue

        gain = r["rms_untouched"] / max(r["rms"], 1e-6)
        scores.append(r["rms"])
        spreads.append(r["spread"])
        print(
            f"{label:<26} {r['rules']:>5} {r['rms']:>8.1f} "
            f"{r['rms_untouched']:>8.1f} {gain:>6.2f}x {r['spread']:>7.1f}"
        )

    if scores:
        print("-" * 68)
        print(f"{'mean':<26} {'':>5} {np.mean(scores):>8.1f}")
        print(f"\nMean within-cluster CIELAB spread: {np.mean(spreads):.1f}")
        print("Reference: a per-color lookup table reaches 7.7/255 on this data.")
        print("'no-op' is the distance if nothing were recolored at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
