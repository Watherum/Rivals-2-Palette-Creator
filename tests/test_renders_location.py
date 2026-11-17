"""Start-up lookup of the render library (renders_location.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from palette_creator import config, renders_location
from palette_creator.downloader import DownloadOptions
from palette_creator.library import Library


def make_renders(root: Path) -> Path:
    """A minimal Character_Renders_Source: one character, one PNG."""
    (root / "Ranno").mkdir(parents=True)
    (root / "Ranno" / "T_Ran_Default_Neutral_CSP.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return root


@pytest.fixture
def install(tmp_path, monkeypatch):
    """Pretend the exe lives in tmp_path/app, and restore SOURCE_DIR after."""
    app = tmp_path / "app"
    app.mkdir()
    resources = app / "Resources"
    monkeypatch.setattr(config, "DEFAULT_SOURCE_DIR", resources / "Character_Renders_Source")
    monkeypatch.setattr(config, "SETTINGS_FILE", resources / "settings.json")
    monkeypatch.setattr(config, "SOURCE_DIR", config.SOURCE_DIR)
    return app


def test_has_renders_needs_a_png_in_a_character_folder(tmp_path):
    assert not renders_location.has_renders(tmp_path / "missing")
    (tmp_path / "Ranno").mkdir()
    assert not renders_location.has_renders(tmp_path)  # empty character folder
    (tmp_path / "loose.png").write_bytes(b"x")
    assert not renders_location.has_renders(tmp_path)  # PNG not in a character folder
    (tmp_path / "Ranno" / "a.PNG").write_bytes(b"x")
    assert renders_location.has_renders(tmp_path)


@pytest.mark.parametrize("pick", ["release", "resources", "renders"])
def test_normalize_accepts_any_level_of_the_release_folder(tmp_path, pick):
    renders = make_renders(tmp_path / "release" / "Resources" / "Character_Renders_Source")
    picked = {
        "release": tmp_path / "release",
        "resources": tmp_path / "release" / "Resources",
        "renders": renders,
    }[pick]
    assert renders_location.normalize_choice(picked) == renders.resolve()


def test_normalize_rejects_a_folder_without_renders(tmp_path):
    assert renders_location.normalize_choice(tmp_path) is None


def test_renders_next_to_the_exe_win(install, tmp_path):
    make_renders(config.DEFAULT_SOURCE_DIR)
    elsewhere = make_renders(tmp_path / "elsewhere")
    renders_location.remember(elsewhere)
    assert renders_location.resolve()
    assert config.SOURCE_DIR == config.DEFAULT_SOURCE_DIR


def test_remembered_folder_is_used_when_none_next_to_the_exe(install, tmp_path):
    elsewhere = make_renders(tmp_path / "elsewhere")
    renders_location.remember(elsewhere)
    assert renders_location.resolve()
    assert config.SOURCE_DIR == elsewhere
    # Everything that reads the renders follows the repointed folder.
    assert Library().source_dir == elsewhere
    assert DownloadOptions().dest_root == elsewhere


def test_nothing_found_falls_back_to_the_default(install, tmp_path):
    renders_location.remember(tmp_path / "gone")  # stale: folder was deleted
    assert not renders_location.resolve()
    assert config.SOURCE_DIR == config.DEFAULT_SOURCE_DIR


def test_corrupt_settings_are_ignored(install):
    config.SETTINGS_FILE.parent.mkdir(parents=True)
    config.SETTINGS_FILE.write_text("{not json", encoding="utf-8")
    assert renders_location.saved_location() is None
    assert not renders_location.resolve()


def test_startup_prompt_answer_is_remembered(install, tmp_path, monkeypatch):
    picked = make_renders(tmp_path / "picked")
    monkeypatch.setattr(renders_location, "choose_folder", lambda parent=None: picked)
    renders_location.locate_at_startup()
    assert config.SOURCE_DIR == picked
    assert renders_location.saved_location() == picked


def test_cancelling_the_startup_prompt_keeps_the_default(install, monkeypatch):
    monkeypatch.setattr(renders_location, "choose_folder", lambda parent=None: None)
    renders_location.locate_at_startup()
    assert config.SOURCE_DIR == config.DEFAULT_SOURCE_DIR
    assert renders_location.saved_location() is None
