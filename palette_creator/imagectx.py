"""Loaded-image state: original pixels plus their precomputed LAB/LCh arrays.

Two resolutions are kept per image. The 512px proxy drives interactive preview
(15-25 ms per render, so slider drags stay smooth); the full-res arrays are used
for the settle pass and for export.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .colorspace import rgb_to_lab
from .config import (
    ALPHA_MIN_CLUSTER,
    ALPHA_MIN_RECOLOR,
    IMAGE_CACHE_SIZE,
    PROXY_SIZE,
    SELECT_DENOISE_RADIUS,
    SELECT_DENOISE_SIGMA_R,
    SELECT_DENOISE_SIGMA_S,
)


@dataclass
class PixelSet:
    """The recolorable pixels of one image, flattened.

    ``idx`` indexes into the flattened ``H*W`` frame, so results scatter back
    with ``frame.reshape(-1, 4)[idx] = ...``.
    """

    idx: np.ndarray  # (N,) int64
    rgb: np.ndarray  # (N,3) float32, 0..1, original -- never mutated
    lab: np.ndarray  # (N,3) float32
    alpha: np.ndarray  # (N,) uint8
    shape: tuple[int, int] | None = None
    """``(H, W)`` of the frame ``idx`` points into. Needed only to build
    :attr:`sel_lab`; without it selection falls back to the raw colors."""
    _sel_lab: np.ndarray | None = field(default=None, repr=False, compare=False)
    _sel_C: np.ndarray | None = field(default=None, repr=False, compare=False)

    @property
    def L(self) -> np.ndarray:
        return self.lab[:, 0]

    @property
    def a(self) -> np.ndarray:
        return self.lab[:, 1]

    @property
    def b(self) -> np.ndarray:
        return self.lab[:, 2]

    def __post_init__(self) -> None:
        self.C = np.hypot(self.lab[:, 1], self.lab[:, 2]).astype(np.float32)
        self.h = np.arctan2(self.lab[:, 2], self.lab[:, 1]).astype(np.float32)

    def __len__(self) -> int:
        return int(self.idx.shape[0])

    @property
    def sel_lab(self) -> np.ndarray:
        """Denoised LAB, for deciding *which* pixels a rule selects.

        The renders come out of block-compressed textures and carry a few
        levels of 4x4 block noise, invisible in the art. A selection weight is a
        steep function of color, so measured on the raw pixels that noise flips
        the weight from block to block and the recolor comes out splotched.

        Only selection reads this. The transform always works from the real
        pixels, so no detail is smoothed out of the result.

        Built on first use (about 0.1 s for the proxy, under a second for a
        1024px render) rather than at load, so flipping between renders that
        have no rules yet stays instant.
        """
        if self._sel_lab is None:
            if self.shape is None or len(self) == 0:
                self._sel_lab = self.lab
            else:
                self._sel_lab = _denoise_for_selection(self.lab, self.idx, self.shape)
        return self._sel_lab

    @property
    def sel_C(self) -> np.ndarray:
        if self._sel_C is None:
            sel = self.sel_lab
            self._sel_C = np.hypot(sel[:, 1], sel[:, 2]).astype(np.float32)
        return self._sel_C

    def opaque_mask(self, threshold: int = ALPHA_MIN_CLUSTER) -> np.ndarray:
        return self.alpha >= threshold


def _build_pixelset(rgba: np.ndarray) -> PixelSet:
    flat = rgba.reshape(-1, 4)
    alpha = flat[:, 3]
    idx = np.flatnonzero(alpha >= ALPHA_MIN_RECOLOR).astype(np.int64)
    rgb = flat[idx, :3].astype(np.float32) / 255.0
    return PixelSet(
        idx=idx,
        rgb=rgb,
        lab=rgb_to_lab(rgb),
        alpha=alpha[idx].copy(),
        shape=(rgba.shape[0], rgba.shape[1]),
    )


def _denoise_for_selection(
    lab: np.ndarray, idx: np.ndarray, shape: tuple[int, int]
) -> np.ndarray:
    """Bilateral-filter the recolorable pixels' LAB. Returns ``(N, 3)`` float32.

    Edge preserving: a neighbour only contributes if its color is close
    (``SELECT_DENOISE_SIGMA_R`` LAB units), so block noise of a few units is
    averaged away while a real material boundary, tens of units across, stays
    exactly where it is. The radius is 3 so the window spans a whole 4x4 block.
    Transparent pixels never contribute.
    """
    radius = SELECT_DENOISE_RADIUS
    h, w = shape
    ys, xs = np.divmod(idx, w)
    # Work inside the art's bounding box: the character rarely fills the frame.
    y0, x0 = int(ys.min()), int(xs.min())
    bh, bw = int(ys.max()) - y0 + 1, int(xs.max()) - x0 + 1
    ys = ys - y0 + radius
    xs = xs - x0 + radius

    planes = np.zeros((3, bh + 2 * radius, bw + 2 * radius), dtype=np.float32)
    valid = np.zeros((bh + 2 * radius, bw + 2 * radius), dtype=np.float32)
    planes[:, ys, xs] = lab.T
    valid[ys, xs] = 1.0

    core = (slice(radius, radius + bh), slice(radius, radius + bw))
    center = planes[(slice(None),) + core]
    acc = np.zeros_like(center)
    wsum = np.zeros((bh, bw), dtype=np.float32)
    inv_s = 1.0 / (2.0 * SELECT_DENOISE_SIGMA_S**2)
    inv_r = 1.0 / (2.0 * SELECT_DENOISE_SIGMA_R**2)

    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            window = (
                slice(radius + dy, radius + dy + bh),
                slice(radius + dx, radius + dx + bw),
            )
            shifted = planes[(slice(None),) + window]
            diff = shifted - center
            d2 = np.einsum("chw,chw->hw", diff, diff)
            k = np.exp(-(dx * dx + dy * dy) * inv_s - d2 * inv_r) * valid[window]
            acc += k * shifted
            wsum += k

    # Every recolorable pixel counts itself at weight 1, so wsum >= 1 there.
    out = acc / np.maximum(wsum, 1e-6)
    return out[:, ys - radius, xs - radius].T.astype(np.float32)


def _resize_rgba(rgba: np.ndarray, size: int) -> np.ndarray:
    """Downscale RGBA via premultiplied alpha.

    Resizing straight alpha bleeds transparent black into antialiased edges, so
    the preview would show a dark fringe the export does not have.
    """
    h, w = rgba.shape[:2]
    if max(h, w) <= size:
        return rgba.copy()

    scale = size / float(max(h, w))
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))

    a = rgba[..., 3:4].astype(np.float32) / 255.0
    premul = np.concatenate(
        [rgba[..., :3].astype(np.float32) * a, rgba[..., 3:4].astype(np.float32)],
        axis=-1,
    )
    small = np.asarray(
        Image.fromarray(np.rint(premul).astype(np.uint8), "RGBA").resize(
            new_size, Image.Resampling.LANCZOS
        )
    ).astype(np.float32)

    sa = small[..., 3:4] / 255.0
    rgb = np.where(sa > 1e-4, small[..., :3] / np.maximum(sa, 1e-4), 0.0)
    out = np.concatenate([np.clip(rgb, 0, 255), small[..., 3:4]], axis=-1)
    return np.rint(out).astype(np.uint8)


@dataclass
class ImageContext:
    """Everything the recolor engine needs about one loaded render."""

    path: Path
    rgba_full: np.ndarray  # (H,W,4) uint8, ORIGINAL, never mutated
    full: PixelSet
    rgba_proxy: np.ndarray
    proxy: PixelSet
    clusters: object | None = None  # palette_extract.ClusterResult, set lazily

    @property
    def shape_full(self) -> tuple[int, int]:
        return self.rgba_full.shape[0], self.rgba_full.shape[1]

    @property
    def shape_proxy(self) -> tuple[int, int]:
        return self.rgba_proxy.shape[0], self.rgba_proxy.shape[1]

    def pixels(self, use_proxy: bool) -> PixelSet:
        return self.proxy if use_proxy else self.full

    def frame(self, use_proxy: bool) -> np.ndarray:
        return self.rgba_proxy if use_proxy else self.rgba_full

    def shape(self, use_proxy: bool) -> tuple[int, int]:
        return self.shape_proxy if use_proxy else self.shape_full

    def sample_rgb(self, x: int, y: int, radius: int = 1) -> tuple[int, int, int]:
        """Median RGB of the ``(2*radius+1)`` square at ``(x, y)`` in the full image.

        A median rather than the single pixel, because on a fully antialiased
        render a click very often lands on a blend of two materials.
        """
        h, w = self.shape_full
        x = int(np.clip(x, 0, w - 1))
        y = int(np.clip(y, 0, h - 1))
        if radius <= 0:
            px = self.rgba_full[y, x]
            return int(px[0]), int(px[1]), int(px[2])

        y0, y1 = max(0, y - radius), min(h, y + radius + 1)
        x0, x1 = max(0, x - radius), min(w, x + radius + 1)
        patch = self.rgba_full[y0:y1, x0:x1].reshape(-1, 4)
        opaque = patch[patch[:, 3] >= ALPHA_MIN_CLUSTER]
        if opaque.shape[0] == 0:
            opaque = patch
        med = np.median(opaque[:, :3].astype(np.float32), axis=0)
        return tuple(int(v) for v in np.rint(med))

    def alpha_at(self, x: int, y: int) -> int:
        h, w = self.shape_full
        if not (0 <= x < w and 0 <= y < h):
            return 0
        return int(self.rgba_full[y, x, 3])


def load_context(path: Path | str, proxy_size: int = PROXY_SIZE) -> ImageContext:
    """Read a PNG and precompute both pixel sets (~135 ms for a 1024px render)."""
    path = Path(path)
    with Image.open(path) as im:
        rgba_full = np.asarray(im.convert("RGBA"), dtype=np.uint8).copy()

    rgba_proxy = _resize_rgba(rgba_full, proxy_size)
    return ImageContext(
        path=path,
        rgba_full=rgba_full,
        full=_build_pixelset(rgba_full),
        rgba_proxy=rgba_proxy,
        proxy=_build_pixelset(rgba_proxy),
    )


class ContextCache:
    """Small LRU so flipping between palettes of one skin stays instant."""

    def __init__(self, size: int = IMAGE_CACHE_SIZE) -> None:
        self.size = max(1, size)
        self._items: OrderedDict[Path, ImageContext] = OrderedDict()

    def get(self, path: Path | str) -> ImageContext:
        path = Path(path)
        ctx = self._items.get(path)
        if ctx is not None:
            self._items.move_to_end(path)
            return ctx

        ctx = load_context(path)
        self._items[path] = ctx
        while len(self._items) > self.size:
            self._items.popitem(last=False)
        return ctx

    def clear(self) -> None:
        self._items.clear()
