"""CLI entry point for the dragdown.wiki render downloader.

The implementation lives in ``palette_creator.downloader`` so the GUI's
Downloads tab can drive the same code with a progress callback and a cancel
event. Run ``--help`` for options.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from palette_creator.downloader import main

if __name__ == "__main__":
    sys.exit(main())
