"""Start-up self-test, used by scripts/build_exe.py to check a built exe.

    main.py --smoke-test [report.txt]

Exercises every third-party dependency and builds the real window once, then
exits 0. A PyInstaller build that left out a module or a data file fails here
rather than on a user's machine. The exe has no console, so the outcome goes to
the report file as well as the exit code.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _checks() -> list[str]:
    lines = [f"python {sys.version.split()[0]} frozen={getattr(sys, 'frozen', False)}"]

    import numpy as np
    from scipy import ndimage
    from scipy.cluster.vq import kmeans2
    from skimage import color as skcolor

    rgb = np.random.default_rng(0).random((64, 3))
    lab = skcolor.rgb2lab(rgb.reshape(1, -1, 3)).reshape(-1, 3)
    back = skcolor.lab2rgb(lab.reshape(1, -1, 3)).reshape(-1, 3)
    if not np.allclose(rgb, back, atol=1e-3):
        raise AssertionError("sRGB -> Lab -> sRGB round trip drifted")
    kmeans2(lab, 4, minit="++", seed=0)
    ndimage.gaussian_filter(rgb, 1.0)
    lines.append("numpy / scipy / scikit-image ok")

    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(buffer, "PNG")
    buffer.seek(0)
    Image.open(buffer).load()
    lines.append("pillow png ok")

    import certifi
    import requests  # noqa: F401

    if not Path(certifi.where()).is_file():
        raise AssertionError("certifi CA bundle is missing (https would fail)")
    lines.append("requests / certifi ok")

    from . import colorspace, downloader, library, palette_extract, presets, recolor  # noqa: F401
    from .config import PROJECT_ROOT
    from .ui.app import App

    lines.append(f"project root {PROJECT_ROOT}")

    from . import config
    from .renders_location import resolve

    found = resolve()  # never prompts; the startup prompt lives in ui.app.main
    lines.append(f"renders {'found' if found else 'not found'} at {config.SOURCE_DIR}")

    app = App()  # needs customtkinter's bundled theme json and fonts
    app.update()
    app._on_close()
    lines.append("window ok")
    return lines


def run(report: str | None = None) -> int:
    try:
        lines = _checks()
        code = 0
    except BaseException:  # noqa: BLE001 -- report anything, including SystemExit
        lines = [traceback.format_exc()]
        code = 1
    lines.append("SMOKE TEST PASSED" if code == 0 else "SMOKE TEST FAILED")
    text = "\n".join(lines) + "\n"
    if report:
        Path(report).write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text, end="")
    return code
