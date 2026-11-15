"""Entry point for the Rivals 2 Palette Creator GUI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        # Build check for the exe, see palette_creator/selftest.py.
        from palette_creator.selftest import run

        rest = sys.argv[sys.argv.index("--smoke-test") + 1 :]
        sys.exit(run(rest[0] if rest else None))

    from palette_creator.ui.app import main

    sys.exit(main())
