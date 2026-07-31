"""One row in the rules list: source -> target, with reorder and delete."""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from ..colorspace import lab_to_hex
from ..recolor import RecolorRule
from .widgets import FONT_MONO, FONT_SMALL, Swatch, Tooltip

SELECTED_BG = "#2f5f8f"
NORMAL_BG = "#2b2b2b"


class RuleRow(ctk.CTkFrame):
    def __init__(
        self,
        master,
        rule: RecolorRule,
        *,
        index: int,
        on_select: Callable[[RecolorRule], None],
        on_toggle: Callable[[RecolorRule], None],
        on_move: Callable[[RecolorRule, int], None],
        on_delete: Callable[[RecolorRule], None],
        **kw,
    ):
        super().__init__(master, fg_color=NORMAL_BG, corner_radius=6, **kw)
        self.rule = rule
        self.index = index
        self._on_select = on_select

        self.grid_columnconfigure(2, weight=1)

        self._num = ctk.CTkLabel(self, text=str(index + 1), font=FONT_MONO, width=18)
        self._num.grid(row=0, column=0, padx=(6, 2), pady=6)

        colors = ctk.CTkFrame(self, fg_color="transparent")
        colors.grid(row=0, column=1, padx=2)
        self._src = Swatch(colors, size=20, color=lab_to_hex(rule.source_lab))
        self._src.grid(row=0, column=0)
        ctk.CTkLabel(colors, text="→", font=FONT_SMALL, width=16).grid(
            row=0, column=1
        )
        self._dst = Swatch(colors, size=20, color=lab_to_hex(rule.target_lab))
        self._dst.grid(row=0, column=2)

        self._label = ctk.CTkLabel(self, text="", font=FONT_SMALL, anchor="w")
        self._label.grid(row=0, column=2, sticky="ew", padx=6)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=0, column=3, padx=(0, 4))

        self._eye = ctk.CTkButton(
            buttons,
            text="",
            width=26,
            height=24,
            command=lambda: on_toggle(rule),
        )
        self._eye.grid(row=0, column=0, padx=1)
        Tooltip(self._eye, "Enable or disable this rule")

        up = ctk.CTkButton(
            buttons, text="▲", width=24, height=24, command=lambda: on_move(rule, -1)
        )
        up.grid(row=0, column=1, padx=1)
        Tooltip(up, "Move up (earlier rules win contested pixels)")

        down = ctk.CTkButton(
            buttons, text="▼", width=24, height=24, command=lambda: on_move(rule, 1)
        )
        down.grid(row=0, column=2, padx=1)
        Tooltip(down, "Move down")

        delete = ctk.CTkButton(
            buttons,
            text="✕",
            width=24,
            height=24,
            fg_color="#7a2c2c",
            hover_color="#9b3838",
            command=lambda: on_delete(rule),
        )
        delete.grid(row=0, column=3, padx=1)
        Tooltip(delete, "Delete this rule")

        for widget in (self, self._label, self._num, colors):
            widget.bind("<Button-1>", self._clicked)

        self.refresh()

    def _clicked(self, _event=None) -> None:
        self._on_select(self.rule)

    def refresh(self, *, selected: bool = False) -> None:
        rule = self.rule
        self._num.configure(text=str(self.index + 1))
        self._src.set_color(lab_to_hex(rule.source_lab))
        self._dst.set_color(lab_to_hex(rule.target_lab))

        flags = []
        if rule.contiguous:
            flags.append("region")
        if rule.smooth_edges:
            flags.append("smooth")
        if rule.lock_to_cluster:
            flags.append("locked")
        suffix = f"  [{', '.join(flags)}]" if flags else ""

        if rule.is_identity():
            # Selected but no replacement colour chosen yet, so it changes nothing.
            text = f"pick a color · tol {rule.tolerance:.0f}"
            color = "#ffcc66"
        else:
            text = f"{rule.mode.value}  tol {rule.tolerance:.0f}{suffix}"
            color = "#e6e6e6" if rule.enabled else "#777777"

        self._label.configure(text=text, text_color=color)
        self._eye.configure(
            text="\U0001f441" if rule.enabled else "—",
            fg_color="#2e5f2e" if rule.enabled else "#4a4a4a",
            hover_color="#3a763a" if rule.enabled else "#5a5a5a",
        )
        self.configure(fg_color=SELECTED_BG if selected else NORMAL_BG)
