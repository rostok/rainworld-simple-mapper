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
python mapper.py --slugcat artificer          # campaign-specific file variants (repeatable; default: the --mod's campaigns)
python mapper.py --no-local-mods              # ignore StreamingAssets/mods/*
python mapper.py --no-deps                    # don't pull in the --mod's modinfo `requirements`
python mapper.py --no-load-order              # ignore the game's ModLoadOrder, use the default stacking
python mapper.py --no-merged                  # don't use StreamingAssets/mergedmods (game-baked modify/ patches)
python mapper.py --no-inherit                 # only the slugcat's own -<id> variants, no SlugBase world_state chain
python mapper.py --options C:\path\options    # game options file (EnabledMods / ModLoadOrder) if not in the default place
python mapper.py --separate-layers            # also emit <region>_L<N>.svg per layer
python mapper.py --outline                    # cream-bg wireframe style instead of the default filled style
python mapper.py --invert                     # invert every colour in the palette
python mapper.py --no-optimize                # one rect per solid-tile row (legacy output) instead of merged paths
python mapper.py --fix-overlaps --optimize    # Kamada-Kawai layout pass to shorten connections, then de-overlap
python mapper.py --mod drought --changes      # report: how each region differs from vanilla + DLC (no SVGs)
python mapper.py --mod drought --changes --changes-json d.json   # + every per-room detail as JSON
python mapper.py --mod drought --only-changed # SVGs only for regions that differ from the baseline
python mapper.py --mod drought --show-changed # tint rooms by change status + legend (combine with --only-changed)
```

`--mod` accepts a workshop id, an absolute path, or a substring of the id or
the mod's `name` (from `modinfo.json`). A substring that matches more than one
mod is rejected — make it more specific, or pass each mod as its own `--mod`
argument. Run `--scan` first to see what's available.

`--game` points at the Rain World install root (the folder that contains `RainWorld_Data/`). The vanilla world path and the workshop content path (`<steamapps>/workshop/content/312520`) are both derived from it. Use `--vanilla` or `--workshop` to override either independently.

Output: `out/<region>.svg` — one baked map per region. Only when the
selected mods offer more than one campaign slugcat (or several `--slugcat`
are given) does each get its own folder: `out/<slugcat>/<region>.svg`. Open in
a browser or convert with ImageMagick:

```
magick out/su.svg -density 200 out/su.png
```

## Change metrics vs vanilla (`--changes`, `--only-changed`, `--show-changed`)

Code: `mapdiff.py`. Tests: `tests/test_mapdiff.py`.

**Baseline** = vanilla + the enabled local mods (More Slugcats, Watcher, …)
with *only their own* `modify/world` patches applied. The game's `mergedmods`
can't be used for it: it mixes in every enabled workshop mod's patches. So
`mapdiff.build_baseline_merged` re-applies the DLC patches into
`.cache/baseline-merged/`, a port of the game's `ModMerger` (`ModManager.cs`):
`[FIND*]/[REPLACE*]/[ADD*]` line edits and the `[MERGE]` mergers for
`world_*.txt`, `*_settings*.txt`, `properties.txt` and `map_*.txt`. Checked
against the real `mergedmods`: 389/391 baked files are identical; the other 2
are files that enabled workshop mods also patch. Not ported:
`[FILEDESTINATION]` (6 sub-patches, reported in the console) and map PNGs.
With `--no-merged` the baseline skips patches too, so both sides match.
The baseline takes every layer from `StreamingAssets/mods` except a `--mod`,
including local mods pulled in as a `--mod`'s requirement (e.g. More Slugcats
for The Bestowed).

**Modded side.** With `--mod`, the diff doesn't read the game's
`mergedmods`: that folder carries the patches of *every* enabled workshop
mod (e.g. The Bestowed's `DM_REALGATE`, Pilgrims' Ascent's UW edits), which
would be blamed on the `--mod`, and it lacks the `--mod`'s own patches when
the mod is disabled in the game. Instead the patches of the mapper's layers
(DLC + `--mod` + requirements) are baked into `.cache/mod-merged/` with the
same merger. Without `--mod` the modded side is the whole current game
(real `mergedmods`). Regions whose comparison fails are listed under
`NOT COMPARED` in the report.

Both sides go through the same `Resolver` and slugcat chain, so the diff
compares what the game would load for that campaign. Per region, each
dimension is a 0..1 distance:

| Dimension | Source | Measure |
|---|---|---|
| rooms (20%) | `world_<r>.txt` ROOMS, timeline-filtered, HIDEROOM/EXCLUSIVEROOM applied | 1 − Jaccard of room names |
| geometry (30%) | room files, tile grid | changed tiles / total area (new and removed rooms count fully; size changes count the difference) |
| connections (15%) | ROOMS connection lists (+ substitute links) | 1 − Jaccard of room-pair edges |
| creatures (20%) | CREATURES, timeline-filtered | 1 − Σmin/Σmax over (room, creature) counts; LINEAGE = 1 per den |
| objects (10%) | `<room>_settings.txt` PlacedObjects | same multiset distance over (room, object type) |
| layout (2.5%) | `map_<r>.txt` devPos / layer | share of shared rooms that moved, after removing the median whole-map shift |
| properties (2.5%) | `properties.txt` | 1 − Jaccard of lines |

**Score** = weighted sum. **Class**: `new` (region not in baseline),
`same` (nothing differs), `tweaked` (score < 10%), `reworked`.

**Room status**: `new`, `new-copy`, `new-derived`, `geometry`, `moved`,
`content` (creatures / objects / connections / tag), `same`, `removed`.

**Where new rooms come from.** Every baseline room (vanilla + DLC, ~3.4k) gets
a fingerprint: an exact hash of its tile grid plus a 24×24 air-occupancy
bitmap. A room new to a region is matched against all of them: identical →
`new-copy` (e.g. a gate copied from `GATE_UW_LC`); bitmap Jaccard ≥ 0.85
between rooms within 25% of each other's size → `new-derived`; else original.
Fingerprints are cached per file in `.cache/room-index.pkl` (the first run
parses all rooms, ~12 s).

`--changes` output: one row per region (class, score, room counts
`+new/-removed/~changed/=same`, every dimension, new-room origins by source
region), then a mod summary: rooms that are new original / copied or derived
from baseline / changed baseline / untouched baseline / removed.

`--show-changed` tints rooms (45% opacity, under the geometry) by status,
outlines removed rooms with a dashed red frame at their baseline position,
and adds a legend with the region's class and score. Hovering a room in a
browser shows its details (source room, % tiles changed, creature deltas).
Without the flag the SVG colours are unchanged. `--only-changed` skips
regions whose class is `same`; the per-region log line ends with
`[class score]`.

## Baking: what goes into a map

The goal is the map the game itself would use for the mod's campaign. With
`--mod X` the mapper stacks, by default:

1. **Requirements** — `X`'s modinfo `requirements`, recursively. Missing ones
   are a warning. Code-only ones (SlugBase, Fisobs, …) appear in the printed
   dependency tree but add no layer. `--no-deps` turns this off.
2. **Local mods** — `StreamingAssets/mods/*`, but only those enabled in the
   game (`EnabledMods` in the options file); disabled ones (e.g. The Watcher)
   are listed as skipped.
3. **Load order** — layers are sorted by the game's `ModLoadOrder` from the
   options file (`%USERPROFILE%\AppData\LocalLow\Videocult\Rain World\options`):
   higher load order wins, as in `AssetManager.ResolveFilePath`. Without it
   (or with `--no-load-order`): `--mod` mods, requirements (dependents above
   their dependencies, workshop above local), other local mods.
4. **mergedmods** — `StreamingAssets/mergedmods/world`, the game's own bake of
   every enabled mod's `modify/` patches, is the top layer (`--no-merged`
   turns it off). `modify/world` folders themselves are never read as files:
   they are patches (`[MERGE]`, `[FIND]`, `[FILEDESTINATION]`, …). mergedmods
   only reflects mods enabled when the game last applied mods — a warning
   lists layer mods that aren't enabled.
5. **Vanilla** — `StreamingAssets/world`.

**Slugcat & inheritance.** Campaign slugcats of the `--mod` mods come from
SlugBase definitions (`slugbase/*.json`) and from `map_<region>-<id>.txt`
suffixes. A SlugBase slugcat's `world_state` list is its inheritance chain —
e.g. The Evergreen: `TrynsEvergreen -> Saint`. A suffix that belongs to a chain
(Evergreen's `map_su-saint.txt`) is not a separate campaign. `--slugcat`
overrides the choice; `--no-inherit` drops the chain.

**Lookup.** Variant-major, like the game's per-file resolution
(`HUD/Map.cs:3567`): all layers are searched for `-<chain[0]>`, then
`-<chain[1]>`, …, then the plain name; within one variant the highest layer
wins. So Evergreen's SL map is MSC's `map_sl-saint.txt` (with the SL_SCRUSHED
shelter) even though no Evergreen file exists for SL.

The map file decides which rooms are drawn, with one exception. A campaign
map (`map_<region>-<slugcat>.txt`, own or inherited) can predate rooms that
other mods patch into the plain `map_<region>.txt`. The game still loads them
from `world_<region>.txt`; only its map omits them. Example: Evergreen's
`map_sb-TrynsEvergreen.txt` lacks Forsaken Station's `SB_SE01`–`04` and
`GATE_FT_SB`. Such rooms are copied from the plain map when
`world_<region>.txt` keeps them for the slugcat: the room line passes its
`(a,b)` / `(X-a,b)` filter, and the room is not `HIDEROOM`ed for it or
`EXCLUSIVEROOM` to another slugcat. These are the same rooms the dev-tools
map writer would save. They are shifted by the median devPos offset between
the two maps' shared rooms, and their `Connection:` lines come along.

## Console output

Before rendering the mapper prints the dependency tree, skipped (disabled)
local mods, the final layer stack with each layer's role (`merged` /
`selected` / `dependency` / `local` / `vanilla`) and where the order came from,
and each slugcat with its lookup chain. With several campaign slugcats an
`info:` line says each goes to its own folder.

Each region line is prefixed with a source tag derived from which layers actually contributed files for that region:

- `vanilla` — every file came from `StreamingAssets/world`.
- `MOD[<a>]` — that mod fully replaced the region; nothing fell through to vanilla.
- `MOD[<a>+<b>+vanilla]` — multiple sources contributed. Non-vanilla layers are listed first so they stand out at a glance. `mergedmods` is the game-baked layer.

When a slugcat is active the line also names the map file that was baked in
and why: `map=map_sl-Saint.txt[More Slugcats Expansion] (inherited TrynsEvergreen->Saint)`,
`(own)` for the slugcat's own variant, `(plain)` for the unsuffixed file.
Rooms filled in from the plain map are appended:
`+5 rooms from map_sb.txt[mergedmods]`.

After the region lines a closing summary repeats what the early `info:` line
said, where it can't scroll away: with several campaigns, each campaign's
folder; and campaigns the `--mod` mods offer that a `--slugcat` choice left
out.

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
