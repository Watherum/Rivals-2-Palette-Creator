"""Paths and tunable constants shared across the app."""

import sys
from pathlib import Path


def _project_root() -> Path:
    """Folder that holds Resources/.

    In the PyInstaller exe, __file__ points into a temp folder that is deleted
    on exit, so downloads, exports and presets go next to the exe instead.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()
RESOURCES_DIR = PROJECT_ROOT / "Resources"
DEFAULT_SOURCE_DIR = RESOURCES_DIR / "Character_Renders_Source"
SOURCE_DIR = DEFAULT_SOURCE_DIR
"""Where the renders are read from and downloaded to.

Starts at DEFAULT_SOURCE_DIR and may be repointed at start-up by
renders_location.py. Read it as ``config.SOURCE_DIR`` at call time, never with
``from .config import SOURCE_DIR``, or the module keeps the stale default."""

SETTINGS_FILE = RESOURCES_DIR / "settings.json"
"""Per-install settings; currently just a remembered renders folder."""

EXPORT_DIR = RESOURCES_DIR / "Character_Renders_Custom"
PRESETS_DIR = RESOURCES_DIR / "Presets"

# Character display name -> the 3-letter code used in T_{Code}_{Skin}_{Palette}_CSP.png.
# Used to filter wiki downloads per character; the app never infers the character
# from this prefix (see library.py -- Random/T_Ran_*.png collides with Ranno).
CHAR_CODES = {
    "Absa": "Abs",
    "Armando": "Arm",
    "Clairen": "Cla",
    "Etalus": "Eta",
    "Fleet": "Fle",
    "Forsburn": "For",
    "Galvan": "Gal",
    "Kragg": "Kra",
    "La Reina": "Lar",
    "Loxodont": "Lox",
    "Maypul": "May",
    "Olympia": "Oly",
    "Orcane": "Orc",
    "Ranno": "Ran",
    "Slade": "Sla",
    "Wrastor": "Wra",
    "Zetterburn": "Zet",
}

# Folders under SOURCE_DIR that hold something other than character CSPs.
NON_CHARACTER_FOLDERS = {"Random"}

# --- recolor engine tuning -------------------------------------------------

ALPHA_MIN_RECOLOR = 1
"""Recolor any pixel with a nonzero alpha.

Including near-transparent antialiased pixels avoids leaving a faint ring of the
*old* color around every recolored region, which shows up immediately once the
render is composited over a dark background.
"""

ALPHA_MIN_CLUSTER = 128
"""Only well-opaque pixels feed k-means.

Antialiased edge pixels in a straight-alpha PNG carry blended RGB that would
otherwise create phantom clusters.
"""

SELECT_DENOISE_RADIUS = 3
"""Window radius of the selection denoise. 3 gives 7x7, enough to span one 4x4
texture-compression block from any pixel inside it."""

SELECT_DENOISE_SIGMA_S = 2.0
"""Spatial falloff of the selection denoise, in pixels."""

SELECT_DENOISE_SIGMA_R = 5.0
"""Color falloff of the selection denoise, in LAB units. Block noise is a few
units and gets averaged; a material boundary is tens of units and does not."""

PROXY_SIZE = 512
"""Edge length of the interactive preview proxy. Full res is used on export."""

KMEANS_SAMPLE = 40_000
KMEANS_DEFAULT_K = 12
KMEANS_ITERS = 30

CHROMA_EPS = 2.0
"""Below this C*, a color is treated as neutral and its hue is meaningless."""

CHROMA_MAX = 128.0
"""Upper anchor for the chroma remap curve."""

IMAGE_CACHE_SIZE = 4
"""How many ImageContexts to keep loaded (flipping between palettes stays instant)."""

# --- wiki downloader -------------------------------------------------------

WIKI_BASE_URL = "https://dragdown.wiki"
WIKI_API_URL = f"{WIKI_BASE_URL}/w/api.php"
WIKI_PAGE_PREFIX = "RoA2/"
USER_AGENT = (
    "RoA2-render-downloader/2.0 "
    "(https://github.com/Watherum/Rivals-2-Palette-Creator)"
)
DOWNLOAD_DELAY = 0.4
DOWNLOAD_TIMEOUT = 60
API_TIMEOUT = 30
