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
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

VANILLA_DEFAULT = r"C:\Program Files (x86)\Steam\steamapps\common\Rain World\RainWorld_Data\StreamingAssets"
WORKSHOP_DEFAULT = r"C:\Program Files (x86)\Steam\steamapps\workshop\content\312520"

T_AIR, T_SOLID, T_SLOPE, T_FLOOR, T_SHORTCUT = 0, 1, 2, 3, 4
# devPos units per tile = 2, matching generateGeoJSON.py:74 (`devPos*10` with
# 20 px per tile). devPos is the dev-editor placement — rooms tile naturally.
TILE_UNITS = 2.0


def resolve_mod(arg):
    if os.path.isabs(arg) and os.path.isdir(arg):
        return arg
    p = os.path.join(WORKSHOP_DEFAULT, arg)
    if os.path.isdir(p):
        return p
    raise SystemExit(f"mod not found: {arg}")


class Resolver:
    """File lookup. Layer order (highest priority first): per-mod modify/world,
    per-mod world, then vanilla world. Within a layer, slugcat-suffixed
    variants (e.g. `map_oe-thechampion.txt`) win over the plain name."""

    def __init__(self, mod_roots, vanilla_root, slugcat=None):
        self.layers = []
        for m in mod_roots:
            self.layers.append(os.path.join(m, "modify", "world"))
            self.layers.append(os.path.join(m, "world"))
        self.layers.append(os.path.join(vanilla_root, "world"))
        self.slugcat = slugcat

    def _variants(self, fname):
        if not self.slugcat:
            return [fname]
        base, ext = os.path.splitext(fname)
        return [f"{base}-{self.slugcat}{ext}", fname]

    def region_file(self, region, fname):
        for layer in self.layers:
            for v in self._variants(fname):
                p = os.path.join(layer, region, v)
                if os.path.isfile(p):
                    return p
        return None

    def room_file(self, region, room_name):
        for layer in self.layers:
            for v in self._variants(room_name.lower() + ".txt"):
                for sub in (f"{region}-rooms", "gates", "gate shelters"):
                    p = os.path.join(layer, sub, v)
                    if os.path.isfile(p):
                        return p
        return None

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


def _emit_filled(out, tiles, ll_x, ll_y):
    """Filled style: solid 3-unit black rects (RLE per row), platform shelves,
    beams as thin lines through tile centers."""
    by_row = {}
    beams = []
    for tx, ty, terrain, vbeam, hbeam in tiles:
        if terrain in (T_SOLID, T_SLOPE, T_FLOOR):
            by_row.setdefault((ty, terrain), []).append(tx)
        if vbeam or hbeam:
            beams.append((tx, ty, vbeam, hbeam))
    for (ty, terrain), xs in by_row.items():
        xs.sort()
        runs, start, prev = [], xs[0], xs[0]
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
            if terrain == T_FLOOR:
                out.append(
                    f'<rect x="{px:.2f}" y="{y_top:.2f}" '
                    f'width="{run_w:.2f}" height="1" fill="#555"/>'
                )
            else:
                out.append(
                    f'<rect x="{px:.2f}" y="{y_top:.2f}" '
                    f'width="{run_w:.2f}" height="{TILE_UNITS:.2f}" fill="#000"/>'
                )
    for tx, ty, vb, hb in beams:
        _emit_beams(out, ll_x, ll_y, tx, ty, vb, hb)


def _emit_beams(out, ll_x, ll_y, tx, ty, vb, hb):
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
            f'stroke="#777" stroke-width="0.4"/>'
        )
    if hb:
        out.append(
            f'<line x1="{tile_l:.2f}" y1="{mid_y:.2f}" '
            f'x2="{tile_r:.2f}" y2="{mid_y:.2f}" '
            f'stroke="#777" stroke-width="0.4"/>'
        )


def _emit_outline(out, tiles, ll_x, ll_y):
    """Outline style: black 1-tile-wide boundary lines between solid and
    non-solid tiles (RLE'd), slope tiles as diagonals oriented by their two
    solid neighbors, half-floors as a shelf with side stubs, plus beams."""
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
                _emit_h_edge(out, ll_x, ll_y, start, prev + 1, yg)
                start = prev = v
        _emit_h_edge(out, ll_x, ll_y, start, prev + 1, yg)

    for xg, ys in v_edges.items():
        ys.sort()
        start = prev = ys[0]
        for v in ys[1:]:
            if v == prev + 1:
                prev = v
            else:
                _emit_v_edge(out, ll_x, ll_y, xg, start, prev + 1)
                start = prev = v
        _emit_v_edge(out, ll_x, ll_y, xg, start, prev + 1)

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
            _emit_seg(out, tl_x, y_top, tr_x, y_bot)
        elif L and U:
            _emit_seg(out, tl_x, y_bot, tr_x, y_top)
        elif R and D:
            _emit_seg(out, tl_x, y_bot, tr_x, y_top)
        elif R and U:
            _emit_seg(out, tl_x, y_top, tr_x, y_bot)

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
        _emit_seg(out, tile_l, y_top, tile_r, y_top)
        # mid-line (skipped if hbeam, since that line would coincide)
        if not hb:
            _emit_seg(out, tile_l, y_mid, tile_r, y_mid)
        # left stub
        left = by_pos.get((x - 1, y), (T_AIR, False, False))[0]
        if left == T_SOLID:
            _emit_seg(out, tile_l, y_bot, tile_l, y_mid)
        else:
            _emit_seg(out, tile_l, y_mid, tile_l, y_top)
        # right stub
        right = by_pos.get((x + 1, y), (T_AIR, False, False))[0]
        if right == T_SOLID:
            _emit_seg(out, tile_r, y_bot, tile_r, y_mid)
        else:
            _emit_seg(out, tile_r, y_mid, tile_r, y_top)

    # Beams
    for (x, y), (terrain, vb, hb) in by_pos.items():
        if vb or hb:
            _emit_beams(out, ll_x, ll_y, x, y, vb, hb)


def _emit_h_edge(out, ll_x, ll_y, x_start, x_end, y_grid):
    x1 = ll_x + x_start * TILE_UNITS
    x2 = ll_x + x_end * TILE_UNITS
    yy = _svg_y(ll_y, y_grid)
    out.append(
        f'<line x1="{x1:.2f}" y1="{yy:.2f}" x2="{x2:.2f}" y2="{yy:.2f}" '
        f'stroke="#000" stroke-width="0.35"/>'
    )


def _emit_v_edge(out, ll_x, ll_y, x_grid, y_start, y_end):
    xx = ll_x + x_grid * TILE_UNITS
    y1 = _svg_y(ll_y, y_start)
    y2 = _svg_y(ll_y, y_end)
    out.append(
        f'<line x1="{xx:.2f}" y1="{y1:.2f}" x2="{xx:.2f}" y2="{y2:.2f}" '
        f'stroke="#000" stroke-width="0.35"/>'
    )


def _emit_seg(out, x1, y1, x2, y2):
    out.append(
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="#000" stroke-width="0.35"/>'
    )


def render_region(resolver, region, out_dir):
    map_path = resolver.region_file(region, f"map_{region}.txt")
    if not map_path:
        return region, "skip (no map_*.txt)"

    rooms_map, conn_entries = parse_map_file(map_path)

    # Cache geometry-derived per-room data we need below.
    room_data = {}  # name -> (w, h, ll_x, ll_y, tiles, layer)
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    for name, (dx, dy, layer, _sub) in rooms_map.items():
        room_path = resolver.room_file(region, name)
        if not room_path:
            continue
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

    for name, (w, h, ll_x, ll_y, tiles, layer, cx, cy) in room_data.items():
        rects = layer_geo.setdefault(layer, [])
        if style == "filled":
            _emit_filled(rects, tiles, ll_x, ll_y)
        else:
            _emit_outline(rects, tiles, ll_x, ll_y)
        layer_labels.setdefault(layer, []).append(
            f'<text x="{cx:.1f}" y="{(-cy):.1f}" font-size="6" '
            f'fill="#c0392b" text-anchor="middle" '
            f'font-family="monospace">{name}</text>'
        )

    # ── Build connection lines anchored at shortcut tiles, plus `≡` glyphs ─
    # Marks live on the layer of the room they attach to (so they dim with it).
    # Connection lines are kept layer-agnostic and drawn on top of all geometry.
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
                f'text-anchor="middle" fill="#1f4e79" '
                f'font-family="monospace">≡</text>'
            )
        if in_b:
            lb = room_data[b][5]
            wbx, wby = tile_to_world(b, bx, by)
            layer_marks.setdefault(lb, []).append(
                f'<text x="{wbx:.2f}" y="{(-wby + 2.0):.2f}" font-size="6" '
                f'text-anchor="middle" fill="#1f4e79" '
                f'font-family="monospace">≡</text>'
            )
        if in_a and in_b:
            line = (
                f'<line x1="{wax:.2f}" y1="{(-way):.2f}" '
                f'x2="{wbx:.2f}" y2="{(-wby):.2f}" '
                f'stroke="#27ae60" stroke-width="0.8" opacity="0.85"/>'
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
        f'height="{vb_h:.1f}" fill="#f6f1e7"/>'
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
            f.write(bg_rect + "\n")
            for layer in render_layers:
                f.write(f'<g id="layer-{layer}">\n')
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
    return region, (
        f"ok ({len(room_data)}/{len(rooms_map)} rooms [{layer_summary}], "
        f"{drawn_conn} conns) -> {out_path}{suffix}"
    )


def _worker(args):
    (mod_roots, vanilla_root, slugcat, separate_layers, style,
     region, out_dir) = args
    try:
        resolver = Resolver(mod_roots, vanilla_root, slugcat)
        resolver.separate_layers = separate_layers
        resolver.style = style
        return render_region(resolver, region, out_dir)
    except Exception as e:
        return region, f"error: {type(e).__name__}: {e}"


def main():
    p = argparse.ArgumentParser(
        description="Render Rain World region geometry as SVG, "
                    "with workshop mod files prioritized over vanilla."
    )
    p.add_argument("--mod", action="append", default=None,
                   help="Workshop ID or absolute path. Repeat for multiple "
                        "mods (left = higher priority). Default: 3033569804")
    p.add_argument("--no-local-mods", action="store_true",
                   help="Skip auto-including StreamingAssets/mods/* as layers")
    p.add_argument("--separate-layers", action="store_true",
                   help="Also emit one SVG per canLayer: <region>_L<N>.svg")
    p.add_argument("--filled", action="store_true",
                   help="Use the filled (solid black blocks) style. "
                        "Default is outline: boundary lines + slope diagonals.")
    p.add_argument("--region", default="all",
                   help="Region code (e.g. su, dm) or 'all'")
    p.add_argument("--slugcat", default="thechampion",
                   help="Slugcat id for campaign-specific file variants "
                        "(e.g. map_oe-<id>.txt). Empty string disables.")
    p.add_argument("--vanilla", default=VANILLA_DEFAULT)
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "out"))
    args = p.parse_args()

    mod_args = args.mod or ["3033569804"]
    mod_roots = [resolve_mod(m) for m in mod_args]
    if not args.no_local_mods:
        local_mods_dir = os.path.join(args.vanilla, "mods")
        if os.path.isdir(local_mods_dir):
            for entry in sorted(os.listdir(local_mods_dir)):
                p = os.path.join(local_mods_dir, entry)
                if os.path.isdir(p) and p not in mod_roots:
                    mod_roots.append(p)
    slugcat = args.slugcat or None
    os.makedirs(args.out, exist_ok=True)

    resolver = Resolver(mod_roots, args.vanilla, slugcat)
    available = resolver.regions()
    if args.region.lower() == "all":
        regions = available
    else:
        regions = [args.region.lower()]
        if regions[0] not in available:
            print(f"warning: '{regions[0]}' not in any layer", file=sys.stderr)

    print(f"mods:    {mod_roots}")
    print(f"vanilla: {args.vanilla}")
    print(f"slugcat: {slugcat}")
    print(f"regions: {regions}")
    print(f"out:     {args.out}\n")

    style = "filled" if args.filled else "outline"
    work = [(mod_roots, args.vanilla, slugcat, args.separate_layers, style,
             r, args.out) for r in regions]
    with ProcessPoolExecutor() as ex:
        for region, msg in ex.map(_worker, work):
            print(f"[{region:>4}] {msg}")


if __name__ == "__main__":
    main()
