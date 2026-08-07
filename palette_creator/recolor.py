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
from PIL import Image, ImageDraw
from scipy import ndimage

from .colorspace import gamut_compress, lab_to_rgb
from .config import CHROMA_EPS, CHROMA_MAX
from .imagectx import ImageContext, PixelSet


CHROMA_GAIN_MAX = 2.0
"""Ceiling on how much the chroma remap may multiply a pixel's distance from the
neutral axis. Chosen to sit above the ratio a genuinely colored material needs
(a chroma-40 source retargeted to chroma-72 wants 1.8x) while cutting off the
runaway gains that near-gray sources produce."""

WRITE_EPS = 1.0 / 512.0
"""Below this a rule's contribution cannot move an 8-bit channel, so it does not
count as writing to the pixel and does not take ownership of it."""


@dataclass
class Region:
    """A user-drawn area that confines a rule, in normalized 0..1 coords.

    Normalized rather than pixel coords for the same reason as ``seed_uv``: one
    region has to rasterize correctly against both the proxy and the full-res
    frame. ``kind`` is ``"rect"`` (two opposite corners), ``"lasso"`` (polygon
    vertices, implicitly closed) or ``"stroke"`` (a pen path, thickened by
    ``width``).
    """

    kind: str
    points: list[tuple[float, float]]
    width: float = 0.0
    """Stroke thickness, as a fraction of the image's *width*. Normalized for
    the same reason the points are: one pen stroke has to come out the same
    thickness relative to the art on the 512px proxy and on the full render."""
    _mask_cache: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.points = [(float(u), float(v)) for u, v in self.points]
        self.width = float(self.width)

    def is_valid(self) -> bool:
        if self.kind == "rect":
            return len(self.points) == 2
        if self.kind == "lasso":
            return len(self.points) >= 3
        if self.kind == "stroke":
            # A single click is a legitimate pen dab, so one point is enough.
            return len(self.points) >= 1 and self.width > 0.0
        return False

    def key(self) -> tuple:
        return (
            self.kind,
            tuple((round(u, 6), round(v, 6)) for u, v in self.points),
            round(self.width, 8),
        )

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        """Rasterize to a ``(H, W)`` bool mask, cached per shape.

        Drawn with PIL rather than a numpy point-in-polygon test: it handles
        every shape in one call and is fast enough (~1 ms at 1024px) to stay in
        the interactive render path.
        """
        cached = self._mask_cache.get(shape)
        if cached is not None:
            return cached

        h, w = shape
        img = Image.new("L", (w, h), 0)
        if self.is_valid():
            pts = [
                (u * (w - 1), v * (h - 1)) for u, v in self.points
            ]
            draw = ImageDraw.Draw(img)
            if self.kind == "rect":
                (x0, y0), (x1, y1) = pts
                draw.rectangle(
                    [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], fill=255
                )
            elif self.kind == "stroke":
                _draw_stroke(draw, pts, self.width * max(w - 1, 1))
            else:
                draw.polygon(pts, fill=255)

        out = np.asarray(img, dtype=np.uint8) > 0
        self._mask_cache[shape] = out
        return out


def _draw_stroke(draw: ImageDraw.ImageDraw, pts: list, thickness: float) -> None:
    """Rasterize a pen path as a round-capped, round-joined band.

    PIL's ``joint="curve"`` rounds the *interior* corners only, so the caps are
    added here as discs. Drawing one at every vertex rather than just the two
    ends also covers the case where the path doubles back on itself faster than
    the join logic follows.
    """
    line_w = max(int(round(thickness)), 1)

    if len(pts) >= 2:
        draw.line(pts, fill=255, width=line_w, joint="curve")
    elif line_w <= 1:
        draw.point(pts, fill=255)

    if line_w > 1:
        r = thickness / 2.0
        for x, y in pts:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=255)


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
    region: Region | None = None
    """A drawn area confining this rule. Unlike ``contiguous`` this needs no
    connectivity, so it can split two parts the shading bridges together.

    A ``"stroke"`` region does more than confine: it *is* the selection. See
    :attr:`is_pen`."""
    match_all: bool = False
    """Ignore ``region`` and match everywhere. A temporary override, so a region
    can be kept while checking what the rule would do unconfined."""
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
            bool(self.smooth_edges),
            bool(self.contiguous),
            None if self.seed_uv is None else tuple(round(v, 6) for v in self.seed_uv),
            bool(self.lock_to_cluster),
            self.cluster_id,
            None if self.region is None else self.region.key(),
            bool(self.match_all),
        )

    def is_identity(self) -> bool:
        return bool(np.allclose(self.source_lab, self.target_lab, atol=1e-6))

    @property
    def is_pen(self) -> bool:
        """Whether this rule is a pen stroke rather than a color selection.

        A pen rule takes every pixel its stroke covers, whatever color it is:
        the user pointed at those pixels directly, so there is nothing left for
        the color match to decide. ``mode``, ``tolerance`` and ``feather`` are
        inert on one, and the UI grays them out to say so.

        So is ``match_all``, which is why there is no special case for it: it
        lifts a *confinement*, and :func:`_pen_weight` reads the stroke mask
        directly rather than confining anything. Honouring it would otherwise
        turn one stroke into a whole-image recolor of whatever it landed on.
        """
        return (
            self.region is not None
            and self.region.kind == "stroke"
            and self.region.is_valid()
        )

    @property
    def region_active(self) -> bool:
        return (
            self.region is not None
            and self.region.is_valid()
            and not self.match_all
        )


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
    """How strongly this rule *selects* each pixel, in 0..1.

    The ramp is a smoothstep in *color* space, never a hard threshold. An
    antialiased pixel blending two materials lands partway along the ramp and is
    partially recolored, which is the correct answer for a pixel that is itself
    a blend.

    Deliberately excludes ``opacity``: this is a claim on the pixel, not an
    amount of paint. Folding Strength in here would make a half-strength rule
    surrender half of every pixel it owns to whatever rule sits below it, when
    what should show through is the *original* color. :func:`apply_rules`
    applies Strength after ownership is settled.
    """
    if rule.is_pen:
        return _pen_weight(ps, rule, shape)

    d = rule_distance(ps, rule)
    d_in = rule.tolerance * (1.0 - rule.feather)
    d_out = rule.tolerance * (1.0 + rule.feather)
    span = max(d_out - d_in, 1e-6)

    t = np.clip((d_out - d) / span, 0.0, 1.0)
    w = (t * t * (3.0 - 2.0 * t)).astype(np.float32)

    if rule.lock_to_cluster and labels is not None and rule.cluster_id is not None:
        w = np.where(labels == rule.cluster_id, w, 0.0).astype(np.float32)

    needs_spatial = rule.smooth_edges or rule.contiguous or rule.region_active
    if needs_spatial and shape is not None:
        w = _spatial_postprocess(w, ps, rule, shape)

    return w


def _pen_weight(
    ps: PixelSet, rule: RecolorRule, shape: tuple[int, int] | None
) -> np.ndarray:
    """A pen rule's selection: the stroke, and nothing else.

    No color ramp at all. Every opaque pixel under the band is taken at full
    weight -- transparent ones are excluded for free, since ``ps.idx`` only
    covers the recolorable pixels.

    ``Smooth edges`` is the one matching control that still means something
    here: it feathers the band's own edge, which the hard rasterization
    otherwise leaves stair-stepped against antialiased art. It is applied after
    the mask rather than before, unlike the color path, because the edge being
    softened is the stroke's rather than the material's.
    """
    if shape is None or rule.region is None:
        return np.zeros(len(ps), dtype=np.float32)

    plane = rule.region.mask(shape).astype(np.float32)
    if rule.smooth_edges:
        plane = ndimage.gaussian_filter(plane, sigma=0.8, mode="constant")
    return plane.reshape(-1)[ps.idx].astype(np.float32)


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

    # Before the flood fill, so "this region only" traces connectivity *within*
    # the drawn area -- otherwise a blob that leaks outside would drag the
    # outside part back in.
    if rule.region_active:
        plane = np.where(rule.region.mask(shape), plane, 0.0)

    if rule.contiguous:
        plane = _keep_connected_blob(plane, rule, shape)

    return plane.reshape(-1)[ps.idx].astype(np.float32)


def _keep_connected_blob(
    plane: np.ndarray, rule: RecolorRule, shape: tuple[int, int]
) -> np.ndarray:
    """Keep only the connected component containing the rule's seed point.

    Falls back to the largest component when the rule has no seed -- rules built
    from a palette swatch click used to have ``seed_uv=None``, which silently
    turned this whole flag into a no-op.
    """
    h, width = shape
    labeled, count = ndimage.label(plane > 0.35)
    if not count:
        return plane

    target = 0
    if rule.seed_uv is not None:
        u, v = rule.seed_uv
        sx = int(np.clip(round(u * (width - 1)), 0, width - 1))
        sy = int(np.clip(round(v * (h - 1)), 0, h - 1))
        target = int(labeled[sy, sx])
        if target == 0:
            # The click landed just off the region; take the nearest label.
            ys, xs = np.nonzero(labeled)
            if ys.size:
                nearest = np.argmin((ys - sy) ** 2 + (xs - sx) ** 2)
                target = int(labeled[ys[nearest], xs[nearest]])

    if target == 0:
        sizes = np.bincount(labeled.ravel())
        sizes[0] = 0
        target = int(np.argmax(sizes))

    return np.where(labeled == target, plane, 0.0)


# -- transform --------------------------------------------------------------


def _anchored(
    x: np.ndarray, x0: float, y0: float, hi: float, *, max_gain: float | None = None
) -> np.ndarray:
    """Monotone piecewise-linear remap sending 0->0, x0->y0, hi->hi.

    This is a levels curve. It moves the material's anchor tone to the target
    while keeping every other tone in order and inside the range, so shading
    survives and nothing needs clamping. A plain additive shift would instead
    translate the whole ramp and flatten highlights against the ceiling.

    ``max_gain`` caps the slope of the segment *below* the anchor. That segment
    rises at ``y0 / x0``, which explodes when the source sits near zero -- a
    material at chroma 2.9 retargeted to chroma 72 amplifies by 25x, and the
    +/-1 of 8-bit quantization noise in a flat gray comes back as visible
    speckle. Capping it keeps the anchor exactly on ``y0`` and lets the bottom
    of the ramp land above zero instead, which is the honest answer: a pixel
    with no chroma to scale should adopt the target's, not be scaled up from
    nothing. The cap can never drive the segment negative, because it only
    engages when the uncapped slope is steeper.
    """
    x0 = float(np.clip(x0, 1e-3, hi - 1e-3))
    y0 = float(np.clip(y0, 0.0, hi))
    out = np.empty_like(x)
    lo = x <= x0

    gain = y0 / x0
    if max_gain is not None and gain > max_gain:
        out[lo] = np.maximum(y0 - max_gain * (x0 - x[lo]), 0.0)
    else:
        out[lo] = y0 * (x[lo] / x0)

    out[~lo] = y0 + (hi - y0) * ((x[~lo] - x0) / max(hi - x0, 1e-6))
    return out


def _pen_transform(ps: PixelSet, rule: RecolorRule) -> np.ndarray:
    """A pen paints: every pixel under the stroke goes to the target color.

    Not the shading-preserving levels curve the color rules use, and the
    difference is the whole point. That curve anchors the ramp on the rule's
    *source* color, which is right when the rule was aimed at one material --
    but a stroke crosses whatever it crosses. Anchored on the dominant material
    under it, a stroke drawn across an orange leg onto a white wrap left the
    white all but untouched: red over white came out as white. Drawing red and
    getting white back is not a pen.

    Shading is not lost, it is moved onto the sliders that already exist and
    already say what they do:

    * ``Lightness shift`` 0 keeps every pixel's own brightness and takes only
      the hue and saturation -- a tint that follows the art underneath.
    * ``Saturation shift`` does the same for chroma.
    * ``Strength`` blends the whole thing back toward the original pixel, so
      partway down is a shaded version of the stroke color.

    At the defaults of 1 that is a flat fill, which is what a pen is for.
    """
    lt, at, bt = (float(v) for v in rule.target_lab)
    ct = float(np.hypot(at, bt))
    ht = float(np.arctan2(bt, at))

    l_out = ps.L + rule.lightness_amount * (lt - ps.L)
    c_out = np.clip(ps.C + rule.chroma_amount * (ct - ps.C), 0.0, CHROMA_MAX)

    if rule.hue_amount >= 1.0:
        h_out = np.full_like(ps.h, ht)
    else:
        # Interpolated as unit vectors, so the blend takes the short way round
        # the circle instead of jumping at the +/-pi wrap.
        a = float(rule.hue_amount)
        blended = (1.0 - a) * np.exp(1j * ps.h) + a * np.exp(1j * ht)
        h_out = np.angle(blended).astype(np.float32)

    return np.stack(
        [l_out, c_out * np.cos(h_out), c_out * np.sin(h_out)], axis=1
    ).astype(np.float32)


def rule_transform(ps: PixelSet, rule: RecolorRule) -> np.ndarray:
    """The recolored LAB for every pixel, as if this rule applied everywhere."""
    if rule.is_pen:
        return _pen_transform(ps, rule)

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
        c_new = _anchored(ps.C, cs, ct, CHROMA_MAX, max_gain=CHROMA_GAIN_MAX)
        hs = float(np.arctan2(bs, a_s))
        dh = float(np.angle(np.exp(1j * (ht - hs))))  # shortest signed rotation
        rotated = ps.h + rule.hue_amount * dh

        # A near-neutral *pixel* has no meaningful hue either, whatever the
        # source's chroma was: its h is 8-bit quantization noise around the
        # neutral axis. Rotating that noise scatters hues across the wheel, and
        # since c_new then lifts the pixel to the target's chroma, the noise
        # comes back as fully saturated confetti -- a black material told to go
        # red arriving as green speckle.
        #
        # Fade to the target hue as the pixel approaches neutral. Interpolated
        # as unit vectors so the blend takes the short way round the circle
        # instead of jumping at the +/-pi wrap.
        gate = np.clip((ps.C - CHROMA_EPS) / 8.0, 0.0, 1.0)
        blended = gate * np.exp(1j * rotated) + (1.0 - gate) * np.exp(1j * ht)
        h_out = np.angle(blended).astype(np.float32)

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

    Rules layer like a stack: **a rule owns every pixel it writes to, outright.**
    Once a rule has touched a pixel -- at any strength, however faintly -- no
    rule below it in the list may write to that pixel again, in preview or in
    export. A lower rule is not allowed to "top up" the share a higher rule left
    unpainted; whatever the owning rule did not cover stays original art.

    Two weaker models were tried first and both leaked. ``w * (1 - cov)`` let
    every lower rule tint the soft share of the rule above, haloing every
    feathered edge. Subtracting coverage (``w - cov``) fixed the fully-claimed
    core but still let a broad rule fill in the remainder of a partial claim --
    which is how a tight rule for the shoes, claiming its pixels at 0.09 to
    0.99, ended up wearing the color of the suit rule beneath it.

    A drawn ``region`` claims its whole area, not just the pixels the owning
    rule matched, so a sloppy outline still shields everything inside it.

    Every rule matches and transforms against the *original* pixels, so chaining
    red -> blue and then blue -> green cannot double-apply.
    """
    n = ps.lab.shape[0]
    accum = np.zeros((n, 3), dtype=np.float32)
    cov = np.zeros(n, dtype=np.float32)
    # Pixels already owned by a rule above. Kept separate from ``cov``: a region
    # locks its whole area, but the parts of it nobody painted must still
    # resolve to the original art rather than to black.
    locked = np.zeros(n, dtype=bool)

    for rule in rules:
        if not rule.enabled:
            continue
        if rule.opacity <= 0.0:
            # Strength 0 means "off", so it must not claim pixels and block the
            # rules underneath it.
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

        # Below this a "write" is not visible at 8-bit, and a Gaussian tail from
        # Smooth edges would otherwise lock a wide halo it never actually paints.
        take = np.where((w > WRITE_EPS) & ~locked, w, 0.0).astype(np.float32)

        # Claim territory even when nothing was painted this pass, so an empty
        # or badly-aimed region still shields its area from the rules below.
        if rule.region_active and shape is not None:
            locked |= rule.region.mask(shape).reshape(-1)[ps.idx]

        if not take.any():
            continue

        # Strength blends this rule's own result toward the ORIGINAL pixel, not
        # toward whatever is underneath, so turning a rule down reveals the
        # source art rather than the next rule in the stack.
        result = rule_transform(ps, rule)
        opacity = float(rule.opacity)
        if opacity < 1.0:
            result = ps.lab + opacity * (result - ps.lab)

        accum += take[:, None] * result
        cov += take
        locked |= take > 0.0

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
