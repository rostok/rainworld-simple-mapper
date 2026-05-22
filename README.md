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
python mapper.py --separate-layers            # also emit <region>_L<N>.svg per layer
python mapper.py --filled                     # solid-block style instead of outline (default)
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

Default style is **outline** (wireframe, mirrors `rain-world-map`'s look):

- **Black 0.35-stroke lines** — boundary edges between solid and non-solid tiles (RLE'd per row/column)
- **Black diagonal lines** — slope tiles, oriented by their two solid neighbors (per `generateGeoJSON.py:305–315`)
- **Black shelf glyphs** — half-floors (`Floor` terrain): top + mid horizontals plus side stubs depending on whether the neighbor is solid or air
- **Grey 0.4-stroke lines** — vertical / horizontal beams (climbable poles/bars) through tile centers
- **`≡` glyphs** — shortcut entrances/exits (anchored at the tile coords from `Connection:` lines)
- **Green lines** — room-to-room shortcut connections, anchored at the two `≡` endpoints
- **Red labels** — uppercase room names at room centers
- Cream background

Pass `--filled` to revert to the older solid-block style (3-unit black rects for solid/slope, thin grey shelves for floors, beams as thin lines).

Rooms are placed using `devPos` (the dev-editor layout, fields [2,3] of `map_<region>.txt`), matching `rain-world-map/generateGeoJSON.py:74` (`devPos*10` with 20 px/tile). devPos lays rooms out flat with no overlap; the in-game pause map's `canPos` (fields [0,1]) compresses rooms into stacked layers and is not used here. 1 tile = 2 devPos units; the room's `devPos` value is its lower-left corner.

SVG draw order: cream background → per-layer geometry (`<g id="layer-N">`, all full opacity) → connection lines (`<g id="connections">`, on top of solids) → room labels (`<g id="labels">`, top of everything). Pass `--separate-layers` to additionally emit one `<region>_L<N>.svg` per `canLayer` — rarely needed with devPos.

## Format notes

Sourced from decompiled `quicksave-mod/game-source/`:

- `World.cs:917` — `mapPos = float.Parse(fields[0]), float.Parse(fields[1])` from `<x>><<y>><...`
- `World.cs:1091` — room footprint = `size * 3` map units (so 1 tile = 3 map units, used as SVG unit)
- `Room.cs:5418` — tile data on `lines[11]`, `|`-separated entries, `,`-separated attrs; iteration order: `x=0, y=H-1`; `y--`; on `y<0` reset and `x++`
- `Room.cs:58` — `TerrainType { Air, Solid, Slope, Floor, ShortcutEntrance }`
