"""Zoomable, pannable image view with a checkerboard and an eyedropper."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import numpy as np
from PIL import Image, ImageTk

CHECKER_LIGHT = (78, 78, 82)
CHECKER_DARK = (58, 58, 62)
CHECKER_SIZE = 12


def _checkerboard(h: int, w: int, y0: int = 0, x0: int = 0) -> np.ndarray:
    """``h`` x ``w`` of checkerboard, phased as if cut from a board whose
    origin is at image pixel (0, 0) -- so a cropped tile lines up with its
    neighbours instead of restarting the pattern."""
    yy, xx = np.mgrid[y0 : y0 + h, x0 : x0 + w]
    on = ((yy // CHECKER_SIZE) + (xx // CHECKER_SIZE)) % 2 == 0
    board = np.empty((h, w, 3), np.uint8)
    board[on] = CHECKER_LIGHT
    board[~on] = CHECKER_DARK
    return board


def composite_over_checker(
    rgba: np.ndarray, y0: int = 0, x0: int = 0
) -> np.ndarray:
    """Flatten RGBA onto a checkerboard so transparency is visible.

    ``y0``/``x0`` are the crop's origin in the full image, and only shift the
    checker's phase.
    """
    h, w = rgba.shape[:2]
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    rgb = rgba[..., :3].astype(np.float32)
    board = _checkerboard(h, w, y0, x0).astype(np.float32)
    return np.rint(rgb * a + board * (1.0 - a)).astype(np.uint8)


class ImageCanvas(tk.Frame):
    """Displays a numpy RGBA frame; reports image-space coords on click."""

    def __init__(
        self,
        master,
        *,
        on_pick: Callable[[int, int, str], None] | None = None,
        on_region: Callable[[str, list], None] | None = None,
        on_view: Callable[[int, int, float], None] | None = None,
        **kw,
    ):
        super().__init__(master, bd=0, highlightthickness=0, **kw)
        self.on_pick = on_pick
        self.on_region = on_region
        self.on_view = on_view
        """Called with (width, height, zoom) of the image as displayed, after
        every render -- zoom, fit, resize or a new frame."""

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

        # None, or a recolor.Region kind: "rect", "lasso" or "stroke". The mode
        # name IS the kind -- _on_release hands it straight to the callback as
        # the region's kind, so the two must not drift apart.
        self._draw_mode: str | None = None
        self._drawing: list[tuple[float, float]] | None = None  # image coords
        self._region: tuple[str, list, float] | None = None  # committed, normalized
        self._pen_width = 0.0
        """Pen thickness for the stroke being drawn, as a fraction of the image
        width. A committed stroke carries its own instead, so changing the pen
        size never resizes a stroke already drawn."""
        self._pending_strokes: list[tuple[list, float]] = []
        """Strokes drawn but not yet given a color, other than the selected
        one. Marked faintly so a run of strokes stays findable."""

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
        old = self._frame
        self._frame = rgba

        if old is None or not keep_view:
            self._fit_pending = True
        elif old.shape[:2] != rgba.shape[:2]:
            # The same render at a different resolution -- the proxy standing in
            # mid-edit, then the full-resolution pass replacing it. Rescale the
            # zoom so what is on screen stays exactly where it was: refitting
            # here would throw away the user's zoom and pan on every edit.
            self._zoom *= old.shape[1] / rgba.shape[1]
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
        """Zoom about the middle of the viewport.

        Which is what a zoom control away from the image should do: the wheel
        has a cursor to pin, this does not, and pinning the image's top-left
        instead would throw the subject off the bottom-right as you zoom in.
        """
        new = float(np.clip(factor, 0.05, 16.0))
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        if self._frame is not None and self._zoom > 0:
            mx = (cw / 2.0 - self._offset[0]) / self._zoom
            my = (ch / 2.0 - self._offset[1]) / self._zoom
            self._offset[0] = cw / 2.0 - mx * new
            self._offset[1] = ch / 2.0 - my * new

        self._zoom = new
        self._fit_pending = False
        self._center_if_smaller()
        self._render()

    def set_eyedropper(self, active: bool) -> None:
        self._eyedropper = active
        self._apply_cursor()

    # -- region drawing -----------------------------------------------------

    def set_draw_mode(self, mode: str | None) -> None:
        """``None``, ``"rect"``, ``"lasso"`` or ``"stroke"``.

        Drawing preempts the eyedropper.
        """
        self._draw_mode = mode
        self._drawing = None
        self._apply_cursor()
        self._draw_overlay()

    @property
    def draw_mode(self) -> str | None:
        return self._draw_mode

    def set_pen_width(self, width: float) -> None:
        """Thickness for the *next* stroke, as a fraction of the image width."""
        self._pen_width = max(float(width), 0.0)
        if self._draw_mode == "stroke":
            self._draw_overlay()

    def set_pending_strokes(self, regions) -> None:
        """Mark strokes that have been drawn but have no color yet.

        Anything not on this list and not the selected region is left alone: a
        stroke that has been colored is visible in the render already, and a
        marker over it would only hide the thing you drew it for.
        """
        self._pending_strokes = [
            (list(r.points), r.width)
            for r in regions
            if r is not None and r.kind == "stroke" and r.is_valid()
        ]
        self._draw_overlay()

    def set_region(self, region) -> None:
        """Show ``region`` (a recolor.Region) as the committed outline."""
        if region is None or not region.is_valid():
            self._region = None
        else:
            self._region = (region.kind, list(region.points), region.width)
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

        def to_canvas(norm):
            return [
                self._image_to_canvas(u * (iw - 1), v * (ih - 1)) for u, v in norm
            ]

        # Drawn first, so the selected stroke below sits on top of them.
        for norm, width in self._pending_strokes:
            self._draw_stroke_overlay(
                to_canvas(norm), width * max(iw - 1, 1) * self._zoom, style="faint"
            )

        if self._drawing:
            kind, pts, width = self._draw_mode, self._drawing, self._pen_width
            canvas_pts = [self._image_to_canvas(x, y) for x, y in pts]
        elif self._region:
            kind, norm, width = self._region
            canvas_pts = to_canvas(norm)
        else:
            return

        if kind == "stroke":
            self._draw_stroke_overlay(
                canvas_pts,
                width * max(iw - 1, 1) * self._zoom,
                style="live" if self._drawing else "marker",
            )
            return

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

    STROKE_COLOR = "#ff00c8"

    def _draw_stroke_overlay(self, pts: list, width: float, *, style: str) -> None:
        """Show a pen path as the band it will actually paint.

        An outline would be the wrong feedback here: with the pen, the width is
        the whole point, so the band itself is drawn.

        ``live`` is solid -- the stroke is under the cursor and you are watching
        where it lands. Everything else is stippled at the same weight, so the
        recolor underneath stays readable and, more to the point, so an
        uncolored stroke is *findable*: it paints nothing into the render, so
        without a mark it is simply not on screen. Which of them is selected is
        said with a dashed centerline rather than by fading the others out --
        the same black-under-white outline the Box and Lasso regions use, and
        a fainter stipple turned out to be unreadable against the checkerboard.
        """
        width = max(width, 1.0)
        kw = {} if style == "live" else {"stipple": "gray50"}

        if len(pts) == 1:
            (x, y), r = pts[0], width / 2.0
            box = (x - r, y - r, x + r, y + r)
            self.canvas.create_oval(
                *box, outline="", fill=self.STROKE_COLOR, tags="region", **kw
            )
            if style == "marker":
                self.canvas.create_oval(
                    *box, outline="#ffffff", fill="", width=1, dash=(4, 3),
                    tags="region",
                )
            return

        flat = [c for point in pts for c in point]
        self.canvas.create_line(
            flat,
            fill=self.STROKE_COLOR,
            width=width,
            capstyle="round",
            joinstyle="round",
            tags="region",
            **kw,
        )
        if style == "marker":
            self.canvas.create_line(
                flat, fill="#ffffff", width=1, dash=(5, 4), tags="region"
            )

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def frame_width(self) -> int | None:
        """Width of the frame on display, which is the proxy's mid-edit."""
        return None if self._frame is None else int(self._frame.shape[1])

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
        full_w = max(int(round(iw * self._zoom)), 1)
        full_h = max(int(round(ih * self._zoom)), 1)

        # Only the part of the image inside the viewport is scaled. Scaling the
        # whole thing costs with the square of the zoom -- 2.5 s and ~800 MB at
        # 16x on a 1024px render -- while the visible area never exceeds the
        # canvas, so this keeps a render flat in cost at any zoom.
        x0 = max(int(np.floor(-self._offset[0] / self._zoom)), 0)
        y0 = max(int(np.floor(-self._offset[1] / self._zoom)), 0)
        x1 = min(int(np.ceil((cw - self._offset[0]) / self._zoom)) + 1, iw)
        y1 = min(int(np.ceil((ch - self._offset[1]) / self._zoom)) + 1, ih)

        self.canvas.delete("all")
        if x1 <= x0 or y1 <= y0:  # panned entirely off screen
            self._photo = None
            self._draw_overlay()
            if self.on_view is not None:
                self.on_view(full_w, full_h, self._zoom)
            return

        crop = self._frame[y0:y1, x0:x1]
        dw = max(int(round((x1 - x0) * self._zoom)), 1)
        dh = max(int(round((y1 - y0) * self._zoom)), 1)

        flat = composite_over_checker(crop, y0, x0)
        # Nearest above 1:1 so zooming in shows real pixels, not a blur.
        resample = (
            Image.Resampling.NEAREST if self._zoom >= 1.0 else Image.Resampling.LANCZOS
        )
        img = Image.fromarray(flat, "RGB").resize((dw, dh), resample)

        self._photo = ImageTk.PhotoImage(img)  # strong ref required
        self.canvas.create_image(
            self._offset[0] + x0 * self._zoom,
            self._offset[1] + y0 * self._zoom,
            anchor="nw",
            image=self._photo,
        )
        self._draw_overlay()

        if self.on_view is not None:
            # The whole image's size, not the visible crop's: callers ask how
            # big the render is on screen, not how much of it fits.
            self.on_view(full_w, full_h, self._zoom)

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
            # the polygon without changing its shape. Safe for the pen too --
            # consecutive points are joined by a line, so thinning never opens
            # a gap in the band, it only drops vertices that were on it anyway.
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

        # The pen is exempt: a single click is a dab, not a stray gesture.
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
        self._region = (kind, norm, self._pen_width if kind == "stroke" else 0.0)
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
