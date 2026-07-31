"""k-means palette extraction -- turns a render into clickable swatches.

Clustering is used for *selection only*. Measured on the official palettes in
this repo, a k=12 CIELAB cluster still spans 33.7/255 RMS of the official target
color, so flat-filling a cluster would destroy shading. Clicking a swatch
therefore builds an ordinary :class:`~palette_creator.recolor.RecolorRule` whose
per-pixel transform preserves the material's tonal ramp.

Clustering runs in full CIELAB with no L* downweighting: downweighting was
measured *worse* (43.7 vs 33.7) because it merges materials that differ mainly
in lightness.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.cluster.vq import kmeans2

from .colorspace import lab_to_hex
from .config import (
    ALPHA_MIN_CLUSTER,
    CHROMA_EPS,
    KMEANS_DEFAULT_K,
    KMEANS_ITERS,
    KMEANS_SAMPLE,
)
from .imagectx import PixelSet

_CHUNK = 50_000
"""Rows per assignment chunk. A naive (N,1,3)-(1,k,3) broadcast at N=289k, k=24
would allocate ~166 MB."""


@dataclass
class Swatch:
    index: int
    centroid_lab: np.ndarray
    medoid_lab: np.ndarray
    """Nearest *actual* pixel to the centroid. Shown in the UI -- a centroid of a
    shaded material is often a color that does not occur in the image."""
    midtone_lab: np.ndarray
    """The cluster's median-lightness color. Used as a rule's source, because
    anchoring the remap at a mid-tone keeps both shadows and highlights sane."""
    fraction: float
    spread: float
    """RMS CIELAB distance within the cluster."""
    ab_spread_p85: float
    """85th percentile of (a*, b*) distance -- the cluster's natural tolerance."""
    pixel_count: int

    @property
    def hex(self) -> str:
        return lab_to_hex(self.medoid_lab)

    @property
    def is_neutral(self) -> bool:
        return bool(np.hypot(self.medoid_lab[1], self.medoid_lab[2]) < CHROMA_EPS * 2)

    @property
    def suggested_tolerance(self) -> float:
        return float(np.clip(self.ab_spread_p85, 6.0, 40.0))

    @property
    def spans_materials(self) -> bool:
        """High spread means this color covers more than one material."""
        return self.spread > 28.0


@dataclass
class ClusterResult:
    swatches: list[Swatch]
    centroids: np.ndarray
    k: int
    seed: int
    _label_cache: dict = field(default_factory=dict, repr=False)

    def labels_for(self, ps: PixelSet) -> np.ndarray:
        """Cluster index per pixel of ``ps``, computed lazily and cached."""
        key = id(ps)
        labels = self._label_cache.get(key)
        if labels is None:
            labels = assign(ps.lab, self.centroids)
            self._label_cache[key] = labels
        return labels

    def swatch(self, index: int) -> Swatch | None:
        for s in self.swatches:
            if s.index == index:
                return s
        return None


def seed_for_path(path: Path | str) -> int:
    """A stable seed per file, so a render always yields the same swatch order."""
    digest = hashlib.sha256(str(path).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def assign(lab: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    """Nearest-centroid index per row, chunked to bound memory."""
    lab = np.asarray(lab, dtype=np.float32)
    cent = np.asarray(centroids, dtype=np.float32)
    cent_sq = (cent * cent).sum(axis=1)

    out = np.empty(lab.shape[0], dtype=np.int32)
    for start in range(0, lab.shape[0], _CHUNK):
        block = lab[start : start + _CHUNK]
        d2 = (block * block).sum(axis=1)[:, None] - 2.0 * (block @ cent.T) + cent_sq
        out[start : start + _CHUNK] = np.argmin(d2, axis=1)
    return out


def extract_palette(
    ps: PixelSet,
    *,
    k: int = KMEANS_DEFAULT_K,
    seed: int = 0,
    group_neutrals: bool = False,
    drop_below: float = 0.005,
    alpha_threshold: int = ALPHA_MIN_CLUSTER,
) -> ClusterResult:
    """Cluster the well-opaque pixels of ``ps`` into at most ``k`` swatches."""
    opaque = ps.opaque_mask(alpha_threshold)
    lab = ps.lab[opaque]
    if lab.shape[0] == 0:
        return ClusterResult(swatches=[], centroids=np.zeros((0, 3), np.float32), k=0, seed=seed)

    rng = np.random.default_rng(seed)

    neutral_lab = None
    if group_neutrals:
        chroma = np.hypot(lab[:, 1], lab[:, 2])
        neutral = chroma < 5.0
        if neutral.sum() > 0.01 * lab.shape[0] and (~neutral).sum() > k:
            neutral_lab = lab[neutral]
            lab_for_kmeans = lab[~neutral]
        else:
            lab_for_kmeans = lab
    else:
        lab_for_kmeans = lab

    k_eff = int(max(1, min(k, lab_for_kmeans.shape[0])))
    if lab_for_kmeans.shape[0] > KMEANS_SAMPLE:
        sample = lab_for_kmeans[
            rng.choice(lab_for_kmeans.shape[0], KMEANS_SAMPLE, replace=False)
        ]
    else:
        sample = lab_for_kmeans

    centroids, _ = kmeans2(
        sample.astype(np.float64),
        k_eff,
        minit="++",
        iter=KMEANS_ITERS,
        seed=seed,
        missing="warn",
    )
    centroids = np.asarray(centroids, dtype=np.float32)

    if neutral_lab is not None and neutral_lab.shape[0]:
        centroids = np.vstack([centroids, neutral_lab.mean(axis=0)[None, :]])

    labels = assign(lab, centroids)
    total = float(lab.shape[0])

    swatches: list[Swatch] = []
    for j in range(centroids.shape[0]):
        member = labels == j
        count = int(member.sum())
        if count == 0:
            continue
        fraction = count / total
        if fraction < drop_below and len(swatches) >= 3:
            continue

        members = lab[member]
        centroid = members.mean(axis=0).astype(np.float32)

        delta = members - centroid
        d = np.sqrt((delta * delta).sum(axis=1))
        medoid = members[int(np.argmin(d))].astype(np.float32)

        order = np.argsort(members[:, 0])
        midtone = members[order[len(order) // 2]].astype(np.float32)

        ab = members[:, 1:] - centroid[1:]
        ab_dist = np.sqrt((ab * ab).sum(axis=1))

        swatches.append(
            Swatch(
                index=j,
                centroid_lab=centroid,
                medoid_lab=medoid,
                midtone_lab=midtone,
                fraction=fraction,
                spread=float(np.sqrt((d * d).mean())),
                ab_spread_p85=float(np.percentile(ab_dist, 85)),
                pixel_count=count,
            )
        )

    swatches.sort(key=lambda s: s.fraction, reverse=True)
    return ClusterResult(
        swatches=swatches, centroids=centroids, k=centroids.shape[0], seed=seed
    )


def rule_from_swatch(swatch: Swatch, target_lab: np.ndarray):
    """Build a normal RecolorRule from a swatch.

    Deliberately not a special cluster-bound mask: there is exactly one masking
    code path, so the tolerance slider behaves identically whether the rule came
    from a swatch or the eyedropper.
    """
    from .recolor import MatchMode, RecolorRule

    return RecolorRule(
        source_lab=swatch.midtone_lab,
        target_lab=target_lab,
        mode=MatchMode.MATERIAL,
        tolerance=swatch.suggested_tolerance,
        cluster_id=swatch.index,
    )
