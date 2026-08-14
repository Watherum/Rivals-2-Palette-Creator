"""Zip the project into a release archive under dist/.

By default this packs source only (~300 KB). The Character_Renders_Source tree
is 382 MB and the app can refetch it from the Downloads tab, so it ships only
when you ask for it with --include-renders.

    python scripts/make_release.py                   source only
    python scripts/make_release.py --include-renders source + 382 MB of renders
    python scripts/make_release.py --force           overwrite an existing zip
"""

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A release is only what an end user needs to install, run, and troubleshoot the
# app. Anything not listed here never reaches the zip, so a stray file in the
# working tree cannot leak into it either.
#
# run_debug.bat earns its place: RUN_PALETTE_CREATOR.bat launches through
# pythonw.exe with no console, so without it a failed start is silent and the
# user has no traceback to report. check_env.py is the matching diagnostic.
#
# Deliberately left out as developer-only:
#   tests/, pytest.ini      pytest is not in requirements.txt, so an end user
#                           cannot run them anyway
#   scripts/bench_recolor.py     accuracy benchmark against official palettes
#   scripts/validate_assets.py   bulk PNG corruption sweep
#   scripts/make_release.py      this script
#   .gitignore              meaningless outside a checkout
SOURCE_ITEMS = [
    "palette_creator",
    "Python_Scripts",
    "scripts/check_env.py",
    "main.py",
    "requirements.txt",
    "README.md",
    "RUN_PALETTE_CREATOR.bat",
    "run_debug.bat",
    "install_requirements.bat",
]

RENDERS = Path("Resources/Character_Renders_Source")

# Skipped anywhere in the tree.
SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".vscode",
    ".idea",
    "htmlcov",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd", ".swp"}
SKIP_NAMES = {"Thumbs.db", "desktop.ini", ".coverage"}

# PNGs are already compressed. Deflating them does not just waste time, it
# measurably *grows* the archive - 400.9 MB of renders came out as 418.3 MB,
# because fixed-Huffman coding spends 9 bits on every byte >= 143 and PNG data
# is high entropy. Stored, those same files cost exactly their own size.
PRECOMPRESSED = {".png", ".jpg", ".jpeg", ".zip", ".gz"}


def read_version() -> str:
    """Pull __version__ out of the package without importing it.

    Parsed rather than imported so the script still runs on an interpreter
    where the app's dependencies are not installed.
    """
    text = (ROOT / "palette_creator" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit("ERROR: no __version__ found in palette_creator/__init__.py")
    return match.group(1)


def is_skipped(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    return path.suffix in SKIP_SUFFIXES or path.name in SKIP_NAMES


def collect(items: list[str]) -> list[Path]:
    """Expand top-level entries into project-relative file paths."""
    files: list[Path] = []
    for item in items:
        target = ROOT / item
        if not target.exists():
            print(f"  WARNING: {item} not found, skipping")
            continue
        if target.is_file():
            files.append(Path(item))
            continue
        for path in sorted(target.rglob("*")):
            if path.is_file():
                relative = path.relative_to(ROOT)
                if not is_skipped(relative):
                    files.append(relative)
    return files


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-renders",
        action="store_true",
        help=f"bundle {RENDERS.as_posix()} (382 MB) into the archive",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the output zip if it already exists",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist",
        help="directory to write the zip into (default: dist/)",
    )
    args = parser.parse_args()

    version = read_version()
    suffix = "-with-renders" if args.include_renders else ""
    stem = f"Rivals-2-Palette-Creator-v{version}{suffix}"
    out_path = args.output / f"{stem}.zip"
    # Everything sits under one folder so extracting never sprays the cwd.
    root_in_zip = f"Rivals-2-Palette-Creator-{version}"

    print(f"Rivals 2 Palette Creator - release {version}")
    print()

    if out_path.exists():
        existing = out_path.stat()
        print(f"  Existing: {out_path.name}  {human(existing.st_size)}")
        if not args.force:
            print()
            print("ERROR: that archive already exists. Re-run with --force to")
            print("       overwrite it, or bump __version__ first.")
            return 1
        print("  --force given, it will be overwritten")
        print()

    print("Collecting files...")
    files = collect(SOURCE_ITEMS)
    print(f"  source   {len(files):,} files")

    if args.include_renders:
        if not (ROOT / RENDERS).is_dir():
            print(f"ERROR: --include-renders but {RENDERS.as_posix()} does not exist.")
            return 1
        renders = collect([RENDERS.as_posix()])
        print(f"  renders  {len(renders):,} files")
        files += renders
    else:
        print("  renders  skipped (pass --include-renders to bundle them)")

    total = sum((ROOT / f).stat().st_size for f in files)
    print(f"  total    {human(total)} uncompressed")
    print()

    args.output.mkdir(parents=True, exist_ok=True)
    print(f"Writing {out_path}...")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for relative in files:
            stored = relative.suffix.lower() in PRECOMPRESSED
            zf.write(
                ROOT / relative,
                arcname=f"{root_in_zip}/{relative.as_posix()}",
                compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED,
                compresslevel=None if stored else 9,
            )

    # testzip() decompresses every entry and checks it against the CRC-32 that
    # was computed from the original file while writing, so a pass means the
    # archived bytes reconstruct the source exactly. Images are never decoded
    # or re-encoded anywhere in this script.
    print("Verifying (CRC-32 of every entry against its source bytes)...")
    with zipfile.ZipFile(out_path) as zf:
        damaged = zf.testzip()
        if damaged:
            print(f"ERROR: archive is corrupt at {damaged}")
            return 1
        names = set(zf.namelist())
        required = [
            f"{root_in_zip}/main.py",
            f"{root_in_zip}/requirements.txt",
            f"{root_in_zip}/install_requirements.bat",
            f"{root_in_zip}/RUN_PALETTE_CREATOR.bat",
            f"{root_in_zip}/run_debug.bat",
            f"{root_in_zip}/palette_creator/__init__.py",
        ]
        missing = [n for n in required if n not in names]
        if missing:
            print(f"ERROR: archive is missing {', '.join(missing)}")
            return 1
        count = len(names)

    packed = out_path.stat().st_size
    ratio = f" ({packed / total:.0%} of source)" if total else ""
    print()
    print("=" * 60)
    print(f"  {out_path.name}")
    print(f"  {count:,} files, {human(packed)}{ratio}")
    print(f"  {out_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
