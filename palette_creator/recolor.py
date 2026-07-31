"""The recolor engine: selection masks and shading-preserving color transforms.

Design notes, grounded in measurements taken against the official palettes in
this repository (comparing e.g. ``T_Ran_Default_Neutral`` with
``T_Ran_Default_Red``):

* The game recolors by material/UV region, not by color -- one source RGB maps to
  ~2.8 distinct targets. So no color-driven tool can reproduce an official
  palette exactly. ``RecolorRule.contiguous`` is the escape hatch, being the one
  spatial discriminator available when color genuinely cannot separate two
  materials.
* Color is nonetheless a strong proxy: predicting the official target from the
  source color alone lands within 7.7/255 RMS.
* Within a k=12 CIELAB cluster the official target still varies by 33.7/255 RMS.
  That is far too much to flat-fill a cluster, which is why every transform here
  is per-pixel and preserves each pixel's place in its material's tonal ramp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from scipy import ndimage

from .colorspace import gamut_compress, lab_to_rgb
from .config import CHROMA_EPS, CHROMA_MAX
from .imagectx import ImageContext, PixelSet


class MatchMode(str, Enum):
    MATERIAL = "material"
    """Ignore lightness, so one click grabs a whole shaded material. Default."""

    EXACT = "exact"
    """Full CIELAB distance -- only near-identical colors match."""

    HUE = "hue"
    """Hue angle only, gated by chroma. For a saturated material that shares its
    a*/b* neighbourhood with a differently-hued one."""


W_BY_MODE: dict[MatchMode, tuple[float, float]] = {
    MatchMode.EXACT: (1.0, 1.0),
    MatchMode.MATERIAL: (0.15, 0.35),
    MatchMode.HUE: (0.0, 0.0),
}
"""``(lightness weight, chroma weight)`` per mode.

Distance is measured as ``(wL*dL)^2 + (wC*dC)^2 + dH^2`` -- the CIE94
decomposition. Lighting a material moves it in *both* L* and C* (highlights
desaturate toward white) while leaving hue essentially fixed, so hue is the
invariant worth matching on and the other two want downweighting.

Measured on ``T_Ran_Default_Neutral``: with lightness downweighted alone, a
click on a saturated material captured only 62% more of its tonal range than an
exact match; adding the chroma term takes that well past 3x.

At ``(0.15, 0.35)`` and the default tolerance of 12, a DeltaL of 60 -- shadow to
highlight of one material -- costs 9, comfortably inside, while a DeltaL of 100
costs 15 and falls outside, so white and black do not merge into a mid-gray
source.
"""


@dataclass(eq=False)
class RecolorRule:
    """One source color -> target color replacement.

    ``eq=False`` keeps identity comparison: the generated ``__eq__`` would
    compare numpy arrays and raise inside ``list.index``/``list.remove``, and
    two rules holding the same values are still distinct entries in the list.
    """

    source_lab: np.ndarray
    target_lab: np.ndarray
    mode: MatchMode = MatchMode.MATERIAL
    tolerance: float = 12.0
    feather: float = 0.35
    l_weight: float | None = None
    c_weight: float | None = None
    lightness_amount: float = 1.0
    chroma_amount: float = 1.0
    hue_amount: float = 1.0
    opacity: float = 1.0
    smooth_edges: bool = False
    contiguous: bool = False
    seed_uv: tuple[float, float] | None = None
    """Where the user clicked, in normalized 0..1 coords so the same rule works
    at proxy and full resolution."""
    enabled: bool = True
    cluster_id: int | None = None
    lock_to_cluster: bool = False
    name: str = ""

    def __post_init__(self) -> None:
        self.source_lab = np.asarray(self.source_lab, dtype=np.float32).reshape(3)
        self.target_lab = np.asarray(self.target_lab, dtype=np.float32).reshape(3)
        if isinstance(self.mode, str):
            self.mode = MatchMode(self.mode)

    @property
    def effective_l_weight(self) -> float:
        if self.l_weight is not None:
            return float(self.l_weight)
        return W_BY_MODE[self.mode][0]

    @property
    def effective_c_weight(self) -> float:
        if self.c_weight is not None:
            return float(self.c_weight)
        return W_BY_MODE[self.mode][1]

    def fingerprint(self) -> tuple:
        """Identity for mask caching: everything that affects the mask only."""
        return (
            tuple(np.round(self.source_lab, 4)),
            self.mode.value,
            round(float(self.tolerance), 4),
            round(float(self.feather), 4),
            round(self.effective_l_weight, 4),
            round(self.effective_c_weight, 4),
            round(float(self.opacity), 4),
            bool(self.smooth_edges),
            bool(self.contiguous),
            None if self.seed_uv is None else tuple(round(v, 6) for v in self.seed_uv),
            bool(self.lock_to_cluster),
            self.cluster_id,
        )

    def is_identity(self) -> bool:
        return bool(np.allclose(self.source_lab, self.target_lab, atol=1e-6))


# -- selection --------------------------------------------------------------


def rule_distance(ps: PixelSet, rule: RecolorRule) -> np.ndarray:
    """Distance from every pixel to the rule's source color, in LAB units."""
    L0, a0, b0 = (float(v) for v in rule.source_lab)

    if rule.mode is MatchMode.HUE:
        # Chroma-gated hue angle. Near-neutral pixels have no meaningful hue, so
        # the gate pushes them far away instead of letting them match anything.
        c0 = float(np.hypot(a0, b0))
        h0 = float(np.arctan2(b0, a0))
        dh = np.abs(np.angle(np.exp(1j * (ps.h - h0))))
        gate = np.clip((ps.C - CHROMA_EPS) / 8.0, 0.0, 1.0)
        return (dh * max(c0, 1.0) / np.maximum(gate, 1e-3)).astype(np.float32)

    da = ps.a - a0
    db = ps.b - b0
    ab2 = da * da + db * db

    wl = rule.effective_l_weight
    wc = rule.effective_c_weight

    if wc == 1.0:
        # dC and dH carry equal weight, so no decomposition is needed.
        d2 = ab2
    else:
        # Split the (a*, b*) offset into chroma and hue parts. The identity
        # dH^2 = da^2 + db^2 - dC^2 avoids any trigonometry, and stays
        # well-behaved near neutral -- grays sit at the origin, so they match
        # grays rather than matching every hue as a raw angle would.
        c0 = float(np.hypot(a0, b0))
        dc = ps.C - c0
        dh2 = np.maximum(ab2 - dc * dc, 0.0)
        d2 = dh2 + (wc * wc) * (dc * dc)

    if wl > 0.0:
        dl = (ps.L - L0) * wl
        d2 = d2 + dl * dl

    return np.sqrt(d2, dtype=np.float32)


def rule_weight(
    ps: PixelSet,
    rule: RecolorRule,
    *,
    shape: tuple[int, int] | None = None,
    labels: np.ndarray | None = None,
) -> np.ndarray:
    """Per-pixel blend weight in 0..1 for this rule.

    The ramp is a smoothstep in *color* space, never a hard threshold. An
    antialiased pixel blending two materials lands partway along the ramp and is
    partially recolored, which is the correct answer for a pixel that is itself
    a blend.
    """
    d = rule_distance(ps, rule)
    d_in = rule.tolerance * (1.0 - rule.feather)
    d_out = rule.tolerance * (1.0 + rule.feather)
    span = max(d_out - d_in, 1e-6)

    t = np.clip((d_out - d) / span, 0.0, 1.0)
    w = (t * t * (3.0 - 2.0 * t) * float(rule.opacity)).astype(np.float32)

    if rule.lock_to_cluster and labels is not None and rule.cluster_id is not None:
        w = np.where(labels == rule.cluster_id, w, 0.0).astype(np.float32)

    if (rule.smooth_edges or rule.contiguous) and shape is not None:
        w = _spatial_postprocess(w, ps, rule, shape)

    return w


def _spatial_postprocess(
    w: np.ndarray, ps: PixelSet, rule: RecolorRule, shape: tuple[int, int]
) -> np.ndarray:
    """Scatter the weights into the frame, filter spatially, gather back."""
    h, width = shape
    plane = np.zeros(h * width, dtype=np.float32)
    plane[ps.idx] = w
    plane = plane.reshape(h, width)

    if rule.smooth_edges:
        plane = ndimage.gaussian_filter(plane, sigma=0.8, mode="constant")

    if rule.contiguous and rule.seed_uv is not None:
        labeled, count = ndimage.label(plane > 0.35)
        if count:
            u, v = rule.seed_uv
            sx = int(np.clip(round(u * (width - 1)), 0, width - 1))
            sy = int(np.clip(round(v * (h - 1)), 0, h - 1))
            target = labeled[sy, sx]
            if target == 0:
                # The click landed just off the region; take the nearest label.
                ys, xs = np.nonzero(labeled)
                if ys.size:
                    nearest = np.argmin((ys - sy) ** 2 + (xs - sx) ** 2)
                    target = labeled[ys[nearest], xs[nearest]]
            if target != 0:
                plane = np.where(labeled == target, plane, 0.0)

    return plane.reshape(-1)[ps.idx].astype(np.float32)


# -- transform --------------------------------------------------------------


def _anchored(x: np.ndarray, x0: float, y0: float, hi: float) -> np.ndarray:
    """Monotone piecewise-linear remap sending 0->0, x0->y0, hi->hi.

    This is a levels curve. It moves the material's anchor tone to the target
    while keeping every other tone in order and inside the range, so shading
    survives and nothing needs clamping. A plain additive shift would instead
    translate the whole ramp and flatten highlights against the ceiling.
    """
    x0 = float(np.clip(x0, 1e-3, hi - 1e-3))
    y0 = float(np.clip(y0, 0.0, hi))
    out = np.empty_like(x)
    lo = x <= x0
    out[lo] = y0 * (x[lo] / x0)
    out[~lo] = y0 + (hi - y0) * ((x[~lo] - x0) / max(hi - x0, 1e-6))
    return out


def rule_transform(ps: PixelSet, rule: RecolorRule) -> np.ndarray:
    """The recolored LAB for every pixel, as if this rule applied everywhere."""
    ls, a_s, bs = (float(v) for v in rule.source_lab)
    lt, at, bt = (float(v) for v in rule.target_lab)

    cs = float(np.hypot(a_s, bs))
    ct = float(np.hypot(at, bt))
    ht = float(np.arctan2(bt, at))

    l_new = _anchored(ps.L, ls, lt, 100.0)
    l_out = ps.L + rule.lightness_amount * (l_new - ps.L)

    if cs < CHROMA_EPS:
        # A gray source has no hue to rotate: tint additively and adopt the
        # target's hue outright.
        c_new = np.clip(ps.C + ct, 0.0, CHROMA_MAX)
        h_out = np.full_like(ps.h, ht)
    else:
        c_new = _anchored(ps.C, cs, ct, CHROMA_MAX)
        hs = float(np.arctan2(bs, a_s))
        dh = float(np.angle(np.exp(1j * (ht - hs))))  # shortest signed rotation
        h_out = ps.h + rule.hue_amount * dh

    c_out = ps.C + rule.chroma_amount * (c_new - ps.C)
    c_out = np.clip(c_out, 0.0, CHROMA_MAX)

    return np.stack(
        [l_out, c_out * np.cos(h_out), c_out * np.sin(h_out)], axis=1
    ).astype(np.float32)


# -- composition ------------------------------------------------------------


def apply_rules(
    ps: PixelSet,
    rules,
    *,
    shape: tuple[int, int] | None = None,
    labels: np.ndarray | None = None,
    weight_cache: dict | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Composite every enabled rule. Returns ``(new_lab, coverage)``.

    Rules are painter's-order: earlier rules keep the pixels they claim. Every
    rule matches and transforms against the *original* pixels, so chaining
    red -> blue and then blue -> green cannot double-apply.
    """
    n = ps.lab.shape[0]
    accum = np.zeros((n, 3), dtype=np.float32)
    cov = np.zeros(n, dtype=np.float32)

    for rule in rules:
        if not rule.enabled:
            continue

        key = None
        if weight_cache is not None:
            key = (id(ps), rule.fingerprint())
            w = weight_cache.get(key)
        else:
            w = None

        if w is None:
            w = rule_weight(ps, rule, shape=shape, labels=labels)
            if weight_cache is not None and key is not None:
                weight_cache[key] = w

        # Coverage never exceeds 1, so overlapping rules blend by priority
        # instead of summing into something oversaturated.
        w = np.clip(w * (1.0 - cov), 0.0, 1.0)
        if not w.any():
            continue

        accum += w[:, None] * rule_transform(ps, rule)
        cov += w

    new_lab = ps.lab * (1.0 - cov)[:, None] + accum
    return new_lab.astype(np.float32), cov


def render(
    ctx: ImageContext,
    rules,
    *,
    use_proxy: bool = True,
    gamut_safe: bool = True,
    weight_cache: dict | None = None,
) -> np.ndarray:
    """Apply ``rules`` to ``ctx`` and return a fresh ``(H, W, 4)`` uint8 frame.

    The alpha channel is copied through byte for byte, and pixels no rule
    touches stay bit-identical to the source -- so an unedited region is never
    silently re-encoded by an rgb->lab->rgb round trip.
    """
    ps = ctx.pixels(use_proxy)
    out = ctx.frame(use_proxy).copy()

    active = [r for r in rules if r.enabled and not r.is_identity()]
    if not active:
        return out

    labels = None
    if any(r.lock_to_cluster for r in active) and ctx.clusters is not None:
        labels = ctx.clusters.labels_for(ps)

    new_lab, _cov = apply_rules(
        ps,
        active,
        shape=ctx.shape(use_proxy),
        labels=labels,
        weight_cache=weight_cache,
    )

    changed = np.any(np.abs(new_lab - ps.lab) > 1e-4, axis=1)
    if not changed.any():
        return out

    lab_c = new_lab[changed]
    if gamut_safe:
        lab_c = gamut_compress(lab_c)

    rgb = lab_to_rgb(lab_c)
    out.reshape(-1, 4)[ps.idx[changed], :3] = np.rint(rgb * 255.0).astype(np.uint8)
    return out


def selection_overlay(
    ctx: ImageContext,
    rule: RecolorRule,
    *,
    use_proxy: bool = True,
    color: tuple[int, int, int] = (255, 0, 200),
) -> np.ndarray:
    """A tinted copy of the frame showing what ``rule`` currently selects."""
    ps = ctx.pixels(use_proxy)
    out = ctx.frame(use_proxy).copy()
    w = rule_weight(ps, rule, shape=ctx.shape(use_proxy))
    if not w.any():
        return out

    flat = out.reshape(-1, 4)
    base = flat[ps.idx, :3].astype(np.float32)
    tint = np.asarray(color, dtype=np.float32)
    blend = base + (0.6 * w)[:, None] * (tint - base)
    flat[ps.idx, :3] = np.rint(np.clip(blend, 0, 255)).astype(np.uint8)
    return out
