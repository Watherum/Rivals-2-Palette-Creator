"""The Downloads tab: drive the wiki downloader with progress and cancel."""

from __future__ import annotations

import queue
import threading
import tkinter as tk

import customtkinter as ctk

from .. import config
from ..config import DOWNLOAD_DELAY
from ..downloader import (
    DownloadOptions,
    ProgressEvent,
    discover_characters,
    make_session,
    run_download,
)
from ..renders_location import choose_folder, remember, use
from .widgets import FONT_MONO, FONT_SMALL, LabeledSlider, Tooltip, section_label

MAX_LOG_LINES = 2000


class DownloadTab(ctk.CTkFrame):
    def __init__(self, master, app, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.app = app

        self._events: queue.Queue[ProgressEvent] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._checkboxes: dict[str, ctk.CTkCheckBox] = {}
        self._running = False
        self._alive = True
        self._pump_after: str | None = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_left()
        self._build_right()
        self._pump_after = self.after(60, self._pump)

    def destroy(self) -> None:
        """Stop the worker and cancel the drain before the widgets go away."""
        self._alive = False
        self._cancel.set()
        if self._pump_after is not None:
            try:
                self.after_cancel(self._pump_after)
            except tk.TclError:
                pass
            self._pump_after = None
        super().destroy()

    # -- layout -------------------------------------------------------------

    def _build_left(self) -> None:
        panel = ctk.CTkFrame(self, width=300)
        panel.grid(row=0, column=0, sticky="nsw", padx=(8, 4), pady=8)
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(3, weight=1)

        section_label(panel, "Characters").grid(
            row=0, column=0, sticky="ew", padx=10, pady=(10, 6)
        )

        ctk.CTkButton(
            panel, text="Discover from wiki", height=30, command=self._discover
        ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))

        picks = ctk.CTkFrame(panel, fg_color="transparent")
        picks.grid(row=2, column=0, sticky="ew", padx=10)
        picks.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(
            picks, text="All", height=24, command=lambda: self._set_all(True)
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ctk.CTkButton(
            picks, text="None", height=24, command=lambda: self._set_all(False)
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        self._char_list = ctk.CTkScrollableFrame(panel)
        self._char_list.grid(row=3, column=0, sticky="nsew", padx=10, pady=8)
        self._char_hint = ctk.CTkLabel(
            self._char_list,
            text="Press 'Discover from wiki'.\nLeave everything unchecked to\nfetch all characters.",
            font=FONT_SMALL,
            text_color="#999",
            justify="left",
        )
        self._char_hint.grid(row=0, column=0, sticky="w", padx=4, pady=4)

        section_label(panel, "Options").grid(
            row=4, column=0, sticky="ew", padx=10, pady=(4, 4)
        )

        self._overwrite = ctk.CTkCheckBox(
            panel, text="Re-download existing files", font=FONT_SMALL
        )
        self._overwrite.grid(row=5, column=0, sticky="w", padx=12, pady=2)

        self._dry_run = ctk.CTkCheckBox(
            panel, text="Dry run (list only, no writes)", font=FONT_SMALL
        )
        self._dry_run.grid(row=6, column=0, sticky="w", padx=12, pady=2)
        Tooltip(self._dry_run, "Show what would be fetched without downloading it.")

        self._delay = LabeledSlider(
            panel, "Delay between files (s)", from_=0.0, to=2.0,
            value=DOWNLOAD_DELAY, fmt="{:.2f}",
        )
        self._delay.grid(row=7, column=0, sticky="ew", padx=12, pady=(8, 12))

    def _build_right(self) -> None:
        panel = ctk.CTkFrame(self)
        panel.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(5, weight=1)

        where = ctk.CTkFrame(panel, fg_color="transparent")
        where.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        where.grid_columnconfigure(0, weight=1)
        self._dest_label = ctk.CTkLabel(
            where,
            text="",
            font=FONT_SMALL,
            anchor="w",
            text_color="#999",
            wraplength=640,
            justify="left",
        )
        self._dest_label.grid(row=0, column=0, sticky="ew")
        self._locate_btn = ctk.CTkButton(
            where, text="Use existing renders...", width=150, height=26,
            command=self._locate_existing,
        )
        self._locate_btn.grid(row=0, column=1, padx=(8, 0))
        Tooltip(
            self._locate_btn,
            "Already have the renders (e.g. from the release zip)? Point the app "
            "at them here instead of downloading them again.",
        )
        self._show_dest()

        buttons = ctk.CTkFrame(panel, fg_color="transparent")
        buttons.grid(row=1, column=0, sticky="ew", padx=12)
        buttons.grid_columnconfigure(2, weight=1)

        self._start = ctk.CTkButton(
            buttons, text="Start download", width=140, height=34, command=self._start_run
        )
        self._start.grid(row=0, column=0, padx=(0, 6))

        self._cancel_btn = ctk.CTkButton(
            buttons, text="Cancel", width=90, height=34, state="disabled",
            fg_color="#7a2c2c", hover_color="#9b3838", command=self._request_cancel,
        )
        self._cancel_btn.grid(row=0, column=1)

        self._summary = ctk.CTkLabel(buttons, text="", font=FONT_SMALL, anchor="e")
        self._summary.grid(row=0, column=2, sticky="e")

        self._overall_label = ctk.CTkLabel(
            panel, text="Overall", font=FONT_SMALL, anchor="w"
        )
        self._overall_label.grid(row=2, column=0, sticky="ew", padx=12, pady=(10, 0))
        self._overall = ctk.CTkProgressBar(panel)
        self._overall.set(0)
        self._overall.grid(row=3, column=0, sticky="ew", padx=12, pady=(2, 6))

        self._current = ctk.CTkProgressBar(panel)
        self._current.set(0)
        self._current.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 8))

        self._log = ctk.CTkTextbox(panel, font=FONT_MONO, wrap="none")
        self._log.grid(row=5, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._log.configure(state="disabled")

    # -- renders folder -----------------------------------------------------

    def _show_dest(self) -> None:
        self._dest_label.configure(text=f"Renders are read from and saved to  {config.SOURCE_DIR}")

    def _locate_existing(self) -> None:
        if self._running:
            return
        found = choose_folder(self, intro=False)
        if found is None:
            return
        use(found)
        remember(found)
        self._show_dest()
        self._append(f"Using renders in {found}")
        for tab in (self.app.recolor_tab, self.app.reference_tab):
            try:
                tab.refresh_library()
            except Exception:
                pass

    # -- character list -----------------------------------------------------

    def _discover(self) -> None:
        self._append("Discovering characters from dragdown.wiki...")

        def work():
            try:
                names = discover_characters(make_session())
            except Exception as exc:
                self._events.put(ProgressEvent("error", message=f"Discovery failed: {exc}"))
                return
            self._events.put(
                ProgressEvent("log", message="__CHARS__" + "\x1f".join(names))
            )

        threading.Thread(target=work, daemon=True).start()

    def _set_characters(self, names: list[str]) -> None:
        for box in self._checkboxes.values():
            box.destroy()
        self._checkboxes = {}
        self._char_hint.grid_remove()

        for i, name in enumerate(names):
            box = ctk.CTkCheckBox(self._char_list, text=name, font=FONT_SMALL)
            box.grid(row=i, column=0, sticky="w", padx=4, pady=2)
            self._checkboxes[name] = box

        self._append(f"Found {len(names)} character(s).")

    def _set_all(self, value: bool) -> None:
        for box in self._checkboxes.values():
            box.select() if value else box.deselect()

    def _selected_characters(self) -> tuple[str, ...] | None:
        chosen = tuple(n for n, b in self._checkboxes.items() if b.get())
        return chosen or None

    # -- run ----------------------------------------------------------------

    def _start_run(self) -> None:
        if self._running:
            return
        self._running = True
        self._cancel.clear()
        self._start.configure(state="disabled", text="Downloading...")
        self._locate_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._summary.configure(text="")
        self._overall.set(0)
        self._current.set(0)

        options = DownloadOptions(
            characters=self._selected_characters(),
            overwrite=bool(self._overwrite.get()),
            dry_run=bool(self._dry_run.get()),
            delay=self._delay.get(),
        )
        self._append(
            f"Starting{' (dry run)' if options.dry_run else ''}: "
            f"{', '.join(options.characters) if options.characters else 'all characters'}"
        )

        def work():
            run_download(options, progress=self._events.put, cancel=self._cancel)

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _request_cancel(self) -> None:
        self._cancel.set()
        self._cancel_btn.configure(state="disabled", text="Cancelling...")
        self._append("Cancel requested; finishing the current file...")

    # -- the single UI-thread drain -----------------------------------------

    def _pump(self) -> None:
        """Drain worker events. The only place download output touches widgets."""
        if not self._alive:
            return
        try:
            while True:
                event = self._events.get_nowait()
                self._handle(event)
        except queue.Empty:
            pass
        self._pump_after = self.after(60, self._pump)

    def _handle(self, e: ProgressEvent) -> None:
        if e.kind == "log":
            if e.message.startswith("__CHARS__"):
                self._set_characters(e.message[len("__CHARS__") :].split("\x1f"))
            else:
                self._append(e.message)

        elif e.kind == "char_start":
            self._append(f"\n=== {e.character} ===")
            self._current.set(0)
            if e.chars_total:
                self._overall.set(e.chars_done / e.chars_total)
                self._overall_label.configure(
                    text=f"Overall  {e.chars_done}/{e.chars_total} characters"
                )

        elif e.kind == "found":
            self._append(f"  {e.total} render(s) on the wiki")

        elif e.kind == "download":
            suffix = f" {e.message}" if e.message else ""
            self._append(f"  [get ] {e.filename}{suffix}")
            if e.total:
                self._current.set(e.current / e.total)

        elif e.kind == "skip":
            if e.total:
                self._current.set(e.current / e.total)

        elif e.kind == "error":
            target = f"{e.filename}: " if e.filename else ""
            self._append(f"  [ERR ] {target}{e.message}")

        elif e.kind == "char_done":
            if e.chars_total:
                self._overall.set(e.chars_done / e.chars_total)

        elif e.kind == "done":
            self._finish(e.message)

    def _finish(self, message: str) -> None:
        if not self._alive:
            return
        self._running = False
        self._overall.set(1.0)
        self._current.set(1.0)
        self._start.configure(state="normal", text="Start download")
        self._locate_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled", text="Cancel")
        self._summary.configure(text=message)
        self._append(f"\nDone: {message}")

        # New renders should show up in the Recolor tab without a restart.
        try:
            self.app.recolor_tab.refresh_library()
        except Exception:
            pass

    def _append(self, text: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n")

        line_count = int(float(self._log.index("end-1c").split(".")[0]))
        if line_count > MAX_LOG_LINES:
            self._log.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")

        self._log.see("end")
        self._log.configure(state="disabled")
