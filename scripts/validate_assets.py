"""Open every downloaded render and report anything unusual.

Worth running once to establish a clean baseline, and again after any large
download.

    python scripts/validate_assets.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from palette_creator.config import SOURCE_DIR
from palette_creator.library import Library


def main() -> int:
    if not SOURCE_DIR.is_dir():
        print(f"Not found: {SOURCE_DIR}")
        return 1

    files = sorted(SOURCE_DIR.rglob("*.png"))
    print(f"Checking {len(files)} PNG(s) under {SOURCE_DIR}\n")

    sizes: Counter = Counter()
    modes: Counter = Counter()
    corrupt: list[tuple[Path, str]] = []
    total_bytes = 0

    for i, path in enumerate(files, 1):
        if i % 200 == 0:
            print(f"  ...{i}/{len(files)}")
        try:
            total_bytes += path.stat().st_size
            with Image.open(path) as im:
                im.load()  # decodes fully, so truncation surfaces here
                sizes[im.size] += 1
                modes[im.mode] += 1
        except Exception as exc:
            corrupt.append((path, str(exc)))

    print("\nDimensions:")
    for size, count in sizes.most_common():
        print(f"  {size[0]:>5} x {size[1]:<5} {count:>5}")

    print("\nModes:")
    for mode, count in modes.most_common():
        print(f"  {mode:<8} {count:>5}")

    print(f"\nTotal size: {total_bytes / (1024 ** 3):.2f} GB")

    partials = list(SOURCE_DIR.rglob(".dl_*.part"))
    if partials:
        print(f"\n{len(partials)} leftover partial download(s):")
        for path in partials[:10]:
            print(f"  {path}")

    if corrupt:
        print(f"\n{len(corrupt)} CORRUPT file(s):")
        for path, err in corrupt:
            print(f"  {path.relative_to(SOURCE_DIR)}: {err}")
    else:
        print("\nNo corrupt files.")

    library = Library()
    library.scan()
    print(f"\nLibrary index: {library.status_line()}")
    if library.unparsed:
        print("  Non-standard names (not shown in the app):")
        for path in library.unparsed[:10]:
            print(f"    {path.relative_to(SOURCE_DIR)}")
    if library.duplicates:
        print("  Duplicates hidden (percent-encoded twins):")
        for path in library.duplicates[:10]:
            print(f"    {path.relative_to(SOURCE_DIR)}")

    return 1 if corrupt else 0


if __name__ == "__main__":
    raise SystemExit(main())
