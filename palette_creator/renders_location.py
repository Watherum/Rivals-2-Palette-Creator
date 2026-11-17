"""Find the render library, so a user never has to redownload it from the wiki.

At start-up the app looks for renders in this order:

1. ``Resources/Character_Renders_Source`` next to the exe (or the repo root
   when running from source) - where the release zip puts them;
2. a folder the user picked on an earlier launch, kept in settings.json;
3. failing both, it asks the user where they are.

Cancelling the prompt keeps the default next-to-the-exe folder, which is where
the Downloads tab will save to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

_SETTINGS_KEY = "renders_dir"


def has_renders(folder: Path) -> bool:
    """True if any character subfolder of ``folder`` holds a PNG.

    A bare or empty Character_Renders_Source does not count: the release
    folder structure can exist without anything worth pointing at.
    """
    try:
        with os.scandir(folder) as entries:
            subdirs = [e.path for e in entries if e.is_dir()]
        for sub in subdirs:
            with os.scandir(sub) as files:
                if any(f.is_file() and f.name.lower().endswith(".png") for f in files):
                    return True
    except OSError:
        return False
    return False


def normalize_choice(folder: Path | str) -> Path | None:
    """Turn whatever folder the user picked into the renders root.

    People will pick the renders folder itself, the Resources folder above it,
    or the extracted release folder above that; accept all three.
    """
    folder = Path(folder)
    for candidate in (
        folder,
        folder / "Character_Renders_Source",
        folder / "Resources" / "Character_Renders_Source",
    ):
        if has_renders(candidate):
            return candidate.resolve()
    return None


def _read_settings() -> dict:
    try:
        data = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def saved_location() -> Path | None:
    value = _read_settings().get(_SETTINGS_KEY)
    return Path(value) if isinstance(value, str) and value else None


def remember(folder: Path | None) -> None:
    """Save (or with None, forget) the chosen folder. Best effort: an exe in a
    read-only folder still works this session, it just asks again next time."""
    data = _read_settings()
    if folder is None:
        data.pop(_SETTINGS_KEY, None)
    else:
        data[_SETTINGS_KEY] = str(folder)
    try:
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def use(folder: Path) -> None:
    config.SOURCE_DIR = Path(folder)


def resolve() -> bool:
    """Point config.SOURCE_DIR at the renders without asking anyone.

    Returns True if renders were found, False if the caller should ask.
    """
    if has_renders(config.DEFAULT_SOURCE_DIR):
        use(config.DEFAULT_SOURCE_DIR)
        return True
    saved = saved_location()
    if saved is not None and has_renders(saved):
        use(saved)
        return True
    use(config.DEFAULT_SOURCE_DIR)
    return False


def choose_folder(parent=None, *, intro: bool = True) -> Path | None:
    """Ask the user for their renders folder until they pick a valid one or
    cancel. Returns the renders root, or None if they cancelled."""
    from tkinter import filedialog, messagebox

    if intro:
        go = messagebox.askokcancel(
            "Find your character renders",
            "No character renders were found next to the app.\n\n"
            "If you already have them (for example from the release zip), "
            "press OK and select the folder they are in. Any of these work:\n"
            "  - the extracted release folder\n"
            "  - its Resources folder\n"
            "  - Resources\\Character_Renders_Source\n\n"
            "Press Cancel to skip; you can download them from the Downloads "
            "tab instead.",
            parent=parent,
        )
        if not go:
            return None

    initial = Path.home()
    while True:
        picked = filedialog.askdirectory(
            title="Select the folder with your character renders",
            initialdir=str(initial),
            mustexist=True,
            parent=parent,
        )
        if not picked:
            return None
        found = normalize_choice(picked)
        if found is not None:
            return found
        initial = Path(picked)
        if not messagebox.askretrycancel(
            "No renders there",
            f"No character renders were found in\n{picked}\n\n"
            "Pick the folder that holds the character folders "
            "(Absa, Clairen, ...) or one of its parents.",
            parent=parent,
        ):
            return None


def locate_at_startup(parent=None) -> None:
    """resolve(), and if that finds nothing, ask. Remembers the answer."""
    if resolve():
        return
    found = choose_folder(parent)
    if found is None:
        return
    use(found)
    remember(found)
