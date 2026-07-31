"""Downloader unit tests plus a few that hit the live wiki (``-m network``)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from palette_creator.downloader import (
    FALLBACK_CHARACTERS,
    DownloadCancelled,
    char_code,
    collect_csp_images,
    discover_characters,
    download_file,
    is_valid_png,
    make_session,
    normalize_filename,
    sweep_partials,
)

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IENDaeB`\x82"
)


# -- filename handling ------------------------------------------------------


def test_normalize_filename_decodes_percent_encoding():
    assert (
        normalize_filename("T_Cla_L%C3%A9on_Red_CSP.png") == "T_Cla_Léon_Red_CSP.png"
    )


def test_normalize_filename_strips_file_prefix_and_spaces():
    assert normalize_filename("File:T Ran Abyss Neutral CSP.png") == (
        "T_Ran_Abyss_Neutral_CSP.png"
    )


def test_char_code_known_and_unknown():
    assert char_code("La Reina") == "Lar"
    assert char_code("Zetterburn") == "Zet"
    assert char_code("Newcomer") == "New"  # not yet in the table


def test_fallback_list_excludes_armando():
    """RoA2/Armando does not exist on the wiki and no T_Arm_* files are uploaded."""
    assert "Armando" not in FALLBACK_CHARACTERS
    assert len(FALLBACK_CHARACTERS) == 16


# -- atomic download --------------------------------------------------------


class _FakeResponse:
    def __init__(self, chunks, status=200):
        self._chunks = chunks
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=0):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, chunks, status=200):
        self._chunks = chunks
        self._status = status

    def get(self, url, **kw):
        return _FakeResponse(self._chunks, self._status)


def test_download_writes_valid_png(tmp_path):
    dest = tmp_path / "Ranno" / "T_Ran_Default_Neutral_CSP.png"
    download_file(_FakeSession([PNG_BYTES]), "http://x/y.png", dest)
    assert dest.is_file()
    assert is_valid_png(dest)
    assert not list(tmp_path.rglob(".dl_*.part"))


def test_interrupted_download_leaves_nothing(tmp_path):
    def exploding():
        yield PNG_BYTES[:20]
        raise ConnectionError("connection reset")

    dest = tmp_path / "Ranno" / "T_Ran_Default_Neutral_CSP.png"
    with pytest.raises(ConnectionError):
        download_file(_FakeSession(exploding()), "http://x/y.png", dest)

    assert not dest.exists()
    assert not list(tmp_path.rglob(".dl_*.part"))


def test_truncated_png_is_rejected(tmp_path):
    dest = tmp_path / "Ranno" / "T_Ran_Default_Neutral_CSP.png"
    with pytest.raises(ValueError, match="not a valid PNG"):
        download_file(_FakeSession([PNG_BYTES[:30]]), "http://x/y.png", dest)
    assert not dest.exists()
    assert not list(tmp_path.rglob(".dl_*.part"))


def test_non_png_payload_is_rejected(tmp_path):
    dest = tmp_path / "Ranno" / "x.png"
    with pytest.raises(ValueError):
        download_file(_FakeSession([b"<html>403 Forbidden</html>"]), "http://x", dest)
    assert not dest.exists()


def test_cancel_stops_mid_stream(tmp_path):
    cancel = threading.Event()

    def chunks():
        yield PNG_BYTES[:10]
        cancel.set()
        yield PNG_BYTES[10:]

    dest = tmp_path / "Ranno" / "x.png"
    with pytest.raises(DownloadCancelled):
        download_file(_FakeSession(chunks()), "http://x", dest, cancel=cancel)
    assert not dest.exists()
    assert not list(tmp_path.rglob(".dl_*.part"))


def test_existing_file_survives_a_failed_redownload(tmp_path):
    """The atomic rename must not clobber a good file with a bad fetch."""
    dest = tmp_path / "Ranno" / "x.png"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(PNG_BYTES)

    with pytest.raises(ValueError):
        download_file(_FakeSession([b"garbage"]), "http://x", dest)
    assert dest.read_bytes() == PNG_BYTES


def test_sweep_partials(tmp_path):
    (tmp_path / "Ranno").mkdir()
    (tmp_path / "Ranno" / ".dl_abc.part").write_bytes(b"junk")
    (tmp_path / "Ranno" / "keep.png").write_bytes(PNG_BYTES)

    assert sweep_partials(tmp_path) == 1
    assert (tmp_path / "Ranno" / "keep.png").exists()


# -- live wiki --------------------------------------------------------------


@pytest.mark.network
def test_discover_characters_matches_the_wiki():
    names = discover_characters(make_session())
    assert 15 <= len(names) <= 30
    for expected in ("Ranno", "Clairen", "Zetterburn", "La Reina"):
        assert expected in names
    assert "Armando" not in names, "RoA2/Armando should not exist as a wiki page"
    # RoA2/Items has a /Data subpage but no /Matchups, so it must not slip in.
    assert "Items" not in names


@pytest.mark.network
def test_prefix_query_finds_renders_the_page_query_misses():
    session = make_session()
    ranno = collect_csp_images(session, "Ranno", "Ran")
    assert "T_Ran_Basketball_Grassroots_CSP.png" in ranno
    assert "T_Ran_Clown_Green_CSP.png" in ranno

    clairen = collect_csp_images(session, "Clairen", "Cla")
    assert "T_Cla_Baseball_SSE_CSP.png" in clairen


@pytest.mark.network
def test_collected_names_are_clean():
    names = collect_csp_images(make_session(), "Ranno", "Ran")
    assert names
    for name in names:
        assert "CSP" in name.upper()
        assert name.startswith("T_Ran_"), name
        assert "%" not in name, name
        assert " " not in name, name
