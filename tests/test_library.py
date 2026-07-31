"""Library scanning against the real Resources tree and synthetic fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from palette_creator.config import SOURCE_DIR
from palette_creator.library import Library, normalize_stem, parse_stem

real_assets = pytest.mark.skipif(
    not SOURCE_DIR.is_dir(), reason="Resources/Character_Renders_Source not present"
)


# -- filename parsing -------------------------------------------------------


def test_parse_conforming_stem():
    assert parse_stem("T_Ran_Abyss_Neutral_CSP") == ("Ran", "Abyss", "Neutral")


def test_parse_rejects_random_folder_names():
    # Random/T_Ran_Black.png has 3 fields and no CSP suffix.
    assert parse_stem("T_Ran_Black") is None


def test_parse_rejects_wrong_suffix():
    assert parse_stem("T_Ran_Abyss_Neutral_PNG") is None


def test_normalize_stem_decodes_percent_encoding():
    assert normalize_stem("T_Cla_L%C3%A9on_Red_CSP") == "T_Cla_Léon_Red_CSP"


def test_parse_percent_encoded_matches_decoded():
    assert parse_stem("T_Cla_L%C3%A9on_Red_CSP") == parse_stem("T_Cla_Léon_Red_CSP")


# -- synthetic tree ---------------------------------------------------------


def _png(path: Path, color=(255, 0, 0, 255)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (4, 4), color).save(path)


def test_palettes_are_derived_not_hardcoded(tmp_path):
    """Guards against anyone replacing the disk scan with a fixed list."""
    _png(tmp_path / "Ranno" / "T_Ran_Default_Neutral_CSP.png")
    _png(tmp_path / "Ranno" / "T_Ran_Default_ZZTestPalette_CSP.png")

    lib = Library(tmp_path)
    lib.scan()
    assert "ZZTestPalette" in lib.all_palettes()
    assert lib.palettes("Ranno", "Default") == ["Neutral", "ZZTestPalette"]


def test_character_comes_from_folder_not_prefix(tmp_path):
    """Random/ ships T_Ran_* files that must never be filed under Ranno."""
    _png(tmp_path / "Ranno" / "T_Ran_Default_Neutral_CSP.png")
    _png(tmp_path / "Random" / "T_Ran_Black.png")

    lib = Library(tmp_path)
    lib.scan()
    assert lib.characters() == ["Ranno"]
    assert lib.count() == 1


def test_percent_encoded_duplicate_is_hidden_and_decoded_kept(tmp_path):
    _png(tmp_path / "Clairen" / "T_Cla_L%C3%A9on_Red_CSP.png")
    _png(tmp_path / "Clairen" / "T_Cla_Léon_Red_CSP.png")

    lib = Library(tmp_path)
    lib.scan()
    assert lib.count() == 1
    assert len(lib.duplicates) == 1
    kept = lib.get("Clairen", "Léon", "Red")
    assert kept is not None
    assert "%" not in kept.path.stem


def test_unparsed_files_are_reported_not_fatal(tmp_path):
    _png(tmp_path / "Ranno" / "T_Ran_Default_Neutral_CSP.png")
    _png(tmp_path / "Ranno" / "garbage.png")
    (tmp_path / "Ranno" / "notes.txt").write_text("ignore me")

    lib = Library(tmp_path)
    lib.scan()
    assert lib.count() == 1
    assert len(lib.unparsed) == 1
    assert lib.unparsed[0].name == "garbage.png"


def test_loose_files_at_top_level_are_skipped(tmp_path):
    (tmp_path / "Dragdown wiki link.txt").write_text("https://dragdown.wiki/")
    _png(tmp_path / "Ranno" / "T_Ran_Default_Neutral_CSP.png")

    lib = Library(tmp_path)
    lib.scan()
    assert lib.characters() == ["Ranno"]


def test_default_and_neutral_are_pinned_first(tmp_path):
    for skin in ("Abyss", "Default", "Zephyr"):
        for palette in ("Blue", "Neutral", "Red"):
            _png(tmp_path / "Ranno" / f"T_Ran_{skin}_{palette}_CSP.png")

    lib = Library(tmp_path)
    lib.scan()
    assert lib.skins("Ranno")[0] == "Default"
    assert lib.palettes("Ranno", "Default")[0] == "Neutral"


# -- the real tree ----------------------------------------------------------


@real_assets
def test_real_tree_shape():
    lib = Library()
    lib.scan()

    chars = lib.characters()
    assert "Ranno" in chars and "La Reina" in chars
    assert "Random" not in chars

    # Clairen's 103 files collapse to 98 once the 5 percent-encoded twins hide.
    assert len(lib.palettes("Clairen", "Léon")) == 5
    assert len(lib.duplicates) == 5
    assert all("%" in p.stem for p in lib.duplicates)

    # Random/'s 5 files are the only non-conforming names in the tree.
    assert len(lib.unparsed) == 0  # Random/ is excluded before parsing


@real_assets
def test_real_tree_single_render_character():
    """A character with only one render must still produce usable dropdowns.

    Armando is the natural case (one render, no wiki page), but the folder is
    optional -- skip rather than fail if it has been removed.
    """
    lib = Library()
    lib.scan()

    sparse = [c for c in lib.characters() if len(lib.all_skins()) and
              sum(len(lib.palettes(c, s)) for s in lib.skins(c)) == 1]
    if not sparse:
        pytest.skip("no single-render character present")

    character = sparse[0]
    assert len(lib.skins(character)) == 1
    skin = lib.skins(character)[0]
    assert len(lib.palettes(character, skin)) == 1
    assert lib.get(character, skin, lib.palettes(character, skin)[0]) is not None
