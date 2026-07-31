"""Paths and tunable constants shared across the app."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_ROOT / "Resources"
SOURCE_DIR = RESOURCES_DIR / "Character_Renders_Source"
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
