"""Invariants the recolor engine must never break.

Several of these encode measured facts about the asset set; see the module
docstring in palette_creator/recolor.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from palette_creator.colorspace import hex_to_lab, lab_to_hex, rgb8_to_lab
from palette_creator.config import SOURCE_DIR
from palette_creator.imagectx import load_context
from palette_creator.palette_extract import extract_palette, seed_for_path
from palette_creator.recolor import (
    MatchMode,
    RecolorRule,
    apply_rules,
    render,
    rule_weight,
)

RANNO = SOURCE_DIR / "Ranno" / "T_Ran_Default_Neutral_CSP.png"
real_assets = pytest.mark.skipif(not RANNO.is_file(), reason="render assets missing")


@pytest.fixture(scope="module")
def ctx():
    if not RANNO.is_file():
        pytest.skip("render assets missing")
    return load_context(RANNO)


def _dominant_rule(ctx, target_hex="#20C0FF", **kw):
    """A rule sourced from the render's largest cluster."""
    res = extract_palette(ctx.proxy, k=12, seed=seed_for_path(ctx.path))
    swatch = res.swatches[0]
    return RecolorRule(
        source_lab=swatch.midtone_lab,
        target_lab=hex_to_lab(target_hex),
        tolerance=swatch.suggested_tolerance,
        **kw,
    )


# -- synthetic --------------------------------------------------------------


def test_hex_roundtrip():
    for value in ("#000000", "#FFFFFF", "#20C0FF", "#7F3A11"):
        assert lab_to_hex(hex_to_lab(value)) == value


@pytest.fixture
def gradient_ctx(tmp_path):
    """A vertical blue ramp beside a red ramp, with a transparent margin."""
    h = w = 64
    arr = np.zeros((h, w, 4), np.uint8)
    ramp = np.linspace(40, 230, h).astype(np.uint8)
    arr[:, : w // 2, 2] = ramp[:, None]
    arr[:, w // 2 :, 0] = ramp[:, None]
    arr[..., 3] = 255
    arr[:4, :, 3] = 0
    path = tmp_path / "grad.png"
    Image.fromarray(arr, "RGBA").save(path)
    return load_context(path)


def test_identity_rule_is_byte_identical(gradient_ctx):
    src = rgb8_to_lab((0, 0, 140))
    rule = RecolorRule(source_lab=src, target_lab=src.copy(), tolerance=40)
    out = render(gradient_ctx, [rule], use_proxy=False)
    assert np.array_equal(out, gradient_ctx.rgba_full)


def test_alpha_is_preserved_exactly(gradient_ctx):
    rule = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 140)),
        target_lab=hex_to_lab("#00FF00"),
        tolerance=40,
    )
    out = render(gradient_ctx, [rule], use_proxy=False)
    assert np.array_equal(out[..., 3], gradient_ctx.rgba_full[..., 3])


def test_unselected_pixels_are_bit_identical(gradient_ctx):
    """No global lab round-trip drift on regions the user never touched."""
    ps = gradient_ctx.full
    rule = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 140)),
        target_lab=hex_to_lab("#00FF00"),
        tolerance=20,
    )
    w = rule_weight(ps, rule, shape=gradient_ctx.shape_full)
    out = render(gradient_ctx, [rule], use_proxy=False)

    untouched = ps.idx[w == 0.0]
    before = gradient_ctx.rgba_full.reshape(-1, 4)[untouched]
    after = out.reshape(-1, 4)[untouched]
    assert np.array_equal(before, after)
    assert (w == 0.0).any(), "fixture should leave some pixels unselected"


def _shading_report(ctx, rule):
    """L* span and distinct-color count of a rule's selection, before and after.

    L* span is the load-bearing measure. Distinct-color count alone is a poor
    proxy: retargeting a vivid source to a less saturated one lands in a smaller
    region of the sRGB cube and legitimately yields fewer 8-bit values, even
    though the tonal ramp is fully intact.
    """
    from palette_creator.colorspace import rgb_to_lab

    ps = ctx.full
    w = rule_weight(ps, rule, shape=ctx.shape_full)
    sel = w > 0.5
    idx = ps.idx[sel]

    before = ctx.rgba_full.reshape(-1, 4)[idx, :3]
    after = render(ctx, [rule], use_proxy=False).reshape(-1, 4)[idx, :3]
    after_L = rgb_to_lab(after.astype(np.float32) / 255.0)[:, 0]
    before_L = ps.lab[sel][:, 0]

    return {
        "n": int(sel.sum()),
        "span_before": float(before_L.max() - before_L.min()),
        "span_after": float(after_L.max() - after_L.min()),
        "unique_before": len(np.unique(before, axis=0)),
        "unique_after": len(np.unique(after, axis=0)),
    }


def test_shading_is_preserved_not_flat_filled(gradient_ctx):
    """The direct regression guard against replacing the transform with a fill.

    A flat fill would collapse the span to 0 and the color count to 1.
    """
    rule = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 140)),
        target_lab=hex_to_lab("#00A000"),
        tolerance=40,
    )
    r = _shading_report(gradient_ctx, rule)
    assert r["n"] > 100
    assert r["span_after"] >= 0.8 * r["span_before"], r
    assert r["unique_after"] >= 0.4 * r["unique_before"], r


def test_lightness_order_is_monotone(gradient_ctx):
    """No shading inversions: darker stays darker."""
    from palette_creator.colorspace import rgb_to_lab

    ps = gradient_ctx.full
    rule = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 140)),
        target_lab=hex_to_lab("#FFD000"),
        tolerance=40,
    )
    w = rule_weight(ps, rule, shape=gradient_ctx.shape_full)
    sel = w > 0.9
    idx = ps.idx[sel]

    out = render(gradient_ctx, [rule], use_proxy=False)
    after_lab = rgb_to_lab(out.reshape(-1, 4)[idx, :3].astype(np.float32) / 255.0)

    order = np.argsort(ps.lab[sel][:, 0], kind="stable")
    seq = after_lab[order][:, 0]
    agree = np.mean(np.diff(seq) >= -1e-3)
    assert agree > 0.99, f"only {agree:.3%} of adjacent pairs kept their order"


def test_lightness_amount_zero_keeps_brightness(gradient_ctx):
    from palette_creator.colorspace import rgb_to_lab

    ps = gradient_ctx.full
    rule = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 140)),
        target_lab=hex_to_lab("#FFD000"),
        tolerance=40,
        lightness_amount=0.0,
    )
    w = rule_weight(ps, rule, shape=gradient_ctx.shape_full)
    sel = w > 0.99
    idx = ps.idx[sel]

    out = render(gradient_ctx, [rule], use_proxy=False, gamut_safe=False)
    after_lab = rgb_to_lab(out.reshape(-1, 4)[idx, :3].astype(np.float32) / 255.0)
    delta = np.abs(after_lab[:, 0] - ps.lab[sel][:, 0])
    assert delta.max() < 3.0, f"L* moved by up to {delta.max():.2f}"


def test_rules_do_not_cascade(gradient_ctx):
    """red->blue then blue->green must not turn the reds green."""
    red = rgb8_to_lab((200, 0, 0))
    blue = hex_to_lab("#0000C8")

    r1 = RecolorRule(source_lab=red, target_lab=blue, tolerance=30)
    r2 = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 200)), target_lab=hex_to_lab("#00C800"), tolerance=30
    )

    only_first = render(gradient_ctx, [r1], use_proxy=False)
    both = render(gradient_ctx, [r1, r2], use_proxy=False)

    ps = gradient_ctx.full
    w1 = rule_weight(ps, r1, shape=gradient_ctx.shape_full)
    claimed = ps.idx[w1 > 0.99]
    assert claimed.size > 100
    assert np.array_equal(
        only_first.reshape(-1, 4)[claimed], both.reshape(-1, 4)[claimed]
    )


def test_coverage_never_exceeds_one(gradient_ctx):
    src = rgb8_to_lab((0, 0, 140))
    rules = [
        RecolorRule(source_lab=src, target_lab=hex_to_lab("#00FF00"), tolerance=60),
        RecolorRule(source_lab=src, target_lab=hex_to_lab("#FF00FF"), tolerance=60),
        RecolorRule(source_lab=src, target_lab=hex_to_lab("#FFFF00"), tolerance=60),
    ]
    _lab, cov = apply_rules(gradient_ctx.full, rules, shape=gradient_ctx.shape_full)
    assert cov.max() <= 1.0 + 1e-5


def test_output_is_always_in_range(gradient_ctx):
    rule = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 140)),
        target_lab=hex_to_lab("#FF00FF"),
        tolerance=60,
        chroma_amount=1.0,
    )
    out = render(gradient_ctx, [rule], use_proxy=False)
    assert out.dtype == np.uint8
    assert out.min() >= 0 and out.max() <= 255


@pytest.fixture
def shaded_ctx(tmp_path):
    """One material lit from dark to bright: constant a*/b*, sweeping L*.

    This is what shading actually is. (A ramp built in sRGB instead would swing
    b* by ~50 units as it darkens, which is a property of sRGB primaries rather
    than of the material.)
    """
    from palette_creator.colorspace import lab_to_rgb

    # Modest chroma so the whole ramp stays inside sRGB; otherwise the dark end
    # clips on the round trip through uint8 and its a*/b* drift.
    h = w = 64
    lab = np.zeros((h * w, 3), np.float32)
    lab[:, 0] = np.repeat(np.linspace(25.0, 85.0, h), w)
    lab[:, 1] = 12.0
    lab[:, 2] = -24.0

    rgb = np.rint(lab_to_rgb(lab) * 255).astype(np.uint8).reshape(h, w, 3)
    arr = np.dstack([rgb, np.full((h, w, 1), 255, np.uint8)])
    path = tmp_path / "shaded.png"
    Image.fromarray(arr, "RGBA").save(path)
    return load_context(path)


def test_material_mode_spans_the_ramp_exact_mode_does_not(shaded_ctx):
    """One click on a mid-tone should grab the whole shaded material."""
    ps = shaded_ctx.full
    mid = ps.lab[len(ps) // 2]

    def covered(mode):
        w = rule_weight(
            ps,
            RecolorRule(source_lab=mid, target_lab=mid, mode=mode, tolerance=12),
            shape=shaded_ctx.shape_full,
        )
        return int((w > 0.5).sum())

    material = covered(MatchMode.MATERIAL)
    exact = covered(MatchMode.EXACT)

    assert material > 0.9 * len(ps), "material mode should grab nearly the whole ramp"
    assert material > 2 * exact, f"material {material} vs exact {exact}"


@real_assets
def test_material_mode_spans_more_tonal_range_on_real_renders(ctx):
    """The acceptance criterion for MATERIAL mode, on actual game art.

    What matters is not raw pixel count but how much of a material's *shading
    ramp* one click captures -- that is the difference between recoloring a
    material and recoloring only its midtones.
    """
    res = extract_palette(ctx.proxy, k=12, seed=seed_for_path(ctx.path))

    def l_span(source, mode):
        w = rule_weight(
            ctx.full,
            RecolorRule(source_lab=source, target_lab=source, mode=mode, tolerance=12),
            shape=ctx.shape_full,
        )
        sel = w > 0.5
        if not sel.any():
            return 0.0
        L = ctx.full.lab[sel][:, 0]
        return float(L.max() - L.min())

    checked = 0
    for swatch in res.swatches[:6]:
        exact = l_span(swatch.midtone_lab, MatchMode.EXACT)
        material = l_span(swatch.midtone_lab, MatchMode.MATERIAL)
        assert material > 1.8 * exact, (
            f"swatch {swatch.index}: L* span {exact:.1f} -> {material:.1f}"
        )
        checked += 1
    assert checked >= 5


# -- real renders -----------------------------------------------------------


@real_assets
def test_real_render_alpha_and_dimensions(ctx):
    rule = _dominant_rule(ctx)
    out = render(ctx, [rule], use_proxy=False)
    assert out.shape == ctx.rgba_full.shape
    assert np.array_equal(out[..., 3], ctx.rgba_full[..., 3])


@real_assets
@pytest.mark.parametrize("target", ["#20C0FF", "#FF3030", "#FFE000", "#404040"])
def test_real_render_preserves_shading(ctx, target):
    """The material's tonal ramp survives retargeting, across very different
    targets -- including a desaturating one and a darkening one."""
    rule = _dominant_rule(ctx, target)
    r = _shading_report(ctx, rule)
    assert r["n"] > 1000
    assert r["span_after"] >= 0.8 * r["span_before"], r
    assert r["unique_after"] >= 0.4 * r["unique_before"], r


@real_assets
def test_real_render_step_size_stays_below_banding_threshold(ctx):
    """Adjacent tones must stay under ~1 DeltaL apart, where banding shows."""
    rule = _dominant_rule(ctx, "#20C0FF")
    r = _shading_report(ctx, rule)
    step = r["span_after"] / max(r["unique_after"] - 1, 1)
    assert step < 1.0, f"{step:.3f} DeltaL per tone step: {r}"


@real_assets
def test_proxy_matches_full_resolution(ctx):
    """Catches the premultiplied-resize bug in the proxy path."""
    rule = _dominant_rule(ctx, "#20C0FF")
    full = render(ctx, [rule], use_proxy=False)
    proxy = render(ctx, [rule], use_proxy=True)

    h, w = ctx.shape_proxy
    downscaled = np.asarray(
        Image.fromarray(full, "RGBA").resize((w, h), Image.Resampling.LANCZOS)
    )
    opaque = (downscaled[..., 3] > 200) & (proxy[..., 3] > 200)
    diff = np.abs(
        downscaled[..., :3][opaque].astype(np.int16)
        - proxy[..., :3][opaque].astype(np.int16)
    )
    assert diff.mean() < 3.0, f"proxy/full mean abs diff {diff.mean():.2f}"


@real_assets
def test_no_rules_returns_original(ctx):
    assert np.array_equal(render(ctx, [], use_proxy=False), ctx.rgba_full)
