"""Download character select portraits from dragdown.wiki.

Usable two ways: as a CLI (``python Python_Scripts/download_rivals_renders.py``)
and from the GUI's Downloads tab, which supplies a progress callback and a
cancel event.

dragdown.wiki's HTML pages sit behind a Cloudflare JavaScript challenge and
return 403 to plain HTTP clients, so everything here goes through the MediaWiki
API at /w/api.php, which is not challenged.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

import requests

from . import config
from .config import (
    API_TIMEOUT,
    CHAR_CODES,
    DOWNLOAD_DELAY,
    DOWNLOAD_TIMEOUT,
    USER_AGENT,
    WIKI_API_URL,
    WIKI_PAGE_PREFIX,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

FALLBACK_CHARACTERS = (
    "Absa", "Clairen", "Etalus", "Fleet", "Forsburn", "Galvan", "Kragg",
    "La Reina", "Loxodont", "Maypul", "Olympia", "Orcane", "Ranno", "Slade",
    "Wrastor", "Zetterburn",
)
"""Used only if wiki discovery returns something implausible.

Deliberately excludes Armando: ``RoA2/Armando`` does not exist as a wiki page,
and no ``T_Arm_*`` files are uploaded, so asking for it just wastes a request.
"""


class DownloadCancelled(Exception):
    """Raised internally when the cancel event trips."""


@dataclass(frozen=True)
class DownloadOptions:
    # A factory, not a default value: SOURCE_DIR can be repointed at start-up.
    dest_root: Path = field(default_factory=lambda: config.SOURCE_DIR)
    characters: tuple[str, ...] | None = None
    overwrite: bool = False
    delay: float = DOWNLOAD_DELAY
    timeout: int = DOWNLOAD_TIMEOUT
    dry_run: bool = False


EventKind = Literal[
    "log", "char_start", "found", "download", "skip", "error", "char_done", "done"
]


@dataclass
class ProgressEvent:
    kind: EventKind
    character: str = ""
    filename: str = ""
    current: int = 0
    total: int = 0
    chars_done: int = 0
    chars_total: int = 0
    message: str = ""


ProgressCB = Callable[[ProgressEvent], None]


@dataclass
class DownloadSummary:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False

    def describe(self) -> str:
        bits = [f"{self.downloaded} downloaded", f"{self.skipped} skipped"]
        if self.failed:
            bits.append(f"{self.failed} failed")
        if self.cancelled:
            bits.append("cancelled")
        return ", ".join(bits)


def _emit(progress: ProgressCB | None, kind: EventKind, **kw) -> None:
    if progress is not None:
        progress(ProgressEvent(kind=kind, **kw))


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()


# -- session and API --------------------------------------------------------


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    )
    return session


def _api_query(session: requests.Session, params: dict) -> Iterable[dict]:
    """Yield each response page of a paginated MediaWiki query."""
    params = dict(params)
    params.setdefault("action", "query")
    params.setdefault("format", "json")
    seen_continues: set[str] = set()

    while True:
        resp = session.get(WIKI_API_URL, params=params, timeout=API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        yield data

        cont = data.get("continue")
        if not cont:
            return
        token = repr(sorted(cont.items()))
        if token in seen_continues:
            return  # defensive: never loop forever on a misbehaving continue
        seen_continues.add(token)
        params.update(cont)


def normalize_filename(name: str) -> str:
    """Canonical on-disk name for a wiki file title.

    Percent-decoding and NFC normalization are what stop the
    ``T_Cla_L%C3%A9on_*`` / ``T_Cla_Léon_*`` duplicate pairs from reappearing.
    """
    name = name.split("File:", 1)[-1]
    name = urllib.parse.unquote(name)
    name = unicodedata.normalize("NFC", name)
    return name.replace(" ", "_")


def discover_characters(
    session: requests.Session, progress: ProgressCB | None = None
) -> list[str]:
    """List character pages from the wiki rather than hardcoding them.

    A page counts as a character if it is a direct child of ``RoA2/`` and has
    both a ``/Data`` and a ``/Matchups`` child. ``/Data`` alone is not enough --
    ``RoA2/Items`` has one too -- but only characters have matchup pages.
    """
    titles: list[str] = []
    try:
        for data in _api_query(
            session,
            {"list": "allpages", "apprefix": WIKI_PAGE_PREFIX, "aplimit": "max"},
        ):
            titles.extend(p["title"] for p in data.get("query", {}).get("allpages", []))
    except Exception as exc:
        _emit(progress, "error", message=f"Character discovery failed: {exc}")
        return list(FALLBACK_CHARACTERS)

    prefix = WIKI_PAGE_PREFIX
    tops = {t[len(prefix) :] for t in titles if t.startswith(prefix) and t.count("/") == 1}

    def children_named(suffix: str) -> set[str]:
        tail = f"/{suffix}"
        return {
            t[len(prefix) : -len(tail)]
            for t in titles
            if t.startswith(prefix) and t.endswith(tail)
        }

    candidates = tops & children_named("Data") & children_named("Matchups")
    names = sorted(n.replace("_", " ") for n in candidates if n)

    if len(names) < 10:
        _emit(
            progress,
            "log",
            message=f"Discovery returned only {len(names)} character(s); using fallback list.",
        )
        return list(FALLBACK_CHARACTERS)

    return names


def char_code(display_name: str) -> str:
    """The ``T_Xxx`` code for a character name."""
    if display_name in CHAR_CODES:
        return CHAR_CODES[display_name]
    # New character not yet in the table: the wiki convention is the first three
    # letters of the first word, capitalized.
    word = display_name.replace("_", " ").split()[0]
    return word[:3].capitalize()


def list_page_images(session: requests.Session, slug: str) -> dict[str, str]:
    """Files embedded on ``RoA2/{slug}``, as ``{filename: url}``."""
    out: dict[str, str] = {}
    params = {
        "generator": "images",
        "titles": f"{WIKI_PAGE_PREFIX}{slug}",
        "gimlimit": "max",
        "prop": "imageinfo",
        "iiprop": "url",
    }
    for data in _api_query(session, params):
        for page in data.get("query", {}).get("pages", {}).values():
            info = page.get("imageinfo")
            if not info or not info[0].get("url"):
                continue
            out[normalize_filename(page.get("title", ""))] = info[0]["url"]
    return out


def list_prefix_images(session: requests.Session, code: str) -> dict[str, str]:
    """Every uploaded file starting ``T_{code}_``, as ``{filename: url}``.

    Page-independent, and a strict superset of :func:`list_page_images` in
    practice -- it surfaces renders that exist on the wiki but are not linked
    from the character page (e.g. ``T_Ran_Basketball_Grassroots_CSP.png``).
    """
    out: dict[str, str] = {}
    params = {
        "list": "allimages",
        "aiprefix": f"T_{code}_",
        "ailimit": "max",
        "aiprop": "url",
    }
    for data in _api_query(session, params):
        for item in data.get("query", {}).get("allimages", []):
            url = item.get("url")
            if url:
                out[normalize_filename(item.get("name", ""))] = url
    return out


def collect_csp_images(
    session: requests.Session,
    slug: str,
    code: str,
    progress: ProgressCB | None = None,
) -> dict[str, str]:
    """Union both listings, filtered to this character's CSP renders."""
    out: dict[str, str] = {}
    sources = (
        ("page images", lambda: list_page_images(session, slug)),
        ("file prefix", lambda: list_prefix_images(session, code)),
    )

    for label, fetch in sources:
        try:
            found = fetch()
        except Exception as exc:
            _emit(
                progress,
                "error",
                character=slug,
                message=f"{label} query failed: {exc}",
            )
            continue

        for name, url in found.items():
            if not name.lower().endswith(".png"):
                continue
            # The CSP filter also excludes stray 3-field names like
            # T_Ran_Black.png; the prefix check prevents cross-character bleed.
            if "CSP" not in name.upper():
                continue
            if not name.startswith(f"T_{code}_"):
                continue
            out.setdefault(name, url)

    return out


# -- downloading ------------------------------------------------------------


def is_valid_png(path: Path) -> bool:
    """Magic bytes plus a structural check, which catches truncation."""
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != PNG_MAGIC:
                return False
        from PIL import Image

        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def sweep_partials(root: Path) -> int:
    """Remove leftover ``.dl_*.part`` files from an interrupted run."""
    removed = 0
    if not root.is_dir():
        return 0
    for path in root.rglob(".dl_*.part"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    *,
    timeout: int = DOWNLOAD_TIMEOUT,
    cancel: threading.Event | None = None,
) -> None:
    """Fetch ``url`` to ``dest`` atomically.

    Streams to a sibling temp file and renames only after the bytes validate as
    a complete PNG, so an interrupted run can never leave a corrupt render in
    the library.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".dl_", suffix=".part")
    tmp = Path(tmp_name)

    try:
        with os.fdopen(fd, "wb") as fh:
            with session.get(url, timeout=timeout, stream=True) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=65536):
                    _check(cancel)
                    fh.write(chunk)

        if not is_valid_png(tmp):
            raise ValueError("downloaded file is not a valid PNG")

        os.replace(tmp, dest)  # atomic on NTFS within one volume
    finally:
        tmp.unlink(missing_ok=True)


def run_download(
    options: DownloadOptions | None = None,
    progress: ProgressCB | None = None,
    cancel: threading.Event | None = None,
) -> DownloadSummary:
    """Fetch every missing CSP render. Safe to re-run; already-present files are
    skipped."""
    options = options or DownloadOptions()
    summary = DownloadSummary()
    session = make_session()

    removed = sweep_partials(options.dest_root)
    if removed:
        _emit(progress, "log", message=f"Cleaned up {removed} partial download(s).")

    try:
        if options.characters:
            characters = list(options.characters)
        else:
            _emit(progress, "log", message="Discovering characters from the wiki...")
            characters = discover_characters(session, progress)
            _emit(
                progress,
                "log",
                message=f"Found {len(characters)}: {', '.join(characters)}",
            )

        total_chars = len(characters)
        for i, character in enumerate(characters):
            _check(cancel)
            slug = character.replace(" ", "_")
            code = char_code(character)
            _emit(
                progress,
                "char_start",
                character=character,
                chars_done=i,
                chars_total=total_chars,
            )

            images = collect_csp_images(session, slug, code, progress)
            if not images:
                _emit(
                    progress,
                    "log",
                    character=character,
                    message=f"{character}: no CSP renders found.",
                )
                _emit(
                    progress,
                    "char_done",
                    character=character,
                    chars_done=i + 1,
                    chars_total=total_chars,
                )
                continue

            _emit(
                progress,
                "found",
                character=character,
                total=len(images),
                chars_done=i,
                chars_total=total_chars,
            )

            char_dir = options.dest_root / character
            for n, (filename, url) in enumerate(sorted(images.items()), start=1):
                _check(cancel)
                dest = char_dir / filename

                if dest.exists() and not options.overwrite:
                    summary.skipped += 1
                    _emit(
                        progress,
                        "skip",
                        character=character,
                        filename=filename,
                        current=n,
                        total=len(images),
                    )
                    continue

                if options.dry_run:
                    summary.downloaded += 1
                    _emit(
                        progress,
                        "download",
                        character=character,
                        filename=filename,
                        current=n,
                        total=len(images),
                        message="(dry run)",
                    )
                    continue

                try:
                    download_file(
                        session, url, dest, timeout=options.timeout, cancel=cancel
                    )
                except DownloadCancelled:
                    raise
                except Exception as exc:
                    summary.failed += 1
                    summary.errors.append((filename, str(exc)))
                    _emit(
                        progress,
                        "error",
                        character=character,
                        filename=filename,
                        current=n,
                        total=len(images),
                        message=str(exc),
                    )
                    continue

                summary.downloaded += 1
                _emit(
                    progress,
                    "download",
                    character=character,
                    filename=filename,
                    current=n,
                    total=len(images),
                )
                if options.delay > 0:
                    time.sleep(options.delay)

            _emit(
                progress,
                "char_done",
                character=character,
                chars_done=i + 1,
                chars_total=total_chars,
            )

    except DownloadCancelled:
        summary.cancelled = True
        _emit(progress, "log", message="Cancelled.")

    _emit(progress, "done", message=summary.describe())
    return summary


# -- CLI --------------------------------------------------------------------


def _print_progress(event: ProgressEvent) -> None:
    if event.kind == "char_start":
        print(f"\n{'=' * 50}\n  {event.character}\n{'=' * 50}")
    elif event.kind == "found":
        print(f"  Found {event.total} image(s)")
    elif event.kind == "download":
        suffix = f" {event.message}" if event.message else ""
        print(f"  [dl]   {event.filename}{suffix}")
    elif event.kind == "skip":
        print(f"  [skip] {event.filename}")
    elif event.kind == "error":
        target = event.filename or event.character
        print(f"  [error] {target}: {event.message}")
    elif event.kind in ("log", "done"):
        print(event.message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download Rivals of Aether 2 CSP renders from dragdown.wiki."
    )
    parser.add_argument(
        "--characters",
        nargs="+",
        metavar="NAME",
        help="Only these characters (default: discover from the wiki).",
    )
    parser.add_argument(
        "--dest", type=Path, default=None,
        help="Destination root folder (default: the app's renders folder).",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Re-download files already present."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List what would be fetched."
    )
    parser.add_argument(
        "--delay", type=float, default=DOWNLOAD_DELAY, help="Seconds between downloads."
    )
    parser.add_argument(
        "--list-characters",
        action="store_true",
        help="Print the discovered character list and exit.",
    )
    args = parser.parse_args(argv)

    if args.dest is None:
        # Same lookup as the GUI (minus the prompt), so the CLI tops up the
        # library the app actually reads rather than starting a second one.
        from .renders_location import resolve

        resolve()
        args.dest = config.SOURCE_DIR

    if args.list_characters:
        for name in discover_characters(make_session(), _print_progress):
            print(name)
        return 0

    summary = run_download(
        DownloadOptions(
            dest_root=args.dest,
            characters=tuple(args.characters) if args.characters else None,
            overwrite=args.overwrite,
            delay=args.delay,
            dry_run=args.dry_run,
        ),
        progress=_print_progress,
    )
    return 1 if summary.failed and not summary.downloaded else 0


if __name__ == "__main__":
    sys.exit(main())
