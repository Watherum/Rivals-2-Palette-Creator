"""Print the interpreter and dependency versions. Run this first when something
misbehaves."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PACKAGES = [
    ("tkinter", None),
    ("customtkinter", "__version__"),
    ("numpy", "__version__"),
    ("scipy", "__version__"),
    ("skimage", "__version__"),
    ("PIL", "__version__"),
    ("requests", "__version__"),
]


def main() -> int:
    print(f"Python     {sys.version.split()[0]}")
    print(f"Executable {sys.executable}")
    print()

    missing = []
    for name, attr in PACKAGES:
        try:
            module = __import__(name)
            version = getattr(module, attr, "(bundled)") if attr else "(bundled)"
            print(f"  {name:<16} {version}")
        except Exception as exc:
            missing.append(name)
            print(f"  {name:<16} MISSING - {exc}")

    print()
    from palette_creator.config import SOURCE_DIR

    print(f"Renders    {SOURCE_DIR}")
    if SOURCE_DIR.is_dir():
        from palette_creator.library import Library

        lib = Library()
        lib.scan()
        print(f"           {lib.status_line()}")
    else:
        print("           NOT FOUND - run the Downloads tab to fetch renders")

    print()
    if missing:
        print(f"FAIL: {len(missing)} package(s) missing. Run install_requirements.bat")
        return 1
    print("All dependencies OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
