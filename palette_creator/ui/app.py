"""Application shell."""

from __future__ import annotations

import sys
import tkinter as tk
import traceback
from tkinter import messagebox

import customtkinter as ctk

from .. import __version__
from .download_tab import DownloadTab
from .recolor_tab import RecolorTab
from .reference_tab import ReferenceTab


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(f"Rivals 2 Palette Creator {__version__} Powered by Watherum")
        self.geometry("1480x920")  # what restoring down returns to
        self.minsize(1180, 760)
        self.after(0, self._maximize)

        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        self.tabs.add("Recolor")
        self.tabs.add("Reference")
        self.tabs.add("Downloads")

        self.recolor_tab = RecolorTab(self.tabs.tab("Recolor"), app=self)
        self.recolor_tab.pack(fill="both", expand=True)

        self.reference_tab = ReferenceTab(self.tabs.tab("Reference"), app=self)
        self.reference_tab.pack(fill="both", expand=True)

        self.download_tab = DownloadTab(self.tabs.tab("Downloads"), app=self)
        self.download_tab.pack(fill="both", expand=True)

        self.report_callback_exception = self._on_tk_error
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_shortcuts()

    def _maximize(self) -> None:
        """Open maximized. Deferred by one tick so the window exists first.

        'zoomed' is the Windows and macOS spelling; X11 window managers want
        the -zoomed attribute instead, and on the ones that support neither an
        ordinary 1480x920 window is a fine outcome -- not a crash on startup.
        """
        for attempt in (
            lambda: self.state("zoomed"),
            lambda: self.attributes("-zoomed", True),
        ):
            try:
                attempt()
                return
            except tk.TclError:
                continue

    def _bind_shortcuts(self) -> None:
        """Global undo/redo.

        bind_all so the shortcut works wherever focus sits -- Tk entries have no
        undo of their own, so there is nothing to shadow.
        """
        for seq in ("<Control-z>", "<Control-Z>"):
            self.bind_all(seq, self._undo)
        for seq in ("<Control-r>", "<Control-R>", "<Control-y>", "<Control-Y>"):
            self.bind_all(seq, self._redo)

    def _history_target(self):
        """Only the Recolor tab has an edit history."""
        try:
            if self.tabs.get() != "Recolor":
                return None
        except Exception:
            return None
        return self.recolor_tab

    def _undo(self, event=None) -> str:
        tab = self._history_target()
        return tab.undo(event) if tab is not None else "break"

    def _redo(self, event=None) -> str:
        tab = self._history_target()
        return tab.redo(event) if tab is not None else "break"

    def _on_close(self) -> None:
        """Tear the tabs down explicitly so their timers are cancelled before
        the interpreter goes away."""
        for tab in (self.recolor_tab, self.reference_tab, self.download_tab):
            try:
                tab.destroy()
            except Exception:
                pass
        self.destroy()

    def _on_tk_error(self, exc_type, value, tb) -> None:
        """Surface widget-callback exceptions instead of letting Tk swallow
        them into stderr, which pythonw.exe discards."""
        traceback.print_exception(exc_type, value, tb)
        if getattr(sys, "frozen", False):
            # The exe has no console and no run_debug.bat, so the dialog is
            # the only place the traceback can go.
            detail = "".join(traceback.format_exception(exc_type, value, tb))[-1500:]
        else:
            detail = "Run run_debug.bat to see the full traceback."
        messagebox.showerror(
            "Unexpected error", f"{exc_type.__name__}: {value}\n\n{detail}"
        )


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        print(f"tkinter is unavailable: {exc}", file=sys.stderr)
        print("Re-run the Python installer and enable 'tcl/tk and IDLE'.", file=sys.stderr)
        return 1

    _locate_renders()
    App().mainloop()
    return 0


def _locate_renders() -> None:
    """Find the renders before the window is built, asking if they are not
    next to the app. Runs on a throwaway hidden root so the dialogs have an
    owner; the smoke test builds App() directly and so never blocks here."""
    from ..renders_location import locate_at_startup, resolve

    if resolve():
        return
    root = tk.Tk()
    root.withdraw()
    try:
        locate_at_startup(root)
    finally:
        root.destroy()
