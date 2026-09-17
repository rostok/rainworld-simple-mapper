# simple-mapper

Renders rudimentary Rain World region geometry maps as SVG. Solids / platforms / slopes only — no background, no decorations. Workshop mod files take priority over vanilla; no source files are modified.

## Usage

```
python mapper.py                              # vanilla + local mods, all regions
python mapper.py --scan                       # list every mod that contributes world data, then exit
python mapper.py --region su                  # one region
python mapper.py --mod 1234567890             # workshop id
python mapper.py --mod champion               # partial id or modinfo `name` match (case-insensitive)
python mapper.py --mod C:\path\to\mod\folder  # absolute path
python mapper.py --mod champion --mod outsider # multiple, left = higher priority
python mapper.py --game D:\Games\RainWorld    # non-Steam or non-default install path
python mapper.py --slugcat artificer          # campaign-specific file variants (default: none)
python mapper.py --no-local-mods              # ignore StreamingAssets/mods/*
python mapper.py --separate-layers            # also emit <region>_L<N>.svg per layer
python mapper.py --outline                    # cream-bg wireframe style instead of the default filled style
python mapper.py --invert                     # invert every colour in the palette
python mapper.py --no-optimize                # one rect per solid-tile row (legacy output) instead of merged paths
python mapper.py --fix-overlaps --optimize    # Kamada-Kawai layout pass to shorten connections, then de-overlap
```

`--mod` accepts a workshop id, an absolute path, or a substring of the id or
the mod's `name` (from `modinfo.json`). A substring that matches more than one
mod is rejected — make it more specific, or pass each mod as its own `--mod`
argument. Run `--scan` first to see what's available.

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

Source labels use the `name` field from each mod's `modinfo.json` (e.g. `The Champion`, `More Slugcats Expansion`). Falls back to the workshop ID / folder name when `modinfo.json` is missing or malformed.

## Output optimization

By default the filled style merges every solid + slope tile in a connected component into a single `<path>` per room — no per-row rects, no seams between adjacent fills. Cuts the SVG output roughly in half (≈71 MB → ≈37 MB across all regions) and eliminates the anti-alias hairlines that used to leak through between adjacent rects.

Pass `--no-optimize` to fall back to the legacy per-row-rect emit (one `<rect>` per solid run, with a 0.02-unit overlap and `shape-rendering="crispEdges"` to hide seams). Outline style is unchanged either way — its output is line-based, not rect-based.

## Layout optimization (`--optimize`)

`devPos` (the layout `mapper.py` renders from) lays rooms out flat with no overlap, but on some maps the dev-editor placement is arbitrary — rooms end up scattered such that `Connection:` lines cross each other messily even though nothing overlaps. `--optimize` (must be combined with `--fix-overlaps`) runs a **Kamada-Kawai** layout pass before overlap resolution to shorten and untangle those connection lines:

1. BFS over the `Connection:` graph gives the hop-count distance between every pair of rooms (not just direct neighbours).
2. Each pair gets an ideal geometric distance proportional to that hop count (so directly-connected rooms want to be close, 2-hop rooms roughly twice as far, etc.), weighted `1/hop²` so nearby structure dominates. Rooms with no path between them get a mild fixed separation so unconnected components don't collapse onto each other.
3. Stress majorization (SMACOF) iterates every room's position to the weighted average of where it should sit to hit those ideal distances — this provably never increases total stress, converging to a local minimum without hand-tuned force constants.
4. `resolve_overlaps` then runs its existing AABB-separation pass, since the stress-minimized layout doesn't itself guarantee zero overlap.

Console output reports total connection length before/after the pass (sum of centre-to-centre distances over all connections) so you can gauge the improvement, e.g. `[su] layout optimize: total connection length 15148.3 -> 7747.1`.

An earlier force-directed (spring-embedder) attempt at the same problem is kept for reference in `mapper_springs.py` — same CLI, but `optimize_layout` there uses Hooke's-law attraction + inverse-square repulsion instead of stress majorization. It actually produces *shorter* total connection length on some maps (since it isn't constrained to respect multi-hop graph distance proportionally, just to shrink), but Kamada-Kawai's layout is more principled about preserving the graph's overall shape rather than just minimizing edge length.

The KK version measures success by centre-to-centre connection length, which is a poor proxy on maps with long/narrow or off-centre-door rooms (a shortcut at one end of a corridor, say): two rooms can have well-placed centres and still have their actual `Connection:` tiles far apart. `mapper.py`'s pipeline therefore also runs a door-offset alignment pass (`_refine_door_alignment` — treats "make connected doors coincide" as a linear constraint on room centres and relaxes toward it) interleaved with `resolve_overlaps`, since a naive one-shot resolve undoes the alignment the KK+door-align pass just built. Parameters (`scale_mult=0.85`, 14 realignment cycles, `damping=0.3`) were swept against `su`, `cc`, `sh`, `gw`, `sl`, `rp`.

A third variant, `mapper_vpsc.py`, replaces the "KK once, then repeatedly patch up overlap vs. door alignment" approach with **constrained stress majorization** (the algorithm behind VPSC/Adaptagrams/WebCola): each iteration does an unconstrained stress-majorization step, then *projects* onto the non-overlapping region via exact 1D isotonic-regression overlap removal (`_remove_overlap_1d`, axis-at-a-time) instead of `resolve_overlaps`'s greedy pairwise push. There's no pure-Python VPSC solver to depend on (reference implementations are C++ `libcola`/WebCola's JS port), so this hand-rolls the well-documented 2-step recipe (Dwyer, Koren & Marriott) rather than the real thing's single-shot 2D QP — it can still leave a handful of overlaps (pairs that don't collide "adjacent in sorted order" along either axis alone), caught by the same `resolve_overlaps` fallback. Results vs. the KK version are a wash: better on `rp` (13459 vs 14722 final connection length), worse on `su` (11866 vs 10444) — worth comparing directly on your map rather than assuming one wins.

`mapper_vpsc.py`'s `optimize_layout` also takes a `direction_bias` parameter (default `0.0`, off), from `_build_direction_overrides`: a door's position relative to its *own* room's centre (which half of the room it's in — not which wall it's nearest, and independent of which way the door glyph "faces") hints where the connected room should sit, so the connecting line doesn't have to double back across the room's own rectangle to reach an off-centre door. Measured with a segment/rectangle-intersection count (connection lines that cut across an uninvolved room — the actual visual complaint that motivated this): helps `su` (14 → 7 crossings at `direction_bias=1.0`) but hurts `rp` (15 → 19–28 depending on strength). Forcing a fixed angle breaks stress majorization's non-increasing-stress guarantee, and `rp`'s extreme room-size variance (one room 122×232 tiles vs. most under 100×100) seems to push it into oscillation instead of a better layout. Left off by default; try `direction_bias=1.0` (or intermediate values) and compare crossing counts on your specific map before turning it on.

### Automatic parameter search (`optimize_layout_search`)

`scale_mult` (the per-edge ideal-distance multiplier) turned out to have no single value that wins across regions: swept 0.5–1.0 on `rp`, `su`, `cc`, `sh` and each region's best differed (0.65, 0.65, 0.85, 0.7 respectively) — picking any one fixed default left 20–40% of achievable improvement on the table depending which map you ran it on. `--optimize` in `mapper_vpsc.py` therefore runs `optimize_layout_search`, which brute-forces a small grid (`scale_mult` × random seed, 6×3 = 18 combinations by default) and keeps whichever attempt has the shortest *actual door-to-door* connection length (not centre-to-centre — see `_total_door_length`) among those with the fewest remaining overlaps. Each attempt is a full `optimize_layout` + `resolve_overlaps` run, but at well under a second per attempt even on the largest regions (~130 rooms), the whole grid costs a few seconds per region — a full all-regions sweep (~107 regions) takes well under a minute in parallel. Console output reports which `scale_mult`/seed combination won, e.g. `[rp] layout optimize: door-to-door length 18443.4 -> 8746.4 (best of grid: scale_mult=0.65, seed=0)`.

### Which script should I use?

Three scripts implement `--optimize`, all with the same CLI:

| Script | Algorithm | When to reach for it |
|---|---|---|
| `mapper.py` | Kamada-Kawai stress majorization + door-offset alignment cycles | Default choice. Principled (preserves multi-hop graph shape), fixed `scale_mult=0.85` tuned across several regions. |
| `mapper_vpsc.py` | Constrained stress majorization (stress step + exact 1D overlap-removal projection) + `optimize_layout_search` grid search over `scale_mult`/seed | Generally the best result of the three (`_total_door_length`-optimal by construction), at the cost of ~5-10x runtime (18 attempts per region instead of 1). Use when `mapper.py`'s output still looks tangled and you can spare the extra seconds. Has an experimental `direction_bias` knob (off by default — regresses some maps, see below). |
| `mapper_springs.py` | Hooke's-law spring embedder + inverse-square repulsion | Reference/comparison only. Sometimes produces the shortest raw connection length of the three (no multi-hop-proportionality constraint holding it back), but with no principled reasoning about *why* — kept for A/B comparison, not the recommended default. |

None of the three guarantees a *globally* optimal, crossing-free layout — that would need a true 2D VPSC solve (a real QP solver), which none of these zero-dependency implementations have. All three still run `resolve_overlaps` (or in `mapper_vpsc.py`'s case, its own exact projection) as a final correctness net, so the output is always non-overlapping even when the layout isn't visually ideal.

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
