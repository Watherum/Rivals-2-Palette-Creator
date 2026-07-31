"""Grid of extracted palette swatches."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from ..palette_extract import ClusterResult, Swatch
from .widgets import FONT_SMALL, Tooltip

COLUMNS = 5
CHIP = 34


class SwatchGrid(ctk.CTkScrollableFrame):
    def __init__(
        self,
        master,
        *,
        on_select: Callable[[Swatch], None] | None = None,
        on_hover: Callable[[Swatch | None], None] | None = None,
        **kw,
    ):
        super().__init__(master, **kw)
        self.on_select = on_select
        self.on_hover = on_hover
        self._chips: list[tk.Canvas] = []
        self._empty: ctk.CTkLabel | None = None

    def clear(self) -> None:
        for chip in self._chips:
            chip.destroy()
        self._chips = []
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None

    def show_message(self, text: str) -> None:
        self.clear()
        self._empty = ctk.CTkLabel(self, text=text, font=FONT_SMALL, text_color="#999")
        self._empty.grid(row=0, column=0, padx=8, pady=8, sticky="w")

    def set_result(self, result: ClusterResult | None) -> None:
        self.clear()
        if result is None or not result.swatches:
            self.show_message("No palette extracted yet.")
            return

        for i in range(COLUMNS):
            self.grid_columnconfigure(i, weight=1)

        for i, swatch in enumerate(result.swatches):
            chip = tk.Canvas(
                self,
                width=CHIP,
                height=CHIP,
                highlightthickness=1,
                highlightbackground="#5a5a5a",
                bd=0,
                cursor="hand2",
            )
            chip.configure(bg=swatch.hex)
            chip.grid(row=i // COLUMNS, column=i % COLUMNS, padx=3, pady=3)

            if swatch.spans_materials:
                # A high-spread cluster covers more than one material; flag it
                # rather than letting the user wonder why selection leaks.
                chip.create_oval(
                    CHIP - 11, 3, CHIP - 3, 11, fill="#ffcc00", outline="#000000"
                )

            tip = (
                f"{swatch.hex}\n"
                f"{swatch.fraction * 100:.1f}% of the render\n"
                f"spread {swatch.spread:.1f}  ->  tolerance {swatch.suggested_tolerance:.0f}"
            )
            if swatch.spans_materials:
                tip += (
                    "\n\nThis color spans multiple materials.\n"
                    "Use 'This region only' or a tighter tolerance."
                )
            Tooltip(chip, tip)

            chip.bind("<Button-1>", lambda _e, s=swatch: self._select(s))
            chip.bind("<Enter>", lambda _e, s=swatch: self._hover(s))
            chip.bind("<Leave>", lambda _e: self._hover(None))
            self._chips.append(chip)

    def _select(self, swatch: Swatch) -> None:
        if self.on_select is not None:
            self.on_select(swatch)

    def _hover(self, swatch: Swatch | None) -> None:
        if self.on_hover is not None:
            self.on_hover(swatch)
