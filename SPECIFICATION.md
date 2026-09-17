# simple-mapper — Specification

Formal reference for what the tool does and how. `README.md` is the user-facing
usage guide; `CONVO.md` is the narrative design history; this document is the
specification — what each part is for, its inputs/outputs, and its contracts.

## 1. Purpose

Render rudimentary geometry maps (solids / platforms / slopes / shortcuts) of
Rain World regions as SVG, resolving files through the game's mod-override
chain (workshop mods > local `StreamingAssets/mods` > vanilla) without ever
writing to, or requiring write access to, the game installation. No game
runtime is involved — this is a pure offline parser + renderer over the
game's plain-text world data files.

Out of scope: cosmetic decoration (poles, background art, palette-driven
rendering), anything requiring the Unity/Mono runtime, and editing world
files (this is a read-only tool).

## 2. Inputs

| Source | Path pattern | Priority |
|---|---|---|
| Per-mod overlay | `<mod>/modify/world/<region>/...` | Highest (per `--mod` order) |
| Per-mod full replacement | `<mod>/world/<region>/...`, `<region>-rooms/...` | Next |
| Local enabled mods | `StreamingAssets/mods/<localmod>/world/...` | Next (unless `--no-local-mods`) |
| Vanilla | `StreamingAssets/world/<region>/...` | Lowest (always present as fallback) |

File types consumed per region:

- `map_<region>.txt` (or `map_<region>-<slugcat>.txt`) — room placement
  (`devPos`/`canPos`) and `Connection:` lines (shortcut endpoints).
- `world_<region>.txt` — `ROOMS` section; merged across **all** layers per
  mod `[MERGE]` semantics (not just the winning layer).
- `<region>-rooms/<roomname>.txt` (or `gates/`, `gate shelters/`) — per-room
  tile geometry.
- `DisplayName.txt` — in-game region name, same layer/slugcat resolution as
  `map_*.txt`.
- `modinfo.json` (per mod root) — `name` field, used for console source tags
  and `--mod` substring matching; tolerant of BOM and trailing commas.

All reads are `errors="replace"` decoded UTF-8; malformed lines are skipped,
not fatal.

## 3. Outputs

- `out/<region>.svg` — one file per region (default: `out/` next to the
  script; overridable with `--out`).
- `out/<region>_L<N>.svg` — one per `canLayer`, only with `--separate-layers`.
- Console: one line per region, prefixed with a source tag (`vanilla` /
  `MOD[<a>+<b>+vanilla]`) and a `(w×h [L0:n,...], N conns) -> path` summary.
  `--optimize` adds a `layout optimize: ... -> ...` line to stderr per
  region.

No PNG output is produced directly; convert externally
(`magick out/su.svg -density 200 out/su.png`).

## 4. Coordinate system

- **Tile grid** (per room): `(0,0)` = bottom-left, iteration order in the
  room file is `x=0, y=H-1` then `y--`, wrapping to next column on
  `y<0` (`Room.cs:5418`).
- **World placement**: `devPos` (fields `[2,3]` of the room's line in
  `map_<region>.txt`) is the room's lower-left corner, in `devPos` units.
  `TILE_UNITS = 2.0` devPos-units-per-tile (matches
  `rain-world-map/generateGeoJSON.py:74`, `devPos*10` at 20px/tile).
  `canPos` (fields `[0,1]`, the in-game pause-map layout, which compresses
  rooms into overlapping stacked layers) is parsed but **not** used for
  placement.
- **SVG space**: y-down (SVG convention), so world y is negated
  (`_svg_y`). 1 SVG unit = 1 devPos unit = `TILE_UNITS` per tile.

## 5. Rendering pipeline (`render_region`)

1. Resolve and parse `map_<region>.txt` → room placements + `Connection:`
   entries.
2. Resolve and parse each room's geometry file → per-tile terrain
   (`T_AIR/T_SOLID/T_SLOPE/T_FLOOR/T_SHORTCUT`) + beam flags.
3. Compute each room's world AABB from `devPos` + `(w,h)*TILE_UNITS`.
4. `detect_overlaps` — pairwise AABB overlap check across all rooms; always
   run, always reported as a warning if non-empty.
5. **Optional** (`--optimize`, requires `--fix-overlaps`): reposition rooms
   to shorten `Connection:` lines. See §6.
6. **Optional** (`--fix-overlaps`): `resolve_overlaps` — separate any
   remaining overlapping pairs.
7. Emit geometry per `canLayer` (`_emit_filled`/`_emit_filled_optimized`/
   `_emit_outline` depending on `--outline`/`--no-optimize`), connections
   (`≡` glyphs + lines, drawn on top), and labels (drawn topmost).
8. Write SVG(s).

Steps 1-4 and 7-8 are identical across `mapper.py`, `mapper_springs.py`,
`mapper_vpsc.py` — only step 5's algorithm differs (§6). Step 6 is likewise
shared, except `mapper_vpsc.py`'s own step-5 already performs overlap
removal internally, so step 6 there is a safety net rather than the primary
mechanism.

## 6. Layout optimization (`--optimize`)

**Problem statement**: `devPos` guarantees a flat, non-overlapping layout,
but on some maps the dev-editor placement is visually arbitrary — long,
crossing `Connection:` lines even with zero overlap. `--optimize`
repositions rooms to shorten and untangle those lines while `resolve_overlaps`
(or the algorithm's own overlap handling) keeps the result non-overlapping.

Three independent implementations exist, all sharing the parsing/rendering
pipeline (`mapper_springs.py` and `mapper_vpsc.py` are full copies of
`mapper.py` with only the layout step replaced). See README's
"Which script should I use?" table for the practical recommendation; details
below are the algorithmic contract of each.

### 6.1 `mapper.py` — Kamada-Kawai + door alignment (recommended default)

1. `_bfs_hop_distances`: all-pairs BFS hop count over the `Connection:`
   graph.
2. Ideal distance per pair: `hop==1` pairs use `(radius_a + radius_b) *
   scale_mult` (each room's own half-diagonal, not a global average — needed
   for maps with wildly uneven room sizes); farther pairs use
   `avg_edge_target * hop`. Weight `1/hop²`.
3. SMACOF stress majorization to a local minimum (bounded by `iterations`,
   early-exits on small movement).
4. `_refine_door_alignment`: treats "make connected doors coincide" as the
   linear constraint `pos_a - pos_b == offset_b - offset_a` (each door's
   offset from its own room's centre is translation-invariant), solved by
   damped Jacobi relaxation — corrects for off-centre doors that pure
   centre-to-centre stress ignores.
5. `render_region` alternates `_door_align_pass` (step 4, cheap) with
   *partial* `resolve_overlaps` passes (several cycles) rather than one
   KK pass + one full resolve, because a single full resolve undoes the
   door alignment (it's a greedy pairwise push blind to which rooms are
   connected).

Defaults: `scale_mult=0.85`, 14 realignment cycles, `damping=0.3` — swept
against `su`, `cc`, `sh`, `gw`, `sl`, `rp`.

### 6.2 `mapper_vpsc.py` — constrained stress majorization + parameter search

Implements the Dwyer/Koren/Marriott "stress majorization + separation
constraints" algorithm (the technique behind VPSC/Adaptagrams/WebCola),
built from scratch with no dependency beyond the standard library (no QP
solver exists in a zero-dependency Python stack):

1. Same graph-distance-derived stress targets as §6.1.
2. Per outer iteration: a few unconstrained SMACOF sweeps, a light
   `_refine_door_alignment` pass, then **projection**:
   `_remove_overlap_1d` along X then Y — an *exact* (not greedy-pairwise)
   least-squares projection onto the nearest non-overlapping layout for the
   current axis ordering, via a reduction to weighted isotonic regression
   (`_pava`, pool-adjacent-violators): with cumulative required gap `G_k`
   up to the k-th room in sorted order, the ordering+gap-constrained
   least-squares problem reduces exactly to `pava(x - G) + G`.
3. Axis-at-a-time projection isn't a complete overlap-removal guarantee
   (pairs not adjacent-in-order on *either* axis can still collide) — the
   same `resolve_overlaps` from `mapper.py` runs afterward as a safety net.
4. Optional `direction_bias` (default `0.0`, off): blends a door's
   position-relative-to-its-own-centre into the stress target's direction
   (not merely magnitude) for directly connected pairs. Measurably helps
   some maps (`su`: 14→7 line/room crossings) and measurably hurts others
   (`rp`: 15→19-28) by breaking stress majorization's non-increasing-stress
   guarantee on graphs with high room-size variance — left off by default,
   A/B on your map before enabling.
5. `optimize_layout_search`: no single `scale_mult` wins across regions
   (swept 0.5-1.0 on 4 regions, each region's optimum differed). Brute-forces
   a grid (`scale_mult` × seed, 18 combinations by default), running full
   optimize+resolve per combination, and keeps whichever has the shortest
   **actual door-to-door length** (`_total_door_length` — the literal
   endpoints of the rendered connection line, not room centres) among the
   attempts with fewest remaining overlaps. This is the function `--optimize`
   actually calls in this script.

### 6.3 `mapper_springs.py` — force-directed spring embedder (reference only)

Eades-style force-directed layout: Hooke's-law attraction along
`Connection:` edges (force grows with distance) plus inverse-square
repulsion between all room pairs (prevents disconnected components
collapsing together), iterated with damping, then `resolve_overlaps`.
Ad hoc (hand-tuned `k_attr`/`k_rep` constants, no formal objective, no
multi-hop distance reasoning) but occasionally produces the shortest raw
total connection length of the three, since it isn't constrained to respect
graph-distance proportionality the way Kamada-Kawai is. Kept for comparison,
not the recommended default.

### 6.4 Common metrics

- `_total_conn_length` — sum of room-centre-to-centre distances over all
  connections. Cheap proxy, used during development/tuning.
- `_total_door_length` (`mapper_vpsc.py` only) — sum of actual door-tile
  world-position distances. The metric that matches what's visually drawn;
  used by `optimize_layout_search`'s selection criterion. Centre-distance is
  a poor proxy for long/narrow or off-centre-door rooms.
- Segment/rectangle-intersection crossing count (ad hoc, used only in
  development sweeps, not wired into any script's console output) — counts
  connection lines that cut across an uninvolved room's bounding box, the
  literal visual complaint that motivated `direction_bias`.

None of the three implementations guarantees a globally optimal or
guaranteed-crossing-free layout; all reduce, not eliminate, the problem.

## 7. Known limitations

- Axis-at-a-time overlap projection (`mapper_vpsc.py`) is not a complete
  overlap-removal guarantee; relies on a greedy-pairwise fallback for the
  residue.
- `direction_bias` is unreliable across maps (helps some, hurts others) —
  off by default, not recommended without per-map A/B.
- No true 2D VPSC (QP-based) solver exists in any of the three scripts;
  all are approximations of the real algorithm family.
- Layout optimization operates on room **translation only** — no rotation,
  since Rain World rooms are axis-aligned and rotation isn't a real
  placement option in-game.
- `canLayer`/`--separate-layers` is a vestigial feature from before the
  switch to `devPos`; rarely needed since `devPos` doesn't produce
  overlapping layers the way `canPos` does.

## 8. File map

| File | Role |
|---|---|
| `mapper.py` | Main script — parsing, resolver, rendering, KK layout optimization (recommended). |
| `mapper_vpsc.py` | Full copy of `mapper.py` with constrained-stress-majorization + parameter-search layout optimization. |
| `mapper_springs.py` | Full copy of `mapper.py` with spring-embedder layout optimization (reference/comparison). |
| `README.md` | User-facing usage guide. |
| `CONVO.md` | Narrative design history (why decisions were made, in dialogue form). |
| `SPECIFICATION.md` | This file — formal contract/reference. |
