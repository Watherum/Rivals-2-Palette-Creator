"""Index of the downloaded renders under Resources/Character_Renders_Source.

Renders are named ``T_{Code}_{Skin}_{Palette}_CSP.png``. The character is always
taken from the containing folder, never from the ``T_{Code}`` prefix -- the
``Random`` folder ships files prefixed ``T_Ran`` that would otherwise be
misfiled as Ranno's.
"""

from __future__ import annotations

import os
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .config import CHAR_CODES, NON_CHARACTER_FOLDERS, SOURCE_DIR

# Shown first in their dropdowns; everything else sorts alphabetically after.
PINNED_SKINS = ("Default",)
PINNED_PALETTES = ("Neutral",)


@dataclass(frozen=True)
class RenderFile:
    path: Path
    character: str
    code: str
    skin: str
    palette: str

    @property
    def label(self) -> str:
        return f"{self.character} / {self.skin} / {self.palette}"


def normalize_stem(stem: str) -> str:
    """Percent-decode and NFC-normalize a filename stem.

    ``T_Cla_L%C3%A9on_Red_CSP`` and ``T_Cla_Léon_Red_CSP`` are the same render;
    the wiki has been served under both spellings.
    """
    return unicodedata.normalize("NFC", urllib.parse.unquote(stem))


def parse_stem(stem: str) -> tuple[str, str, str] | None:
    """Return ``(code, skin, palette)`` for a conforming stem, else ``None``."""
    parts = normalize_stem(stem).split("_")
    if len(parts) != 5:
        return None
    prefix, code, skin, palette, suffix = parts
    if prefix != "T" or suffix != "CSP":
        return None
    if not code or not skin or not palette:
        return None
    return code, skin, palette


def _sort_key(pinned: tuple[str, ...]):
    def key(name: str) -> tuple[int, str]:
        try:
            return (pinned.index(name), "")
        except ValueError:
            return (len(pinned), name.lower())

    return key


class Library:
    """Scans SOURCE_DIR into a character -> skin -> palette -> RenderFile tree."""

    def __init__(self, source_dir: Path | None = None) -> None:
        self.source_dir = Path(source_dir) if source_dir is not None else SOURCE_DIR
        self._tree: dict[str, dict[str, dict[str, RenderFile]]] = {}
        self._raw_stems: dict[tuple[str, str, str], str] = {}
        self.unparsed: list[Path] = []
        self.duplicates: list[Path] = []

    # -- scanning -----------------------------------------------------------

    def scan(self) -> None:
        self._tree = {}
        self._raw_stems = {}
        self.unparsed = []
        self.duplicates = []

        if not self.source_dir.is_dir():
            return

        for entry in sorted(os.scandir(self.source_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue  # e.g. the loose "Dragdown wiki link.txt"
            if entry.name in NON_CHARACTER_FOLDERS:
                continue
            self._scan_character(entry.name, Path(entry.path))

        # Drop characters whose files were all unparseable.
        self._tree = {c: s for c, s in self._tree.items() if s}

    def _scan_character(self, character: str, folder: Path) -> None:
        skins = self._tree.setdefault(character, {})

        for entry in sorted(os.scandir(folder), key=lambda e: e.name):
            if not entry.is_file() or not entry.name.lower().endswith(".png"):
                continue

            path = Path(entry.path)
            parsed = parse_stem(path.stem)
            if parsed is None:
                self.unparsed.append(path)
                continue

            code, skin, palette = parsed
            key = (character, skin, palette)
            existing = skins.get(skin, {}).get(palette)

            if existing is not None:
                # Same render under two spellings. Prefer the properly decoded
                # name (the percent-encoded one is a downloader artifact).
                if "%" in path.stem and "%" not in self._raw_stems[key]:
                    self.duplicates.append(path)
                    continue
                if "%" not in path.stem and "%" in self._raw_stems[key]:
                    self.duplicates.append(existing.path)
                else:
                    self.duplicates.append(path)
                    continue

            skins.setdefault(skin, {})[palette] = RenderFile(
                path=path,
                character=character,
                code=code,
                skin=skin,
                palette=palette,
            )
            self._raw_stems[key] = path.stem

    # -- queries ------------------------------------------------------------

    def characters(self) -> list[str]:
        return sorted(self._tree)

    def skins(self, character: str) -> list[str]:
        return sorted(self._tree.get(character, {}), key=_sort_key(PINNED_SKINS))

    def palettes(self, character: str, skin: str) -> list[str]:
        return sorted(
            self._tree.get(character, {}).get(skin, {}), key=_sort_key(PINNED_PALETTES)
        )

    def get(self, character: str, skin: str, palette: str) -> RenderFile | None:
        return self._tree.get(character, {}).get(skin, {}).get(palette)

    def all_palettes(self) -> list[str]:
        """Every palette name present on disk. Derived, never hardcoded."""
        names: set[str] = set()
        for skins in self._tree.values():
            for palettes in skins.values():
                names.update(palettes)
        return sorted(names, key=_sort_key(PINNED_PALETTES))

    def all_skins(self) -> list[str]:
        names: set[str] = set()
        for skins in self._tree.values():
            names.update(skins)
        return sorted(names, key=_sort_key(PINNED_SKINS))

    def iter_renders(self):
        for character in self.characters():
            for skin in self.skins(character):
                for palette in self.palettes(character, skin):
                    yield self._tree[character][skin][palette]

    def count(self) -> int:
        return sum(
            len(palettes)
            for skins in self._tree.values()
            for palettes in skins.values()
        )

    def code_for(self, character: str) -> str:
        """The 3-letter code for a character, preferring what's on disk."""
        skins = self._tree.get(character)
        if skins:
            for palettes in skins.values():
                for render in palettes.values():
                    return render.code
        return CHAR_CODES.get(character, character[:3])

    def status_line(self) -> str:
        parts = [f"{self.count()} renders across {len(self._tree)} characters"]
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} duplicate(s) hidden")
        if self.unparsed:
            parts.append(f"{len(self.unparsed)} file(s) skipped: non-standard names")
        return " · ".join(parts)
