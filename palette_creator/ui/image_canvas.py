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
        on_region: Callable[[str, list], None] | None = None,
        **kw,
    ):
        super().__init__(master, bd=0, highlightthickness=0, **kw)
        self.on_pick = on_pick
        self.on_region = on_region

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

        self._draw_mode: str | None = None  # None | "rect" | "lasso"
        self._drawing: list[tuple[float, float]] | None = None  # image coords
        self._region: tuple[str, list] | None = None  # committed, normalized

        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Button-1>", self._on_button1)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
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
        self._apply_cursor()

    # -- region drawing -----------------------------------------------------

    def set_draw_mode(self, mode: str | None) -> None:
        """``None``, ``"rect"`` or ``"lasso"``. Drawing preempts the eyedropper."""
        self._draw_mode = mode
        self._drawing = None
        self._apply_cursor()
        self._draw_overlay()

    @property
    def draw_mode(self) -> str | None:
        return self._draw_mode

    def set_region(self, region) -> None:
        """Show ``region`` (a recolor.Region) as the committed outline."""
        if region is None or not region.is_valid():
            self._region = None
        else:
            self._region = (region.kind, list(region.points))
        self._draw_overlay()

    def _apply_cursor(self) -> None:
        if self._draw_mode is not None:
            cursor = "crosshair"
        elif self._eyedropper:
            cursor = "tcross"
        else:
            cursor = "arrow"
        self.canvas.configure(cursor=cursor)

    def _image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return (self._offset[0] + x * self._zoom, self._offset[1] + y * self._zoom)

    def _draw_overlay(self) -> None:
        """Redraw the region outline as canvas items.

        Kept as canvas items rather than painted into the numpy frame so that
        dragging an outline never triggers a re-render of the image itself.
        """
        self.canvas.delete("region")
        if self._frame is None:
            return
        ih, iw = self._frame.shape[:2]

        if self._drawing:
            kind, pts = self._draw_mode, self._drawing
        elif self._region:
            kind, norm = self._region
            pts = [(u * (iw - 1), v * (ih - 1)) for u, v in norm]
        else:
            return

        canvas_pts = [self._image_to_canvas(x, y) for x, y in pts]
        if kind == "rect" and len(canvas_pts) == 2:
            (x0, y0), (x1, y1) = canvas_pts
            shape = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        elif kind == "lasso" and len(canvas_pts) >= 2:
            shape = canvas_pts
        else:
            return

        flat = [c for point in shape for c in point]
        # Black under white so the outline reads against any color beneath it.
        self.canvas.create_polygon(
            flat, outline="#000000", fill="", width=3, tags="region"
        )
        self.canvas.create_polygon(
            flat, outline="#ffffff", fill="", width=1, dash=(5, 4), tags="region"
        )

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
        self._draw_overlay()

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

    def _canvas_to_image_clamped(self, cx: float, cy: float) -> tuple[float, float] | None:
        """Like :meth:`canvas_to_image` but clamps instead of rejecting.

        Dragging a selection past the edge of the sprite should pin to the
        border, not abort the gesture.
        """
        if self._frame is None or self._zoom <= 0:
            return None
        ih, iw = self._frame.shape[:2]
        x = (cx - self._offset[0]) / self._zoom
        y = (cy - self._offset[1]) / self._zoom
        return (float(np.clip(x, 0, iw - 1)), float(np.clip(y, 0, ih - 1)))

    # -- events -------------------------------------------------------------

    def _on_resize(self, _event) -> None:
        if self._frame is not None:
            self._fit_pending = True
            self._render()

    def _on_button1(self, event) -> None:
        if self._draw_mode is not None:
            point = self._canvas_to_image_clamped(event.x, event.y)
            if point is None:
                return
            self._drawing = [point, point] if self._draw_mode == "rect" else [point]
            self._draw_overlay()
            return
        self._on_click(event)

    def _on_drag(self, event) -> None:
        if self._draw_mode is None or self._drawing is None:
            return
        point = self._canvas_to_image_clamped(event.x, event.y)
        if point is None:
            return
        if self._draw_mode == "rect":
            self._drawing[1] = point
        else:
            # Thin the trail: raw motion events give sub-pixel steps that bloat
            # the polygon without changing its shape.
            last = self._drawing[-1]
            if abs(point[0] - last[0]) + abs(point[1] - last[1]) >= 1.5:
                self._drawing.append(point)
        self._draw_overlay()

    def _on_release(self, _event) -> None:
        if self._draw_mode is None or self._drawing is None:
            return
        pts, kind = self._drawing, self._draw_mode
        self._drawing = None

        if self._frame is None:
            return
        ih, iw = self._frame.shape[:2]

        too_small = (
            kind == "rect"
            and (
                abs(pts[1][0] - pts[0][0]) < 2 or abs(pts[1][1] - pts[0][1]) < 2
            )
        ) or (kind == "lasso" and len(pts) < 3)
        if too_small:
            # A stray click rather than a drag; leave any existing region alone.
            self._draw_overlay()
            return

        norm = [(x / max(iw - 1, 1), y / max(ih - 1, 1)) for x, y in pts]
        self._region = (kind, norm)
        self._draw_overlay()
        if self.on_region is not None:
            self.on_region(kind, norm)

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
