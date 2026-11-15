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

The window opens maximized and is split by question:

| Panel | Answers |
|---|---|
| **Left** | *What gets recolored* — source render, extracted palette, Eyedropper, Box/Lasso regions, the Pen, Match mode, Tolerance, Edge softness |
| **Right** | *What it becomes* — the rules stack, the color wheel, palette colors, apply strength |
| **Toolbar** | View, Fit, undo/redo, Export / presets, and the zoom slider |

1. Pick a **character → skin → palette**. The dropdowns are built by scanning
   `Resources/Character_Renders_Source`.
2. Pick the color to replace, either way:
   - click a chip in **Extracted palette** (k-means over the render), or
   - press **Eyedropper** and click the image.
3. Set the replacement with the **wheel** — **Brightness** underneath it is the
   wheel's third axis, since the disc itself only shows hue and saturation — or
   the **hex** field, the **R/G/B** fields, the **system picker**, or a chip in
   **Palette colors**. They all stay in sync. That chip strip has its own
   **Colors (k)**, independent of the left grid's: how many colors you want to
   pick *from* is a different question to how finely you want to split the
   render for *selecting*.
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
| Paint a stroke | **✎ Pen**, then drag on the image (a click is a dab) |
| Zoom | Mouse wheel (about the cursor), or the toolbar **Zoom** slider (about the centre) |
| Fit the whole sprite | **Fit** |
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
- **Box / Lasso** confine a rule to an area you draw — the way to separate two
  parts that happen to share a color. Finishing the shape creates the rule,
  sourced from the dominant material inside it.
- **Draw onto selected rule** changes that: while it is on, a finished Box or
  Lasso confines the rule you have *already* selected instead of making a new
  one, keeping its source, target, tolerance and mode. This is how you scope a
  rule you have tuned — drawing normally rebuilds a rule from whatever is inside
  the shape and throws all of that away. It stays on until you turn it off, and
  it lights up green like the drawing tools. With nothing selected, or with a
  pen rule selected, the shape falls back to making its own rule and the status
  line says why. The **Pen** always makes its own rule: dropping a stroke onto a
  color rule would replace that rule's entire selection.
- **Strength** is how far toward the target color a pixel moves, not layer
  opacity. Turning a rule down reveals the *original art*, never the rule below
  it — see the ownership rule above.

### The Pen

**✎ Pen** paints pixels by hand, for the details no color rule can reach: a
stripe, a marking, a stray pixel the k-means split the wrong way. **Pen size**
sets the stroke thickness in pixels of the *full-resolution* render, so the
number means the same thing at any zoom and in the export.

- **Each unbroken stroke becomes its own rule.** Draw, then pick a color for it
  on the right — and pick a different one later by selecting that rule again. A
  single click is a valid dab.
- The pen stays armed after a stroke, so strokes come in runs. Press **✎ Pen**
  again to put it down.
- Finishing a stroke does **not** flip the canvas to the Selection view the way
  a palette or eyedropper rule does. That view shows one rule's mask over the
  original art, so it would blank every stroke you already had after every
  single one — and a stroke is already drawn on the canvas, so it would be
  telling you nothing you could not see. A stroke with no color yet is marked
  with a magenta band; the selected one carries a dashed centerline.
- **Every pixel the stroke covers takes the color flat**, whatever it was
  before. There is no color match involved, so **Match**, **Tolerance** and
  **Edge softness** gray out while a stroke is selected; **Smooth edges** stays
  live, because it feathers the band itself.
- This is the one place the engine does not preserve a tonal ramp, and it is
  deliberate. The color rules anchor their levels curve on the rule's *source*
  color, which is right when the rule was aimed at one material — but a stroke
  crosses whatever it crosses. Anchored that way, a stroke drawn across an
  orange leg onto a white wrap left the white all but untouched: red over white
  came out white. Shading is still available, on the sliders that already say
  what they do — **Lightness shift 0** keeps each pixel's own brightness and
  takes only hue and saturation, **Strength** blends back toward the original.
- **New strokes go to the top of the rules list.** Rules own their pixels
  outright, so a stroke appended below the color rules already in the list would
  simply be painted over — 83% of one stroke's pixels, measured on a real
  render. A hand-drawn stroke is the most specific thing you can say about a
  pixel, so it gets the last word.
- Transparent pixels are never painted, however far past the silhouette the
  stroke goes.

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
bites, draw a **Box** or **Lasso** — that is the spatial discriminator
available when color alone cannot separate two materials.

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
| Canvas redraw, any zoom from 6% to 1600% | ~15 ms |

Interaction runs against the proxy; 250 ms after you stop moving a slider the
full-resolution pass swaps in and the toolbar's zoom badge turns green. Slider
callbacks only set a dirty flag, and a single 16 ms pump renders at most once
per tick, so a fast drag cannot queue up hundreds of renders.

The canvas scales **only the part of the image inside the viewport**. Scaling
the whole frame costs with the square of the zoom — 2.5 s and ~800 MB for one
redraw at 16× on a 1024px render — while the visible area never exceeds the
canvas, which is what keeps the last row above flat. Swapping the proxy for the
full-resolution frame rescales the zoom rather than refitting, so an edit never
throws away where you were looking.

## Layout

```
main.py                     entry point
install_requirements.bat    one-time dependency install
RUN_PALETTE_CREATOR.bat     launch
run_debug.bat               launch with a console
MAKE_RELEASE.bat            zip a release into dist/            (dev only)
BUILD_EXE.bat               build the standalone exe into dist/ (dev only)
palette_creator.spec        PyInstaller recipe for the exe      (dev only)
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
Python_Scripts/
  download_rivals_renders.py   the downloader as a standalone CLI
scripts/
  check_env.py              interpreter and package versions
  validate_assets.py        open every PNG, report corruption    (dev only)
  bench_recolor.py          accuracy against official palettes   (dev only)
  make_release.py           build the release zip                (dev only)
  build_exe.py              build and smoke test the exe         (dev only)
tests/                      pytest                               (dev only)
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
The pen has its own set: a stroke rasterizes to the same band at proxy and full
resolution, takes every pixel it covers whatever the color, paints flat, and
never reaches a transparent pixel.

## Releases

```
MAKE_RELEASE.bat                                 source only, ~85 KB
MAKE_RELEASE.bat --include-renders               adds the 382 MB render tree
python scripts\make_release.py --force           overwrite an existing zip
```

Writes `dist\Rivals-2-Palette-Creator-v<version>.zip`, versioned from
`palette_creator.__version__` and rooted in a single folder so extracting never
sprays the current directory. Re-running refuses to clobber an existing archive
until you pass `--force`.

The zip carries only what an end user needs to install, run, and troubleshoot:
the package, the three launch scripts, `requirements.txt`, this README,
`check_env.py`, and the downloader CLI. Everything marked *(dev only)* above is
left out — `pytest` is not in `requirements.txt`, so a release recipient could
not run the tests anyway. `run_debug.bat` ships precisely because
`RUN_PALETTE_CREATOR.bat` launches through `pythonw.exe` with no console:
without it, a failed start is silent and leaves the user nothing to report.

Renders are excluded by default, since the Downloads tab refetches them. When
included they are **stored rather than deflated** — PNG data is already
compressed, and deflating it *grew* the archive from 382 MB to 418 MB. No step
in the release path decodes or re-encodes an image; every archived file is
byte-identical to its source, which the build verifies by CRC-32 against the
original bytes before it reports success.

## Standalone exe

```
BUILD_EXE.bat                                    dist\Rivals-2-Palette-Creator-v<version>.exe
BUILD_EXE.bat --include-renders                  also zips the exe with the render tree
BUILD_EXE.bat --clean                            rebuild the build venv and caches first
```

One windowed exe, about 60 MB, that runs on a Windows machine with no Python
installed. It keeps its data in a `Resources` folder **next to the exe**
(created on first download, export or preset save), so put the exe in a folder
of its own rather than somewhere read-only. Renders come from the Downloads tab
or from the `--include-renders` zip.

`scripts\build_exe.py` does the work in three steps:

1. Creates a private venv, `.venv-build`, with only `requirements.txt` and
   PyInstaller. PyInstaller bundles whatever the interpreter can import, so
   building from the global Python would drag unrelated packages in.
2. Runs `palette_creator.spec`. The version comes from
   `palette_creator.__version__`; drop a `Resources\app.ico` in to give the exe
   an icon.
3. Copies the exe to an empty temp folder and starts it with `--smoke-test`,
   which imports every dependency, round-trips a color through scikit-image,
   and builds the real window once. The build fails unless that passes, so a
   module or data file PyInstaller missed is caught here rather than on a
   user's machine.

The first build takes a few minutes; later ones reuse the venv and the
PyInstaller cache. The exe unpacks itself on every launch, so expect a few
seconds before the window appears.

`.github\workflows\build-exe.yml` runs the same build on GitHub: start it by
hand from the Actions tab to get the exe as a run artifact, or push a tag that
matches `__version__` (`git tag v1.0.1 && git push origin v1.0.1`) to have the
exe attached to a GitHub release.

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
