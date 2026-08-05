"""The Recolor tab: pick a render, select colors, retarget them, export."""

from __future__ import annotations

import copy
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import numpy as np
from PIL import Image
from scipy import ndimage

from ..colorspace import hex_to_rgb, lab_to_hex, rgb8_to_lab, rgb_to_hex
from ..config import EXPORT_DIR, KMEANS_DEFAULT_K, PRESETS_DIR
from ..imagectx import ContextCache
from ..library import Library
from ..palette_extract import Swatch, extract_palette, seed_for_path
from ..presets import load_preset, save_preset
from ..recolor import MatchMode, RecolorRule, Region, render, rule_weight
from .color_wheel import ColorEditor
from .image_canvas import ImageCanvas
from .rule_row import RuleRow
from .swatch_grid import SwatchStrip
from .widgets import FONT_MONO, FONT_SMALL, LabeledSlider, Swatch as ChipSwatch, Tooltip, section_label

SETTLE_MS = 250
"""How long after the last input before the full-resolution pass runs."""

ZOOM_LOG_MIN = -4.0
ZOOM_LOG_MAX = 4.0
"""Zoom slider range, in doublings: 6% to 1600%. Log-spaced because the useful
range below 1:1 is a sixteenth of a linear track and would be unusable."""

UNDO_LIMIT = 100
COALESCE_S = 1.0
"""Repeated edits of the same control within this window collapse into one undo
step, so dragging a slider is one action rather than a hundred."""


class RecolorTab(ctk.CTkFrame):
    def __init__(self, master, app, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.app = app

        self.library = Library()
        self.cache = ContextCache()
        self.ctx = None
        self.rules: list[RecolorRule] = []
        self.selected: RecolorRule | None = None

        self._rows: list[RuleRow] = []
        self._weight_cache: dict = {}
        self._palette_params: tuple[int, bool] | None = None
        """(k, group_neutrals) behind the left grid, so the replacement strip
        can tell whether it would be asking k-means the same question."""
        self._preview_dirty = False
        self._settle_after: str | None = None
        self._showing_hq = False
        self._hover_swatch: Swatch | None = None
        self._view_mode = "Preview"
        self._suspend_rule_sync = False
        self._suspend_zoom_sync = False
        self._alive = True
        self._pump_after: str | None = None
        self._auto_selection = False
        self._pending_region: Region | None = None
        """A drawn shape not yet attached to any rule. Held rather than applied
        so drawing never mutates whichever rule happens to be selected."""

        self._undo_stack: list[tuple[str, list, int]] = []
        self._redo_stack: list[tuple[str, list, int]] = []
        self._coalesce_key = None
        self._coalesce_at = 0.0
        self._restoring = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_left()
        self._build_center()
        self._build_right()

        self.after(100, self._initial_load)
        self._pump_after = self.after(16, self._pump_preview)

    def destroy(self) -> None:
        """Cancel pending timers first; otherwise they fire against a
        half-destroyed widget tree and Tk reports 'invalid command name'."""
        self._alive = False
        for token in (self._pump_after, self._settle_after):
            if token is not None:
                try:
                    self.after_cancel(token)
                except tk.TclError:
                    pass
        self._pump_after = self._settle_after = None
        super().destroy()

    # ------------------------------------------------------------------ left

    def _build_left(self) -> None:
        """Everything that decides *what* gets recolored.

        Source render, the palette it was extracted into, the canvas gestures
        that pick pixels, and the matching controls -- one column, read top to
        bottom. The right panel answers the other question, what to change the
        selection *to*.
        """
        panel = ctk.CTkFrame(self, width=300)
        panel.grid(row=0, column=0, sticky="nsw", padx=(8, 4), pady=8)
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        self._build_source_picker(panel).grid(row=0, column=0, sticky="ew")

        # The palette and the matching controls share one scroll region. Stacked
        # they are taller than the window at its minimum size, and a control you
        # cannot reach at all is worse than one you have to scroll to. The file
        # picker above stays put -- it is how you get here in the first place.
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
            on_hover=self._on_swatch_hover,
            flag_spans=True,
            tooltip_suffix="Click to make a rule for this color.",
            fg_color="transparent",
        )
        self._swatches.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 4))
        self._swatches.show_message("Load a render to extract its palette.")

        section_label(body, "Selection").grid(
            row=3, column=0, sticky="ew", padx=6, pady=(10, 4)
        )
        self._build_selection_controls(body).grid(
            row=4, column=0, sticky="ew", padx=6, pady=(0, 8)
        )

    def _build_source_picker(self, parent) -> ctk.CTkFrame:
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid_columnconfigure(0, weight=1)

        section_label(box, "Source render").grid(
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

        self._file_label = ctk.CTkLabel(
            box, text="", font=FONT_MONO, anchor="w", wraplength=268, justify="left"
        )
        self._file_label.grid(row=4, column=0, sticky="ew", padx=10, pady=(6, 0))

        self._lib_label = ctk.CTkLabel(
            box, text="", font=FONT_SMALL, anchor="w", text_color="#999",
            wraplength=268, justify="left",
        )
        self._lib_label.grid(row=5, column=0, sticky="ew", padx=10, pady=(2, 6))

        ctk.CTkButton(
            box, text="Refresh library", height=28, command=self.refresh_library
        ).grid(row=6, column=0, sticky="ew", padx=10, pady=(0, 8))

        return box

    # ---------------------------------------------------------------- center

    def _build_center(self) -> None:
        panel = ctk.CTkFrame(self)
        panel.grid(row=0, column=1, sticky="nsew", padx=4, pady=8)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        # View and history only. The tools that pick pixels live in the left
        # panel next to the rest of the selection controls.
        bar = ctk.CTkFrame(panel, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        bar.grid_columnconfigure(4, weight=1)

        self._view = ctk.CTkSegmentedButton(
            bar,
            values=["Original", "Preview", "Selection"],
            command=self._on_view_change,
        )
        self._view.set("Preview")
        self._view.grid(row=0, column=0, sticky="w")

        self._fit_btn = ctk.CTkButton(
            bar, text="Fit", width=48, height=28, command=self._fit
        )
        self._fit_btn.grid(row=0, column=1, padx=(8, 0))

        history = ctk.CTkFrame(bar, fg_color="transparent")
        history.grid(row=0, column=2, padx=(10, 0), sticky="w")
        self._history_group = history
        self._undo_btn = ctk.CTkButton(
            history, text="↶", width=34, height=28, state="disabled",
            command=self.undo,
        )
        self._undo_btn.grid(row=0, column=0, padx=1)
        Tooltip(self._undo_btn, "Undo   (Ctrl+Z)")

        self._redo_btn = ctk.CTkButton(
            history, text="↷", width=34, height=28, state="disabled",
            command=self.redo,
        )
        self._redo_btn.grid(row=0, column=1, padx=1)
        Tooltip(self._redo_btn, "Redo   (Ctrl+R, or Ctrl+Y)")

        files = ctk.CTkFrame(bar, fg_color="transparent")
        files.grid(row=0, column=3, padx=(12, 0), sticky="w")
        self._file_actions = files
        for col, (text, width, command, tip) in enumerate(
            (
                (
                    "Export", 62, self._export,
                    "Write the recolored render straight to the next free "
                    "Custom slot for this character.",
                ),
                (
                    "Export As...", 76, self._export_as,
                    "Write the recolored render to a path you pick.",
                ),
                (
                    "Save preset", 76, self._save_preset,
                    "Save the current rules to a file, to reuse on another "
                    "render or another skin.",
                ),
                (
                    "Load preset", 76, self._load_preset,
                    "Replace the current rules with a saved set.",
                ),
            )
        ):
            btn = ctk.CTkButton(
                files, text=text, width=width, height=28, command=command
            )
            btn.grid(row=0, column=col, padx=(0 if col == 0 else 3, 0))
            Tooltip(btn, tip)

        zoom_box = ctk.CTkFrame(bar, fg_color="transparent")
        zoom_box.grid(row=0, column=4, sticky="e", padx=(12, 0))
        self._zoom_box = zoom_box

        ctk.CTkLabel(zoom_box, text="Zoom", font=FONT_SMALL, anchor="e").grid(
            row=0, column=0, padx=(0, 6)
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
        self._zoom_slider.grid(row=0, column=1)
        Tooltip(
            self._zoom_slider,
            f"Zoom the canvas, {2 ** ZOOM_LOG_MIN:.0%} to {2 ** ZOOM_LOG_MAX:.0%}. "
            "The track is per doubling, so the middle of it is 1:1 rather than "
            "somewhere in the first tenth.\n\n"
            "The wheel over the image does the same thing and zooms about the "
            "cursor.",
        )

        self._hq = ctk.CTkLabel(
            zoom_box, text="", font=FONT_MONO, width=52, anchor="e",
            text_color="#5fbf5f",
        )
        self._hq.grid(row=0, column=2, padx=(6, 6))
        Tooltip(
            self._hq,
            "Canvas zoom. 100% is one screen pixel per render pixel.\n\n"
            "Green once you are looking at the full-resolution render; gray "
            "while the faster proxy stands in as you edit.",
        )

        # Six controls do not fit across a narrow window. Rather than let the
        # right-hand ones fall off the edge, the file actions drop to their own
        # row when the bar gets tight.
        bar.bind("<Configure>", self._reflow_toolbar)

        self.canvas = ImageCanvas(
            panel,
            on_pick=self._on_canvas_pick,
            on_region=self._on_region_drawn,
            on_view=self._on_canvas_view,
        )
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        self._status = ctk.CTkLabel(panel, text="", font=FONT_SMALL, anchor="w")
        self._status.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

    def _reflow_toolbar(self, event) -> None:
        """One row when it fits, two when it does not.

        The small gap between the two thresholds is only there to keep a
        boundary pixel from flapping: wrapping changes the bar's height, not
        its width, so the decision does not feed back into its own input.
        """
        need = 24 + sum(
            w.winfo_reqwidth()
            for w in (
                self._view, self._fit_btn, self._history_group,
                self._file_actions, self._zoom_box,
            )
        )
        wrapped = int(self._file_actions.grid_info().get("row", 0)) == 1
        if not wrapped and event.width < need:
            self._file_actions.grid(
                row=1, column=0, columnspan=5, sticky="w", padx=0, pady=(5, 0)
            )
        elif wrapped and event.width > need + 16:
            self._file_actions.grid(
                row=0, column=3, columnspan=1, sticky="w", padx=(12, 0), pady=0
            )

    # ----------------------------------------------------------------- right

    def _build_right(self) -> None:
        """The rules, and what each one replaces its color with.

        Nothing here narrows a selection: that is the left panel's job. This
        side is the stack of rules plus the replacement -- wheel, palette
        colors, and how hard the new color is applied.
        """
        panel = ctk.CTkFrame(self, width=352)
        panel.grid(row=0, column=2, sticky="nse", padx=(4, 8), pady=8)
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        # The rules list keeps a modest fixed height; the replacement controls
        # take the slack in one scroll region below it, so every control stays
        # reachable even at the minimum window size.
        panel.grid_rowconfigure(2, weight=0)
        panel.grid_rowconfigure(3, weight=1)

        section_label(panel, "Rules").grid(
            row=0, column=0, sticky="ew", padx=10, pady=(10, 4)
        )

        add = ctk.CTkFrame(panel, fg_color="transparent")
        add.grid(row=1, column=0, sticky="ew", padx=10)
        add.grid_columnconfigure((0, 1), weight=1)
        self._add_rule_btn = ctk.CTkButton(
            add, text="+ Add rule", height=28, command=self._add_rule
        )
        self._add_rule_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        Tooltip(
            self._add_rule_btn,
            "Adds a rule. With a Box or Lasso region drawn, the new rule is "
            "confined to it and takes its source color from the dominant "
            "material inside it.",
        )
        ctk.CTkButton(
            add, text="Clear all", height=28, fg_color="#5a3a3a",
            hover_color="#7a4a4a", command=self._clear_rules,
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        self._rule_list = ctk.CTkScrollableFrame(panel, height=175)
        self._rule_list.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)
        self._rule_list.grid_columnconfigure(0, weight=1)

        self._empty_hint = ctk.CTkLabel(
            self._rule_list,
            text="Click a palette swatch on the left,\nor arm the eyedropper and click\nthe image.",
            font=FONT_SMALL,
            text_color="#999",
            justify="left",
        )
        self._empty_hint.grid(row=0, column=0, sticky="w", padx=6, pady=6)

        scroll = ctk.CTkScrollableFrame(panel, label_text="Replace with")
        scroll.grid(row=3, column=0, sticky="nsew", padx=10, pady=(8, 10))
        scroll.grid_columnconfigure(0, weight=1)

        self._editor = ColorEditor(
            scroll, on_change=self._on_target_color, wheel_size=165,
            fg_color="transparent",
        )
        self._editor.grid(row=0, column=0, sticky="ew")

        self._build_target_palette(scroll)
        self._build_blend_controls(scroll)


    def _build_target_palette(self, parent) -> None:
        """The render's own palette again, but as replacement colors.

        Mirrors the extracted-palette grid on the left: over there a click says
        "change this color", here it says "change it to this one" -- which is
        what you want for swapping two materials, or matching a color the
        render already uses instead of eyeballing it on the wheel.
        """
        section_label(parent, "Palette colors").grid(
            row=1, column=0, sticky="ew", pady=(10, 2)
        )

        self._target_k = LabeledSlider(
            parent,
            "Colors (k)",
            from_=4,
            to=24,
            value=KMEANS_DEFAULT_K,
            steps=20,
            fmt="{:.0f}",
            command=self._on_target_k_change,
        )
        self._target_k.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        Tooltip(
            self._target_k,
            "How finely to split the render for *this* strip only -- the left "
            "grid keeps its own k.\n\n"
            "Raise it to pull out a shade the coarse selection palette merged "
            "away; lower it for the handful of colors the render is really "
            "built from.",
        )

        self._target_swatches = SwatchStrip(
            parent,
            on_select=self._on_target_swatch_click,
            columns=6,
            chip_size=30,
            tooltip_suffix="Click to use as the replacement color.",
            fg_color="transparent",
        )
        self._target_swatches.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        self._target_swatches.show_message("Load a render to see its colors.")

    def _on_target_swatch_click(self, swatch: Swatch) -> None:
        if self.selected is None:
            self._status.configure(
                text="No rule selected. Pick a color to replace first, then "
                "choose its replacement here."
            )
            return
        # Routed through the editor rather than straight onto the rule, so the
        # wheel, hex and RGB fields all show the color that is now in effect.
        self._editor.set_color(hex_to_rgb(swatch.hex), "picker")

    def _sync_target_swatch_highlight(self) -> None:
        rule = self.selected
        self._target_swatches.set_active_hex(
            lab_to_hex(rule.target_lab) if rule is not None else None
        )

    def _build_selection_controls(self, parent) -> ctk.CTkFrame:
        """What the selected rule matches: the canvas tools and the matching
        settings, in the order you reach for them."""
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid_columnconfigure(0, weight=1)

        self._eyedropper_btn = ctk.CTkButton(
            box, text="Eyedropper", height=28, command=self._toggle_eyedropper
        )
        self._eyedropper_btn.grid(row=0, column=0, sticky="ew", pady=(2, 9))
        Tooltip(
            self._eyedropper_btn,
            "Click the image to set the selected rule's source color.\n"
            "Shift+click samples one exact pixel instead of a 3x3 median.\n"
            "Ctrl+click sets the TARGET color from the image.",
        )

        region_bar = ctk.CTkFrame(box, fg_color="transparent")
        region_bar.grid(row=1, column=0, sticky="ew")
        region_bar.grid_columnconfigure((0, 1, 2), weight=1, uniform="region")

        self._region_btns: dict[str, ctk.CTkButton] = {}
        for col, (mode, text, tip) in enumerate(
            (
                (
                    "rect",
                    "▭ Box",
                    "Drag a box on the image to claim an area for one rule.\n\n"
                    "The rule is confined to it, and it owns it: no rule below "
                    "it in the list can paint inside, so a broad rule cannot "
                    "creep back over a part you separated out.",
                ),
                (
                    "lasso",
                    "◌ Lasso",
                    "Draw a freehand outline around a part. Released strokes "
                    "close automatically.\n\n"
                    "Same ownership as Box: rules below cannot paint inside it.",
                ),
            )
        ):
            btn = ctk.CTkButton(
                region_bar, text=text, height=28,
                command=lambda m=mode: self._toggle_draw_mode(m),
            )
            btn.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 3, 0))
            Tooltip(btn, tip)
            self._region_btns[mode] = btn

        self._clear_region_btn = ctk.CTkButton(
            region_bar, text="Clear", height=28,
            command=self._clear_region, state="disabled",
        )
        self._clear_region_btn.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        Tooltip(self._clear_region_btn, "Remove the drawn region from this rule.")

        self._apply_region_btn = ctk.CTkButton(
            box, text="Apply region to this rule", height=28, state="disabled",
            command=self._apply_region_to_rule,
        )
        self._apply_region_btn.grid(row=2, column=0, sticky="ew", pady=(9, 6))
        Tooltip(
            self._apply_region_btn,
            "Confine the selected rule to the region you just drew, instead of "
            "making a new rule for it.",
        )

        mode_row = ctk.CTkFrame(box, fg_color="transparent")
        mode_row.grid(row=3, column=0, sticky="ew", pady=(2, 6))
        mode_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(mode_row, text="Match", font=FONT_SMALL, width=54, anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        self._mode = ctk.CTkOptionMenu(
            mode_row,
            values=[m.value for m in MatchMode],
            command=lambda _v: self._sync_rule_from_ui(),
            height=28,
        )
        self._mode.set(MatchMode.MATERIAL.value)
        self._mode.grid(row=0, column=1, sticky="ew")
        Tooltip(
            self._mode,
            "material - ignores lighting, so one click grabs a whole shaded "
            "material. The default.\n"
            "exact - only near-identical colors.\n"
            "hue - hue angle only; for a material sharing its tone with another.",
        )

        self._tolerance = LabeledSlider(
            box, "Tolerance", from_=1, to=60, value=12, fmt="{:.0f}",
            command=lambda _v: self._sync_rule_from_ui(),
        )
        self._tolerance.grid(row=4, column=0, sticky="ew", pady=3)

        self._feather = LabeledSlider(
            box, "Edge softness", from_=0.0, to=1.0, value=0.35,
            command=lambda _v: self._sync_rule_from_ui(),
        )
        self._feather.grid(row=5, column=0, sticky="ew", pady=(3, 6))

        self._match_all = ctk.CTkCheckBox(
            box, text="Match all pixels", font=FONT_SMALL,
            command=self._sync_rule_from_ui,
        )
        self._match_all.grid(row=6, column=0, sticky="w", pady=2)
        Tooltip(
            self._match_all,
            "Ignore the drawn region and recolor every matching pixel in the "
            "image.\n\n"
            "Only does anything once you have drawn a Box or Lasso region; it "
            "is the override for temporarily seeing the rule unconfined.",
        )

        self._contiguous = ctk.CTkCheckBox(
            box, text="This region only", font=FONT_SMALL,
            command=self._sync_rule_from_ui,
        )
        self._contiguous.grid(row=7, column=0, sticky="w", pady=2)
        Tooltip(
            self._contiguous,
            "Restrict the rule to one connected blob -- the one you clicked, or "
            "the largest one if the rule came from a palette swatch.\n\n"
            "Weak on this game's renders: shading in 'material' mode usually "
            "bridges separate parts into a single blob. Draw a Box or Lasso "
            "region instead when this does not separate what you expect.",
        )

        self._smooth = ctk.CTkCheckBox(
            box, text="Smooth edges", font=FONT_SMALL, command=self._sync_rule_from_ui
        )
        self._smooth.grid(row=8, column=0, sticky="w", pady=2)

        self._lock_cluster = ctk.CTkCheckBox(
            box, text="Lock to palette swatch", font=FONT_SMALL,
            command=self._sync_rule_from_ui,
        )
        self._lock_cluster.grid(row=9, column=0, sticky="w", pady=2)

        return box

    def _build_blend_controls(self, parent) -> None:
        """How hard the replacement color lands.

        These are not selection: the pixels are already chosen by the time they
        apply, and each one only scales how far toward the target they move --
        which is why they sit under the color that is being applied.
        """
        section_label(parent, "Apply strength").grid(
            row=4, column=0, sticky="ew", pady=(10, 2)
        )

        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        box.grid_columnconfigure(0, weight=1)

        self._lightness = LabeledSlider(
            box, "Lightness shift", from_=0.0, to=1.0, value=1.0,
            command=lambda _v: self._sync_rule_from_ui(),
        )
        self._lightness.grid(row=0, column=0, sticky="ew", pady=3)
        Tooltip(
            self._lightness,
            "0 keeps every pixel's original brightness and shifts only hue and "
            "saturation - best for subtle palette swaps.",
        )

        self._chroma = LabeledSlider(
            box, "Saturation shift", from_=0.0, to=1.0, value=1.0,
            command=lambda _v: self._sync_rule_from_ui(),
        )
        self._chroma.grid(row=1, column=0, sticky="ew", pady=3)

        self._opacity = LabeledSlider(
            box, "Strength", from_=0.0, to=1.0, value=1.0,
            command=lambda _v: self._sync_rule_from_ui(),
        )
        self._opacity.grid(row=2, column=0, sticky="ew", pady=3)

        self._gamut = ctk.CTkCheckBox(
            box, text="Gamut safe", font=FONT_SMALL, command=self._request_preview
        )
        self._gamut.select()
        self._gamut.grid(row=3, column=0, sticky="w", pady=(6, 2))
        Tooltip(
            self._gamut,
            "Pull impossible colors back into sRGB by reducing saturation "
            "instead of clipping channels, which would shift the hue.",
        )

    # ------------------------------------------------------------- library

    def _initial_load(self) -> None:
        self.refresh_library()

    def refresh_library(self) -> None:
        self.library.scan()
        self._lib_label.configure(text=self.library.status_line())

        characters = self.library.characters()
        if not characters:
            self._character.configure(values=["(no renders found)"])
            self._character.set("(no renders found)")
            self._status.configure(
                text="No renders found. Use the Downloads tab to fetch them."
            )
            return

        current = self._character.get()
        self._character.configure(values=characters)
        if current not in characters:
            current = "Ranno" if "Ranno" in characters else characters[0]
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
        if render_file is None:
            return
        self._load_render(render_file.path)

    def _load_render(self, path: Path) -> None:
        self._status.configure(text=f"Loading {path.name}...")
        self.update_idletasks()
        try:
            self.ctx = self.cache.get(path)
        except Exception as exc:
            messagebox.showerror("Could not open render", f"{path.name}\n\n{exc}")
            self._status.configure(text=f"Failed to open {path.name}")
            return

        self._weight_cache.clear()
        # A shape drawn against the previous render is not a decision the user
        # made about this one.
        self._pending_region = None
        h, w = self.ctx.shape_full
        self._file_label.configure(text=f"{path.name}\n{w} x {h}")
        self.canvas.set_frame(self.ctx.rgba_full, keep_view=False)
        self._extract_palette()
        self._refresh_region_controls()
        self._request_preview()
        self._status.configure(text=f"Loaded {path.name}")

    # ------------------------------------------------------------- palette

    def _on_k_change(self, _value: float) -> None:
        self._extract_palette()

    def _on_target_k_change(self, _value: float) -> None:
        self._extract_target_palette()

    def _extract_palette(self) -> None:
        """The selection palette: what the left grid offers, and what rules
        built from a swatch are bound to."""
        if self.ctx is None:
            return
        self._palette_params = (
            int(round(self._k_slider.get())),
            bool(self._group_neutrals.get()),
        )
        result = extract_palette(
            self.ctx.proxy,
            k=self._palette_params[0],
            seed=seed_for_path(self.ctx.path),
            group_neutrals=self._palette_params[1],
        )
        self.ctx.clusters = result
        self._swatches.set_result(result)
        self._extract_target_palette()

    def _extract_target_palette(self) -> None:
        """The replacement strip, clustered at its own k.

        How many colors you want to *choose from* is a different question to
        how many you want to split the render into for selecting: a coarse
        selection palette next to a fine one to pick a shade out of is a normal
        way to work. Only the left result is stored on the context -- rules
        reference cluster ids from that one, and a second set of ids would
        silently mean something else.
        """
        if self.ctx is None:
            return
        params = (
            int(round(self._target_k.get())),
            bool(self._group_neutrals.get()),
        )
        if params == self._palette_params and self.ctx.clusters is not None:
            result = self.ctx.clusters  # same question, so do not re-run k-means
        else:
            result = extract_palette(
                self.ctx.proxy,
                k=params[0],
                seed=seed_for_path(self.ctx.path),
                group_neutrals=params[1],
            )
        self._target_swatches.set_result(result)
        self._sync_target_swatch_highlight()

    def _on_swatch_click(self, swatch: Swatch) -> None:
        # Target starts equal to source, so picking what to change does not
        # change it. The rule stays a pending selection until a color is chosen.
        self._append_rule(
            RecolorRule(
                source_lab=swatch.midtone_lab,
                target_lab=swatch.midtone_lab.copy(),
                mode=MatchMode.MATERIAL,
                tolerance=swatch.suggested_tolerance,
                cluster_id=swatch.index,
                seed_uv=self._seed_for_cluster(swatch),
            )
        )

    def _seed_for_cluster(self, swatch: Swatch) -> tuple[float, float] | None:
        """A representative pixel location for a cluster, in normalized coords.

        Without this a swatch-built rule has no seed, which used to make "This
        region only" a silent no-op for the entire palette workflow.
        """
        if self.ctx is None or self.ctx.clusters is None:
            return None
        ps = self.ctx.proxy
        labels = self.ctx.clusters.labels_for(ps)
        member = np.flatnonzero(labels == swatch.index)
        if member.size == 0:
            return None

        h, w = self.ctx.shape_proxy

        # Restrict to the cluster's largest connected blob first. A material
        # scatters specks of its own color across the sheet, and seeding into
        # one of those would make "This region only" keep the speck and discard
        # the actual part.
        plane = np.zeros(h * w, dtype=bool)
        plane[ps.idx[member]] = True
        labeled, count = ndimage.label(plane.reshape(h, w))
        if count:
            sizes = np.bincount(labeled.ravel())
            sizes[0] = 0
            biggest = labeled.reshape(-1)[ps.idx[member]] == int(np.argmax(sizes))
            if biggest.any():
                member = member[biggest]

        # Then the member closest to the midtone -- the same color the rule
        # matches on, so the seed avoids antialiased fringe pixels.
        d = np.linalg.norm(ps.lab[member] - swatch.midtone_lab[None, :], axis=1)
        flat = int(ps.idx[member[int(np.argmin(d))]])
        y, x = divmod(flat, w)
        return (x / max(w - 1, 1), y / max(h - 1, 1))

    # -------------------------------------------------------------- regions

    def _toggle_draw_mode(self, mode: str) -> None:
        active = self.canvas.draw_mode != mode
        self.canvas.set_draw_mode(mode if active else None)
        if active:
            self._set_eyedropper(False)  # one canvas gesture at a time
        self._refresh_region_controls()
        if active:
            self._status.configure(
                text=f"Drag on the image to draw a "
                f"{'box' if mode == 'rect' else 'lasso'} region."
            )

    def _on_region_drawn(self, kind: str, points: list) -> None:
        """Hold the drawn shape as *pending*, attached to no rule yet.

        Drawing does not silently rewrite whatever rule happened to be selected;
        the region is committed by "+ Add rule" (a new rule for that area) or by
        "Apply region to this rule".
        """
        self._pending_region = Region(kind=kind, points=points)
        self.canvas.set_draw_mode(None)
        self._refresh_region_controls()
        self._status.configure(
            text="Region drawn. '+ Add rule' makes a rule for it, or "
            "'Apply region to this rule' confines the selected rule."
        )

    def _consume_pending_region(self, rule: RecolorRule) -> bool:
        """Move any pending region onto ``rule``. Returns whether one moved."""
        if self._pending_region is None:
            return False
        rule.region = self._pending_region
        rule.match_all = False  # a freshly drawn region is meant to apply
        self._pending_region = None
        return True

    def _apply_region_to_rule(self) -> None:
        if self.selected is None or self._pending_region is None:
            return
        self._push_undo("apply region to rule")
        self._consume_pending_region(self.selected)
        self._match_all.deselect()
        self._refresh_region_controls()

        # Show what the region actually selects before it recolors anything,
        # rather than jumping straight to a changed image.
        self._show_selection_view()
        self._refresh_rows()
        self._request_preview()
        self._report_selection()

    def _clear_region(self) -> None:
        """Drop the pending shape if there is one, else the rule's own region."""
        if self._pending_region is not None:
            self._pending_region = None
            self._refresh_region_controls()
            self._status.configure(text="Discarded the drawn region.")
            return

        if self.selected is None or self.selected.region is None:
            return
        self._push_undo("clear region")
        self.selected.region = None
        self._refresh_region_controls()
        self._refresh_rows()
        self._request_preview()
        self._report_selection()

    def _refresh_region_controls(self) -> None:
        """Keep the toolbar in step with the canvas mode and the selected rule."""
        mode = self.canvas.draw_mode
        theme = ctk.ThemeManager.theme["CTkButton"]
        for name, btn in self._region_btns.items():
            on = name == mode
            btn.configure(
                fg_color="#2e5f2e" if on else theme["fg_color"],
                hover_color="#3a763a" if on else theme["hover_color"],
            )

        pending = self._pending_region is not None
        own = self.selected is not None and self.selected.region is not None
        self._clear_region_btn.configure(
            state="normal" if (pending or own) else "disabled"
        )
        self._apply_region_btn.configure(
            state="normal" if (pending and self.selected is not None) else "disabled"
        )
        self._add_rule_btn.configure(
            text="+ Add rule for region" if pending else "+ Add rule"
        )

        # The pending shape wins on screen: it is the one awaiting a decision.
        if pending:
            self.canvas.set_region(self._pending_region)
        else:
            self.canvas.set_region(self.selected.region if self.selected else None)

    def _on_swatch_hover(self, swatch: Swatch | None) -> None:
        self._hover_swatch = swatch
        self._request_preview()

    # ------------------------------------------------------------- history

    def _snapshot(self, label: str) -> tuple[str, list, int]:
        """Deep-copy the rules so restoring is lossless.

        Serializing through the preset format would round-trip colors via
        8-bit hex and quietly lose precision on every undo.
        """
        index = self.rules.index(self.selected) if self.selected in self.rules else -1
        return (label, copy.deepcopy(self.rules), index)

    def _push_undo(self, label: str, coalesce_key=None) -> None:
        """Record the state *before* a mutation.

        ``coalesce_key`` folds a continuous gesture -- a slider drag, a wheel
        sweep -- into the single snapshot taken when it began.
        """
        if self._restoring:
            return

        now = time.monotonic()
        if (
            coalesce_key is not None
            and coalesce_key == self._coalesce_key
            and (now - self._coalesce_at) < COALESCE_S
        ):
            self._coalesce_at = now  # sliding window, so long drags stay one step
            return

        self._undo_stack.append(self._snapshot(label))
        if len(self._undo_stack) > UNDO_LIMIT:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._coalesce_key = coalesce_key
        self._coalesce_at = now
        self._update_history_buttons()

    def _restore(self, snap: tuple[str, list, int]) -> None:
        _label, rules, index = snap
        self._restoring = True
        try:
            self.rules = copy.deepcopy(rules)
            self.selected = self.rules[index] if 0 <= index < len(self.rules) else None
            self._rebuild_rule_list()
            if self.selected is not None:
                self._sync_ui_from_rule()
            else:
                self._sync_target_swatch_highlight()
        finally:
            self._restoring = False

        self._coalesce_key = None
        self._refresh_region_controls()
        self._request_preview()
        self._report_selection()
        self._update_history_buttons()

    def undo(self, _event=None) -> str:
        if not self._undo_stack:
            self._status.configure(text="Nothing to undo.")
            return "break"
        snap = self._undo_stack.pop()
        self._redo_stack.append(self._snapshot(snap[0]))
        self._restore(snap)
        self._status.configure(
            text=f"Undid: {snap[0]}  ({len(self._undo_stack)} more)"
        )
        return "break"

    def redo(self, _event=None) -> str:
        if not self._redo_stack:
            self._status.configure(text="Nothing to redo.")
            return "break"
        snap = self._redo_stack.pop()
        self._undo_stack.append(self._snapshot(snap[0]))
        self._restore(snap)
        self._status.configure(text=f"Redid: {snap[0]}")
        return "break"

    def _update_history_buttons(self) -> None:
        self._undo_btn.configure(
            state="normal" if self._undo_stack else "disabled"
        )
        self._redo_btn.configure(
            state="normal" if self._redo_stack else "disabled"
        )

    # --------------------------------------------------------------- rules

    def _add_rule(self) -> None:
        if self.ctx is None:
            return

        # With a region pending, "add rule" means "a rule for that area", so the
        # source color comes from inside it rather than from swatch 0.
        if self._pending_region is not None:
            rule = self._rule_for_region(self._pending_region)
            if rule is not None:
                self._append_rule(rule)
                return

        source = (
            self.ctx.clusters.swatches[0].midtone_lab
            if self.ctx.clusters and self.ctx.clusters.swatches
            else np.array([50.0, 0.0, 0.0], np.float32)
        )
        self._append_rule(RecolorRule(source_lab=source, target_lab=source.copy()))

    def _rule_for_region(self, region: Region) -> RecolorRule | None:
        """Build a rule matching the dominant material inside ``region``.

        The dominant *cluster* rather than a mean color: a mean over a region
        spanning two materials is a blend that belongs to neither, whereas the
        biggest cluster inside the box is the thing the user drew around, and it
        brings its own measured tolerance with it.
        """
        ps = self.ctx.proxy
        inside = region.mask(self.ctx.shape_proxy).reshape(-1)[ps.idx]
        if not inside.any():
            self._status.configure(text="That region contains no opaque pixels.")
            return None

        clusters = self.ctx.clusters
        if clusters is None:
            source = np.median(ps.lab[inside], axis=0).astype(np.float32)
            return RecolorRule(source_lab=source, target_lab=source.copy())

        labels = clusters.labels_for(ps)
        index = int(np.argmax(np.bincount(labels[inside], minlength=clusters.k)))
        swatch = clusters.swatch(index)
        if swatch is None:
            source = np.median(ps.lab[inside], axis=0).astype(np.float32)
            return RecolorRule(source_lab=source, target_lab=source.copy())

        return RecolorRule(
            source_lab=swatch.midtone_lab,
            target_lab=swatch.midtone_lab.copy(),
            mode=MatchMode.MATERIAL,
            tolerance=swatch.suggested_tolerance,
            cluster_id=swatch.index,
            seed_uv=self._seed_in_region(ps, inside, labels, swatch),
        )

    def _seed_in_region(self, ps, inside, labels, swatch) -> tuple[float, float] | None:
        """Seed 'this region only' from a pixel inside the drawn area."""
        member = np.flatnonzero(inside & (labels == swatch.index))
        if member.size == 0:
            return None
        d = np.linalg.norm(ps.lab[member] - swatch.midtone_lab[None, :], axis=1)
        h, w = self.ctx.shape_proxy
        y, x = divmod(int(ps.idx[member[int(np.argmin(d))]]), w)
        return (x / max(w - 1, 1), y / max(h - 1, 1))

    def _append_rule(self, rule: RecolorRule) -> None:
        self._push_undo("add rule")
        # Any new rule created while a shape is pending is scoped to it, so the
        # eyedropper and the palette swatches honour a drawn region too.
        self._consume_pending_region(rule)
        self.rules.append(rule)
        self.selected = rule
        self._rebuild_rule_list()
        self._sync_ui_from_rule()

        # A brand-new rule changes nothing yet, so show what it has selected
        # instead of a preview that looks identical to the original.
        if rule.is_identity():
            self._show_selection_view()
        self._request_preview()
        self._report_selection()

    def _show_selection_view(self) -> None:
        if self._view_mode != "Selection":
            self._auto_selection = True
            self._view_mode = "Selection"
            self._view.set("Selection")

    def _leave_selection_view(self) -> None:
        """Return to Preview once a target color makes the rule do something."""
        if self._auto_selection and self._view_mode == "Selection":
            self._auto_selection = False
            self._view_mode = "Preview"
            self._view.set("Preview")

    def _report_selection(self) -> None:
        """Tell the user how much the selected rule covers, and what to do next."""
        rule = self.selected
        if rule is None or self.ctx is None:
            return
        ps = self.ctx.proxy
        w = rule_weight(ps, rule, shape=self.ctx.shape_proxy)
        covered = float((w > 0.5).sum()) / max(len(ps), 1)

        if rule.region is None:
            note = ""
        elif rule.match_all:
            note = "  [region ignored - 'Match all pixels' is on]"
        else:
            note = f"  [owns its drawn {rule.region.kind}; rules below cannot paint there]"

        if rule.is_identity():
            self._status.configure(
                text=f"Selected {covered * 100:.1f}% of the render.{note} "
                "Adjust Tolerance if needed, then pick a replacement color."
            )
        else:
            self._status.configure(
                text=f"Rule covers {covered * 100:.1f}% of the render.{note}"
            )

    def _clear_rules(self) -> None:
        if self.rules and not messagebox.askyesno(
            "Clear all rules", f"Remove all {len(self.rules)} rule(s)?"
        ):
            return
        self._push_undo("clear all rules")
        self.rules = []
        self.selected = None
        self._weight_cache.clear()
        self._rebuild_rule_list()
        self._sync_target_swatch_highlight()
        self._refresh_region_controls()
        self._request_preview()

    def _rebuild_rule_list(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows = []

        if not self.rules:
            self._empty_hint.grid()
            return
        self._empty_hint.grid_remove()

        for i, rule in enumerate(self.rules):
            row = RuleRow(
                self._rule_list,
                rule,
                index=i,
                on_select=self._select_rule,
                on_toggle=self._toggle_rule,
                on_move=self._move_rule,
                on_delete=self._delete_rule,
            )
            row.grid(row=i, column=0, sticky="ew", pady=2)
            row.refresh(selected=rule is self.selected)
            self._rows.append(row)

    def _refresh_rows(self) -> None:
        for i, row in enumerate(self._rows):
            row.index = i
            row.refresh(selected=row.rule is self.selected)

    def _select_rule(self, rule: RecolorRule) -> None:
        self.selected = rule
        self._sync_ui_from_rule()
        self._refresh_rows()
        if rule.is_identity():
            self._show_selection_view()
        self._request_preview()
        self._report_selection()

    def _toggle_rule(self, rule: RecolorRule) -> None:
        self._push_undo("enable rule" if not rule.enabled else "disable rule")
        rule.enabled = not rule.enabled
        self._refresh_rows()
        self._request_preview()

    def _move_rule(self, rule: RecolorRule, delta: int) -> None:
        i = self.rules.index(rule)
        j = i + delta
        if not (0 <= j < len(self.rules)):
            return
        self._push_undo("reorder rules")
        self.rules[i], self.rules[j] = self.rules[j], self.rules[i]
        self._rebuild_rule_list()
        self._request_preview()

    def _delete_rule(self, rule: RecolorRule) -> None:
        self._push_undo("delete rule")
        self.rules.remove(rule)
        if self.selected is rule:
            self.selected = self.rules[-1] if self.rules else None
        self._rebuild_rule_list()
        if self.selected is not None:
            self._sync_ui_from_rule()
        else:
            self._sync_target_swatch_highlight()
            self._refresh_region_controls()
        self._request_preview()

    def _sync_ui_from_rule(self) -> None:
        rule = self.selected
        if rule is None:
            return
        self._suspend_rule_sync = True
        try:
            self._mode.set(rule.mode.value)
            self._tolerance.set(rule.tolerance)
            self._feather.set(rule.feather)
            self._lightness.set(rule.lightness_amount)
            self._chroma.set(rule.chroma_amount)
            self._opacity.set(rule.opacity)
            for widget, flag in (
                (self._contiguous, rule.contiguous),
                (self._smooth, rule.smooth_edges),
                (self._lock_cluster, rule.lock_to_cluster),
                (self._match_all, rule.match_all),
            ):
                widget.select() if flag else widget.deselect()
            self._editor.set_color(
                tuple(int(v, 16) for v in _hex_pairs(lab_to_hex(rule.target_lab))),
                "external",
            )
        finally:
            self._suspend_rule_sync = False

        self._sync_target_swatch_highlight()
        self._refresh_region_controls()

    def _sync_rule_from_ui(self) -> None:
        rule = self.selected
        if rule is None or self._suspend_rule_sync:
            return
        self._push_undo("adjust rule", coalesce_key=("settings", id(rule)))
        rule.mode = MatchMode(self._mode.get())
        rule.tolerance = self._tolerance.get()
        rule.feather = self._feather.get()
        rule.lightness_amount = self._lightness.get()
        rule.chroma_amount = self._chroma.get()
        rule.opacity = self._opacity.get()
        rule.contiguous = bool(self._contiguous.get())
        rule.smooth_edges = bool(self._smooth.get())
        rule.lock_to_cluster = bool(self._lock_cluster.get())
        rule.match_all = bool(self._match_all.get())
        self._refresh_rows()
        self._request_preview()
        self._report_selection()

    def _on_target_color(self, rgb) -> None:
        if self.selected is None or self._suspend_rule_sync:
            return
        self._push_undo(
            "pick color", coalesce_key=("target", id(self.selected))
        )
        self.selected.target_lab = rgb8_to_lab(rgb)
        self._sync_target_swatch_highlight()
        if not self.selected.is_identity():
            self._leave_selection_view()
        self._refresh_rows()
        self._request_preview()
        self._report_selection()

    # ---------------------------------------------------------- eyedropper

    def _toggle_eyedropper(self) -> None:
        active = self._eyedropper_btn.cget("fg_color") != "#2e5f2e"
        self._set_eyedropper(active)

    def _set_eyedropper(self, active: bool) -> None:
        if active and self.canvas.draw_mode is not None:
            self.canvas.set_draw_mode(None)
            self._refresh_region_controls()
        self.canvas.set_eyedropper(active)
        self._eyedropper_btn.configure(
            fg_color="#2e5f2e" if active else ctk.ThemeManager.theme["CTkButton"]["fg_color"],
            hover_color="#3a763a" if active else ctk.ThemeManager.theme["CTkButton"]["hover_color"],
        )

    def _on_canvas_pick(self, x: int, y: int, kind: str) -> None:
        if self.ctx is None:
            return
        rgb = self.ctx.sample_rgb(x, y, radius=0 if kind == "exact" else 1)

        if kind == "target":
            self._editor.set_color(rgb, "picker")
            self._status.configure(text=f"Target set to {rgb_to_hex(rgb)}")
            return

        # A click is discrete, so break any in-flight coalescing window.
        self._coalesce_key = None

        h, w = self.ctx.shape_full
        seed_uv = (x / max(w - 1, 1), y / max(h - 1, 1))

        source = rgb8_to_lab(rgb)

        if self.selected is None:
            self._append_rule(
                RecolorRule(
                    source_lab=source, target_lab=source.copy(), seed_uv=seed_uv
                )
            )
        else:
            self._push_undo("pick source color")
            was_identity = self.selected.is_identity()
            self.selected.source_lab = source
            self.selected.seed_uv = seed_uv
            self.selected.cluster_id = None
            self.selected.lock_to_cluster = False
            self._lock_cluster.deselect()
            # Re-picking the source of an untouched rule keeps it pending.
            if was_identity:
                self.selected.target_lab = source.copy()
                self._sync_ui_from_rule()
                self._show_selection_view()
            self._refresh_rows()
            self._request_preview()
            self._report_selection()

    # ------------------------------------------------------------- preview

    def _on_view_change(self, value: str) -> None:
        self._view_mode = value
        self._auto_selection = False  # an explicit choice sticks
        self._request_preview()

    def _fit(self) -> None:
        self.canvas.fit()

    def _request_preview(self) -> None:
        """Mark dirty only. The 16 ms pump does at most one render per tick, so
        a fast slider drag can never queue hundreds of renders."""
        if not self._alive:
            return
        self._preview_dirty = True
        self._showing_hq = False
        if self._settle_after is not None:
            try:
                self.after_cancel(self._settle_after)
            except tk.TclError:
                pass
        self._settle_after = self.after(SETTLE_MS, self._render_full)

    def _pump_preview(self) -> None:
        if not self._alive:
            return
        if self._preview_dirty:
            self._preview_dirty = False
            self._render_preview()
        self._pump_after = self.after(16, self._pump_preview)

    def _current_frame(self, use_proxy: bool) -> np.ndarray | None:
        if self.ctx is None:
            return None

        if self._view_mode == "Original":
            return self.ctx.frame(use_proxy)

        if self._view_mode == "Selection":
            rule = self.selected
            if rule is None:
                return self.ctx.frame(use_proxy)
            from ..recolor import selection_overlay

            return selection_overlay(self.ctx, rule, use_proxy=use_proxy)

        frame = render(
            self.ctx,
            self.rules,
            use_proxy=use_proxy,
            gamut_safe=bool(self._gamut.get()),
            weight_cache=self._weight_cache,
        )

        if self._hover_swatch is not None and self.ctx.clusters is not None:
            frame = self._tint_cluster(frame, use_proxy, self._hover_swatch)
        return frame

    def _tint_cluster(self, frame: np.ndarray, use_proxy: bool, swatch: Swatch):
        ps = self.ctx.pixels(use_proxy)
        labels = self.ctx.clusters.labels_for(ps)
        mask = labels == swatch.index
        if not mask.any():
            return frame
        out = frame.copy()
        flat = out.reshape(-1, 4)
        idx = ps.idx[mask]
        base = flat[idx, :3].astype(np.float32)
        tint = np.array([255.0, 0.0, 200.0], np.float32)
        flat[idx, :3] = np.rint(base + 0.5 * (tint - base)).astype(np.uint8)
        return out

    def _render_preview(self) -> None:
        frame = self._current_frame(use_proxy=True)
        if frame is not None:
            self.canvas.set_frame(frame)

    def _render_full(self) -> None:
        self._settle_after = None
        if not self._alive or self.ctx is None or self._showing_hq:
            return
        frame = self._current_frame(use_proxy=False)
        if frame is not None:
            # Set before drawing: the canvas reports back synchronously, and the
            # badge's color is read from this flag.
            self._showing_hq = True
            self.canvas.set_frame(frame)

    def _on_zoom_slider(self, value: float) -> None:
        """Slider position -> canvas zoom.

        The slider speaks magnification against the render, so mid-edit it has
        to be converted through whatever frame the canvas is holding: 100% on a
        half-size proxy is a canvas zoom of 2.
        """
        if self._suspend_zoom_sync or self.ctx is None:
            return
        frame_w = self.canvas.frame_width
        if not frame_w:
            return
        magnification = 2.0 ** value
        self.canvas.zoom_to(magnification * self.ctx.shape_full[1] / frame_w)

    def _on_canvas_view(self, width: int, _height: int, zoom: float) -> None:
        """Report the zoom, measured against the render's own resolution.

        Not the canvas's own zoom factor: that one is relative to whatever
        frame it currently holds, and the proxy that stands in mid-edit is half
        size, so it would read 200% at the moment nothing on screen changed
        size. Displayed width over the render's true width is the honest
        magnification, and it holds still across the proxy swap.

        The color answers the other half: green once the pixels come from the
        full-resolution render, gray while the proxy is standing in.
        """
        if self.ctx is not None:
            zoom = width / max(self.ctx.shape_full[1], 1)
        self._hq.configure(
            text=f"{zoom * 100:.0f}%",
            text_color="#5fbf5f" if self._showing_hq else "#8a8a8a",
        )

        # Follow the wheel, Fit and window resizes. Guarded because moving the
        # slider lands back here, and writing to it again mid-drag would fight
        # the pointer.
        self._suspend_zoom_sync = True
        try:
            self._zoom_slider.set(
                float(np.clip(np.log2(max(zoom, 1e-6)), ZOOM_LOG_MIN, ZOOM_LOG_MAX))
            )
        finally:
            self._suspend_zoom_sync = False

    # -------------------------------------------------------------- export

    def _export_frame(self) -> np.ndarray | None:
        if self.ctx is None:
            return None
        return render(
            self.ctx, self.rules, use_proxy=False, gamut_safe=bool(self._gamut.get())
        )

    def _default_export_path(self) -> Path:
        character = self._character.get()
        code = self.library.code_for(character)
        skin = self._skin.get()

        folder = EXPORT_DIR / character
        base = f"T_{code}_{skin}_Custom"
        n = 1
        while (folder / f"{base}{n}_CSP.png").exists():
            n += 1
        return folder / f"{base}{n}_CSP.png"

    def _write(self, path: Path) -> None:
        frame = self._export_frame()
        if frame is None:
            messagebox.showinfo("Nothing to export", "Load a render first.")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(frame, "RGBA").save(path)
        self._status.configure(text=f"Exported {path}")
        messagebox.showinfo("Exported", str(path))

    def _export(self) -> None:
        if self.ctx is None:
            return
        self._write(self._default_export_path())

    def _export_as(self) -> None:
        if self.ctx is None:
            return
        default = self._default_export_path()
        chosen = filedialog.asksaveasfilename(
            title="Export recolored render",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
            initialdir=str(default.parent if default.parent.exists() else EXPORT_DIR.parent),
            initialfile=default.name,
        )
        if chosen:
            self._write(Path(chosen))

    def _save_preset(self) -> None:
        if not self.rules:
            messagebox.showinfo("No rules", "Add at least one rule first.")
            return
        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        chosen = filedialog.asksaveasfilename(
            title="Save rule preset",
            defaultextension=".json",
            filetypes=[("Preset", "*.json")],
            initialdir=str(PRESETS_DIR),
            initialfile=f"{self._character.get()}_{self._skin.get()}.json",
        )
        if not chosen:
            return
        source = self.ctx.path.name if self.ctx else ""
        save_preset(Path(chosen), self.rules, source=source)
        self._status.configure(text=f"Saved preset {Path(chosen).name}")

    def _load_preset(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Load rule preset",
            filetypes=[("Preset", "*.json")],
            initialdir=str(PRESETS_DIR if PRESETS_DIR.exists() else EXPORT_DIR.parent),
        )
        if not chosen:
            return
        try:
            loaded = load_preset(Path(chosen))
        except Exception as exc:
            messagebox.showerror("Could not load preset", str(exc))
            return
        self._push_undo("load preset")
        self.rules = loaded
        self.selected = self.rules[-1] if self.rules else None
        self._weight_cache.clear()
        self._rebuild_rule_list()
        if self.selected is not None:
            self._sync_ui_from_rule()
        self._request_preview()
        self._status.configure(text=f"Loaded {len(self.rules)} rule(s)")


def _hex_pairs(value: str):
    text = value.lstrip("#")
    return (text[0:2], text[2:4], text[4:6])
