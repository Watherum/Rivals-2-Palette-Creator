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
    WRITE_EPS,
    MatchMode,
    _anchored,
    RecolorRule,
    Region,
    apply_rules,
    render,
    rule_transform,
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


@pytest.fixture
def neutral_ctx(tmp_path):
    """A near-neutral dark ramp -- the shaded blacks and grays of a render.

    Their hue is 8-bit quantization noise around the neutral axis, which is
    exactly the input that used to come back as saturated confetti.
    """
    h = w = 64
    arr = np.zeros((h, w, 4), np.uint8)
    ramp = np.linspace(8, 150, h).astype(np.uint8)
    rng = np.random.default_rng(0)
    base = np.repeat(ramp[:, None], w, axis=1)
    for c in range(3):  # +/-1 per channel: real renders are never exactly gray
        arr[..., c] = np.clip(base + rng.integers(-1, 2, (h, w)), 0, 255)
    arr[..., 3] = 255
    path = tmp_path / "neutral.png"
    Image.fromarray(arr, "RGBA").save(path)
    return load_context(path)


@pytest.mark.parametrize("source_hex", ["#848489", "#131413", "#661716", "#353735"])
def test_near_neutral_pixels_do_not_get_random_hues(neutral_ctx, source_hex):
    """Recoloring shaded grays must give one hue, not confetti.

    ``rule_transform`` guarded a neutral *source* but rotated every pixel's own
    hue regardless. A source just over CHROMA_EPS took the rotate branch, and
    since the chroma remap then lifts near-neutral pixels to the target's
    chroma, their meaningless hue came back fully saturated: a black material
    told to go red arrived as green speckle.
    """
    rule = RecolorRule(
        source_lab=hex_to_lab(source_hex),
        target_lab=hex_to_lab("#FF0000"),
        tolerance=60,
    )
    ps = neutral_ctx.full
    out = rule_transform(ps, rule)
    hue = np.arctan2(out[:, 2], out[:, 1])

    resultant = np.abs(np.exp(1j * hue).mean())
    spread = np.degrees(np.sqrt(max(-2.0 * np.log(max(resultant, 1e-9)), 0.0)))
    assert spread < 10.0, f"hues spread over {spread:.0f} deg"

    target_hue = np.arctan2(*hex_to_lab("#FF0000")[[2, 1]])
    off = np.degrees(np.abs(np.angle(np.exp(1j * (hue - target_hue)))))
    assert off.mean() < 15.0, f"mean hue is {off.mean():.0f} deg off the target"


@pytest.mark.parametrize("source_hex", ["#848489", "#3A3D3A", "#5A5F5A"])
def test_flat_material_does_not_come_back_grainy(neutral_ctx, source_hex):
    """Recoloring a near-gray must not amplify its quantization noise.

    The chroma remap rises at ``target_chroma / source_chroma`` below the
    anchor. For a source just over CHROMA_EPS that is ~25x, so the +/-1 of 8-bit
    noise in a flat gray came back as +/-28 of chroma -- visible speckle across
    a material the user asked to be one solid color.
    """
    rule = RecolorRule(
        source_lab=hex_to_lab(source_hex),
        target_lab=hex_to_lab("#C80E3E"),
        tolerance=60,
    )
    ps = neutral_ctx.full
    out = rule_transform(ps, rule)
    out_chroma = np.hypot(out[:, 1], out[:, 2])

    amplification = out_chroma.std() / max(ps.C.std(), 1e-6)
    assert amplification < 4.0, f"chroma noise amplified {amplification:.1f}x"


def test_anchor_still_lands_on_target_under_the_gain_cap():
    """Capping the gain must not move the anchor: that is the whole design."""
    for x0, y0 in ((2.9, 71.7), (0.5, 90.0), (40.0, 71.7), (60.0, 10.0)):
        x = np.array([0.0, x0 * 0.5, x0, x0 * 1.5, 128.0], np.float32)
        out = _anchored(x, x0, y0, 128.0, max_gain=2.0)
        assert out[2] == pytest.approx(y0, abs=1e-3)
        assert out[-1] == pytest.approx(128.0, abs=1e-3)
        assert np.all(np.diff(out) >= -1e-4), "remap must stay monotone"
        assert np.all(out >= -1e-6), "remap must stay non-negative"


def test_saturated_pixels_still_rotate_rather_than_flatten(gradient_ctx):
    """The neutral fallback must not swallow genuine hue variation."""
    rule = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 140)),
        target_lab=hex_to_lab("#FF0000"),
        tolerance=60,
    )
    ps = gradient_ctx.full
    strong = ps.C > 30
    assert strong.sum() > 50
    out = rule_transform(ps, rule)[strong]
    hue = np.arctan2(out[:, 2], out[:, 1])
    assert np.degrees(np.ptp(hue)) > 1.0, "hue variation was flattened away"


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


def test_lower_rule_is_invisible_under_an_identical_one(gradient_ctx):
    """A rule selecting the same material as one above it must not show at all.

    The old ``w * (1 - cov)`` composite let the lower rule tint whatever the
    rule above had left soft -- a visible halo everywhere the top rule feathered.
    """
    src = rgb8_to_lab((200, 0, 0))
    top = RecolorRule(source_lab=src, target_lab=hex_to_lab("#0000C8"), tolerance=30)
    bottom = RecolorRule(source_lab=src, target_lab=hex_to_lab("#00C800"), tolerance=30)

    only_top = render(gradient_ctx, [top], use_proxy=False)
    both = render(gradient_ctx, [top, bottom], use_proxy=False)
    assert np.array_equal(only_top, both)


def test_strength_reveals_the_original_not_the_rule_below(gradient_ctx):
    """Turning a rule down must show the source art, not the next rule down."""
    src = rgb8_to_lab((200, 0, 0))
    faint = RecolorRule(
        source_lab=src, target_lab=hex_to_lab("#0000C8"), tolerance=30, opacity=0.5
    )
    bottom = RecolorRule(source_lab=src, target_lab=hex_to_lab("#00C800"), tolerance=30)

    alone = render(gradient_ctx, [faint], use_proxy=False)
    stacked = render(gradient_ctx, [faint, bottom], use_proxy=False)
    assert np.array_equal(alone, stacked)


def test_zero_strength_does_not_block_the_rule_below(gradient_ctx):
    """Strength 0 means off, so it must not claim pixels it does nothing to."""
    src = rgb8_to_lab((200, 0, 0))
    off = RecolorRule(
        source_lab=src, target_lab=hex_to_lab("#0000C8"), tolerance=30, opacity=0.0
    )
    live = RecolorRule(source_lab=src, target_lab=hex_to_lab("#00C800"), tolerance=30)

    assert np.array_equal(
        render(gradient_ctx, [off, live], use_proxy=False),
        render(gradient_ctx, [live], use_proxy=False),
    )


def test_lower_rule_still_gets_what_the_top_did_not_select(gradient_ctx):
    """Priority must not degenerate into 'only the first rule ever applies'."""
    top = RecolorRule(
        source_lab=rgb8_to_lab((200, 0, 0)), target_lab=hex_to_lab("#0000C8"),
        tolerance=12,
    )
    bottom = RecolorRule(
        source_lab=rgb8_to_lab((0, 0, 200)), target_lab=hex_to_lab("#00C800"),
        tolerance=12,
    )
    only_top = render(gradient_ctx, [top], use_proxy=False)
    both = render(gradient_ctx, [top, bottom], use_proxy=False)
    assert not np.array_equal(only_top, both)


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"opacity": 0.15},
        {"feather": 1.0},
        {"smooth_edges": True},
        {"tolerance": 90},
        {"region": Region("rect", [(0.0, 0.0), (0.5, 1.0)])},
    ],
    ids=["plain", "faint", "feathered", "smoothed", "wide", "region"],
)
def test_a_rule_owns_every_pixel_it_writes(gradient_ctx, kw):
    """No rule may write to a pixel a rule above it already wrote to.

    Not "may not overwrite" and not "may not exceed the remaining share" --
    may not write at all, at any strength, in preview or export.
    """
    src = rgb8_to_lab((200, 0, 0))
    base = dict(source_lab=src, target_lab=hex_to_lab("#0000C8"), tolerance=12)
    top = RecolorRule(**{**base, **kw})
    # Deliberately greedy: matches everything the top rule does, and much more.
    bottom = RecolorRule(
        source_lab=src, target_lab=hex_to_lab("#00C800"), tolerance=120
    )

    ps = gradient_ctx.full
    shape = gradient_ctx.shape_full
    owned = rule_weight(ps, top, shape=shape) > WRITE_EPS
    if top.region_active:
        owned |= top.region.mask(shape).reshape(-1)[ps.idx]
    assert owned.sum() > 100, "test rule must own something to be meaningful"

    alone = render(gradient_ctx, [top], use_proxy=False).reshape(-1, 4)
    stacked = render(gradient_ctx, [top, bottom], use_proxy=False).reshape(-1, 4)
    idx = ps.idx[owned]
    assert np.array_equal(alone[idx], stacked[idx])


def test_region_is_exclusive_territory(gradient_ctx):
    """A drawn region shields its area from every rule below it.

    The real case: a tight rule for the shoes claims most shoe pixels only
    partially, and a broad rule for the suit underneath topped up the remainder
    and recolored the shoes anyway.
    """
    src = rgb8_to_lab((200, 0, 0))
    owner = RecolorRule(
        source_lab=src,
        target_lab=hex_to_lab("#0000C8"),
        tolerance=2,  # deliberately too tight to claim its area fully
        region=Region("rect", [(0.0, 0.0), (0.5, 1.0)]),
    )
    greedy = RecolorRule(
        source_lab=src, target_lab=hex_to_lab("#00C800"), tolerance=90
    )

    alone = render(gradient_ctx, [owner], use_proxy=False)
    stacked = render(gradient_ctx, [owner, greedy], use_proxy=False)

    ps = gradient_ctx.full
    inside = owner.region.mask(gradient_ctx.shape_full).reshape(-1)[ps.idx]
    assert inside.sum() > 100
    idx = ps.idx[inside]
    assert np.array_equal(alone.reshape(-1, 4)[idx], stacked.reshape(-1, 4)[idx])

    # ...and the lower rule is untouched everywhere outside it.
    outside = ps.idx[~inside]
    only_greedy = render(gradient_ctx, [greedy], use_proxy=False)
    assert np.array_equal(
        only_greedy.reshape(-1, 4)[outside], stacked.reshape(-1, 4)[outside]
    )


def test_region_does_not_shield_rules_above_it(gradient_ctx):
    """Ownership runs down the stack only; reordering stays the escape hatch."""
    src = rgb8_to_lab((200, 0, 0))
    owner = RecolorRule(
        source_lab=src, target_lab=hex_to_lab("#0000C8"), tolerance=2,
        region=Region("rect", [(0.0, 0.0), (0.5, 1.0)]),
    )
    greedy = RecolorRule(
        source_lab=src, target_lab=hex_to_lab("#00C800"), tolerance=90
    )
    assert not np.array_equal(
        render(gradient_ctx, [owner, greedy], use_proxy=False),
        render(gradient_ctx, [greedy, owner], use_proxy=False),
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
