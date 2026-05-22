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
TILE_UNITS = 3.0  # map units per tile (size * 3 footprint, per World.cs:1091)


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

    def region_files_all(self, region, fname):
        out = []
        for layer in self.layers:
            for v in self._variants(fname):
                p = os.path.join(layer, region, v)
                if os.path.isfile(p):
                    out.append(p)
                    break
        return out

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
    """Returns {ROOMNAME_UPPER: (mapX, mapY, layer, subregion)}."""
    rooms = {}
    for raw in read_lines(path):
        s = raw.strip()
        if not s or ":" not in s:
            continue
        if "\t" in s:
            s = s.split("\t", 1)[1] if "\t" in s.split(":", 1)[0] else s
        head, rest = s.split(":", 1)
        name = head.strip().upper()
        if not name or name.startswith("//"):
            continue
        fields = re.split(r"><", rest.strip())
        if len(fields) < 2:
            continue
        try:
            x = float(fields[0])
            y = float(fields[1])
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
    return rooms


def parse_world_connections(paths):
    """Union-merge ROOMS sections across all `paths` (mod [MERGE] semantics).
    Each path's ROOMS lines contribute (roomA, roomB) pairs; duplicates dedupe.
    Returns list of unique (roomA_upper, roomB_upper) pairs.
    """
    conns = set()
    for path in paths:
        in_rooms = False
        for raw in read_lines(path):
            line = raw.strip()
            if line == "ROOMS":
                in_rooms = True
                continue
            if line == "END ROOMS":
                in_rooms = False
                continue
            if not in_rooms or not line or line.startswith("//"):
                continue
            parts = [p.strip() for p in line.split(":")]
            if len(parts) < 2:
                continue
            name = parts[0].upper()
            for nb in (n.strip().upper() for n in parts[1].split(",")):
                if nb in ("", "DISCONNECTED"):
                    continue
                conns.add(tuple(sorted((name, nb))))
    return list(conns)


def parse_room_geometry(path):
    """Returns (width, height, [(x, y, terrain), ...]) or None.

    Per Room.cs:5418 — lines[11] holds tile data; '|' separates per-tile
    entries; per entry ','-separated attrs with attrs[0] = TerrainType int.
    Tile iteration order: x=0,y=H-1; y-- each step; on y<0 reset y=H-1, x++.
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
        try:
            terrain = int(entry.split(",", 1)[0])
        except ValueError:
            terrain = 0
        if terrain in (T_SOLID, T_SLOPE, T_FLOOR):
            tiles.append((x, y, terrain))
        y -= 1
        if y < 0:
            y = h - 1
            x += 1
    return w, h, tiles


def render_region(resolver, region, out_dir):
    map_path = resolver.region_file(region, f"map_{region}.txt")
    if not map_path:
        return region, "skip (no map_*.txt)"
    world_paths = resolver.region_files_all(region, f"world_{region}.txt")

    rooms_map = parse_map_file(map_path)
    conns = parse_world_connections(world_paths)

    tile_rects = []   # geometry rects
    labels = []       # room name texts
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    rendered_rooms = 0

    for name, (mx, my, _layer, _sub) in rooms_map.items():
        room_path = resolver.room_file(region, name)
        if not room_path:
            continue
        geo = parse_room_geometry(room_path)
        if not geo:
            continue
        w, h, tiles = geo
        ll_x = mx - (w * TILE_UNITS) / 2.0
        ll_y = my - (h * TILE_UNITS) / 2.0

        # Run-length encode consecutive solid tiles within each (row, terrain) group.
        by_row = {}
        for tx, ty, terrain in tiles:
            by_row.setdefault((ty, terrain), []).append(tx)
        for (ty, terrain), xs in by_row.items():
            xs.sort()
            runs = []
            start = prev = xs[0]
            for v in xs[1:]:
                if v == prev + 1:
                    prev = v
                else:
                    runs.append((start, prev))
                    start = prev = v
            runs.append((start, prev))
            # SVG y is flipped; place at y=-(ll_y + (ty+1)*TILE_UNITS) so the
            # tile top maps to the higher screen position.
            svg_y_top = -(ll_y + (ty + 1) * TILE_UNITS)
            for sx, ex in runs:
                px = ll_x + sx * TILE_UNITS
                run_w = (ex - sx + 1) * TILE_UNITS
                if terrain == T_FLOOR:
                    tile_rects.append(
                        f'<rect x="{px:.2f}" y="{svg_y_top:.2f}" '
                        f'width="{run_w:.2f}" height="1" fill="#555"/>'
                    )
                else:
                    tile_rects.append(
                        f'<rect x="{px:.2f}" y="{svg_y_top:.2f}" '
                        f'width="{run_w:.2f}" height="{TILE_UNITS:.2f}" fill="#000"/>'
                    )

        rx1 = ll_x + w * TILE_UNITS
        ry1 = ll_y + h * TILE_UNITS
        if ll_x < min_x: min_x = ll_x
        if ll_y < min_y: min_y = ll_y
        if rx1 > max_x:  max_x = rx1
        if ry1 > max_y:  max_y = ry1

        labels.append(
            f'<text x="{mx:.1f}" y="{(-my):.1f}" font-size="6" '
            f'fill="#c0392b" text-anchor="middle" '
            f'font-family="monospace">{name}</text>'
        )
        rendered_rooms += 1

    conn_lines = []
    for a, b in conns:
        if a not in rooms_map or b not in rooms_map:
            continue
        ax, ay, *_ = rooms_map[a]
        bx, by, *_ = rooms_map[b]
        conn_lines.append(
            f'<line x1="{ax:.1f}" y1="{(-ay):.1f}" x2="{bx:.1f}" y2="{(-by):.1f}" '
            f'stroke="#27ae60" stroke-width="0.8" opacity="0.6"/>'
        )

    if min_x == float("inf"):
        return region, f"skip (0/{len(rooms_map)} rooms had geometry)"

    pad = 30
    vb_x = min_x - pad
    vb_y = -max_y - pad
    vb_w = (max_x - min_x) + 2 * pad
    vb_h = (max_y - min_y) + 2 * pad

    out_path = os.path.join(out_dir, f"{region}.svg")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{vb_x:.1f} {vb_y:.1f} {vb_w:.1f} {vb_h:.1f}">\n'
        )
        f.write(
            f'<rect x="{vb_x:.1f}" y="{vb_y:.1f}" width="{vb_w:.1f}" '
            f'height="{vb_h:.1f}" fill="#f6f1e7"/>\n'
        )
        if conn_lines:
            f.write("<g id=\"connections\">\n" + "\n".join(conn_lines) + "\n</g>\n")
        f.write("<g id=\"geometry\">\n" + "\n".join(tile_rects) + "\n</g>\n")
        f.write("<g id=\"labels\">\n" + "\n".join(labels) + "\n</g>\n")
        f.write("</svg>\n")
    return region, (
        f"ok ({rendered_rooms}/{len(rooms_map)} rooms, "
        f"{len(conn_lines)} conns) -> {out_path}"
    )


def _worker(args):
    mod_roots, vanilla_root, slugcat, region, out_dir = args
    try:
        return render_region(
            Resolver(mod_roots, vanilla_root, slugcat), region, out_dir
        )
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

    work = [(mod_roots, args.vanilla, slugcat, r, args.out) for r in regions]
    with ProcessPoolExecutor() as ex:
        for region, msg in ex.map(_worker, work):
            print(f"[{region:>4}] {msg}")


if __name__ == "__main__":
    main()
