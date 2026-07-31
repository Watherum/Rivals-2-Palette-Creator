"""Loaded-image state: original pixels plus their precomputed LAB/LCh arrays.

Two resolutions are kept per image. The 512px proxy drives interactive preview
(15-25 ms per render, so slider drags stay smooth); the full-res arrays are used
for the settle pass and for export.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .colorspace import rgb_to_lab
from .config import ALPHA_MIN_CLUSTER, ALPHA_MIN_RECOLOR, IMAGE_CACHE_SIZE, PROXY_SIZE


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

    def opaque_mask(self, threshold: int = ALPHA_MIN_CLUSTER) -> np.ndarray:
        return self.alpha >= threshold


def _build_pixelset(rgba: np.ndarray) -> PixelSet:
    flat = rgba.reshape(-1, 4)
    alpha = flat[:, 3]
    idx = np.flatnonzero(alpha >= ALPHA_MIN_RECOLOR).astype(np.int64)
    rgb = flat[idx, :3].astype(np.float32) / 255.0
    return PixelSet(idx=idx, rgb=rgb, lab=rgb_to_lab(rgb), alpha=alpha[idx].copy())


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
