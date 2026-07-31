# Rivals 2 Palette Creator

A desktop tool for recoloring Rivals of Aether 2 character select portraits, plus
the downloader that fetches them from [dragdown.wiki](https://dragdown.wiki/wiki/RoA2).

## Quick start

```
install_requirements.bat      once
run_palette_creator.bat       launch
run_debug.bat                 launch with a console, if something goes wrong
```

## The Recolor tab

1. Pick a **character → skin → palette**. The dropdowns are built by scanning
   `Resources/Character_Renders_Source`.
2. Pick the color to replace, either way:
   - click a chip in **Extracted palette** (k-means over the render), or
   - press **Eyedropper** and click the image.
3. Set the replacement with the **wheel**, the **hex** field, the **R/G/B**
   fields, or the **system picker**. All four stay in sync.
4. Tune **Tolerance** and the **Match** mode until the selection covers what you
   want. Switch the view to **Selection** to see exactly what a rule has grabbed.
5. Add as many rules as you need. Add rule 2 by clicking another swatch.
6. **Export** writes to `Resources/Character_Renders_Custom/{Character}/`.
   **Export As…** opens a save dialog. Source renders are never modified.

### Controls

| Action | Input |
|---|---|
| Set a rule's source color | Eyedropper + left click (3×3 median) |
| Sample one exact pixel | Eyedropper + **Shift** + click |
| Set a rule's *target* from the image | Eyedropper + **Ctrl** + click |
| Zoom | Mouse wheel (zooms at the cursor) |
| Pan | Right-click drag |
| **Undo** | **Ctrl+Z** (or the ↶ button) |
| **Redo** | **Ctrl+R** / **Ctrl+Y** (or the ↷ button) |

Undo covers every edit to the rules: adding, deleting, reordering, enabling,
picking colors, and moving any slider. A continuous gesture — dragging the
tolerance slider, sweeping the color wheel — collapses into a single undo step
rather than hundreds, and the status bar names what was undone. History holds
100 steps.

Rules run top to bottom, and an earlier rule keeps any pixel it claims. Reorder
with ▲▼. Every rule matches against the **original** image, so chaining
red→blue and blue→green cannot double-apply.

### Match modes

- **material** (default) — ignores lighting, so one click grabs a whole shaded
  material from its darkest shadow to its brightest highlight.
- **exact** — full CIELAB distance; only near-identical colors.
- **hue** — hue angle only, for a material that shares its tone with another.

### Useful sliders

- **Lightness shift = 0** keeps every pixel's original brightness and moves only
  hue and saturation. This is usually what you want for a subtle palette swap.
- **Edge softness** widens the ramp between selected and unselected, which is
  what keeps antialiased edges clean.
- **This region only** restricts a rule to the connected region you clicked —
  the way to separate two parts that happen to share a color.

## The Downloads tab

**Discover from wiki** lists the character pages, then **Start download** fetches
every CSP render into `Resources/Character_Renders_Source/{Character}/`.
Already-present files are skipped, so re-running only picks up what is new.
**Dry run** shows what would be fetched without writing anything.

Same thing from the command line:

```
python Python_Scripts\download_rivals_renders.py                       # everything missing
python Python_Scripts\download_rivals_renders.py --dry-run             # preview
python Python_Scripts\download_rivals_renders.py --characters Ranno    # one character
python Python_Scripts\download_rivals_renders.py --list-characters
```

Downloads stream to a temp file and are renamed only after the bytes validate as
a complete PNG, so an interrupted run cannot leave a corrupt render behind.

## How the recolor works, and what it cannot do

These renders are fully shaded and antialiased — around 7,000–13,000 distinct
colors in a single 1024×1024 image. A naive "replace RGB X with RGB Y" would
produce hard-edged blotches, so the engine does two separate things:

**Selection** measures distance in CIELAB, splitting the a\*/b\* offset into its
chroma and hue parts and downweighting lightness and chroma
(`(0.15, 0.35)` in material mode). Lighting a material moves it in both L\* and
C\* while leaving hue essentially fixed, so hue is the invariant worth matching
on. The cutoff is a smoothstep, never a hard threshold, which is what makes
antialiased edges come out right: a pixel that is a blend of two materials gets
partially recolored.

**The transform** is per-pixel and never a flat fill. L\* and C\* go through
monotone piecewise-linear curves anchored so the source tone maps exactly to the
target tone while 0 and the maximum stay put, plus a constant hue rotation.
That is a levels curve: it moves the material's anchor without flattening its
highlights or inverting its shading.

k-means is used **only for selection** — to extract the swatch palette and to
offer whole-cluster selection. Clustering runs in full CIELAB with no lightness
downweighting.

### Measured on this asset set

| | |
|---|---|
| Is an official recolor a color→color lookup table? | **No** — one source RGB maps to ~2.8 different targets |
| Predicting the official target from source color alone | 7.7/255 RMS |
| Official target spread *within* one k=12 CIELAB cluster | 33.7/255 |
| Same, with L\* downweighted before clustering | 43.7/255 — worse |

The first row is the important one: **the game recolors by material/UV region,
not by color**, so no color-driven tool can reproduce an official palette
exactly. Two parts that share a color in Neutral can diverge in Red. When that
bites, use **This region only** — it is the one spatial discriminator available
when color alone cannot separate two materials.

The third and fourth rows are why a cluster is never flat-filled, and why
lightness is not compressed before clustering.

Run the benchmark yourself:

```
python scripts\bench_recolor.py
```

It rebuilds each official palette from 12 auto-derived rules with no human
tuning and reports the error. Expect roughly 32/255 mean RMS, about 2.7× closer
than leaving the render untouched, against a 7.7 floor that only a
thousands-of-entries lookup table could reach.

## Performance

Measured on a 1024×1024 render, ~289k opaque pixels:

| | |
|---|---|
| Load + CIELAB precompute | ~135 ms, once per render |
| k-means, k=12 | ~50 ms |
| Preview render (512px proxy, 3 rules) | **~13 ms** |
| Full-resolution render | ~64 ms |

Interaction runs against the proxy; 250 ms after you stop moving a slider the
full-resolution pass swaps in and the **● full res** indicator lights. Slider
callbacks only set a dirty flag, and a single 16 ms pump renders at most once
per tick, so a fast drag cannot queue up hundreds of renders.

## Layout

```
main.py                     entry point
palette_creator/
  config.py                 paths and tunable constants
  library.py                scans the renders into character/skin/palette
  colorspace.py             sRGB <-> CIELAB, hex, gamut compression
  imagectx.py               cached pixel arrays + the 512px proxy
  recolor.py                masks, transforms, compositing      <- the core
  palette_extract.py        k-means -> swatches
  downloader.py             dragdown.wiki, GUI + CLI
  presets.py                rule sets as JSON
  ui/                       CustomTkinter widgets
scripts/
  check_env.py              interpreter and package versions
  validate_assets.py        open every PNG, report corruption
  bench_recolor.py          accuracy against the official palettes
tests/                      pytest
Resources/
  Character_Renders_Source/ downloaded renders (never modified)
  Character_Renders_Custom/ your exports
  Presets/                  saved rule sets
```

## Tests

```
python -m pytest                       # everything
python -m pytest -m "not network"      # skip the live-wiki tests
```

The recolor tests are written as invariants rather than golden images: alpha is
preserved byte for byte, unselected pixels stay bit-identical, shading spans
survive retargeting, lightness ordering never inverts, and rules never cascade.

## Notes on the asset set

- **Character comes from the folder name, never the `T_Xxx` prefix.**
  `Random/` ships files prefixed `T_Ran`, which collides with Ranno.
- `Random/` is excluded — its 5 files are not CSP renders.
- Clairen has 5 percent-encoded duplicates (`T_Cla_L%C3%A9on_*` alongside
  `T_Cla_Léon_*`). The app hides them and keeps the decoded name. Nothing is
  deleted; the downloader normalizes names so they cannot recur.
- **Armando** has no wiki page (`RoA2/Armando` does not exist and no `T_Arm_*`
  files are uploaded), so downloads will never produce renders for him. The
  folder was removed from this library.
- Palette and skin lists are always derived from what is on disk, never
  hardcoded — new ones appear automatically after a download.

## Possible next step

The library already contains paired ground truth: the same skin in Neutral, Red,
Blue and so on, differing only in color. Clustering each pixel's *signature
across all of a skin's palettes* could recover the true material/UV regions and
sidestep the limitation above entirely, giving exact selection. An initial probe
was inconclusive — the obvious metric measures the wrong thing, since one
material legitimately spans a wide color range — so this needs real research
time rather than a quick patch.
