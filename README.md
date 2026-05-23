# simple-mapper

Renders rudimentary Rain World region geometry maps as SVG. Solids / platforms / slopes only — no background, no decorations. Workshop mod files take priority over vanilla; no source files are modified.

## Usage

```
python mapper.py                              # Champion mod, all regions
python mapper.py --region su                  # one region
python mapper.py --mod 1234567890             # different workshop id
python mapper.py --mod C:\path\to\mod\folder  # absolute path
python mapper.py --mod 3033569804 --mod 9876  # multiple, left = higher priority
python mapper.py --game D:\Games\RainWorld    # non-Steam or non-default install path
python mapper.py --slugcat artificer          # different campaign-specific files
python mapper.py --no-local-mods              # ignore StreamingAssets/mods/*
python mapper.py --separate-layers            # also emit <region>_L<N>.svg per layer
python mapper.py --outline                    # cream-bg wireframe style instead of the default filled style
python mapper.py --invert                     # invert every colour in the palette
python mapper.py --no-optimize                # one rect per solid-tile row (legacy output) instead of merged paths
python mapper.py --name-from-modinfo          # show "The Champion" instead of "3033569804" in source tags
```

`--game` points at the Rain World install root (the folder that contains `RainWorld_Data/`). The vanilla world path and the workshop content path (`<steamapps>/workshop/content/312520`) are both derived from it. Use `--vanilla` or `--workshop` to override either independently.

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

## Console output

Each region line is prefixed with a source tag derived from which layers actually contributed files for that region:

- `vanilla` — every file came from `StreamingAssets/world`.
- `MOD[<a>]` — that mod fully replaced the region; nothing fell through to vanilla.
- `MOD[<a>+<b>+vanilla]` — multiple sources contributed. Non-vanilla layers are listed first so they stand out at a glance. `<a>/modify` indicates the per-mod `modify/world` overlay layer.

Source labels default to the workshop ID (or local mod folder name). Pass `--name-from-modinfo` to read the `name` field from each mod's `modinfo.json` instead — e.g. `3033569804` becomes `The Champion`. Falls back to the folder name when `modinfo.json` is missing or malformed.

## Output optimization

By default the filled style merges every solid + slope tile in a connected component into a single `<path>` per room — no per-row rects, no seams between adjacent fills. Cuts the SVG output roughly in half (≈71 MB → ≈37 MB across all regions) and eliminates the anti-alias hairlines that used to leak through between adjacent rects.

Pass `--no-optimize` to fall back to the legacy per-row-rect emit (one `<rect>` per solid run, with a 0.02-unit overlap and `shape-rendering="crispEdges"` to hide seams). Outline style is unchanged either way — its output is line-based, not rect-based.

## What's drawn

Default style is **filled** (card-and-fills):

- **Dark background** (`#1a1a1a`) — same colour as solids, so solid masses appear to bleed off room edges.
- **White card per room** — a `room_bg` rect at each room's lower-left corner, sized `(w × h × TILE_UNITS)`.
- **Solids** — dark filled rects (RLE per row) on top of the room card.
- **Slopes** — dark triangles (`<polygon>`) oriented by the two solid neighbours, so only the diagonal hypotenuse cuts into the white card; the right-angle sides abut adjacent solid rects and visually merge with them.
- **Half-floors** — thin dark shelves at the top of the tile.
- **Beams** — mid-grey thin lines through tile centres.
- **Connections** — gold (`#ffd700`) dotted lines (`stroke-dasharray="1.2 1.2"`), anchored at `≡` glyphs; contrasts both the dark outer bg and the white room cards.
- **Red labels** — uppercase room names at room centers.

Pass `--outline` for the wireframe style (mirrors `rain-world-map`'s look):

- **Black 0.35-stroke lines** — boundary edges between solid and non-solid tiles (RLE'd per row/column)
- **Black diagonal lines** — slope tiles, oriented by their two solid neighbours (per `generateGeoJSON.py:305–315`)
- **Black shelf glyphs** — half-floors: top + mid horizontals plus side stubs
- **Grey lines** — vertical / horizontal beams through tile centres
- **Green lines** — connections, anchored at `≡` glyphs
- Cream background

Pass `--invert` (compatible with either style) to invert every hex colour in the palette.

Rooms are placed using `devPos` (the dev-editor layout, fields [2,3] of `map_<region>.txt`), matching `rain-world-map/generateGeoJSON.py:74` (`devPos*10` with 20 px/tile). devPos lays rooms out flat with no overlap; the in-game pause map's `canPos` (fields [0,1]) compresses rooms into stacked layers and is not used here. 1 tile = 2 devPos units; the room's `devPos` value is its lower-left corner.

SVG draw order: cream background → per-layer geometry (`<g id="layer-N">`, all full opacity) → connection lines (`<g id="connections">`, on top of solids) → room labels (`<g id="labels">`, top of everything). Pass `--separate-layers` to additionally emit one `<region>_L<N>.svg` per `canLayer` — rarely needed with devPos.

## Format notes

Sourced from decompiled `quicksave-mod/game-source/`:

- `World.cs:917` — `mapPos = float.Parse(fields[0]), float.Parse(fields[1])` from `<x>><<y>><...`
- `World.cs:1091` — room footprint = `size * 3` map units (so 1 tile = 3 map units, used as SVG unit)
- `Room.cs:5418` — tile data on `lines[11]`, `|`-separated entries, `,`-separated attrs; iteration order: `x=0, y=H-1`; `y--`; on `y<0` reset and `x++`
- `Room.cs:58` — `TerrainType { Air, Solid, Slope, Floor, ShortcutEntrance }`
