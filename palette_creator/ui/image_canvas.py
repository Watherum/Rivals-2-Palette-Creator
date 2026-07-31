"""Zoomable, pannable image view with a checkerboard and an eyedropper."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import numpy as np
from PIL import Image, ImageTk

CHECKER_LIGHT = (78, 78, 82)
CHECKER_DARK = (58, 58, 62)
CHECKER_SIZE = 12


def _checkerboard(h: int, w: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    on = ((yy // CHECKER_SIZE) + (xx // CHECKER_SIZE)) % 2 == 0
    board = np.empty((h, w, 3), np.uint8)
    board[on] = CHECKER_LIGHT
    board[~on] = CHECKER_DARK
    return board


def composite_over_checker(rgba: np.ndarray) -> np.ndarray:
    """Flatten RGBA onto a checkerboard so transparency is visible."""
    h, w = rgba.shape[:2]
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    rgb = rgba[..., :3].astype(np.float32)
    board = _checkerboard(h, w).astype(np.float32)
    return np.rint(rgb * a + board * (1.0 - a)).astype(np.uint8)


class ImageCanvas(tk.Frame):
    """Displays a numpy RGBA frame; reports image-space coords on click."""

    def __init__(
        self,
        master,
        *,
        on_pick: Callable[[int, int, str], None] | None = None,
        **kw,
    ):
        super().__init__(master, bd=0, highlightthickness=0, **kw)
        self.on_pick = on_pick

        self.canvas = tk.Canvas(
            self, highlightthickness=0, bd=0, bg="#1b1b1b", cursor="arrow"
        )
        self.canvas.pack(fill="both", expand=True)

        self._frame: np.ndarray | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._zoom = 1.0
        self._fit_zoom = 1.0
        self._offset = [0.0, 0.0]  # top-left of the image in canvas coords
        self._fit_pending = True
        self._eyedropper = False
        self._pan_anchor: tuple[float, float] | None = None

        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Control-Button-1>", lambda e: self._on_click(e, "target"))
        self.canvas.bind("<Shift-Button-1>", lambda e: self._on_click(e, "exact"))
        self.canvas.bind("<Button-3>", self._start_pan)
        self.canvas.bind("<B3-Motion>", self._do_pan)
        self.canvas.bind("<ButtonRelease-3>", self._end_pan)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    # -- content ------------------------------------------------------------

    def set_frame(self, rgba: np.ndarray, *, keep_view: bool = True) -> None:
        first = self._frame is None or self._frame.shape != rgba.shape
        self._frame = rgba
        if first or not keep_view:
            self._fit_pending = True
        self._render()

    def clear(self) -> None:
        self._frame = None
        self._photo = None
        self.canvas.delete("all")

    # -- view ---------------------------------------------------------------

    def fit(self) -> None:
        self._fit_pending = True
        self._render()

    def zoom_to(self, factor: float) -> None:
        self._zoom = float(np.clip(factor, 0.05, 16.0))
        self._fit_pending = False
        self._center_if_smaller()
        self._render()

    def set_eyedropper(self, active: bool) -> None:
        self._eyedropper = active
        self.canvas.configure(cursor="tcross" if active else "arrow")

    @property
    def zoom(self) -> float:
        return self._zoom

    def _compute_fit(self) -> None:
        if self._frame is None:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        ih, iw = self._frame.shape[:2]
        self._fit_zoom = min(cw / iw, ch / ih) * 0.97
        self._zoom = self._fit_zoom
        self._offset = [(cw - iw * self._zoom) / 2.0, (ch - ih * self._zoom) / 2.0]

    def _center_if_smaller(self) -> None:
        if self._frame is None:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        ih, iw = self._frame.shape[:2]
        dw, dh = iw * self._zoom, ih * self._zoom
        if dw <= cw:
            self._offset[0] = (cw - dw) / 2.0
        if dh <= ch:
            self._offset[1] = (ch - dh) / 2.0

    def _render(self) -> None:
        if self._frame is None:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        if cw <= 1 or ch <= 1:
            self.after(30, self._render)
            return

        if self._fit_pending:
            self._compute_fit()
            self._fit_pending = False

        ih, iw = self._frame.shape[:2]
        dw = max(int(round(iw * self._zoom)), 1)
        dh = max(int(round(ih * self._zoom)), 1)

        flat = composite_over_checker(self._frame)
        # Nearest above 1:1 so zooming in shows real pixels, not a blur.
        resample = (
            Image.Resampling.NEAREST if self._zoom >= 1.0 else Image.Resampling.LANCZOS
        )
        img = Image.fromarray(flat, "RGB").resize((dw, dh), resample)

        self._photo = ImageTk.PhotoImage(img)  # strong ref required
        self.canvas.delete("all")
        self.canvas.create_image(
            self._offset[0], self._offset[1], anchor="nw", image=self._photo
        )

    # -- coordinate mapping -------------------------------------------------

    def canvas_to_image(self, cx: float, cy: float) -> tuple[int, int] | None:
        if self._frame is None or self._zoom <= 0:
            return None
        ih, iw = self._frame.shape[:2]
        x = (cx - self._offset[0]) / self._zoom
        y = (cy - self._offset[1]) / self._zoom
        if not (0 <= x < iw and 0 <= y < ih):
            return None
        return int(x), int(y)

    # -- events -------------------------------------------------------------

    def _on_resize(self, _event) -> None:
        if self._frame is not None:
            self._fit_pending = True
            self._render()

    def _on_click(self, event, kind: str = "source") -> None:
        if not self._eyedropper or self.on_pick is None:
            return
        point = self.canvas_to_image(event.x, event.y)
        if point is None:
            return
        self.on_pick(point[0], point[1], kind)

    def _start_pan(self, event) -> None:
        self._pan_anchor = (event.x, event.y)
        self.canvas.configure(cursor="fleur")

    def _do_pan(self, event) -> None:
        if self._pan_anchor is None:
            return
        dx = event.x - self._pan_anchor[0]
        dy = event.y - self._pan_anchor[1]
        self._pan_anchor = (event.x, event.y)
        self._offset[0] += dx
        self._offset[1] += dy
        self._render()

    def _end_pan(self, _event) -> None:
        self._pan_anchor = None
        self.canvas.configure(cursor="tcross" if self._eyedropper else "arrow")

    def _on_wheel(self, event) -> None:
        if self._frame is None:
            return
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        new_zoom = float(np.clip(self._zoom * factor, 0.05, 16.0))
        if abs(new_zoom - self._zoom) < 1e-6:
            return

        # Keep the point under the cursor pinned while zooming.
        ix = (event.x - self._offset[0]) / self._zoom
        iy = (event.y - self._offset[1]) / self._zoom
        self._zoom = new_zoom
        self._offset[0] = event.x - ix * self._zoom
        self._offset[1] = event.y - iy * self._zoom

        self._fit_pending = False
        self._center_if_smaller()
        self._render()
