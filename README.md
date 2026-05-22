# simple-mapper

Renders rudimentary Rain World region geometry maps as SVG. Solids / platforms / slopes only — no background, no decorations. Workshop mod files take priority over vanilla; no source files are modified.

## Usage

```
python mapper.py                              # Champion mod, all regions
python mapper.py --region su                  # one region
python mapper.py --mod 1234567890             # different workshop id
python mapper.py --mod C:\path\to\mod\folder  # absolute path
python mapper.py --mod 3033569804 --mod 9876  # multiple, left = higher priority
python mapper.py --slugcat artificer          # different campaign-specific files
python mapper.py --no-local-mods              # ignore StreamingAssets/mods/*
```

Output: `out/<region>.svg` — open in a browser or convert with ImageMagick:

```
magick out/su.svg -density 200 out/su.png
```

## File resolution order

For each lookup, the first existing file wins. Within a layer, slugcat-suffixed variants (`map_oe-thechampion.txt`) beat the plain name.

1. `<mod>/modify/world/<region>/...` — `[MERGE]` overlay layer (per-mod, in `--mod` order)
2. `<mod>/world/<region>/...` and `<region>-rooms/...` — full replacement
3. `StreamingAssets/mods/<localmod>/world/...` — same two-step for each enabled local mod
4. `StreamingAssets/world/<region>/...` — vanilla

`world_<region>.txt` is **merged** across all layers (mod `[MERGE]` semantics): every layer's `ROOMS` section contributes connection pairs.

## What's drawn

- **Black rects** — solid / slope tiles (slope direction not distinguished)
- **Grey thin rects** — `Floor` (platform-from-above) tiles
- **Green lines** — room-to-room connections (centered on `mapPos`)
- **Red labels** — uppercase room names at room centers
- Cream background

## Format notes

Sourced from decompiled `quicksave-mod/game-source/`:

- `World.cs:917` — `mapPos = float.Parse(fields[0]), float.Parse(fields[1])` from `<x>><<y>><...`
- `World.cs:1091` — room footprint = `size * 3` map units (so 1 tile = 3 map units, used as SVG unit)
- `Room.cs:5418` — tile data on `lines[11]`, `|`-separated entries, `,`-separated attrs; iteration order: `x=0, y=H-1`; `y--`; on `y<0` reset and `x++`
- `Room.cs:58` — `TerrainType { Air, Solid, Slope, Floor, ShortcutEntrance }`
