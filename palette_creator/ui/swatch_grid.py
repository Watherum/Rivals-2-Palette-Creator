"""Grid of extracted palette swatches.

The same widget serves both panels: on the left a click picks the color to
*replace*, on the right the one to replace it *with*. It never scrolls on its
own -- both panels already sit inside a scroll region, and CustomTkinter routes
a wheel event to every scrollable frame under the pointer, so a nested one
would drag both at once.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from ..palette_extract import ClusterResult, Swatch
from .widgets import FONT_SMALL, Tooltip

COLUMNS = 5
CHIP = 34

IDLE_BORDER = "#5a5a5a"
ACTIVE_BORDER = "#f0f0f0"


class SwatchStrip(ctk.CTkFrame):
    def __init__(
        self,
        master,
        *,
        on_select: Callable[[Swatch], None] | None = None,
        on_hover: Callable[[Swatch | None], None] | None = None,
        columns: int = COLUMNS,
        chip_size: int = CHIP,
        flag_spans: bool = False,
        tooltip_suffix: str = "",
        **kw,
    ):
        super().__init__(master, **kw)
        self.on_select = on_select
        self.on_hover = on_hover
        self._columns = columns
        self._chip_size = chip_size
        self._flag_spans = flag_spans
        self._tooltip_suffix = tooltip_suffix
        self._chips: list[tk.Canvas] = []
        self._hexes: list[str] = []
        self._active = -1
        self._empty: ctk.CTkLabel | None = None

    def clear(self) -> None:
        for chip in self._chips:
            chip.destroy()
        self._chips = []
        self._hexes = []
        self._active = -1
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None

    def show_message(self, text: str) -> None:
        self.clear()
        self._empty = ctk.CTkLabel(
            self, text=text, font=FONT_SMALL, text_color="#999",
            wraplength=240, justify="left",
        )
        self._empty.grid(row=0, column=0, padx=8, pady=8, sticky="w")

    def set_result(self, result: ClusterResult | None) -> None:
        self.clear()
        if result is None or not result.swatches:
            self.show_message("No palette extracted yet.")
            return

        for i in range(self._columns):
            self.grid_columnconfigure(i, weight=1)

        size = self._chip_size
        for i, swatch in enumerate(result.swatches):
            chip = tk.Canvas(
                self,
                width=size,
                height=size,
                highlightthickness=1,
                highlightbackground=IDLE_BORDER,
                bd=0,
                cursor="hand2",
            )
            chip.configure(bg=swatch.hex)
            chip.grid(row=i // self._columns, column=i % self._columns, padx=3, pady=3)

            if swatch.spans_materials and self._flag_spans:
                # A high-spread cluster covers more than one material; flag it
                # rather than letting the user wonder why selection leaks.
                chip.create_oval(
                    size - 11, 3, size - 3, 11, fill="#ffcc00", outline="#000000"
                )

            tip = (
                f"{swatch.hex}\n"
                f"{swatch.fraction * 100:.1f}% of the render\n"
                f"spread {swatch.spread:.1f}  ->  tolerance {swatch.suggested_tolerance:.0f}"
            )
            if swatch.spans_materials and self._flag_spans:
                tip += (
                    "\n\nThis color spans multiple materials.\n"
                    "Draw a Box or Lasso around the part you want, or use a "
                    "tighter tolerance."
                )
            if self._tooltip_suffix:
                tip += f"\n\n{self._tooltip_suffix}"
            Tooltip(chip, tip)

            chip.bind("<Button-1>", lambda _e, s=swatch: self._select(s))
            chip.bind("<Enter>", lambda _e, s=swatch: self._hover(s))
            chip.bind("<Leave>", lambda _e: self._hover(None))
            self._chips.append(chip)
            self._hexes.append(swatch.hex.lower())

    def set_active_hex(self, value: str | None) -> None:
        """Ring the chip whose color is currently in use, or none of them."""
        wanted = (value or "").lower()
        index = self._hexes.index(wanted) if wanted in self._hexes else -1
        if index == self._active:
            return
        for i in (self._active, index):
            if 0 <= i < len(self._chips):
                self._chips[i].configure(
                    highlightbackground=ACTIVE_BORDER if i == index else IDLE_BORDER,
                    highlightthickness=2 if i == index else 1,
                )
        self._active = index

    def _select(self, swatch: Swatch) -> None:
        if self.on_select is not None:
            self.on_select(swatch)

    def _hover(self, swatch: Swatch | None) -> None:
        if self.on_hover is not None:
            self.on_hover(swatch)
