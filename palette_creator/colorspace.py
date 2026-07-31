"""Color conversions and hex helpers.

Everything downstream works on flattened ``(N, 3)`` arrays. Converting a masked
flat array is roughly 12x faster than converting a full ``H x W`` frame, which is
why nothing here accepts an image-shaped input.
"""

from __future__ import annotations

import warnings

import numpy as np
from skimage import color as skcolor

from .config import CHROMA_EPS

__all__ = [
    "rgb_to_lab",
    "lab_to_rgb",
    "lab_to_lch",
    "lch_to_lab",
    "hex_to_rgb",
    "rgb_to_hex",
    "lab_to_hex",
    "hex_to_lab",
    "rgb8_to_lab",
    "lab_to_rgb8",
    "hsv_to_rgb",
    "rgb_to_hsv",
    "gamut_compress",
    "is_neutral",
]


# -- bulk conversions -------------------------------------------------------


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """``(N, 3)`` float RGB in 0..1 -> ``(N, 3)`` float32 CIELAB."""
    flat = np.asarray(rgb, dtype=np.float64).reshape(-1, 1, 3)
    return skcolor.rgb2lab(flat).reshape(-1, 3).astype(np.float32)


def _lab2rgb_raw(lab: np.ndarray) -> np.ndarray:
    """Unclipped lab2rgb. Out-of-gamut input is expected, so skimage's warning
    about it is suppressed -- callers either clip or gamut-compress."""
    flat = np.asarray(lab, dtype=np.float64).reshape(-1, 1, 3)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*Conversion from CIE-LAB.*")
        return skcolor.lab2rgb(flat).reshape(-1, 3)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    """``(N, 3)`` CIELAB -> ``(N, 3)`` float32 RGB, clipped to 0..1."""
    return np.clip(_lab2rgb_raw(lab), 0.0, 1.0).astype(np.float32)


def rgb8_to_lab(rgb8) -> np.ndarray:
    """A single ``(r, g, b)`` in 0..255 -> ``(3,)`` float32 CIELAB."""
    arr = np.asarray(rgb8, dtype=np.float64).reshape(1, 3) / 255.0
    return rgb_to_lab(arr)[0]


def lab_to_rgb8(lab) -> tuple[int, int, int]:
    """A single ``(3,)`` CIELAB -> ``(r, g, b)`` ints in 0..255."""
    rgb = lab_to_rgb(np.asarray(lab, dtype=np.float64).reshape(1, 3))[0]
    return tuple(int(v) for v in np.rint(rgb * 255.0))


def lab_to_lch(lab: np.ndarray) -> np.ndarray:
    """``(N, 3)`` CIELAB -> ``(N, 3)`` (L, C, h) with h in radians."""
    lab = np.asarray(lab, dtype=np.float32)
    a, b = lab[:, 1], lab[:, 2]
    return np.stack([lab[:, 0], np.hypot(a, b), np.arctan2(b, a)], axis=1)


def lch_to_lab(lch: np.ndarray) -> np.ndarray:
    """``(N, 3)`` (L, C, h) -> ``(N, 3)`` CIELAB."""
    lch = np.asarray(lch, dtype=np.float32)
    L, C, h = lch[:, 0], lch[:, 1], lch[:, 2]
    return np.stack([L, C * np.cos(h), C * np.sin(h)], axis=1)


def hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    """``(..., 3)`` HSV in 0..1 -> ``(..., 3)`` RGB in 0..1."""
    return skcolor.hsv2rgb(np.asarray(hsv, dtype=np.float64))


def rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """``(..., 3)`` RGB in 0..1 -> ``(..., 3)`` HSV in 0..1."""
    return skcolor.rgb2hsv(np.asarray(rgb, dtype=np.float64))


def is_neutral(lab) -> bool:
    """True when a color carries too little chroma for its hue to mean anything."""
    lab = np.asarray(lab, dtype=np.float64)
    return bool(np.hypot(lab[1], lab[2]) < CHROMA_EPS)


# -- hex --------------------------------------------------------------------


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse ``#rgb``, ``#rrggbb`` (with or without ``#``). Raises ValueError."""
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"not a hex color: {value!r}")
    try:
        n = int(text, 16)
    except ValueError as exc:
        raise ValueError(f"not a hex color: {value!r}") from exc
    return ((n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF)


def rgb_to_hex(rgb) -> str:
    r, g, b = (int(np.clip(round(float(v)), 0, 255)) for v in rgb[:3])
    return f"#{r:02X}{g:02X}{b:02X}"


def lab_to_hex(lab) -> str:
    return rgb_to_hex(lab_to_rgb8(lab))


def hex_to_lab(value: str) -> np.ndarray:
    return rgb8_to_lab(hex_to_rgb(value))


# -- gamut ------------------------------------------------------------------


def gamut_compress(lab: np.ndarray, iterations: int = 8) -> np.ndarray:
    """Pull out-of-gamut colors back in by reducing chroma, preserving hue.

    Raw RGB clipping shifts hue (clipping only the channel that overflows tilts
    the color); scaling chroma toward the neutral axis at constant L* and h does
    not. Only the rows that actually clip are touched.
    """
    lab = np.asarray(lab, dtype=np.float32)
    if lab.size == 0:
        return lab

    def out_of_gamut(candidate: np.ndarray) -> np.ndarray:
        rgb = _lab2rgb_raw(candidate)
        return np.any((rgb < -1e-4) | (rgb > 1.0 + 1e-4), axis=1)

    bad = out_of_gamut(lab)
    if not bad.any():
        return lab

    out = lab.copy()
    lch = lab_to_lch(lab[bad])
    L, C, h = lch[:, 0], lch[:, 1], lch[:, 2]

    lo = np.zeros_like(C)
    hi = np.ones_like(C)
    best = np.zeros_like(C)
    for _ in range(iterations):
        mid = 0.5 * (lo + hi)
        trial = lch_to_lab(np.stack([L, C * mid, h], axis=1))
        ok = ~out_of_gamut(trial)
        best = np.where(ok, mid, best)
        lo = np.where(ok, mid, lo)
        hi = np.where(ok, hi, mid)

    out[bad] = lch_to_lab(np.stack([L, C * best, h], axis=1))
    return out
