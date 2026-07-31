"""An HSV color wheel plus the combined target-color editor.

Written here rather than depending on CTkColorPicker: that package is built
around a modal dialog returning one final value, whereas live preview needs a
continuous callback while the cursor is dragged.
"""

from __future__ import annotations

import colorsys
import tkinter as tk
from tkinter import colorchooser
from typing import Callable

import customtkinter as ctk
import numpy as np
from PIL import Image, ImageTk

from ..colorspace import hex_to_rgb, rgb_to_hex
from .widgets import FONT_MONO, FONT_SMALL, LabeledSlider


class HSVWheel(ctk.CTkFrame):
    """Hue around the circumference, saturation along the radius."""

    def __init__(
        self,
        master,
        *,
        size: int = 190,
        on_change: Callable[[tuple[int, int, int]], None] | None = None,
        **kw,
    ):
        super().__init__(master, fg_color="transparent", **kw)
        self._size = size
        self._on_change = on_change
        self._h = 0.0
        self._s = 0.0
        self._v = 1.0
        self._photo: ImageTk.PhotoImage | None = None

        self._canvas = tk.Canvas(
            self,
            width=size,
            height=size,
            highlightthickness=0,
            bd=0,
            bg="#242424",
            cursor="crosshair",
        )
        self._canvas.pack()

        self._base = self._build_wheel(size)
        self._redraw()

        for seq in ("<Button-1>", "<B1-Motion>"):
            self._canvas.bind(seq, self._on_drag)

    @staticmethod
    def _build_wheel(size: int) -> np.ndarray:
        """The V=1 wheel as RGBA, with a 1px antialiased rim."""
        c = (size - 1) / 2.0
        r = max(c, 1.0)
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        x = (xx - c) / r
        y = (c - yy) / r

        rad = np.hypot(x, y)
        ang = np.arctan2(y, x)

        hsv = np.stack(
            [
                (np.degrees(ang) % 360.0) / 360.0,
                np.clip(rad, 0.0, 1.0),
                np.ones_like(rad),
            ],
            axis=-1,
        )
        from skimage import color as skcolor

        rgb = (skcolor.hsv2rgb(hsv.astype(np.float64)) * 255).astype(np.uint8)
        alpha = np.clip((1.0 - rad) * r, 0.0, 1.0)
        return np.dstack([rgb, (alpha * 255).astype(np.uint8)])

    def _redraw(self) -> None:
        img = self._base.copy()
        if self._v < 1.0:
            img[..., :3] = (img[..., :3].astype(np.float32) * self._v).astype(np.uint8)

        # Keep a strong reference: Tk garbage-collects PhotoImage otherwise and
        # the canvas silently goes blank.
        self._photo = ImageTk.PhotoImage(Image.fromarray(img, "RGBA"))
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._draw_cursor()

    def _draw_cursor(self) -> None:
        c = (self._size - 1) / 2.0
        r = max(c, 1.0)
        angle = self._h * 2.0 * np.pi
        px = c + np.cos(angle) * self._s * r
        py = c - np.sin(angle) * self._s * r

        # Two rings so the marker reads against any hue underneath it.
        self._canvas.create_oval(
            px - 7, py - 7, px + 7, py + 7, outline="#000000", width=3
        )
        self._canvas.create_oval(
            px - 7, py - 7, px + 7, py + 7, outline="#ffffff", width=1.5
        )

    def _on_drag(self, event) -> None:
        c = (self._size - 1) / 2.0
        r = max(c, 1.0)
        x = (event.x - c) / r
        y = (c - event.y) / r

        rad = float(np.hypot(x, y))
        # Clamp instead of ignoring: dragging past the rim should hold at full
        # saturation rather than making the marker jump.
        self._s = float(min(rad, 1.0))
        self._h = float((np.degrees(np.arctan2(y, x)) % 360.0) / 360.0)

        self._redraw()
        if self._on_change is not None:
            self._on_change(self.get_rgb())

    def set_value(self, v: float) -> None:
        self._v = float(np.clip(v, 0.0, 1.0))
        self._redraw()

    def get_rgb(self) -> tuple[int, int, int]:
        r, g, b = colorsys.hsv_to_rgb(self._h, self._s, self._v)
        return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))

    def set_rgb(self, rgb) -> None:
        r, g, b = (float(v) / 255.0 for v in rgb[:3])
        self._h, self._s, self._v = colorsys.rgb_to_hsv(r, g, b)
        self._redraw()

    @property
    def value(self) -> float:
        return self._v


class ColorEditor(ctk.CTkFrame):
    """Wheel + value slider + hex field + RGB fields + the system picker.

    All four inputs funnel through :meth:`set_color`, guarded by an updating
    flag -- without it, each control's callback re-fires the others and the
    bindings loop forever.
    """

    def __init__(
        self,
        master,
        *,
        on_change: Callable[[tuple[int, int, int]], None] | None = None,
        wheel_size: int = 190,
        **kw,
    ):
        super().__init__(master, **kw)
        self._on_change = on_change
        self._updating = False
        self._rgb = (255, 255, 255)

        self.grid_columnconfigure(0, weight=1)

        self._wheel = HSVWheel(
            self, size=wheel_size, on_change=lambda rgb: self.set_color(rgb, "wheel")
        )
        self._wheel.grid(row=0, column=0, pady=(6, 2))

        self._value = LabeledSlider(
            self,
            "Brightness",
            from_=0.0,
            to=1.0,
            value=1.0,
            fmt="{:.2f}",
            command=self._on_value,
        )
        self._value.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 6))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=8)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(row, text="Hex", font=FONT_SMALL, width=30, anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        self._hex = ctk.CTkEntry(row, font=FONT_MONO, height=28)
        self._hex.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self._hex.bind("<Return>", self._on_hex)
        self._hex.bind("<FocusOut>", self._on_hex)

        rgb_row = ctk.CTkFrame(self, fg_color="transparent")
        rgb_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(6, 0))
        self._rgb_entries = []
        for i, label in enumerate(("R", "G", "B")):
            rgb_row.grid_columnconfigure(i * 2 + 1, weight=1)
            ctk.CTkLabel(rgb_row, text=label, font=FONT_SMALL, width=14).grid(
                row=0, column=i * 2, padx=(0 if i == 0 else 6, 2)
            )
            entry = ctk.CTkEntry(rgb_row, font=FONT_MONO, height=28, width=48)
            entry.grid(row=0, column=i * 2 + 1, sticky="ew")
            entry.bind("<Return>", self._on_rgb_entry)
            entry.bind("<FocusOut>", self._on_rgb_entry)
            self._rgb_entries.append(entry)

        ctk.CTkButton(
            self,
            text="System picker...",
            height=28,
            command=self._open_system_picker,
        ).grid(row=4, column=0, sticky="ew", padx=8, pady=(8, 8))

        self.set_color((255, 255, 255), "init")

    # -- the single funnel --------------------------------------------------

    def set_color(self, rgb, source: str = "external") -> None:
        if self._updating:
            return
        self._updating = True
        try:
            rgb = tuple(int(np.clip(round(float(v)), 0, 255)) for v in rgb[:3])
            self._rgb = rgb

            if source != "wheel":
                self._wheel.set_rgb(rgb)
                self._value.set(self._wheel.value)
            if source != "hex":
                self._hex.delete(0, "end")
                self._hex.insert(0, rgb_to_hex(rgb))
            if source != "rgb":
                for entry, channel in zip(self._rgb_entries, rgb):
                    entry.delete(0, "end")
                    entry.insert(0, str(channel))
        finally:
            self._updating = False

        if source not in ("init", "external") and self._on_change is not None:
            self._on_change(rgb)

    def get_color(self) -> tuple[int, int, int]:
        return self._rgb

    # -- input handlers -----------------------------------------------------

    def _on_value(self, value: float) -> None:
        if self._updating:
            return
        self._wheel.set_value(value)
        self.set_color(self._wheel.get_rgb(), "wheel")

    def _on_hex(self, _event=None) -> None:
        if self._updating:
            return
        try:
            rgb = hex_to_rgb(self._hex.get())
        except ValueError:
            self.set_color(self._rgb, "external")  # revert an invalid entry
            return
        self.set_color(rgb, "hex")

    def _on_rgb_entry(self, _event=None) -> None:
        if self._updating:
            return
        try:
            rgb = [int(float(e.get())) for e in self._rgb_entries]
        except ValueError:
            self.set_color(self._rgb, "external")
            return
        self.set_color(rgb, "rgb")

    def _open_system_picker(self) -> None:
        current = rgb_to_hex(self._rgb)
        result = colorchooser.askcolor(color=current, parent=self)
        if result and result[0]:
            self.set_color(result[0], "picker")
