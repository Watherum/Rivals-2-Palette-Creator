"""Small shared widgets and Tk helpers."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

FONT_MONO = ("Consolas", 11)
FONT_SMALL = ("Segoe UI", 11)
FONT_LABEL = ("Segoe UI", 12, "bold")


class LabeledSlider(ctk.CTkFrame):
    """A slider with a caption and a live numeric readout."""

    def __init__(
        self,
        master,
        text: str,
        *,
        from_: float = 0.0,
        to: float = 1.0,
        value: float = 0.0,
        steps: int | None = None,
        fmt: str = "{:.2f}",
        command: Callable[[float], None] | None = None,
        **kw,
    ):
        super().__init__(master, fg_color="transparent", **kw)
        self._fmt = fmt
        self._command = command

        self.grid_columnconfigure(0, weight=1)
        self._caption = ctk.CTkLabel(self, text=text, font=FONT_SMALL, anchor="w")
        self._caption.grid(row=0, column=0, sticky="ew")
        self._value = ctk.CTkLabel(
            self, text=fmt.format(value), font=FONT_MONO, anchor="e", width=52
        )
        self._value.grid(row=0, column=1, sticky="e")

        self._slider = ctk.CTkSlider(
            self,
            from_=from_,
            to=to,
            number_of_steps=steps,
            command=self._on_change,
            height=16,
        )
        self._slider.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self._slider.set(value)

        self._text_color = self._caption.cget("text_color")

    def set_enabled(self, enabled: bool) -> None:
        """Gray out the whole control, caption and readout included.

        Disabling only the slider leaves a fully lit label above a dead track,
        which reads as a broken widget rather than an inapplicable one.
        """
        self._slider.configure(state="normal" if enabled else "disabled")
        color = self._text_color if enabled else "#6a6a6a"
        self._caption.configure(text_color=color)
        self._value.configure(text_color=color)

    def _on_change(self, value: float) -> None:
        self._value.configure(text=self._fmt.format(value))
        if self._command is not None:
            self._command(value)

    def get(self) -> float:
        return float(self._slider.get())

    def set(self, value: float, *, notify: bool = False) -> None:
        self._slider.set(value)
        self._value.configure(text=self._fmt.format(value))
        if notify and self._command is not None:
            self._command(value)

    def configure_command(self, command: Callable[[float], None] | None) -> None:
        self._command = command


class Swatch(tk.Canvas):
    """A flat color chip. Plain tk.Canvas so the fill is exact."""

    def __init__(self, master, *, size: int = 22, color: str = "#000000", **kw):
        super().__init__(
            master,
            width=size,
            height=size,
            highlightthickness=1,
            highlightbackground="#5a5a5a",
            bd=0,
            **kw,
        )
        self._size = size
        self._rect = self.create_rectangle(0, 0, size, size, width=0, fill=color)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        try:
            self.itemconfigure(self._rect, fill=color)
            self.configure(bg=color)
        except tk.TclError:
            pass


class Tooltip:
    """Hover tooltip. Tk has no built-in, and several controls here need one.

    Sits below its control by default, and flips above when that would put it
    off the bottom of the screen -- which the tall tips on the controls near
    the foot of the left panel otherwise do.
    """

    GAP = 6
    """Pixels between the control and its tip, on whichever side it lands."""

    def __init__(self, widget, text: str, delay: int = 550):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text: str) -> None:
        self.text = text

    def _schedule(self, _event=None) -> None:
        self._cancel()
        if self.text:
            self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        if self._window is not None or not self.text:
            return
        try:
            wx = self.widget.winfo_rootx()
            wy = self.widget.winfo_rooty()
            wh = self.widget.winfo_height()
        except tk.TclError:
            return

        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        # Built hidden, then measured, then placed. Deciding whether the tip
        # fits below its control needs the tip's own height, and that is not
        # known until the label has been laid out -- placing first and moving
        # afterwards would show it in the wrong spot for a frame.
        window.wm_withdraw()
        tk.Label(
            window,
            text=self.text,
            justify="left",
            background="#1f1f1f",
            foreground="#e6e6e6",
            relief="solid",
            borderwidth=1,
            font=FONT_SMALL,
            padx=8,
            pady=4,
            wraplength=320,
        ).pack()
        window.update_idletasks()

        tw, th = window.winfo_reqwidth(), window.winfo_reqheight()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()

        x = wx + 12
        y = wy + wh + self.GAP
        if y + th > sh:
            # No room underneath -- sit above the control instead of hanging
            # off the bottom of the screen where the text cannot be read.
            y = max(wy - th - self.GAP, 0)
        # Same idea sideways, except a tip that overhangs is nudged back rather
        # than flipped: there is nothing on the other side of the pointer to
        # flip to, and clamping keeps the whole tip on screen either way.
        x = max(min(x, sw - tw), 0)

        window.wm_geometry(f"+{x}+{y}")
        window.wm_deiconify()
        self._window = window

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None


def section_label(master, text: str) -> ctk.CTkLabel:
    return ctk.CTkLabel(master, text=text, font=FONT_LABEL, anchor="w")
