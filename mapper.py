"""Render rudimentary Rain World region geometry maps as SVG.

Reads world data from Steam Workshop mods (priority) with fallback to
vanilla game files. Never writes to source folders.

Format references (from C:/projects/rainworld/quicksave-mod/game-source):
  - World.cs:917      mapPos parsed from "<x>><<y>><..." in map_<region>.txt
  - World.cs:1091     room footprint on map = size * 3 (centered on mapPos)
  - Room.cs:5418      tile data on lines[11]; columns top->bottom, left->right
  - Room.cs:58        TerrainType enum {Air, Solid, Slope, Floor, Shortcut}
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

GAME_DEFAULT = r"C:\Program Files (x86)\Steam\steamapps\common\Rain World"


def vanilla_for(game_root):
    return os.path.join(game_root, "RainWorld_Data", "StreamingAssets")


def workshop_for(game_root):
    # Steam: <steamapps>/common/Rain World → <steamapps>/workshop/content/312520
    return os.path.normpath(os.path.join(
        game_root, "..", "..", "workshop", "content", "312520"
    ))

T_AIR, T_SOLID, T_SLOPE, T_FLOOR, T_SHORTCUT = 0, 1, 2, 3, 4
# devPos units per tile = 2, matching generateGeoJSON.py:74 (`devPos*10` with
# 20 px per tile). devPos is the dev-editor placement — rooms tile naturally.
TILE_UNITS = 2.0

PALETTES = {
    "filled": {
        "bg": "#1a1a1a",
        "room_bg": "#ffffff",
        "solid": "#1a1a1a",
        "floor": "#1a1a1a",
        "beam": "#666666",
        "conn": "#ffd700",
        "conn_dash": "1.2 1.2",
        "mark": "#cccccc",
        "label": "#e63946",
    },
    "outline": {
        "bg": "#f6f1e7",
        "room_bg": None,
        "solid": "#000000",
        "floor": "#000000",
        "beam": "#777777",
        "conn": "#27ae60",
        "conn_dash": None,
        "mark": "#1f4e79",
        "label": "#c0392b",
    },
}


def _invert_hex(c):
    if c is None or not c.startswith("#") or len(c) != 7:
        return c
    r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
    return f"#{255 - r:02x}{255 - g:02x}{255 - b:02x}"


def invert_palette(p):
    return {k: (_invert_hex(v) if isinstance(v, str) and v.startswith("#") else v)
            for k, v in p.items()}


def _load_mod_name(mod_root, fallback):
    """Read the `name` field from `<mod_root>/modinfo.json` and return it.
    Falls back to `fallback` on any failure (missing file, parse error,
    missing/empty field). Tolerates trailing commas — several shipped
    modinfo.json files include them, which the stdlib json parser rejects."""
    p = os.path.join(mod_root, "modinfo.json")
    if not os.path.isfile(p):
        return fallback
    try:
        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
            txt = f.read()
        txt = re.sub(r",(\s*[}\]])", r"\1", txt)
        data = json.loads(txt)
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    return fallback


def scan_world_mods(workshop_root, local_mods_dir):
    """List every workshop / local mod that contributes world data.

    A mod qualifies if its root contains a `world/` or `modify/world/`
    subdirectory — the two layers the Resolver consults for world files.
    Returns a list of dicts with keys: source ('workshop' | 'local'), id,
    folder (== id), name (from modinfo.json, falling back to folder), path.
    """
    results = []
    for root, source in ((workshop_root, "workshop"), (local_mods_dir, "local")):
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            p = os.path.join(root, entry)
            if not os.path.isdir(p):
                continue
            if not (os.path.isdir(os.path.join(p, "world")) or
                    os.path.isdir(os.path.join(p, "modify", "world"))):
                continue
            results.append({
                "source": source,
                "id": entry,
                "folder": entry,
                "name": _load_mod_name(p, entry),
                "path": p,
            })
    return results


def detect_mod_slugcats(mod_root):
    """Return the set of slugcat ids this mod ships files for.

    Looks inside `world/<region>/` and `modify/world/<region>/` for files
    matching `map_<region>-<slugcat>.txt` — the same suffix pattern the
    Resolver consults via `_variants`. A mod with only plain `map_<region>.txt`
    files returns an empty set."""
    slugcats = set()
    for sub in ("world", os.path.join("modify", "world")):
        base = os.path.join(mod_root, sub)
        if not os.path.isdir(base):
            continue
        for region in os.listdir(base):
            rd = os.path.join(base, region)
            if not os.path.isdir(rd):
                continue
            prefix = f"map_{region}-"
            for f in os.listdir(rd):
                if f.startswith(prefix) and f.endswith(".txt"):
                    slug = f[len(prefix):-4]
                    if slug:
                        slugcats.add(slug)
    return slugcats


def resolve_mod(arg, mods_index):
    """Resolve a --mod argument to a mod root path.

    Accepts an absolute path, an exact id/folder match, or a case-insensitive
    substring of either the id or the modinfo `name`. Multiple matches raise
    SystemExit — pass each one as its own --mod arg instead.
    """
    if os.path.isabs(arg) and os.path.isdir(arg):
        return arg
    exact = [m for m in mods_index if m["id"] == arg]
    if len(exact) == 1:
        return exact[0]["path"]
    a = arg.lower()
    partial = [m for m in mods_index
               if a in m["id"].lower() or a in m["name"].lower()]
    if len(partial) == 1:
        return partial[0]["path"]
    if len(partial) > 1:
        listing = ", ".join(f"{m['id']} ({m['name']!r})" for m in partial)
        raise SystemExit(
            f"--mod '{arg}' is ambiguous; matched {len(partial)}: {listing}. "
            f"Use a more specific substring, or pass each as its own --mod arg."
        )
    raise SystemExit(f"--mod '{arg}' did not match any world-changing mod")


class Resolver:
    """File lookup. Layer order (highest priority first): per-mod modify/world,
    per-mod world, then vanilla world. Within a layer, slugcat-suffixed
    variants (e.g. `map_oe-thechampion.txt`) win over the plain name."""

    def __init__(self, mod_roots, vanilla_root, slugcat=None):
        self.layers = []
        self.labels = []   # parallel to self.layers; short tag for console output
        for m in mod_roots:
            base = os.path.basename(os.path.normpath(m))
            # Source tags use the modinfo `name` (e.g. "The Champion") rather
            # than the workshop id, so MOD[…] is recognizable at a glance. Falls
            # back to the folder name when modinfo.json is missing / malformed.
            name = _load_mod_name(m, base)
            self.layers.append(os.path.join(m, "modify", "world"))
            self.labels.append(f"{name}/modify")
            self.layers.append(os.path.join(m, "world"))
            self.labels.append(name)
        self.layers.append(os.path.join(vanilla_root, "world"))
        self.labels.append("vanilla")
        self.slugcat = slugcat

    def _variants(self, fname):
        if not self.slugcat:
            return [fname]
        base, ext = os.path.splitext(fname)
        return [f"{base}-{self.slugcat}{ext}", fname]

    def region_file(self, region, fname):
        for label, layer in zip(self.labels, self.layers):
            for v in self._variants(fname):
                p = os.path.join(layer, region, v)
                if os.path.isfile(p):
                    return p, label
        return None, None

    def room_file(self, region, room_name):
        for label, layer in zip(self.labels, self.layers):
            for v in self._variants(room_name.lower() + ".txt"):
                for sub in (f"{region}-rooms", "gates", "gate shelters"):
                    p = os.path.join(layer, sub, v)
                    if os.path.isfile(p):
                        return p, label
        return None, None

    def regions(self):
        codes = set()
        skip = {"gates", "gate shelters", "indexmaps"}
        for layer in self.layers:
            if not os.path.isdir(layer):
                continue
            for entry in os.listdir(layer):
                if entry in skip or entry.endswith("-rooms"):
                    continue
                p = os.path.join(layer, entry)
                if not os.path.isdir(p):
                    continue
                if os.path.isfile(os.path.join(p, f"map_{entry}.txt")) or \
                   os.path.isfile(os.path.join(p, f"world_{entry}.txt")):
                    codes.add(entry)
        return sorted(codes)


def read_lines(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def parse_map_file(path):
    """Returns (rooms, connections).

    rooms: {ROOMNAME_UPPER: (devX, devY, layer, subregion)}
    The first two fields (canPos) are the in-game pause-map position (layered
    + overlapping); fields [2,3] (devPos) are the dev-editor placement and
    give a flat, non-overlapping layout — that's what we use.

    connections: [(roomA, roomB, ax, ay, bx, by, dirA, dirB), ...] with tile
    positions inside each room (per `Connection:` lines).
    """
    rooms = {}
    connections = []
    for raw in read_lines(path):
        s = raw.strip()
        if not s or ":" not in s:
            continue
        if "\t" in s:
            s = s.split("\t", 1)[1] if "\t" in s.split(":", 1)[0] else s
        head, rest = s.split(":", 1)
        head = head.strip()
        if head == "Connection":
            f = [v.strip() for v in rest.split(",")]
            if len(f) >= 8:
                try:
                    connections.append((
                        f[0].upper(), f[1].upper(),
                        int(f[2]), int(f[3]),
                        int(f[4]), int(f[5]),
                        int(f[6]), int(f[7]),
                    ))
                except ValueError:
                    pass
            continue
        name = head.upper()
        if not name or name.startswith("//"):
            continue
        fields = re.split(r"><", rest.strip())
        if len(fields) < 4:
            continue
        try:
            x = float(fields[2])  # devPos.x
            y = float(fields[3])  # devPos.y
        except ValueError:
            continue
        layer = 0
        sub = ""
        if len(fields) > 4:
            try:
                layer = int(fields[4])
            except ValueError:
                pass
        if len(fields) > 5:
            sub = fields[5]
        rooms[name] = (x, y, layer, sub)
    return rooms, connections


def parse_room_geometry(path):
    """Returns (width, height, [(x, y, terrain, vbeam, hbeam), ...]) or None.

    Per Room.cs:5418 — lines[11] holds tile data; '|' separates per-tile
    entries; each entry is ','-separated: attrs[0] = TerrainType int;
    remaining attrs may include "1" (verticalBeam), "2" (horizontalBeam),
    "3..12" (shortcut codes), etc. Tile iteration order: x=0,y=H-1; y--;
    on y<0 reset y=H-1 and x++.
    """
    lines = read_lines(path)
    if len(lines) < 12:
        return None
    header = lines[1].split("|")[0].split("*")
    try:
        w = int(header[0])
        h = int(header[1])
    except (ValueError, IndexError):
        return None
    entries = lines[11].split("|")
    tiles = []
    x, y = 0, h - 1
    for entry in entries:
        if not entry:
            continue
        if x >= w:
            break
        parts = entry.split(",")
        try:
            terrain = int(parts[0])
        except ValueError:
            terrain = 0
        vbeam = "1" in parts[1:]
        hbeam = "2" in parts[1:]
        if terrain in (T_SOLID, T_SLOPE, T_FLOOR) or vbeam or hbeam:
            tiles.append((x, y, terrain, vbeam, hbeam))
        y -= 1
        if y < 0:
            y = h - 1
            x += 1
    return w, h, tiles


def _svg_y(ll_y, ty_grid):
    """Convert tile-grid y (0 = bottom of room) to SVG y (down-positive)."""
    return -(ll_y + ty_grid * TILE_UNITS)


def _emit_filled(out, tiles, ll_x, ll_y, palette):
    """Filled style: rect runs for solid; triangles for slopes (only the
    diagonal is exposed — the right-angle sides abut solid neighbors and
    blend with them); thin shelves for floors; beams as lines.

    Each rect is grown by `eps` on every side so the renderer's anti-alias
    seams sit inside the next shape rather than on its boundary. crispEdges
    on the geometry <g> is the primary defence; this is belt-and-braces.
    """
    eps = 0.02
    solid_color = palette["solid"]
    floor_color = palette["floor"]
    solid_by_row = {}    # ty -> list[x]  (T_SOLID only)
    floor_by_row = {}    # ty -> list[x]  (T_FLOOR)
    slope_tiles = []     # (x, y) needing neighbor lookup
    solid_set = set()
    beams = []
    for tx, ty, terrain, vb, hb in tiles:
        if terrain == T_SOLID:
            solid_by_row.setdefault(ty, []).append(tx)
            solid_set.add((tx, ty))
        elif terrain == T_SLOPE:
            slope_tiles.append((tx, ty))
        elif terrain == T_FLOOR:
            floor_by_row.setdefault(ty, []).append(tx)
        if vb or hb:
            beams.append((tx, ty, vb, hb))

    for ty, xs in solid_by_row.items():
        xs.sort()
        start = prev = xs[0]
        runs = []
        for v in xs[1:]:
            if v == prev + 1:
                prev = v
            else:
                runs.append((start, prev)); start = prev = v
        runs.append((start, prev))
        y_top = _svg_y(ll_y, ty + 1)
        for sx, ex in runs:
            px = ll_x + sx * TILE_UNITS
            run_w = (ex - sx + 1) * TILE_UNITS
            out.append(
                f'<rect x="{px - eps:.3f}" y="{y_top - eps:.3f}" '
                f'width="{run_w + 2 * eps:.3f}" '
                f'height="{TILE_UNITS + 2 * eps:.3f}" '
                f'fill="{solid_color}"/>'
            )

    for ty, xs in floor_by_row.items():
        xs.sort()
        start = prev = xs[0]
        runs = []
        for v in xs[1:]:
            if v == prev + 1:
                prev = v
            else:
                runs.append((start, prev)); start = prev = v
        runs.append((start, prev))
        y_top = _svg_y(ll_y, ty + 1)
        for sx, ex in runs:
            px = ll_x + sx * TILE_UNITS
            run_w = (ex - sx + 1) * TILE_UNITS
            out.append(
                f'<rect x="{px - eps:.3f}" y="{y_top - eps:.3f}" '
                f'width="{run_w + 2 * eps:.3f}" '
                f'height="{0.6 + 2 * eps:.3f}" fill="{floor_color}"/>'
            )

    for x, y in slope_tiles:
        L = (x - 1, y) in solid_set
        R = (x + 1, y) in solid_set
        D = (x, y - 1) in solid_set
        U = (x, y + 1) in solid_set
        tl_x = ll_x + x * TILE_UNITS
        tr_x = tl_x + TILE_UNITS
        y_top = _svg_y(ll_y, y + 1)
        y_bot = _svg_y(ll_y, y)
        # solid_corner names refer to where the solid mass sits inside the tile
        if L and D:
            pts = f"{tl_x:.2f},{y_top:.2f} {tl_x:.2f},{y_bot:.2f} {tr_x:.2f},{y_bot:.2f}"
        elif L and U:
            pts = f"{tl_x:.2f},{y_top:.2f} {tl_x:.2f},{y_bot:.2f} {tr_x:.2f},{y_top:.2f}"
        elif R and D:
            pts = f"{tl_x:.2f},{y_bot:.2f} {tr_x:.2f},{y_bot:.2f} {tr_x:.2f},{y_top:.2f}"
        elif R and U:
            pts = f"{tl_x:.2f},{y_top:.2f} {tr_x:.2f},{y_top:.2f} {tr_x:.2f},{y_bot:.2f}"
        else:
            # Ambiguous (isolated / 0 or 3+ neighbors) — fall back to full square.
            pts = (f"{tl_x:.2f},{y_top:.2f} {tr_x:.2f},{y_top:.2f} "
                   f"{tr_x:.2f},{y_bot:.2f} {tl_x:.2f},{y_bot:.2f}")
        out.append(f'<polygon points="{pts}" fill="{solid_color}"/>')

    for tx, ty, vb, hb in beams:
        _emit_beams(out, ll_x, ll_y, tx, ty, vb, hb, palette["beam"])


# ── Edge offsets for boundary extraction (math coords, CCW around tile) ──
# Each entry: (dx, dy, my_side, n_side, start_corner, end_corner)
# A cell at (x,y) emits the edge for `my_side` when the neighbor at (x+dx,y+dy)
# does not also cover `n_side` (the shared edge). Corners are (gx,gy) offsets
# added to (x,y).
_EDGE_DIRS = (
    (0, -1, "D", "U", (0, 0), (1, 0)),  # bottom: BL → BR
    (1,  0, "R", "L", (1, 0), (1, 1)),  # right:  BR → TR
    (0,  1, "U", "D", (1, 1), (0, 1)),  # top:    TR → TL
    (-1, 0, "L", "R", (0, 1), (0, 0)),  # left:   TL → BL
)


def _cell_covers(cells, pos, side):
    """True if cells[pos] occupies the given side of its tile.
    'S' = full solid (covers all sides); slope codes 'LD'/'LU'/'RD'/'RU'
    cover the two sides whose letters they contain."""
    kind = cells.get(pos)
    if kind is None:
        return False
    if kind == "S":
        return True
    return side in kind


def _build_solid_edges(cells):
    """Return list of (start_pt, end_pt) boundary edges for the union of
    solid + slope tiles. Shared edges between cells in the set cancel out."""
    edges = []
    for (x, y), kind in cells.items():
        sides = ("D", "R", "U", "L") if kind == "S" else tuple(kind)
        for dx, dy, my_side, n_side, s_off, e_off in _EDGE_DIRS:
            if my_side not in sides:
                continue
            if _cell_covers(cells, (x + dx, y + dy), n_side):
                continue
            sp = (x + s_off[0], y + s_off[1])
            ep = (x + e_off[0], y + e_off[1])
            edges.append((sp, ep))
        # Slope hypotenuse: always emitted (interior to the tile).
        if kind == "LD":
            edges.append(((x + 1, y), (x, y + 1)))      # BR → TL
        elif kind == "LU":
            edges.append(((x, y), (x + 1, y + 1)))      # BL → TR
        elif kind == "RD":
            edges.append(((x + 1, y + 1), (x, y)))      # TR → BL
        elif kind == "RU":
            edges.append(((x, y + 1), (x + 1, y)))      # TL → BR
    return edges


def _chain_loops(edges):
    """Chain directed edges into closed loops. Each vertex has equal in/out
    degree (guaranteed by the cancellation rule), so a greedy walk closes."""
    from collections import defaultdict
    adj = defaultdict(list)
    for s, e in edges:
        adj[s].append(e)
    loops = []
    while adj:
        start_v = next(iter(adj))
        pt = start_v
        loop = [pt]
        while True:
            if pt not in adj or not adj[pt]:
                break
            nxt = adj[pt].pop()
            if not adj[pt]:
                del adj[pt]
            if nxt == start_v:
                break
            loop.append(nxt)
            pt = nxt
        loops.append(loop)
    return loops


def _simplify_loop(loop):
    """Drop vertices that lie on a straight line with their two neighbours."""
    n = len(loop)
    if n < 3:
        return loop
    out = []
    for i in range(n):
        prev = loop[(i - 1) % n]
        p = loop[i]
        nxt = loop[(i + 1) % n]
        dx1 = p[0] - prev[0]; dy1 = p[1] - prev[1]
        dx2 = nxt[0] - p[0];  dy2 = nxt[1] - p[1]
        # collinear and same direction → interior of a straight run, drop it
        if dx1 * dy2 - dy1 * dx2 == 0 and (dx1 * dx2 + dy1 * dy2) > 0:
            continue
        out.append(p)
    return out


def _loops_to_path_d(loops, ll_x, ll_y):
    parts = []
    for loop in loops:
        simp = _simplify_loop(loop)
        if len(simp) < 3:
            continue
        cmds = []
        for i, (gx, gy) in enumerate(simp):
            sx = ll_x + gx * TILE_UNITS
            sy = -(ll_y + gy * TILE_UNITS)
            cmds.append((f"M{sx:.2f},{sy:.2f}" if i == 0
                         else f"L{sx:.2f},{sy:.2f}"))
        cmds.append("Z")
        parts.append("".join(cmds))
    return "".join(parts)


def _emit_filled_optimized(out, tiles, ll_x, ll_y, palette):
    """Optimized filled emit: solid + slope tiles are merged into a single
    `<path>` per connected component (no per-row rects, no eps padding).
    Floors stay as RLE rects bundled into one path; beams unchanged."""
    solid_color = palette["solid"]
    floor_color = palette["floor"]
    beam_color = palette["beam"]

    solid_set = set()
    slope_raw = []   # (x, y) — orientation decided after solid_set is known
    floor_by_row = {}
    beams = []
    for tx, ty, terrain, vb, hb in tiles:
        if terrain == T_SOLID:
            solid_set.add((tx, ty))
        elif terrain == T_SLOPE:
            slope_raw.append((tx, ty))
        elif terrain == T_FLOOR:
            floor_by_row.setdefault(ty, []).append(tx)
        if vb or hb:
            beams.append((tx, ty, vb, hb))

    cells = {pos: "S" for pos in solid_set}
    for x, y in slope_raw:
        L = (x - 1, y) in solid_set
        R = (x + 1, y) in solid_set
        D = (x, y - 1) in solid_set
        U = (x, y + 1) in solid_set
        if L and D:    cells[(x, y)] = "LD"
        elif L and U:  cells[(x, y)] = "LU"
        elif R and D:  cells[(x, y)] = "RD"
        elif R and U:  cells[(x, y)] = "RU"
        else:          cells[(x, y)] = "S"  # ambiguous → full square (matches legacy)

    if cells:
        d = _loops_to_path_d(_chain_loops(_build_solid_edges(cells)), ll_x, ll_y)
        if d:
            out.append(f'<path d="{d}" fill="{solid_color}"/>')

    floor_parts = []
    for ty, xs in floor_by_row.items():
        xs.sort()
        start = prev = xs[0]
        runs = []
        for v in xs[1:]:
            if v == prev + 1:
                prev = v
            else:
                runs.append((start, prev)); start = prev = v
        runs.append((start, prev))
        y_top = _svg_y(ll_y, ty + 1)
        for sx, ex in runs:
            px = ll_x + sx * TILE_UNITS
            run_w = (ex - sx + 1) * TILE_UNITS
            floor_parts.append(
                f"M{px:.2f},{y_top:.2f}h{run_w:.2f}v0.6h-{run_w:.2f}Z"
            )
    if floor_parts:
        out.append(f'<path d="{"".join(floor_parts)}" fill="{floor_color}"/>')

    for tx, ty, vb, hb in beams:
        _emit_beams(out, ll_x, ll_y, tx, ty, vb, hb, beam_color)


def _emit_beams(out, ll_x, ll_y, tx, ty, vb, hb, color):
    tile_l = ll_x + tx * TILE_UNITS
    tile_r = tile_l + TILE_UNITS
    y_top = _svg_y(ll_y, ty + 1)
    y_bot = _svg_y(ll_y, ty)
    mid_x = tile_l + TILE_UNITS / 2.0
    mid_y = (y_top + y_bot) / 2.0
    if vb:
        out.append(
            f'<line x1="{mid_x:.2f}" y1="{y_top:.2f}" '
            f'x2="{mid_x:.2f}" y2="{y_bot:.2f}" '
            f'stroke="{color}" stroke-width="0.4"/>'
        )
    if hb:
        out.append(
            f'<line x1="{tile_l:.2f}" y1="{mid_y:.2f}" '
            f'x2="{tile_r:.2f}" y2="{mid_y:.2f}" '
            f'stroke="{color}" stroke-width="0.4"/>'
        )


def _emit_outline(out, tiles, ll_x, ll_y, palette):
    """Outline style: 1-tile-wide boundary lines between solid and non-solid
    tiles (RLE'd), slope tiles as diagonals oriented by their two solid
    neighbors, half-floors as a shelf with side stubs, plus beams."""
    edge_color = palette["solid"]
    beam_color = palette["beam"]
    by_pos = {}  # (x, y) -> (terrain, vb, hb)
    solid = set()
    for tx, ty, terrain, vb, hb in tiles:
        by_pos[(tx, ty)] = (terrain, vb, hb)
        if terrain == T_SOLID:
            solid.add((tx, ty))

    # Boundary edges: for each solid tile, emit each of 4 edges if neighbor
    # is not also solid. Out-of-bounds neighbor counts as "not solid" so room
    # borders close cleanly.
    h_edges = {}   # y_grid -> list[x] (edge spans tile-x .. tile-x+1)
    v_edges = {}   # x_grid -> list[y]
    for (x, y) in solid:
        if (x, y + 1) not in solid:
            h_edges.setdefault(y + 1, []).append(x)
        if (x, y - 1) not in solid:
            h_edges.setdefault(y, []).append(x)
        if (x + 1, y) not in solid:
            v_edges.setdefault(x + 1, []).append(y)
        if (x - 1, y) not in solid:
            v_edges.setdefault(x, []).append(y)

    for yg, xs in h_edges.items():
        xs.sort()
        start = prev = xs[0]
        for v in xs[1:]:
            if v == prev + 1:
                prev = v
            else:
                _emit_h_edge(out, ll_x, ll_y, start, prev + 1, yg, edge_color)
                start = prev = v
        _emit_h_edge(out, ll_x, ll_y, start, prev + 1, yg, edge_color)

    for xg, ys in v_edges.items():
        ys.sort()
        start = prev = ys[0]
        for v in ys[1:]:
            if v == prev + 1:
                prev = v
            else:
                _emit_v_edge(out, ll_x, ll_y, xg, start, prev + 1, edge_color)
                start = prev = v
        _emit_v_edge(out, ll_x, ll_y, xg, start, prev + 1, edge_color)

    # Slopes: pick diagonal from the pair of solid neighbors (per
    # generateGeoJSON.py:305-315).
    for (x, y), (terrain, _vb, _hb) in by_pos.items():
        if terrain != T_SLOPE:
            continue
        L = (x - 1, y) in solid
        R = (x + 1, y) in solid
        D = (x, y - 1) in solid
        U = (x, y + 1) in solid
        # Tile corners in SVG coords:
        tl_x = ll_x + x * TILE_UNITS
        tr_x = tl_x + TILE_UNITS
        y_top = _svg_y(ll_y, y + 1)
        y_bot = _svg_y(ll_y, y)
        if L and D:
            # solid in lower-left → diagonal TL→BR (`\` in screen-space, since
            # screen y grows downward we draw (tl_x,y_top)→(tr_x,y_bot))
            _emit_seg(out, tl_x, y_top, tr_x, y_bot, edge_color)
        elif L and U:
            _emit_seg(out, tl_x, y_bot, tr_x, y_top, edge_color)
        elif R and D:
            _emit_seg(out, tl_x, y_bot, tr_x, y_top, edge_color)
        elif R and U:
            _emit_seg(out, tl_x, y_top, tr_x, y_bot, edge_color)

    # Half-floors: top shelf line; bottom shelf at mid-tile unless hbeam
    # shares it; small side stubs.
    for (x, y), (terrain, vb, hb) in by_pos.items():
        if terrain != T_FLOOR:
            continue
        tile_l = ll_x + x * TILE_UNITS
        tile_r = tile_l + TILE_UNITS
        y_top = _svg_y(ll_y, y + 1)
        y_mid = _svg_y(ll_y, y + 0.5)
        y_bot = _svg_y(ll_y, y)
        # top of shelf
        _emit_seg(out, tile_l, y_top, tile_r, y_top, edge_color)
        # mid-line (skipped if hbeam, since that line would coincide)
        if not hb:
            _emit_seg(out, tile_l, y_mid, tile_r, y_mid, edge_color)
        # left stub
        left = by_pos.get((x - 1, y), (T_AIR, False, False))[0]
        if left == T_SOLID:
            _emit_seg(out, tile_l, y_bot, tile_l, y_mid, edge_color)
        else:
            _emit_seg(out, tile_l, y_mid, tile_l, y_top, edge_color)
        # right stub
        right = by_pos.get((x + 1, y), (T_AIR, False, False))[0]
        if right == T_SOLID:
            _emit_seg(out, tile_r, y_bot, tile_r, y_mid, edge_color)
        else:
            _emit_seg(out, tile_r, y_mid, tile_r, y_top, edge_color)

    # Beams
    for (x, y), (terrain, vb, hb) in by_pos.items():
        if vb or hb:
            _emit_beams(out, ll_x, ll_y, x, y, vb, hb, beam_color)


def _emit_h_edge(out, ll_x, ll_y, x_start, x_end, y_grid, color):
    x1 = ll_x + x_start * TILE_UNITS
    x2 = ll_x + x_end * TILE_UNITS
    yy = _svg_y(ll_y, y_grid)
    out.append(
        f'<line x1="{x1:.2f}" y1="{yy:.2f}" x2="{x2:.2f}" y2="{yy:.2f}" '
        f'stroke="{color}" stroke-width="0.35"/>'
    )


def _emit_v_edge(out, ll_x, ll_y, x_grid, y_start, y_end, color):
    xx = ll_x + x_grid * TILE_UNITS
    y1 = _svg_y(ll_y, y_start)
    y2 = _svg_y(ll_y, y_end)
    out.append(
        f'<line x1="{xx:.2f}" y1="{y1:.2f}" x2="{xx:.2f}" y2="{y2:.2f}" '
        f'stroke="{color}" stroke-width="0.35"/>'
    )


def _emit_seg(out, x1, y1, x2, y2, color):
    out.append(
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{color}" stroke-width="0.35"/>'
    )


def detect_overlaps(room_data):
    """Pairwise AABB overlap test over devPos room footprints.

    room_data: name -> (w, h, ll_x, ll_y, tiles, layer, cx, cy). devPos is
    supposed to give a flat, non-overlapping layout (see parse_map_file
    docstring); modded maps sometimes violate this. Returns a list of
    (nameA, nameB) pairs whose rects overlap (touching edges don't count).
    """
    boxes = []
    for name, (w, h, ll_x, ll_y, _t, _l, _cx, _cy) in room_data.items():
        boxes.append((name, ll_x, ll_y, ll_x + w * TILE_UNITS, ll_y + h * TILE_UNITS))
    overlaps = []
    for i in range(len(boxes)):
        na, ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, len(boxes)):
            nb, bx0, by0, bx1, by1 = boxes[j]
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                overlaps.append((na, nb))
    return overlaps


def resolve_overlaps(room_data, max_passes=200):
    """Iteratively push overlapping room rects apart.

    Simple shallowest-axis AABB separation: for each overlapping pair, find
    the smaller-penetration axis (x or y) and shift both rooms apart by half
    the overlap depth along it. Repeats until no overlaps remain or
    max_passes is exhausted. Mutates room_data's ll_x/ll_y/cx/cy in place.
    Returns True if fully resolved.
    """
    for _ in range(max_passes):
        overlaps = detect_overlaps(room_data)
        if not overlaps:
            return True
        for na, nb in overlaps:
            wa, ha, ax, ay, ta, la, _cxa, _cya = room_data[na]
            wb, hb, bx, by, tb, lb, _cxb, _cyb = room_data[nb]
            ax1, ay1 = ax + wa * TILE_UNITS, ay + ha * TILE_UNITS
            bx1, by1 = bx + wb * TILE_UNITS, by + hb * TILE_UNITS
            overlap_x = min(ax1, bx1) - max(ax, bx)
            overlap_y = min(ay1, by1) - max(ay, by)
            if overlap_x <= 0 or overlap_y <= 0:
                continue
            if overlap_x < overlap_y:
                shift = overlap_x / 2.0 + 0.05
                if ax < bx:
                    ax -= shift; bx += shift
                else:
                    ax += shift; bx -= shift
            else:
                shift = overlap_y / 2.0 + 0.05
                if ay < by:
                    ay -= shift; by += shift
                else:
                    ay += shift; by -= shift
            room_data[na] = (wa, ha, ax, ay, ta, la, ax + wa * TILE_UNITS / 2.0, ay + ha * TILE_UNITS / 2.0)
            room_data[nb] = (wb, hb, bx, by, tb, lb, bx + wb * TILE_UNITS / 2.0, by + hb * TILE_UNITS / 2.0)
    return not detect_overlaps(room_data)


def _total_conn_length(room_data, conn_entries):
    """Sum of centre-to-centre distances over all room-room connections
    (each connection pair counted once per Connection: entry)."""
    import math
    total = 0.0
    for entry in conn_entries:
        a, b = entry[0], entry[1]
        if a not in room_data or b not in room_data or a == b:
            continue
        _wa, _ha, _ax, _ay, _ta, _la, cxa, cya = room_data[a]
        _wb, _hb, _bx, _by, _tb, _lb, cxb, cyb = room_data[b]
        total += math.hypot(cxb - cxa, cyb - cya)
    return total


def _bfs_hop_distances(names, edges):
    """All-pairs shortest-path distance (hop count) over the undirected
    connection graph. Returns (idx: name->int, dist: NxN list, INF for
    unreachable pairs)."""
    from collections import deque

    idx = {n: i for i, n in enumerate(names)}
    n = len(names)
    adj = {nm: set() for nm in names}
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    INF = float("inf")
    dist = [[INF] * n for _ in range(n)]
    for start in names:
        si = idx[start]
        dist[si][si] = 0
        visited = {start: 0}
        dq = deque([start])
        while dq:
            cur = dq.popleft()
            d = visited[cur]
            for nb in adj[cur]:
                if nb not in visited:
                    visited[nb] = d + 1
                    dq.append(nb)
        for node, d in visited.items():
            dist[si][idx[node]] = d
    return idx, dist


def _refine_door_alignment(pos, idx, room_data, conn_entries,
                            iterations=150, damping=0.6):
    """Post-KK refinement that nudges room centres so connected doors — not
    room centres — end up close together.

    KK/stress-majorization only ever reasons about centre-to-centre
    distance. For a long, narrow, or off-centre-door room (a corridor whose
    shortcut is at one end, say) that's a poor proxy: two rooms can have
    well-placed centres and still have their actual `Connection:` tiles far
    apart. Each `Connection:` entry fixes a door's offset from its room's
    centre (intrinsic to the room's geometry, unaffected by translation), so
    "make door_a coincide with door_b" is the linear constraint
    `pos_a - pos_b == offset_b - offset_a`. Solved by Jacobi relaxation
    (damped, so it nudges the KK layout rather than replacing its topology).
    Rooms with no connections are left untouched.
    """
    edges = []
    for entry in conn_entries:
        a, b, ax, ay, bx, by = entry[0], entry[1], entry[2], entry[3], entry[4], entry[5]
        if a not in idx or b not in idx or a == b:
            continue
        wa, ha = room_data[a][0], room_data[a][1]
        wb, hb = room_data[b][0], room_data[b][1]
        off_a = ((ax + 0.5) * TILE_UNITS - wa * TILE_UNITS / 2.0,
                 (ay + 0.5) * TILE_UNITS - ha * TILE_UNITS / 2.0)
        off_b = ((bx + 0.5) * TILE_UNITS - wb * TILE_UNITS / 2.0,
                 (by + 0.5) * TILE_UNITS - hb * TILE_UNITS / 2.0)
        pref = (off_b[0] - off_a[0], off_b[1] - off_a[1])  # want pos_a - pos_b == pref
        edges.append((idx[a], idx[b], pref))
    if not edges:
        return

    neighbors = {i: [] for i in range(len(pos))}
    for i, j, pref in edges:
        neighbors[i].append((j, pref))
        neighbors[j].append((i, (-pref[0], -pref[1])))

    for _ in range(iterations):
        new_pos = [p[:] for p in pos]
        for i, nbrs in neighbors.items():
            if not nbrs:
                continue
            sx = sum(pos[j][0] + pref[0] for j, pref in nbrs)
            sy = sum(pos[j][1] + pref[1] for j, pref in nbrs)
            tx, ty = sx / len(nbrs), sy / len(nbrs)
            new_pos[i][0] = pos[i][0] + damping * (tx - pos[i][0])
            new_pos[i][1] = pos[i][1] + damping * (ty - pos[i][1])
        pos[:] = new_pos


def _door_align_pass(room_data, conn_entries, iterations=80, damping=0.5):
    """Like `_refine_door_alignment`, but reads/writes room_data's cx/cy
    directly instead of a separate `pos` array — for use between overlap-fix
    nudges in `render_region`'s relax cycle, where re-running the full KK
    stress majorization each cycle would just re-collapse rooms that
    overlap-fix had just spread apart."""
    names = [nm for nm in room_data
             if any((e[0] == nm or e[1] == nm) for e in conn_entries)]
    if not names:
        return
    idx = {nm: i for i, nm in enumerate(names)}
    pos = [[room_data[nm][6], room_data[nm][7]] for nm in names]
    _refine_door_alignment(pos, idx, room_data, conn_entries,
                            iterations=iterations, damping=damping)
    for i, nm in enumerate(names):
        w, h, _ax, _ay, tiles, layer, _cx, _cy = room_data[nm]
        ncx, ncy = pos[i]
        room_data[nm] = (
            w, h, ncx - w * TILE_UNITS / 2.0, ncy - h * TILE_UNITS / 2.0,
            tiles, layer, ncx, ncy,
        )


def optimize_layout(room_data, conn_entries, iterations=300, seed=0,
                     eps=1e-3, scale_mult=0.85, door_align_iters=150):
    """Kamada-Kawai layout via stress majorization (SMACOF).

    Graph-theoretic distance (BFS hop count over the connection graph)
    defines an ideal geometric distance for every room pair: directly
    connected rooms should end up close, rooms several hops apart should end
    up proportionally farther, disconnected components are pushed apart by a
    mild fixed distance so they don't collapse onto each other. Each
    iteration recomputes every room's position as the weighted average of
    "where it should sit relative to every other room to hit its ideal
    distance" (the Guttman transform) — this provably never increases total
    stress and converges to a local stress minimum, without hand-tuned force
    constants. O(n^2) per iteration; fine for the room counts a region has
    (low hundreds).

    `resolve_overlaps` still runs afterwards — this only targets edge
    length, it does not avoid overlap by itself.

    Mutates room_data's ll_x/ll_y/cx/cy in place (w/h unchanged).
    """
    import math
    import random

    names = list(room_data.keys())
    n = len(names)
    if n < 2:
        return

    edges = set()
    for entry in conn_entries:
        a, b = entry[0], entry[1]
        if a not in room_data or b not in room_data or a == b:
            continue
        edges.add((a, b) if a < b else (b, a))

    idx, hop = _bfs_hop_distances(names, edges)
    INF = float("inf")
    finite = [hop[i][j] for i in range(n) for j in range(n)
              if i != j and hop[i][j] != INF]
    max_finite = max(finite) if finite else 1
    far_dist = max_finite * 1.5 + 2
    for i in range(n):
        for j in range(n):
            if i != j and hop[i][j] == INF:
                hop[i][j] = far_dist

    # Ideal distance per pair: for directly-connected rooms (hop==1), use
    # THIS pair's own half-diagonals, not a global average — a map with a
    # few oversized rooms (e.g. one room 4x the rest) needs those specific
    # pairs pushed further apart than a size-blind average would predict,
    # or overlap-resolution ends up shoving the big room off in an
    # arbitrary direction and its doors end up far from their partners.
    # Multi-hop pairs (no direct radius pair to anchor on) fall back to the
    # mean 1-hop target scaled by hop count.
    radius = {nm: math.hypot(room_data[nm][0], room_data[nm][1]) / 2.0
              * TILE_UNITS for nm in names}
    edge_targets = [(radius[a] + radius[b]) * scale_mult for a, b in edges]
    avg_edge_target = (sum(edge_targets) / len(edge_targets)
                        if edge_targets else radius[names[0]] * scale_mult)

    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if hop[i][j] == 1:
                L[i][j] = (radius[names[i]] + radius[names[j]]) * scale_mult
            else:
                L[i][j] = avg_edge_target * hop[i][j]
    W = [[0.0 if i == j else 1.0 / (hop[i][j] * hop[i][j])
          for j in range(n)] for i in range(n)]

    rng = random.Random(seed)
    pos = [[room_data[names[i]][6] + rng.uniform(-0.01, 0.01),
            room_data[names[i]][7] + rng.uniform(-0.01, 0.01)]
           for i in range(n)]

    for _ in range(iterations):
        new_pos = [None] * n
        moved = 0.0
        for i in range(n):
            xi, yi = pos[i]
            wsum = sx = sy = 0.0
            for j in range(n):
                if i == j:
                    continue
                xj, yj = pos[j]
                dx = xi - xj; dy = yi - yj
                d = math.hypot(dx, dy) or 1e-9
                lij = L[i][j]
                w = W[i][j]
                sx += w * (xj + lij * dx / d)
                sy += w * (yj + lij * dy / d)
                wsum += w
            if wsum > 0:
                nx, ny = sx / wsum, sy / wsum
            else:
                nx, ny = xi, yi
            moved += math.hypot(nx - xi, ny - yi)
            new_pos[i] = [nx, ny]
        pos = new_pos
        if moved / n < eps:
            break

    if door_align_iters:
        _refine_door_alignment(pos, idx, room_data, conn_entries,
                                iterations=door_align_iters)

    for i, nm in enumerate(names):
        w, h, _ax, _ay, tiles, layer, _cx, _cy = room_data[nm]
        ncx, ncy = pos[i]
        room_data[nm] = (
            w, h, ncx - w * TILE_UNITS / 2.0, ncy - h * TILE_UNITS / 2.0,
            tiles, layer, ncx, ncy,
        )


def render_region(resolver, region, out_dir):
    map_path, map_src = resolver.region_file(region, f"map_{region}.txt")
    if not map_path:
        return region, "skip (no map_*.txt)"

    rooms_map, conn_entries = parse_map_file(map_path)
    sources = {map_src}

    # In-game display name, e.g. "Garbage Wastes" — World/<region>/DisplayName.txt,
    # same layer+slugcat resolution as map_*.txt (Region.cs:617 GetRegionFullName).
    display_path, _dn_src = resolver.region_file(region, "DisplayName.txt")
    display_name = region.upper()
    if display_path:
        try:
            display_name = read_lines(display_path)[0].strip() or display_name
        except (IndexError, OSError):
            pass

    # Cache geometry-derived per-room data we need below.
    room_data = {}  # name -> (w, h, ll_x, ll_y, tiles, layer)
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    for name, (dx, dy, layer, _sub) in rooms_map.items():
        room_path, room_src = resolver.room_file(region, name)
        if not room_path:
            continue
        sources.add(room_src)
        geo = parse_room_geometry(room_path)
        if not geo:
            continue
        w, h, tiles = geo
        # devPos is the room's lower-left corner directly (per
        # generateGeoJSON.py:74 — `roomcoords = devPos*10` with 20 px/tile).
        ll_x, ll_y = dx, dy
        # Center of the room (for the label) — derived, no separate field.
        cx = ll_x + (w * TILE_UNITS) / 2.0
        cy = ll_y + (h * TILE_UNITS) / 2.0
        room_data[name] = (w, h, ll_x, ll_y, tiles, layer, cx, cy)
        rx1 = ll_x + w * TILE_UNITS
        ry1 = ll_y + h * TILE_UNITS
        if ll_x < min_x: min_x = ll_x
        if ll_y < min_y: min_y = ll_y
        if rx1 > max_x:  max_x = rx1
        if ry1 > max_y:  max_y = ry1

    if not room_data:
        return region, f"skip (0/{len(rooms_map)} rooms had geometry)"

    if getattr(resolver, "layout_optimize", False):
        before_len = _total_conn_length(room_data, conn_entries)
        # A single KK pass + a single overlap-resolve pass fights itself:
        # overlap-resolve is a dumb greedy AABB push, blind to which rooms
        # are connected, so it happily shoves the careful door alignment KK
        # just built back out of alignment. And re-running full KK
        # afterwards is worse — it re-collapses rooms overlap-fix just
        # spread apart, not knowing that spacing was load-bearing. So: one
        # KK pass for overall topology, one full resolve_overlaps to reach a
        # legal (non-overlapping) layout, then alternate cheap door-only
        # realignment with *small* overlap nudges — each cycle only
        # re-introduces a little overlap for the next nudge to clean up,
        # converging to a compromise that stays overlap-free and pulls
        # doors closer than the first resolve alone achieved. Swept
        # scale_mult/cycle-count/damping against `su`, `cc`, `sh`, `rp`;
        # damping=0.3 consistently beat 0.45, gains taper off past ~14-16
        # cycles, scale_mult 0.8-0.9 consistently on top.
        optimize_layout(room_data, conn_entries, scale_mult=0.85)
        resolve_overlaps(room_data, max_passes=2000)
        for _ in range(14):
            _door_align_pass(room_data, conn_entries, iterations=40, damping=0.3)
            resolve_overlaps(room_data, max_passes=500)
        after_len = _total_conn_length(room_data, conn_entries)
        print(f"[{region}] layout optimize: total connection length "
              f"{before_len:.1f} -> {after_len:.1f}", file=sys.stderr)
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        for (w, h, ll_x, ll_y, _t, _l, _cx, _cy) in room_data.values():
            rx1 = ll_x + w * TILE_UNITS
            ry1 = ll_y + h * TILE_UNITS
            if ll_x < min_x: min_x = ll_x
            if ll_y < min_y: min_y = ll_y
            if rx1 > max_x:  max_x = rx1
            if ry1 > max_y:  max_y = ry1

    overlaps = detect_overlaps(room_data)
    if overlaps:
        sample = ", ".join(f"{a}<->{b}" for a, b in overlaps[:5])
        more = f" (+{len(overlaps) - 5} more)" if len(overlaps) > 5 else ""
        print(f"warning: [{region}] {len(overlaps)} overlapping room pair(s): "
              f"{sample}{more}", file=sys.stderr)
        if getattr(resolver, "fix_overlaps", False):
            max_passes = 2000 if getattr(resolver, "layout_optimize", False) else 200
            resolved = resolve_overlaps(room_data, max_passes=max_passes)
            print(f"[{region}] overlap fix: "
                  f"{'resolved' if resolved else 'partially resolved'}",
                  file=sys.stderr)
            # Bounding box may have grown/shifted — recompute.
            min_x = min_y = float("inf")
            max_x = max_y = float("-inf")
            for (w, h, ll_x, ll_y, _t, _l, _cx, _cy) in room_data.values():
                rx1 = ll_x + w * TILE_UNITS
                ry1 = ll_y + h * TILE_UNITS
                if ll_x < min_x: min_x = ll_x
                if ll_y < min_y: min_y = ll_y
                if rx1 > max_x:  max_x = rx1
                if ry1 > max_y:  max_y = ry1

    layer_counts = {}
    for (_w, _h, _x, _y, _t, layer, _cx, _cy) in room_data.values():
        layer_counts[layer] = layer_counts.get(layer, 0) + 1

    def tile_to_world(name, tx, ty):
        # Connection: positions are tile coords with y=0 at the bottom of the
        # room (matches the tile array ordering used in parse_room_geometry).
        # Anchor to tile center.
        w, h, ll_x, ll_y, _t, _l, _mx, _my = room_data[name]
        wx = ll_x + (tx + 0.5) * TILE_UNITS
        wy = ll_y + (ty + 0.5) * TILE_UNITS
        return wx, wy

    # ── Build per-layer geometry rects and labels ──────────────────────────
    layer_geo = {}      # layer -> list[str]
    layer_labels = {}   # layer -> list[str]
    style = getattr(resolver, "style", "outline")
    palette = getattr(resolver, "palette", PALETTES[style])
    label_color = palette["label"]
    room_bg = palette.get("room_bg")

    for name, (w, h, ll_x, ll_y, tiles, layer, cx, cy) in room_data.items():
        rects = layer_geo.setdefault(layer, [])
        if room_bg:
            rects.append(
                f'<rect x="{ll_x:.2f}" y="{_svg_y(ll_y, h):.2f}" '
                f'width="{w * TILE_UNITS:.2f}" '
                f'height="{h * TILE_UNITS:.2f}" '
                f'fill="{room_bg}"/>'
            )
        if style == "filled":
            if getattr(resolver, "optimize", True):
                _emit_filled_optimized(rects, tiles, ll_x, ll_y, palette)
            else:
                _emit_filled(rects, tiles, ll_x, ll_y, palette)
        else:
            _emit_outline(rects, tiles, ll_x, ll_y, palette)
        layer_labels.setdefault(layer, []).append(
            f'<text x="{cx:.1f}" y="{(-cy):.1f}" font-size="6" '
            f'fill="{label_color}" text-anchor="middle" '
            f'font-family="monospace">{name}</text>'
        )

    # ── Build connection lines anchored at shortcut tiles, plus `≡` glyphs ─
    mark_color = palette["mark"]
    conn_color = palette["conn"]
    conn_dash = palette.get("conn_dash")
    dash_attr = f' stroke-dasharray="{conn_dash}"' if conn_dash else ""
    layer_marks = {}        # layer -> list[str] (≡ symbols)
    connections = []        # list[(line_str, {layer_a, layer_b})]
    for (a, b, ax, ay, bx, by, _da, _db) in conn_entries:
        in_a = a in room_data
        in_b = b in room_data
        if not (in_a or in_b):
            continue
        if in_a:
            la = room_data[a][5]
            wax, way = tile_to_world(a, ax, ay)
            layer_marks.setdefault(la, []).append(
                f'<text x="{wax:.2f}" y="{(-way + 2.0):.2f}" font-size="6" '
                f'text-anchor="middle" fill="{mark_color}" '
                f'font-family="monospace">≡</text>'
            )
        if in_b:
            lb = room_data[b][5]
            wbx, wby = tile_to_world(b, bx, by)
            layer_marks.setdefault(lb, []).append(
                f'<text x="{wbx:.2f}" y="{(-wby + 2.0):.2f}" font-size="6" '
                f'text-anchor="middle" fill="{mark_color}" '
                f'font-family="monospace">≡</text>'
            )
        if in_a and in_b:
            line = (
                f'<line x1="{wax:.2f}" y1="{(-way):.2f}" '
                f'x2="{wbx:.2f}" y2="{(-wby):.2f}" '
                f'stroke="{conn_color}" stroke-width="0.8" '
                f'opacity="0.9"{dash_attr}/>'
            )
            connections.append((line, {room_data[a][5], room_data[b][5]}))

    pad = 30
    vb_x = min_x - pad
    vb_y = -max_y - pad
    vb_w = (max_x - min_x) + 2 * pad
    vb_h = (max_y - min_y) + 2 * pad
    viewbox = f"{vb_x:.1f} {vb_y:.1f} {vb_w:.1f} {vb_h:.1f}"
    bg_rect = (
        f'<rect x="{vb_x:.1f}" y="{vb_y:.1f}" width="{vb_w:.1f}" '
        f'height="{vb_h:.1f}" fill="{palette["bg"]}"/>'
    )
    title_font_size = getattr(resolver, "title_font_size", 70)
    title_text = (
        f'<text x="{vb_x + 6:.1f}" y="{vb_y + title_font_size + 2:.1f}" '
        f'font-size="{title_font_size}" '
        f'fill="{label_color}" font-family="monospace">'
        f'{display_name} ({region.upper()})</text>'
    )

    all_layers = sorted(set(list(layer_geo) + list(layer_marks) + list(layer_labels)))

    def write_svg(path, render_layers, conn_filter=None):
        """render_layers: ordered list of layer ints (back-to-front).
        conn_filter: set of layer ints; only connections touching one of these
        are drawn. None = draw all.
        """
        with open(path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">\n')
            f.write(f'<title>{display_name} ({region.upper()})</title>\n')
            f.write(bg_rect + "\n")
            if getattr(resolver, "show_title", True):
                f.write(title_text + "\n")
            for layer in render_layers:
                f.write(
                    f'<g id="layer-{layer}" shape-rendering="crispEdges">\n'
                )
                for chunk in (layer_geo.get(layer, []),
                              layer_marks.get(layer, [])):
                    if chunk:
                        f.write("\n".join(chunk) + "\n")
                f.write("</g>\n")
            # Connections on top of all geometry, never dimmed.
            conn_lines = [
                ln for (ln, touched) in connections
                if conn_filter is None or touched & conn_filter
            ]
            if conn_lines:
                f.write('<g id="connections">\n' + "\n".join(conn_lines) + "\n</g>\n")
            # Labels above everything, full opacity.
            label_lines = []
            for layer in render_layers:
                label_lines.extend(layer_labels.get(layer, []))
            if label_lines:
                f.write('<g id="labels">\n' + "\n".join(label_lines) + "\n</g>\n")
            f.write("</svg>\n")
        return len(conn_lines)

    out_path = os.path.join(out_dir, f"{region}.svg")
    drawn_conn = write_svg(out_path, all_layers)

    written = [out_path]
    if getattr(resolver, "separate_layers", False):
        for layer in all_layers:
            p = os.path.join(out_dir, f"{region}_L{layer}.svg")
            write_svg(p, [layer], conn_filter={layer})
            written.append(p)

    layer_summary = ",".join(f"L{k}:{v}" for k, v in sorted(layer_counts.items()))
    suffix = "" if len(written) == 1 else f" (+{len(written) - 1} per-layer)"
    # Order sources non-vanilla-first so mod-modified regions stand out.
    non_vanilla = sorted(s for s in sources if s and s != "vanilla")
    has_vanilla = "vanilla" in sources
    src_tag = "+".join(non_vanilla + (["vanilla"] if has_vanilla else []))
    tag = "vanilla" if src_tag == "vanilla" else f"MOD[{src_tag}]"
    return region, (
        f"{tag:<24} ok ({len(room_data)}/{len(rooms_map)} rooms "
        f"[{layer_summary}], {drawn_conn} conns) -> {out_path}{suffix}"
    )


def _worker(args):
    (mod_roots, vanilla_root, slugcat, separate_layers, style, palette,
     optimize, fix_overlaps, layout_optimize, show_title, title_font_size,
     region, out_dir) = args
    try:
        resolver = Resolver(mod_roots, vanilla_root, slugcat)
        resolver.separate_layers = separate_layers
        resolver.style = style
        resolver.palette = palette
        resolver.optimize = optimize
        resolver.fix_overlaps = fix_overlaps
        resolver.layout_optimize = layout_optimize
        resolver.show_title = show_title
        resolver.title_font_size = title_font_size
        return render_region(resolver, region, out_dir)
    except Exception as e:
        return region, f"error: {type(e).__name__}: {e}"


def main():
    p = argparse.ArgumentParser(
        description="Render Rain World region geometry as SVG, "
                    "with workshop mod files prioritized over vanilla."
    )
    p.add_argument("--mod", action="append", default=None,
                   help="Workshop ID, absolute path, or substring of a mod's "
                        "id or modinfo `name`. Repeat for multiple mods "
                        "(left = higher priority). A substring matching more "
                        "than one mod is rejected — split it into separate "
                        "--mod args. Use --scan to list available mods.")
    p.add_argument("--scan", action="store_true",
                   help="List every workshop / local mod that contributes "
                        "world data (has world/ or modify/world/) and exit. "
                        "Shows source, id/folder, name, and path.")
    p.add_argument("--game", default=GAME_DEFAULT,
                   help="Rain World install path (the folder containing "
                        "RainWorld_Data/). The vanilla world and workshop "
                        "folder are derived from this. Default: standard "
                        "Steam location on C:.")
    p.add_argument("--no-local-mods", action="store_true",
                   help="Skip auto-including StreamingAssets/mods/* as layers")
    p.add_argument("--separate-layers", action="store_true",
                   help="Also emit one SVG per canLayer: <region>_L<N>.svg")
    p.add_argument("--outline", action="store_true",
                   help="Use the outline (wireframe) style instead of the "
                        "default filled style. Outline is cream-bg with thin "
                        "black edges + slope diagonals.")
    p.add_argument("--invert", action="store_true",
                   help="Invert every colour in the palette.")
    p.add_argument("--no-optimize", action="store_true",
                   help="Emit one rect per solid-tile row (legacy output) "
                        "instead of merging solids + slopes into a single "
                        "<path> per connected component. Filled style only.")
    p.add_argument("--fix-overlaps", action="store_true",
                   help="If overlapping room layouts are detected (devPos "
                        "rects intersecting), run a simple iterative "
                        "separation pass to spread them apart. No-op when "
                        "no overlap is detected. Off by default.")
    p.add_argument("--optimize", action="store_true",
                   help="Force-directed spring layout pass that repositions "
                        "rooms to minimize total connection-line length "
                        "before overlap resolution. For maps whose devPos "
                        "layout is messy (long crossing connections even "
                        "when non-overlapping). Requires --fix-overlaps, "
                        "since the spring pass does not itself guarantee a "
                        "non-overlapping result.")
    p.add_argument("--no-title", action="store_true",
                   help="Don't draw the visible region-name label in the "
                        "top-left corner of the SVG.")
    p.add_argument("--title-font-size", type=float, default=70,
                   help="Font size (px) of the visible region-name label. "
                        "Default: 70.")
    p.add_argument("--region", default="all",
                   help="Region code (e.g. su, dm) or 'all'")
    p.add_argument("--slugcat", default="",
                   help="Slugcat id for campaign-specific file variants "
                        "(e.g. map_oe-<id>.txt). Empty disables (default).")
    p.add_argument("--vanilla", default=None,
                   help="Override the StreamingAssets path (default: derived "
                        "from --game).")
    p.add_argument("--workshop", default=None,
                   help="Override the workshop content path used to resolve "
                        "bare workshop IDs (default: derived from --game).")
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "out"))
    args = p.parse_args()

    vanilla_root = args.vanilla or vanilla_for(args.game)
    workshop_root = args.workshop or workshop_for(args.game)
    local_mods_dir = os.path.join(vanilla_root, "mods")

    mods_index = scan_world_mods(workshop_root, local_mods_dir)

    if args.scan:
        if not mods_index:
            print("no world-changing mods found in:")
            print(f"  workshop: {workshop_root}")
            print(f"  local:    {local_mods_dir}")
            return
        w_src = max(len(m["source"]) for m in mods_index)
        w_id = max(len(m["id"]) for m in mods_index)
        print(f"world-changing mods ({len(mods_index)}):")
        for m in mods_index:
            print(f"  {m['source']:<{w_src}}  {m['id']:<{w_id}}  "
                  f"name={m['name']!r}  -> {m['path']}")
        return

    mod_args = args.mod or []
    explicit_mod_roots = [resolve_mod(m, mods_index) for m in mod_args]
    mod_roots = list(explicit_mod_roots)
    if not args.no_local_mods and os.path.isdir(local_mods_dir):
        for entry in sorted(os.listdir(local_mods_dir)):
            p = os.path.join(local_mods_dir, entry)
            if os.path.isdir(p) and p not in mod_roots:
                mod_roots.append(p)

    # Auto-detect slugcat from the explicitly-selected mods only. Local mods
    # like moreslugcats ship files for many slugcats — auto-detecting from
    # them would be ambiguous, so they're skipped here. Pass --slugcat to
    # override or to pick one when several mods disagree.
    if args.slugcat:
        slugcat = args.slugcat
        slugcat_source = "explicit"
    else:
        detected = set()
        for m in explicit_mod_roots:
            detected |= detect_mod_slugcats(m)
        if len(detected) == 1:
            slugcat = next(iter(detected))
            slugcat_source = "auto"
        elif len(detected) > 1:
            raise SystemExit(
                f"selected mods ship files for multiple slugcats: "
                f"{sorted(detected)}. Pass --slugcat <id> to pick one."
            )
        else:
            slugcat = None
            slugcat_source = "none"
    os.makedirs(args.out, exist_ok=True)

    resolver = Resolver(mod_roots, vanilla_root, slugcat)
    available = resolver.regions()
    if args.region.lower() == "all":
        regions = available
    else:
        regions = [args.region.lower()]
        if regions[0] not in available:
            print(f"warning: '{regions[0]}' not in any layer", file=sys.stderr)

    print(f"game:    {args.game}")
    print(f"mods:    {mod_roots}")
    print(f"vanilla: {vanilla_root}")
    slugcat_tag = (f"{slugcat} (auto)" if slugcat_source == "auto"
                   else str(slugcat))
    print(f"slugcat: {slugcat_tag}")
    print(f"regions: {regions}")
    print(f"out:     {args.out}\n")

    style = "outline" if args.outline else "filled"
    palette = PALETTES[style]
    if args.invert:
        palette = invert_palette(palette)
    if args.optimize and not args.fix_overlaps:
        raise SystemExit("--optimize requires --fix-overlaps "
                          "(the spring pass can itself introduce overlaps)")

    optimize = not args.no_optimize
    work = [(mod_roots, vanilla_root, slugcat, args.separate_layers, style,
             palette, optimize, args.fix_overlaps, args.optimize,
             not args.no_title, args.title_font_size, r, args.out)
            for r in regions]
    with ProcessPoolExecutor() as ex:
        for region, msg in ex.map(_worker, work):
            print(f"[{region:>4}] {msg}")


if __name__ == "__main__":
    main()
