"""The Reference tab: load a second render purely to read colors off it.

Nothing here edits pixels. It exists so a palette can be built against some
other skin's colors -- or against art from outside the library entirely --
without disturbing whatever the Recolor tab has loaded. It therefore keeps its
own Library, its own image cache and its own selection, and shares no state with
Recolor beyond the modules they both call.

The output is a hex code on the clipboard. Picking a color never copies it: the
click chooses, the Copy button commits, and the chosen color sits visible in
between so a misclick costs nothing.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import numpy as np

from ..colorspace import hex_to_rgb, rgb_to_hex
from ..config import ALPHA_MIN_CLUSTER, KMEANS_DEFAULT_K, SOURCE_DIR
from ..imagectx import ContextCache
from ..library import Library
from ..palette_extract import Swatch, extract_palette, seed_for_path
from .image_canvas import ImageCanvas
from .swatch_grid import SwatchStrip
from .widgets import (
    FONT_MONO,
    FONT_SMALL,
    LabeledSlider,
    Swatch as ChipSwatch,
    Tooltip,
    section_label,
)

ZOOM_LOG_MIN = -4.0
ZOOM_LOG_MAX = 4.0
"""Zoom slider range in doublings, matching the Recolor tab's."""

RECENT_LIMIT = 10
"""How many previous picks to keep. Long enough to hold a material's worth of
shades, short enough that the list never needs its own scrollbar to be useful."""

HEX_FONT = ("Consolas", 20, "bold")


class ReferenceTab(ctk.CTkFrame):
    def __init__(self, master, app, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.app = app

        self.library = Library()
        self.cache = ContextCache()
        self.ctx = None

        self._pick: str | None = None
        self._recent: list[str] = []
        self._recent_rows: list[ctk.CTkFrame] = []
        self._suspend_zoom_sync = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_left()
        self._build_center()
        self._build_right()

        self.after(120, self.refresh_library)

    # ------------------------------------------------------------------ left

    def _build_left(self) -> None:
        panel = ctk.CTkFrame(self, width=300)
        panel.grid(row=0, column=0, sticky="nsw", padx=(8, 4), pady=8)
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        self._build_source_picker(panel).grid(row=0, column=0, sticky="ew")

        body = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 6))
        body.grid_columnconfigure(0, weight=1)

        section_label(body, "Extracted palette").grid(
            row=0, column=0, sticky="ew", padx=6, pady=(4, 4)
        )

        controls = ctk.CTkFrame(body, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=6)
        controls.grid_columnconfigure(0, weight=1)

        self._k_slider = LabeledSlider(
            controls,
            "Colors (k)",
            from_=4,
            to=24,
            value=KMEANS_DEFAULT_K,
            steps=20,
            fmt="{:.0f}",
            command=self._on_k_change,
        )
        self._k_slider.grid(row=0, column=0, sticky="ew")

        self._group_neutrals = ctk.CTkCheckBox(
            controls,
            text="Group near-neutrals",
            font=FONT_SMALL,
            command=self._extract_palette,
        )
        self._group_neutrals.grid(row=1, column=0, sticky="w", pady=(6, 6))
        Tooltip(
            self._group_neutrals,
            "Collapse all the near-gray shadow tones into one swatch, so k is "
            "not spent on shades of gray.",
        )

        self._swatches = SwatchStrip(
            body,
            on_select=self._on_swatch_click,
            tooltip_suffix="Click to pick this color.",
            fg_color="transparent",
        )
        self._swatches.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 8))
        self._swatches.show_message("Load a render to extract its palette.")

    def _build_source_picker(self, parent) -> ctk.CTkFrame:
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid_columnconfigure(0, weight=1)

        section_label(box, "Reference render").grid(
            row=0, column=0, sticky="ew", padx=10, pady=(10, 6)
        )

        self._character = ctk.CTkOptionMenu(
            box, values=["(scanning...)"], command=self._on_character
        )
        self._character.grid(row=1, column=0, sticky="ew", padx=10, pady=3)

        self._skin = ctk.CTkOptionMenu(box, values=["-"], command=self._on_skin)
        self._skin.grid(row=2, column=0, sticky="ew", padx=10, pady=3)

        self._palette = ctk.CTkOptionMenu(box, values=["-"], command=self._on_palette)
        self._palette.grid(row=3, column=0, sticky="ew", padx=10, pady=3)

        buttons = ctk.CTkFrame(box, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=10, pady=(6, 0))
        buttons.grid_columnconfigure((0, 1), weight=1)

        open_btn = ctk.CTkButton(
            buttons, text="Open image...", height=28, command=self._open_file
        )
        open_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        Tooltip(
            open_btn,
            "Load any PNG to sample from -- concept art, a screenshot, another "
            "game's sprite. It does not have to live in Resources/.",
        )

        refresh_btn = ctk.CTkButton(
            buttons, text="Refresh", height=28, command=self.refresh_library
        )
        refresh_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        Tooltip(refresh_btn, "Rescan the library after downloading more renders.")

        self._file_label = ctk.CTkLabel(
            box, text="", font=FONT_MONO, anchor="w", wraplength=268, justify="left"
        )
        self._file_label.grid(row=5, column=0, sticky="ew", padx=10, pady=(8, 0))

        self._lib_label = ctk.CTkLabel(
            box, text="", font=FONT_SMALL, anchor="w", text_color="#999",
            wraplength=268, justify="left",
        )
        self._lib_label.grid(row=6, column=0, sticky="ew", padx=10, pady=(2, 8))

        return box

    # ---------------------------------------------------------------- center

    def _build_center(self) -> None:
        panel = ctk.CTkFrame(self)
        panel.grid(row=0, column=1, sticky="nsew", padx=4, pady=8)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(panel, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        bar.grid_columnconfigure(0, weight=1)

        zoom_box = ctk.CTkFrame(bar, fg_color="transparent")
        zoom_box.grid(row=0, column=0, sticky="e")

        # Fit sits with the zoom controls because that is what it is -- it
        # scales the render to the viewport, the far end of the same slider.
        fit_btn = ctk.CTkButton(
            zoom_box, text="Fit", width=48, height=28, command=self._fit
        )
        fit_btn.grid(row=0, column=0, padx=(0, 10))
        Tooltip(fit_btn, "Zoom so the whole render fits the canvas.")

        ctk.CTkLabel(zoom_box, text="Zoom", font=FONT_SMALL, anchor="e").grid(
            row=0, column=1, padx=(0, 6)
        )

        self._zoom_slider = ctk.CTkSlider(
            zoom_box,
            from_=ZOOM_LOG_MIN,
            to=ZOOM_LOG_MAX,
            width=104,
            height=16,
            command=self._on_zoom_slider,
        )
        self._zoom_slider.set(0.0)
        self._zoom_slider.grid(row=0, column=2)
        Tooltip(
            self._zoom_slider,
            "Zoom the canvas. The wheel over the image does the same thing and "
            "zooms about the cursor; right-drag pans.",
        )

        self._zoom_label = ctk.CTkLabel(
            zoom_box, text="", font=FONT_MONO, width=52, anchor="e"
        )
        self._zoom_label.grid(row=0, column=3, padx=(6, 6))

        # Always armed. The canvas has nothing else to do with a left click
        # here -- no rules, no regions -- so a toggle would only ever be a
        # switch you had to remember to turn on.
        self.canvas = ImageCanvas(
            panel, on_pick=self._on_canvas_pick, on_view=self._on_canvas_view
        )
        self.canvas.set_eyedropper(True)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        self._status = ctk.CTkLabel(panel, text="", font=FONT_SMALL, anchor="w")
        self._status.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

    # ----------------------------------------------------------------- right

    def _build_right(self) -> None:
        panel = ctk.CTkFrame(self, width=272)
        panel.grid(row=0, column=2, sticky="nse", padx=(4, 8), pady=8)
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(5, weight=1)

        section_label(panel, "Picked color").grid(
            row=0, column=0, sticky="ew", padx=10, pady=(10, 6)
        )

        readout = ctk.CTkFrame(panel, fg_color="transparent")
        readout.grid(row=1, column=0, sticky="ew", padx=10)
        readout.grid_columnconfigure(1, weight=1)

        self._pick_chip = ChipSwatch(readout, size=76, color="#2b2b2b")
        self._pick_chip.grid(row=0, column=0, rowspan=2, sticky="w")

        self._hex_label = ctk.CTkLabel(
            readout, text="--------", font=HEX_FONT, anchor="w"
        )
        self._hex_label.grid(row=0, column=1, sticky="sw", padx=(12, 0))

        self._rgb_label = ctk.CTkLabel(
            readout, text="", font=FONT_MONO, anchor="w", text_color="#999"
        )
        self._rgb_label.grid(row=1, column=1, sticky="nw", padx=(12, 0), pady=(2, 0))

        self._copy_btn = ctk.CTkButton(
            panel, text="Copy hex", height=32, state="disabled",
            command=self._copy_current,
        )
        self._copy_btn.grid(row=2, column=0, sticky="ew", padx=10, pady=(10, 4))
        Tooltip(
            self._copy_btn,
            "Copy the picked hex to the clipboard.\n\n"
            "Paste it while this app is still running: Tk hands the clipboard "
            "back to Windows on exit, so the copy does not outlive the window.",
        )

        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.grid(row=3, column=0, sticky="ew", padx=10, pady=(12, 2))
        header.grid_columnconfigure(0, weight=1)
        section_label(header, "Recent").grid(row=0, column=0, sticky="w")
        self._clear_recent_btn = ctk.CTkButton(
            header, text="Clear", width=52, height=24, state="disabled",
            fg_color="#5a3a3a", hover_color="#7a4a4a", command=self._clear_recent,
        )
        self._clear_recent_btn.grid(row=0, column=1, sticky="e")

        self._recent_hint = ctk.CTkLabel(
            panel,
            text="Click the image, or a palette swatch,\nto pick a color.",
            font=FONT_SMALL,
            text_color="#999",
            justify="left",
        )
        self._recent_hint.grid(row=4, column=0, sticky="w", padx=16, pady=(4, 0))

        self._recent_list = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        self._recent_list.grid(row=5, column=0, sticky="nsew", padx=6, pady=(4, 8))
        self._recent_list.grid_columnconfigure(0, weight=1)

    # --------------------------------------------------------------- library

    def refresh_library(self) -> None:
        self.library.scan()
        self._lib_label.configure(text=self.library.status_line())

        characters = self.library.characters()
        if not characters:
            self._character.configure(values=["(no renders found)"])
            self._character.set("(no renders found)")
            self._set_status(
                "No renders found. Use the Downloads tab to fetch them, or "
                "Open image... to load one from anywhere."
            )
            return

        current = self._character.get()
        self._character.configure(values=characters)
        if current not in characters:
            current = characters[0]
        self._character.set(current)
        self._on_character(current)

    def _on_character(self, character: str) -> None:
        skins = self.library.skins(character)
        if not skins:
            return
        self._skin.configure(values=skins)
        current = self._skin.get()
        if current not in skins:
            current = skins[0]
        self._skin.set(current)
        self._on_skin(current)

    def _on_skin(self, skin: str) -> None:
        palettes = self.library.palettes(self._character.get(), skin)
        if not palettes:
            return
        self._palette.configure(values=palettes)
        current = self._palette.get()
        if current not in palettes:
            current = palettes[0]
        self._palette.set(current)
        self._on_palette(current)

    def _on_palette(self, palette: str) -> None:
        render_file = self.library.get(
            self._character.get(), self._skin.get(), palette
        )
        if render_file is not None:
            self._load_render(render_file.path)

    def _open_file(self) -> None:
        initial = SOURCE_DIR if SOURCE_DIR.is_dir() else Path.home()
        path = filedialog.askopenfilename(
            title="Open a reference image",
            initialdir=str(initial),
            filetypes=[("Images", "*.png *.PNG"), ("All files", "*.*")],
        )
        if path:
            self._load_render(Path(path))

    def _load_render(self, path: Path) -> None:
        self._set_status(f"Loading {path.name}...")
        self.update_idletasks()
        try:
            self.ctx = self.cache.get(path)
        except Exception as exc:
            messagebox.showerror("Could not open image", f"{path.name}\n\n{exc}")
            self._set_status(f"Failed to open {path.name}")
            return

        h, w = self.ctx.shape_full
        self._file_label.configure(text=f"{path.name}\n{w} x {h}")
        self.canvas.set_frame(self.ctx.rgba_full, keep_view=False)
        self._extract_palette()
        self._set_status("Click the image to sample a color. Shift+click samples "
                         "one exact pixel instead of a 3x3 median.")

    # --------------------------------------------------------------- palette

    def _on_k_change(self, _value: float) -> None:
        self._extract_palette()

    def _extract_palette(self) -> None:
        if self.ctx is None:
            return
        result = extract_palette(
            self.ctx.proxy,
            k=int(round(self._k_slider.get())),
            seed=seed_for_path(self.ctx.path),
            group_neutrals=bool(self._group_neutrals.get()),
        )
        self.ctx.clusters = result
        self._swatches.set_result(result)
        self._swatches.set_active_hex(self._pick)

    def _on_swatch_click(self, swatch: Swatch) -> None:
        self._set_pick(swatch.hex)

    # ----------------------------------------------------------------- picks

    def _on_canvas_pick(self, x: int, y: int, kind: str) -> None:
        """Sample the image. ``kind`` comes from the modifier the canvas saw;
        only "exact" (Shift) means anything here -- there is no target color on
        this tab for Ctrl to set, so it samples like an ordinary click."""
        if self.ctx is None:
            return
        radius = 0 if kind == "exact" else 1
        if not self._has_opaque(x, y, radius):
            self._set_status("Nothing to sample there -- that pixel is transparent.")
            return
        rgb = self.ctx.sample_rgb(x, y, radius)
        self._set_pick(rgb_to_hex(rgb))

    def _has_opaque(self, x: int, y: int, radius: int) -> bool:
        """Whether the patch :meth:`sample_rgb` would read holds any real color.

        A click on the empty checkerboard around the sprite is otherwise a
        silent trap: transparent PNG pixels store black under their zero alpha,
        and ``sample_rgb`` falls back to the raw patch when nothing in it is
        opaque, so the pick history fills with #000000 entries that are not
        colors anyone chose.
        """
        h, w = self.ctx.shape_full
        y0, y1 = max(0, y - radius), min(h, y + radius + 1)
        x0, x1 = max(0, x - radius), min(w, x + radius + 1)
        return bool((self.ctx.rgba_full[y0:y1, x0:x1, 3] >= ALPHA_MIN_CLUSTER).any())

    def _set_pick(self, value: str) -> None:
        self._pick = value
        r, g, b = hex_to_rgb(value)

        self._pick_chip.set_color(value)
        self._hex_label.configure(text=value)
        self._rgb_label.configure(text=f"R {r:<4}G {g:<4}B {b}")
        self._copy_btn.configure(state="normal")
        self._swatches.set_active_hex(value)

        self._remember(value)
        self._set_status(f"Picked {value}  ·  Copy hex puts it on the clipboard")

    def _remember(self, value: str) -> None:
        """Most recent first, no duplicates -- re-picking a color you already
        have should move it back to the top, not add a second row for it."""
        if value in self._recent:
            self._recent.remove(value)
        self._recent.insert(0, value)
        del self._recent[RECENT_LIMIT:]
        self._rebuild_recent()

    def _clear_recent(self) -> None:
        self._recent = []
        self._rebuild_recent()

    def _rebuild_recent(self) -> None:
        for row in self._recent_rows:
            row.destroy()
        self._recent_rows = []

        if self._recent:
            self._recent_hint.grid_remove()
        else:
            self._recent_hint.grid()
        self._clear_recent_btn.configure(
            state="normal" if self._recent else "disabled"
        )

        for i, value in enumerate(self._recent):
            row = ctk.CTkFrame(self._recent_list, fg_color="transparent")
            row.grid(row=i, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(1, weight=1)

            ChipSwatch(row, size=22, color=value).grid(row=0, column=0, padx=(2, 8))
            ctk.CTkLabel(row, text=value, font=FONT_MONO, anchor="w").grid(
                row=0, column=1, sticky="w"
            )
            ctk.CTkButton(
                row, text="Copy", width=52, height=24,
                command=lambda v=value: self._copy(v),
            ).grid(row=0, column=2, padx=(6, 2))

            self._recent_rows.append(row)

    # ------------------------------------------------------------- clipboard

    def _copy_current(self) -> None:
        if self._pick is not None:
            self._copy(self._pick)

    def _copy(self, value: str) -> None:
        """Put ``value`` on the system clipboard.

        Tk owns the offer for as long as the app runs and hands it back on
        exit, so a hex copied here is gone once the window closes -- which is
        Tk's behaviour everywhere, not something this tab can fix.

        ``update_idletasks`` rather than the ``update`` usually cited for this:
        the full version pumps *input* events too, so a second click landing
        while this callback is still on the stack gets processed re-entrantly
        and swallowed. Idle tasks are enough to flush the offer.
        """
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self._set_status(f"Copied {value} to the clipboard")

    # ------------------------------------------------------------------ view

    def _fit(self) -> None:
        self.canvas.fit()

    def _on_zoom_slider(self, value: float) -> None:
        if self._suspend_zoom_sync or self.ctx is None:
            return
        # The canvas always holds the full-resolution frame on this tab -- there
        # is no proxy to convert through, so slider magnification is canvas zoom.
        self.canvas.zoom_to(2.0 ** value)

    def _on_canvas_view(self, width: int, _height: int, zoom: float) -> None:
        if self.ctx is not None:
            zoom = width / max(self.ctx.shape_full[1], 1)
        self._zoom_label.configure(text=f"{zoom * 100:.0f}%")

        # Follow the wheel, Fit and window resizes -- guarded, because moving
        # the slider lands back here and writing to it mid-drag fights the
        # pointer.
        self._suspend_zoom_sync = True
        try:
            self._zoom_slider.set(
                float(np.clip(np.log2(max(zoom, 1e-6)), ZOOM_LOG_MIN, ZOOM_LOG_MAX))
            )
        finally:
            self._suspend_zoom_sync = False

    def _set_status(self, text: str) -> None:
        self._status.configure(text=text)
