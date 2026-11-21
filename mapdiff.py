"""Region change metrics: modded world vs a baseline (vanilla + enabled DLC).

Three parts:

1. Mini ModMerger. The DLC (More Slugcats, Watcher) ship `modify/world`
   patches, not whole files. The game bakes them, together with every other
   enabled mod's patches, into StreamingAssets/mergedmods, so the baseline
   can't be read from there. `build_baseline_merged` re-applies only the
   baseline mods' patches into a cache dir, porting ModManager.ModMerger
   (ModManager.cs: CollectModifications 1584, ApplyModifications 1878,
   ApplyMerges dispatch 1812, MergeWorldFiles 3251, MergeRoomSettings 3108,
   MergeUniqueItemsUnordered 2985, MapMerger.MergeMapFiles 418, file order
   ExecutePendingMerge 2763). [FILEDESTINATION] sub-patches and map image
   merging are not ported (no world data in them for us).

2. Region snapshot + diff. Both sides are read through mapper.Resolver with
   the same slugcat chain, so the comparison sees what the game would load.
   Dimensions (each a 0..1 distance): rooms, connections, geometry,
   layout, creatures, objects, properties. Weighted into one score.

3. Copy detection. Every baseline room gets a geometry fingerprint: an exact
   hash plus a 24x24 air-occupancy bitmap. A room new to a region is
   matched against all baseline rooms: identical -> copy, bitmap Jaccard
   >= DERIVED_MIN -> derived, else original.
"""

import hashlib
import os
import pickle
import re
import statistics
from collections import Counter

import mapper

# ---------------------------------------------------------------------------
# Weights / thresholds
# ---------------------------------------------------------------------------

WEIGHTS = {
    "rooms": 0.20,
    "geometry": 0.30,
    "connections": 0.15,
    "creatures": 0.20,
    "objects": 0.10,
    "layout": 0.025,
    "properties": 0.025,
}
TWEAKED_MAX = 0.10          # score below this (and > 0) -> "tweaked"
DERIVED_MIN = 0.85          # bitmap Jaccard for "derived from" a baseline room
SIG_GRID = 24               # fingerprint bitmap resolution (SIG_GRID^2 bits)
SIZE_TOLERANCE = 0.25       # fuzzy match only between rooms within 25% w/h

# Room status -> (svg colour, legend text). Order = legend order.
STATUS_STYLE = {
    "new": ("#2ecc71", "new room (original)"),
    "new-derived": ("#a9dfbf", "new room, derived from a baseline room"),
    "new-copy": ("#16a085", "new room, copy of a baseline room"),
    "geometry": ("#e67e22", "geometry changed"),
    "moved": ("#3498db", "moved on map"),
    "content": ("#f1c40f", "creatures / objects / connections changed"),
    "removed": ("#e74c3c", "removed (baseline position)"),
}


def _read(path):
    """File.ReadAllLines: BOM-tolerant, splits on \\n and \\r\\n."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        txt = f.read()
    return _norm(txt.split("\n"))


def _norm(lines):
    """Re-split lines that carry embedded newlines ([ADD]\\n...), as a
    WriteAllLines + ReadAllLines round trip in the game would."""
    out = []
    for ln in lines:
        out.extend(ln.replace("\r\n", "\n").replace("\r", "").split("\n"))
    while out and out[-1] == "":
        out.pop()
    return out


# ---------------------------------------------------------------------------
# 1. Mini ModMerger
# ---------------------------------------------------------------------------

def _mrs(line, tag):
    """PendingApply.MatchAndRemoveStart."""
    if line.startswith(tag):
        return line[len(tag):].replace("\\n", "\n")
    return None


class _Repl:
    __slots__ = ("find", "find_re", "find_start", "find_end", "line_no",
                 "find_line", "with_", "kind", "occ", "seen")

    def __init__(self, st, with_, kind, occ=0):
        (self.find, self.find_re, self.find_start, self.find_end,
         self.line_no, self.find_line) = st
        self.with_, self.kind, self.occ, self.seen = with_, kind, occ, 0


def _dotnet_repl(s):
    """.NET replacement string -> Python: $1 -> \\1 (enough for patch files)."""
    return re.sub(r"\$(\d+)", r"\\\1", s.replace("\\", "\\\\"))


class Patch:
    """One modify/ file: merge lines + line replacements (CollectModifications)."""

    def __init__(self, lines):
        self.merge_lines = None
        self.add_at_end, self.add_at_line, self.repls = [], {}, []
        self.unsupported = []
        st = [None, None, None, None, -1, False]   # find/re/start/end/line/findLine
        in_merge = in_dest = False
        for line in lines:
            if in_dest:
                if "[ENDFILEDESTINATION]" in line:
                    in_dest = False
                continue
            if _mrs(line, "[FILEDESTINATION]") is not None:
                in_dest = True
                self.unsupported.append("FILEDESTINATION")
                continue
            if in_merge:
                if "[ENDMERGE]" in line:
                    self.merge_lines.append(line.replace("[ENDMERGE]", ""))
                    in_merge = False
                else:
                    self.merge_lines.append(line)
                continue
            r = _mrs(line, "[MERGE]")
            if r is not None:
                in_merge = True
                if self.merge_lines is None:
                    self.merge_lines = []
                if r.strip():
                    self.merge_lines.append(r.strip())
                continue
            r = _mrs(line, "[ADD]")
            if r is not None:
                self.add_at_end.append(r)
                continue
            m = re.match(r"\[ADD_(\d+)\]", line)
            if m:
                self.add_at_line.setdefault(int(m.group(1)), []).append(
                    line[line.index("]") + 1:].replace("\\n", "\n"))
                continue
            found = False
            for tag, idx, fl in (("[FIND]", 0, False), ("[FINDLINE]", 0, True),
                                 ("[FINDREGEX]", 1, False),
                                 ("[FINDLINEREGEX]", 1, True),
                                 ("[FINDLINESTART]", 2, True),
                                 ("[FINDLINEEND]", 3, True)):
                r = _mrs(line, tag)
                if r is not None:
                    st = [None, None, None, None, -1, fl]
                    st[idx] = r
                    found = True
                    break
            if found:
                continue
            if line.startswith("[TARGETLINE]"):
                try:
                    n = int(line[len("[TARGETLINE]"):].strip())
                except ValueError:
                    n = -1
                st = [None, None, None, None, n if n > 0 else -1, n > 0]
                continue
            has_find = any(x is not None for x in st[:4]) or st[4] > 0
            if not has_find:
                continue
            for tag, kind in (("[REPLACE]", "sub"), ("[ADDBEFORE]", "before"),
                              ("[ADDAFTER]", "after")):
                r = _mrs(line, tag)
                if r is not None:
                    self.repls.append(_Repl(st, r, kind))
                    found = True
                    break
            if found:
                continue
            m = re.match(r"\[(REPLACE|ADDBEFORE|ADDAFTER)_(-?\d+)\]", line)
            if m:
                kind = {"REPLACE": "sub", "ADDBEFORE": "before",
                        "ADDAFTER": "after"}[m.group(1)]
                self.repls.append(_Repl(
                    st, line[line.index("]") + 1:].replace("\\n", "\n"),
                    kind, int(m.group(2))))

    # -- ApplyModifications ------------------------------------------------
    @staticmethod
    def _line(text, w, kind):
        return w if kind == "sub" else (w + text if kind == "before" else text + w)

    @staticmethod
    def _substr(text, find, w, kind):
        rep = w if kind == "sub" else (w + find if kind == "before" else find + w)
        return text.replace(find, rep)

    @staticmethod
    def _regex_all(text, pat, w, kind):
        def f(m):
            last = m.group(m.lastindex) if m.lastindex else m.group(0)
            w2 = m.expand(_dotnet_repl(w))
            return w2 if kind == "sub" else (w2 + last if kind == "before" else last + w2)
        return re.sub(pat, f, text)

    def apply_modifications(self, lines):
        out = []
        for r in self.repls:
            r.seen = 0
        for i, src in enumerate(lines):
            out.extend(self.add_at_line.get(i, []))
            text = src
            for r in self.repls:
                if r.occ == 0:
                    if r.find is not None:
                        if not r.find_line:
                            text = self._substr(text, r.find, r.with_, r.kind)
                        elif r.find in text:
                            text = self._line(text, r.with_, r.kind)
                    elif r.find_re is not None:
                        if not r.find_line:
                            text = self._regex_all(text, r.find_re, r.with_, r.kind)
                        elif re.search(r.find_re, text):
                            text = self._line(text, r.with_, r.kind)
                    elif r.find_start is not None:
                        if text.startswith(r.find_start):
                            text = self._line(text, r.with_, r.kind)
                    elif r.find_end is not None:
                        if text.endswith(r.find_end):
                            text = self._line(text, r.with_, r.kind)
                    elif r.line_no == i + 1:
                        text = self._line(text, r.with_, r.kind)
                    continue
                if r.seen < 0:
                    continue
                if not r.find_line:
                    if r.find is not None:
                        pat = r"\b(" + r.find + r")\b"
                    elif r.find_re is not None:
                        pat = r.find_re
                    else:
                        continue
                    try:
                        n_src = len(re.findall(pat, src))
                    except re.error:
                        continue
                    if r.seen + n_src < r.occ:
                        r.seen += n_src
                        continue
                    num = r.occ - r.seen
                    ms = list(re.finditer(pat, text))
                    if num > len(ms) or num < 1:
                        continue
                    m = ms[num - 1]
                    if r.find is not None:
                        rep = (r.with_ if r.kind == "sub" else
                               r.with_ + r.find if r.kind == "before" else
                               r.find + r.with_)
                        text = text[:m.start()] + rep + text[m.start() + len(r.find):]
                    else:
                        rep = (r.with_ if r.kind == "sub" else
                               r.with_ + m.group(0) if r.kind == "before" else
                               m.group(0) + r.with_)
                        text = (text[:m.start()] +
                                re.sub(pat, _dotnet_repl(rep), text[m.start():], count=1))
                    r.seen = -1
                    continue
                hit = ((r.find is not None and r.find in text) or
                       (r.find is None and r.find_re is not None and re.search(r.find_re, text)) or
                       (r.find is None and r.find_re is None and r.find_start is not None
                        and text.startswith(r.find_start)) or
                       (r.find is None and r.find_re is None and r.find_start is None
                        and r.find_end is not None and text.endswith(r.find_end)))
                if hit:
                    r.seen += 1
                    if r.seen >= r.occ:
                        text = self._line(text, r.with_, r.kind)
                        r.seen = -1
            if text.strip() != "":
                out.append(text)
        out.extend(self.add_at_end)
        return _norm(out)


# -- world_*.txt (ModManager.WorldFile + MergeWorldFiles) --------------------

def _split_spawners(s):
    if "{" not in s:
        return s.split(",")
    out, cur, inside = [], "", False
    for p in s.split(","):
        if not inside:
            if "{" not in p or "}" in p:
                out.append(p)
                continue
            cur, inside = p, True
        else:
            cur += "," + p
            if "}" in p:
                out.append(cur)
                cur, inside = "", False
    return out


def _try_int(s):
    try:
        return int(s.strip())
    except ValueError:
        return 0


def _fmt_float(v):
    s = "%g" % v
    return s.replace("e", "E")


class _Den:
    def __init__(self, s):
        a = s.split("-")
        self.num = _try_int(a[0])
        self.creature = a[1] if len(a) > 1 else ""
        self.attrs, self.qty, inside = "", 0, False
        for p in a[2:]:
            if p.startswith("{"):
                inside, self.attrs = True, p
            elif inside:
                self.attrs += "-" + p
            else:
                self.qty = _try_int(p)
            if "}" in p:
                inside = False

    def __str__(self):
        t = f"{self.num}-{self.creature}"
        if self.attrs:
            t += "-" + self.attrs
        if self.qty > 1:
            t += f"-{self.qty}"
        return t


class _Lineage:
    def __init__(self, s):
        a = s.split("-")
        self.creature, self.attrs, self.chance, inside = a[0], "", 0.0, False
        for p in a[1:]:
            if p.startswith("{"):
                inside, self.attrs = True, p
            elif inside:
                self.attrs += "-" + p
            else:
                try:
                    self.chance = float(p)
                except ValueError:
                    pass
            if "}" in p:
                inside = False

    def __str__(self):
        t = self.creature + ("-" + self.attrs if self.attrs else "")
        return t + "-" + _fmt_float(self.chance)


class _Spawn:
    def __init__(self, character, line, exclude):
        self.character, self.exclude = character, exclude
        if line.startswith("(") and ")" in line:
            line = line[line.index(")") + 1:]
        a = line.split(" : ")
        if a[0].strip() == "LINEAGE":
            self.dens, self.room = None, a[1].strip()
            self.lineage_den = _try_int(a[2]) if len(a) > 2 else 0
            self.lineages = [_Lineage(x.strip()) for x in _split_spawners(a[3] if len(a) > 3 else "")]
        else:
            self.lineages, self.lineage_den, self.room = None, -1, a[0].strip()
            self.dens = [_Den(x.strip()) for x in _split_spawners(a[1] if len(a) > 1 else "") if x.strip()]

    def __str__(self):
        t = f"({'X-' if self.exclude else ''}{self.character})" if self.character else ""
        if self.lineages is not None:
            t += "LINEAGE : "
        t += self.room + " : "
        if self.lineage_den >= 0:
            t += f"{self.lineage_den} : "
        if self.lineages is not None:
            t += ", ".join(str(x) for x in self.lineages)
        elif self.dens is not None:
            t += ", ".join(str(x) for x in self.dens)
        return t


class _RoomLink:
    def __init__(self, line):
        a = line.split(":")
        self.name = a[0].strip()
        self.special = a[2].strip() if len(a) > 2 else ""
        self.conns = [c.strip() for c in (a[1] if len(a) > 1 else "").strip().split(",")]

    def __str__(self):
        t = self.name + " : " + ", ".join(self.conns)
        return t + (" : " + self.special if self.special else "")


class _Cond:
    """Exclusive / hidden / substitute conditional link."""

    def __init__(self, kind, character, line):
        self.kind, self.character = kind, character
        a = line.split(":")
        self.room = self.target = self.subst = ""
        if kind in ("EXCLUSIVEROOM", "HIDEROOM"):
            if len(a) >= 3:
                self.room = a[2].strip()
        elif len(a) >= 4:
            self.room, self.target, self.subst = a[1].strip(), a[2].strip(), a[3].strip()

    def key(self):
        return (self.character + "-" + self.room +
                ("-" + self.target if self.kind == "SUB" else ""))

    def __str__(self):
        if self.kind == "SUB":
            return f"{self.character} : {self.room} : {self.target} : {self.subst}"
        return f"{self.character} : {self.kind} : {self.room}"


class WorldFile:
    def __init__(self, lines):
        self.rooms, self.creatures = [], []
        self.excl, self.hidden, self.subs = [], [], []
        self.blockages, self.unknown = [], []
        sec = ""
        for raw in lines:
            t = raw.strip()
            if not t or t.startswith("//"):
                continue
            if t in ("ROOMS", "CREATURES", "BAT MIGRATION BLOCKAGES", "CONDITIONAL LINKS"):
                sec = t
                continue
            if t.startswith("END ") and sec:
                sec = ""
                continue
            if sec == "":
                self.unknown.append(t)
            elif sec == "ROOMS":
                self.rooms.append(_RoomLink(t))
            elif sec == "CREATURES":
                if t.startswith("(") and ")" in t:
                    spec = t[1:t.index(")")]
                    if spec.startswith("X-"):
                        self.creatures.append(_Spawn(spec[2:], t, True))
                    else:
                        for ch in spec.split(","):
                            self.creatures.append(_Spawn(ch, t, False))
                else:
                    self.creatures.append(_Spawn("", t, False))
            elif sec == "BAT MIGRATION BLOCKAGES":
                self.blockages.append(t)
            elif sec == "CONDITIONAL LINKS" and ":" in t:
                chars = t.split(":")[0].strip().split(",")
                kind = ("EXCLUSIVEROOM" if "EXCLUSIVEROOM" in t else
                        "HIDEROOM" if "HIDEROOM" in t else "SUB")
                target = {"EXCLUSIVEROOM": self.excl, "HIDEROOM": self.hidden,
                          "SUB": self.subs}[kind]
                for ch in chars:
                    target.append(_Cond(kind, ch, t))

    def lines(self):
        out = ["CONDITIONAL LINKS"] + [str(c) for c in self.excl + self.hidden + self.subs]
        out += ["END CONDITIONAL LINKS", "", "ROOMS"] + [str(r) for r in self.rooms]
        out += ["END ROOMS", "", "CREATURES"] + [str(c) for c in self.creatures]
        out += ["END CREATURES", "", "BAT MIGRATION BLOCKAGES"] + self.blockages
        out += ["END BAT MIGRATION BLOCKAGES"]
        if self.unknown:
            out += [""] + self.unknown
        return out


def merge_world(base_lines, merge_lines):
    """ModMerger.MergeWorldFiles."""
    w, m = WorldFile(base_lines), WorldFile(merge_lines)
    for e in m.excl:
        if not any(str(x) == str(e) for x in w.excl):
            w.excl.append(e)
        w.hidden = [h for h in w.hidden if h.key() != e.key()]
    for h in m.hidden:
        if not any(str(x) == str(h) for x in w.hidden):
            w.hidden.append(h)
        w.excl = [e for e in w.excl if e.key() != h.key()]
    for s in m.subs:
        for i, x in enumerate(w.subs):
            if x.key() == s.key():
                w.subs[i] = s
                break
        else:
            w.subs.append(s)
    for nr in m.rooms:
        old = next((r for r in w.rooms if r.name == nr.name), None)
        if old is None:
            w.rooms.append(nr)
            continue
        res, i2, i1 = [], 0, 0
        c2, c1 = nr.conns, old.conns
        while i2 < len(c2) or i1 < len(c1):
            if i2 >= len(c2):
                if c1[i1] != "DISCONNECTED" and c1[i1] not in res:
                    res.append(c1[i1])
                i1 += 1
                continue
            if i1 >= len(c1):
                if c2[i2] != "DISCONNECTED" and c2[i2] not in res:
                    res.append(c2[i2])
                i2 += 1
                continue
            if c1[i1] == c2[i2] and c1[i1] not in res:
                res.append(c1[i1])
                i1 += 1
                i2 += 1
                continue
            if c1[i1] == "DISCONNECTED" and c2[i2] in c1:
                res.append(c1[i1])
                i1 += 1
                continue
            if c2[i2] != "DISCONNECTED" and c2[i2] in c1 and c2[i2] not in res:
                res.append(c2[i2])
                i2 += 1
                continue
            if c2[i2] == "DISCONNECTED" or c2[i2] not in res:
                placed = False
                for k in range(len(res)):
                    if res[k] == "DISCONNECTED":
                        res[k] = c2[i2]
                        placed = True
                        break
                if not placed:
                    for k in range(i1, len(c1)):
                        if c1[k] == "DISCONNECTED":
                            c1[k] = c2[i2]
                            placed = True
                            if k == i1:
                                res.append(c2[i2])
                            break
                if not placed:
                    res.append(c2[i2])
            i2 += 1
            i1 += 1
        old.conns = res
        if nr.special:
            old.special = nr.special
    excl_spawns = [c for c in m.creatures if c.exclude]
    incl_spawns = [c for c in m.creatures if not c.exclude]
    for sp in excl_spawns:
        chars = sp.character.split(",")
        replaced = False
        for i in range(len(w.creatures) - 1, -1, -1):
            c = w.creatures[i]
            if c.room == sp.room and c.lineage_den == sp.lineage_den:
                if c.exclude:
                    replaced = True
                    w.creatures[i] = sp
                elif c.character == "" or c.character not in chars:
                    del w.creatures[i]
        if not replaced:
            w.creatures.append(sp)
    for sp in incl_spawns:
        if sp.lineage_den >= 0 and sp.room == "OFFSCREEN":
            w.creatures.append(sp)
            continue
        replaced = False
        for i in range(len(w.creatures) - 1, -1, -1):
            c = w.creatures[i]
            if c.room == sp.room and c.lineage_den == sp.lineage_den:
                if not c.exclude and c.character == sp.character:
                    w.creatures[i] = sp
                    replaced = True
                if (c.exclude and sp.character != "" and
                        sp.character not in c.character.split(",")):
                    c.character += "," + sp.character
        if not replaced:
            w.creatures.append(sp)
    for b in m.blockages:
        if b not in w.blockages:
            w.blockages.append(b)
    for u in m.unknown:
        if u not in w.unknown:
            w.unknown.append(u)
    return w.lines()


def merge_settings(base_lines, merge_lines):
    """ModMerger.MergeRoomSettings (palette remapping not ported)."""
    d = {}
    for src in (base_lines, merge_lines):
        for text in src:
            n = text.find(":")
            if n < 0:
                continue
            key = text[:n]
            if key in ("Effects", "PlacedObjects", "AmbientSounds", "Triggers") and key in d:
                items = []
                for it in d[key][n + 1:].split(",") + text[n + 1:].split(","):
                    if it.strip() and it not in items:
                        items.append(it)
                d[key] = key + ":" + ",".join(items) + ", "
                continue
            if key == "FadePalette" and key in d:
                if len(text[n + 1:].split(",")) >= len(d[key][n + 1:].split(",")):
                    d[key] = text
            else:
                d[key] = text
    return list(d.values())


def merge_unique_items(base_lines, merge_lines, path):
    """ModMerger.MergeUniqueItemsUnordered (properties.txt and friends)."""
    low = path.lower()
    sounds, props = "sounds.txt" in low, "properties.txt" in low
    d, subregions, templates = {}, [], []
    for src in (base_lines, merge_lines):
        for text in src:
            if text.startswith("//"):
                continue
            n = text.find(":")
            if n < 0:
                continue
            key = text[:n]
            if sounds:
                k = text.find("/")
                if 0 <= k < n:
                    key = text[:k]
            if props:
                for pre in ("Room_Attr:", "Broken Shelters:"):
                    if text.startswith(pre) and ":" in text[len(pre):]:
                        key = text[:len(pre) + text[len(pre):].index(":")]
                if text.startswith("Subregion:"):
                    item = text[len("Subregion:"):].strip()
                    if item not in subregions:
                        subregions.append(item)
                    continue
                if text.startswith("Room Setting Templates:"):
                    for it in text[len("Room Setting Templates:"):].strip().split(","):
                        if it.strip() not in templates:
                            templates.append(it.strip())
                    continue
            d[key] = text
    if templates:
        d["Room Setting Templates"] = "Room Setting Templates: " + ", ".join(templates)
    for i, s in enumerate(subregions):
        d[f"Subregion_{i}"] = "Subregion: " + s
    return list(d.values())


def merge_unique_lines(base_lines, merge_lines):
    """ModMerger.MergeUniqueLinesUnordered (regions.txt, egates.txt)."""
    out = list(base_lines)
    for ln in merge_lines:
        if ln not in out:
            out.append(ln)
    return out


def _map_room_line(line):
    """MapRoomLine.TryParse -> (name, line) or None."""
    a = line.split(": ")
    if len(a) < 2:
        return None
    f = a[1].split("><")
    try:
        float(f[0]), float(f[1])
    except (ValueError, IndexError):
        return None
    return a[0], line


def merge_map(base_lines, merge_lines):
    """MapMerger.MergeMapFiles. Room lines override by name, connections
    replace their room pair (SoftEquals), Def_Mat lines accumulate. A merge
    section is skipped when it shares no room with the base (the game's
    TryFindMapOffset). The offset itself is never applied in the game
    (RemoveOffset runs on a struct copy), so it isn't here either."""
    sections = [[]]
    for t in merge_lines:
        if t.startswith("R: ") or t.startswith("[REFERENCE]"):
            body = t[3:] if t.startswith("R: ") else t[len("[REFERENCE]"):]
            if _map_room_line(body):
                sections.append([])
        sections[-1].append(t)
    rooms, conns, defmat = {}, [], []
    for j in range(len(sections) + 1):
        if j > 0 and not sections[j - 1]:
            continue
        src = base_lines if j == 0 else sections[j - 1]
        rooms2, ref_found = {}, False
        for t in src:
            if t.startswith("Def_Mat:"):
                defmat.append(t)
            elif t.startswith("Connection:"):
                parts = t.split(":", 1)[1].strip().split(",")
                if len(parts) < 8:
                    continue
                a, b = parts[0], parts[1]
                conns = [c for c in conns if {c[0], c[1]} != {a, b}]
                conns.append((a, b, t))
            elif t.startswith("R: ") or t.startswith("[REFERENCE]"):
                body = t[3:] if t.startswith("R: ") else t[len("[REFERENCE]"):]
                p = _map_room_line(body)
                if p and p[0] in rooms:
                    ref_found = True
                    rooms2[p[0]] = p[1]
            elif t.startswith("I: ") or t.startswith("[IMAGE]"):
                continue
            else:
                p = _map_room_line(t)
                if p:
                    rooms2[p[0]] = p[1]
        if j == 0:
            rooms = rooms2
            continue
        if not ref_found and not any(n in rooms for n in rooms2):
            continue
        rooms.update(rooms2)
    return list(rooms.values()) + [c[2] for c in conns] + defmat


def apply_merges(rel, lines, merge_lines):
    """PendingApply.ApplyMerges dispatch by file name."""
    fn = os.path.basename(rel.lower())
    if (fn.endswith("default alignments.txt") or fn.endswith("sounds.txt") or
            fn.endswith("locks.txt") or fn.endswith("properties.txt") or
            ("properties-" in fn and fn.endswith(".txt"))):
        return merge_unique_items(lines, merge_lines, fn)
    if fn.endswith("mpmusic.txt") or fn.endswith("regions.txt") or fn.endswith("egates.txt"):
        return merge_unique_lines(lines, merge_lines)
    if "world_" in fn and fn.endswith(".txt"):
        return merge_world(lines, merge_lines)
    if fn.endswith("_settings.txt") or "_settingstemplate_" in fn or "_settings-" in fn:
        return merge_settings(lines, merge_lines)
    if "map_" in fn and "map_image_" not in fn and fn.endswith(".txt"):
        return merge_map(lines, merge_lines)
    return list(merge_lines)


def build_baseline_merged(base_mod_roots, vanilla_root, out_dir):
    """Bake the baseline mods' modify/world patches into `out_dir`/world.

    `base_mod_roots`: highest priority first (mapper's layer order). Per
    file (ExecutePendingMerge): start from the highest-priority whole file
    among those mods' world/ and vanilla (empty if none), apply every
    patch's [MERGE] block in load order, then every patch's line edits.
    Returns (out_dir, stats) where stats lists files and unsupported tags."""
    import shutil
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    patches = {}   # rel (lower, "world/...") -> [Patch] in load order
    for m in reversed(base_mod_roots):
        mod_dir = os.path.join(m, "modify")
        base = os.path.join(mod_dir, "world")
        if not os.path.isdir(base):
            continue
        for dp, _dn, fns in os.walk(base):
            for fn in fns:
                if not fn.lower().endswith(".txt"):
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, mod_dir).replace("\\", "/").lower()
                patches.setdefault(rel, []).append(Patch(_read(full)))
    unsupported = Counter()
    for rel, plist in patches.items():
        src = None
        for root in list(base_mod_roots) + [vanilla_root]:
            p = os.path.join(root, *rel.split("/"))
            if os.path.isfile(p):
                src = p
                break
        lines = _read(src) if src else []
        for p in plist:
            if p.merge_lines is not None:
                lines = _norm(apply_merges(rel, lines, p.merge_lines))
        for p in plist:
            lines = p.apply_modifications(lines)
            unsupported.update(p.unsupported)
        dst = os.path.join(out_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
    return out_dir, {"files": len(patches), "unsupported": dict(unsupported)}


# ---------------------------------------------------------------------------
# 3. Room fingerprints (copy detection)
# ---------------------------------------------------------------------------

def _tile_codes(geo):
    """(w, h, {(x, y): code}) with code = terrain*4 + vbeam*2 + hbeam."""
    w, h, tiles = geo
    return w, h, {(x, y): t * 4 + vb * 2 + hb for (x, y, t, vb, hb) in tiles}


def room_signature(geo):
    """(w, h, exact_hash, air_bitmap) for a parse_room_geometry result."""
    return signature_from_codes(*_tile_codes(geo))


def signature_from_codes(w, h, codes):
    exact = hashlib.md5(repr((w, h, sorted(codes.items()))).encode()).hexdigest()
    solid = Counter()
    for (x, y), c in codes.items():
        if c // 4 in (mapper.T_SOLID, mapper.T_SLOPE):
            solid[(x * SIG_GRID // max(w, 1), y * SIG_GRID // max(h, 1))] += 1
    mask, bit = 0, 0
    for gy in range(SIG_GRID):
        for gx in range(SIG_GRID):
            # cell area in tiles (cells cover uneven tile counts)
            x0, x1 = -(-gx * w // SIG_GRID), -(-(gx + 1) * w // SIG_GRID)
            y0, y1 = -(-gy * h // SIG_GRID), -(-(gy + 1) * h // SIG_GRID)
            area = max((x1 - x0) * (y1 - y0), 1)
            if solid[(gx, gy)] * 2 < area:
                mask |= 1 << bit
            bit += 1
    return w, h, exact, mask


def _jaccard_bits(a, b):
    u = (a | b).bit_count()
    return (a & b).bit_count() / u if u else 1.0


def build_room_index(world_dirs, cache_path=None):
    """Fingerprint every room file under `world_dirs` (*-rooms, gates,
    gate shelters). Cached per file by (mtime, size) in `cache_path`.
    Returns {"exact": {hash: name}, "rooms": [(name, region, w, h, mask)]}."""
    cache = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cache = pickle.load(f)
        except Exception:
            cache = {}
    new_cache, entries, exact = {}, [], {}
    for wd in world_dirs:
        if not os.path.isdir(wd):
            continue
        for sub in sorted(os.listdir(wd)):
            if not (sub.endswith("-rooms") or sub in ("gates", "gate shelters")):
                continue
            region = sub[:-6] if sub.endswith("-rooms") else sub
            d = os.path.join(wd, sub)
            for fn in sorted(os.listdir(d)):
                low = fn.lower()
                if not low.endswith(".txt") or "_settings" in low or low == "egates.txt":
                    continue
                p = os.path.join(d, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                key = (p, st.st_mtime_ns, st.st_size)
                sig = cache.get(key)
                if sig is None:
                    geo = mapper.parse_room_geometry(p)
                    sig = room_signature(geo) if geo else False
                new_cache[key] = sig
                if not sig:
                    continue
                name = fn[:-4].upper()
                w, h, ex, mask = sig
                exact.setdefault(ex, (name, region))
                entries.append((name, region, w, h, mask))
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(new_cache, f)
        except OSError:
            pass
    return {"exact": exact, "rooms": entries}


def match_room(sig, index):
    """('copy', name, region, 1.0) | ('derived', name, region, sim) | None."""
    w, h, ex, mask = sig
    hit = index["exact"].get(ex)
    if hit:
        return ("copy", hit[0], hit[1], 1.0)
    best = None
    for name, region, w2, h2, m2 in index["rooms"]:
        if abs(w - w2) > SIZE_TOLERANCE * max(w, w2) or abs(h - h2) > SIZE_TOLERANCE * max(h, h2):
            continue
        s = _jaccard_bits(mask, m2)
        if s >= DERIVED_MIN and (best is None or s > best[3]):
            best = ("derived", name, region, s)
    return best


# ---------------------------------------------------------------------------
# 2. Region snapshot + diff
# ---------------------------------------------------------------------------

def _timeline_match(spec, timeline):
    """WorldLoader.TimelineMatch (numbers 0/1/2 = White/Yellow/Red)."""
    neg = spec.startswith("X-")
    if neg:
        spec = spec[2:]
    nums = {"0": "white", "1": "yellow", "2": "red"}
    hit = any(nums.get(s.strip(), s.strip()).lower() == timeline.lower()
              for s in spec.split(","))
    return hit != neg


def _filter_line(s, timeline):
    """WorldLoader line preprocessing: (timeline) filter, {condition} kept."""
    if s.startswith("(") and ")" in s:
        if not _timeline_match(s[1:s.index(")")], timeline):
            return None
        s = s[s.index(")") + 1:]
    if s.startswith("{") and "}" in s:
        s = s[s.index("}") + 1:]
    return s


def parse_world(path, timeline):
    """Effective world_<r>.txt for `timeline`:
    rooms {NAME: (conns tuple, tag)}, creatures Counter {(ROOM, type): n}."""
    rooms, creatures, hidden, subs = {}, Counter(), set(), []
    sec = None
    for raw in _read(path):
        s = raw.strip()
        if s in ("CONDITIONAL LINKS", "ROOMS", "CREATURES", "BAT MIGRATION BLOCKAGES"):
            sec = s
            continue
        if s.startswith("END "):
            sec = None
            continue
        if not sec or not s or s.startswith("//"):
            continue
        if sec == "CONDITIONAL LINKS":
            f = [v.strip() for v in s.split(":")]
            if len(f) >= 3 and f[1] in ("HIDEROOM", "EXCLUSIVEROOM"):
                if _timeline_match(f[0], timeline) == (f[1] == "HIDEROOM"):
                    hidden.add(f[2].upper())
            elif len(f) >= 4 and _timeline_match(f[0], timeline):
                subs.append((f[1].upper(), f[2].upper(), f[3].upper()))
            continue
        s = _filter_line(s, timeline)
        if s is None:
            continue
        if sec == "ROOMS":
            f = [v.strip() for v in s.split(":")]
            conns = tuple(c.strip().upper() for c in (f[1] if len(f) > 1 else "").split(","))
            rooms[f[0].upper()] = (conns, f[2] if len(f) > 2 else "")
        elif sec == "CREATURES":
            sp = _Spawn("", s, False)
            room = sp.room.upper()
            if sp.lineages is not None:
                first = next((l.creature for l in sp.lineages if l.creature.upper() != "NONE"), "NONE")
                creatures[(room, "LINEAGE:" + first)] += 1
            else:
                for d in sp.dens:
                    creatures[(room, d.creature)] += max(d.qty, 1)
    for room, target, sub in subs:
        if room in rooms:
            conns, tag = rooms[room]
            rooms[room] = (tuple(sub if c == target else c for c in conns), tag)
    for h in hidden:
        rooms.pop(h, None)
    return rooms, creatures


def parse_placed_objects(path):
    """Counter of PlacedObjects types in a room settings file."""
    out = Counter()
    for ln in _read(path):
        if ln.startswith("PlacedObjects:"):
            for item in ln[len("PlacedObjects:"):].split(","):
                t = item.split("><")[0].strip()
                if t:
                    out[t] += 1
    return out


def _props(path):
    return {ln.strip() for ln in _read(path) if ln.strip() and not ln.startswith("//")}


def snapshot(res, region):
    """Everything the diff needs for one side, or None if the region is
    absent. Room files are referenced by path and parsed lazily."""
    timeline = res.chain[0] if res.chain else "White"
    world_p, _, _ = res.resolve_region_file(region, f"world_{region}.txt")
    map_p, _, _ = res.resolve_region_file(region, f"map_{region}.txt")
    if not world_p and not map_p:
        return None
    props_p, _, _ = res.resolve_region_file(region, "properties.txt")
    rooms, creatures = parse_world(world_p, timeline) if world_p else ({}, Counter())
    mpos = mapper.parse_map_file(map_p)[0] if map_p else {}
    if not rooms:
        rooms = {n: ((), "") for n in mpos}
    files = {}
    for n in rooms:
        geo_p, _ = res.room_file(region, n)
        set_p, _ = res.room_file(region, n + "_settings")
        files[n] = (geo_p, set_p)
    edges = set()
    for n, (conns, _tag) in rooms.items():
        for c in conns:
            if c and c != "DISCONNECTED":
                edges.add(frozenset((n, c)))
    return {"rooms": rooms, "creatures": creatures, "map": mpos, "files": files,
            "edges": edges, "props": _props(props_p) if props_p else set()}


_geo_cache = {}
_obj_cache = {}


def _geo(path):
    if path not in _geo_cache:
        g = mapper.parse_room_geometry(path) if path else None
        _geo_cache[path] = _tile_codes(g) if g else None
    return _geo_cache[path]


def _objs(path):
    if path not in _obj_cache:
        _obj_cache[path] = parse_placed_objects(path) if path else Counter()
    return _obj_cache[path]


def _geo_diff(a, b):
    """(changed_tiles, total_area) between two _tile_codes results."""
    (wa, ha, ca), (wb, hb, cb) = a, b
    union = max(wa, wb) * max(ha, hb)
    common_w, common_h = min(wa, wb), min(ha, hb)
    changed = union - common_w * common_h
    for k in set(ca) | set(cb):
        if k[0] < common_w and k[1] < common_h and ca.get(k, 0) != cb.get(k, 0):
            changed += 1
    return changed, union


def _multiset_dist(a, b):
    keys = set(a) | set(b)
    mx = sum(max(a.get(k, 0), b.get(k, 0)) for k in keys)
    if not mx:
        return 0.0
    return 1 - sum(min(a.get(k, 0), b.get(k, 0)) for k in keys) / mx


def _set_dist(a, b):
    u = len(a | b)
    return 1 - len(a & b) / u if u else 0.0


def _per_room(counter):
    out = {}
    for (room, t), n in counter.items():
        out.setdefault(room, Counter())[t] += n
    return out


def diff_region(mod_res, base_res, region, index=None):
    """Compare one region. Returns a dict:
    class ('same'|'tweaked'|'reworked'|'new'|'removed'), score, dims
    {name: 0..1}, rooms {NAME: {status, ...}}, removed [names], summary
    counts and new-room origins."""
    mod = snapshot(mod_res, region)
    base = snapshot(base_res, region)
    if mod is None and base is None:
        return None
    if mod is None:
        return {"region": region, "class": "removed", "score": 1.0, "dims": {},
                "rooms": {}, "removed": sorted(base["rooms"]), "counts": {},
                "origins": {}}
    empty = {"rooms": {}, "creatures": Counter(), "map": {}, "files": {},
             "edges": set(), "props": set()}
    is_new = base is None
    base = base or empty

    rooms = {}
    geo_changed = geo_total = 0
    mod_cr, base_cr = _per_room(mod["creatures"]), _per_room(base["creatures"])
    obj_mod, obj_base = Counter(), Counter()
    moved = both_mapped = 0
    origins = Counter()
    # A mod may shift a whole map; only moves relative to that shift count.
    shared = [n for n in mod["map"] if n in base["map"]]
    off_x = statistics.median(mod["map"][n][0] - base["map"][n][0] for n in shared) if shared else 0.0
    off_y = statistics.median(mod["map"][n][1] - base["map"][n][1] for n in shared) if shared else 0.0

    for n in sorted(set(mod["rooms"]) | set(base["rooms"])):
        in_m, in_b = n in mod["rooms"], n in base["rooms"]
        gm_p, sm_p = mod["files"].get(n, (None, None))
        gb_p, sb_p = base["files"].get(n, (None, None))
        om = _objs(sm_p) if in_m else Counter()
        ob = (om if sb_p == sm_p and in_m else _objs(sb_p)) if in_b else Counter()
        for t, c in om.items():
            obj_mod[(n, t)] += c
        for t, c in ob.items():
            obj_base[(n, t)] += c
        info = {}
        if in_m and not in_b:
            g = _geo(gm_p)
            if g:
                geo_changed += g[0] * g[1]
                geo_total += g[0] * g[1]
                info["size"] = [g[0], g[1]]
            status = "new"
            if g and index:
                hit = match_room(signature_from_codes(*g), index)
                if hit:
                    status = "new-copy" if hit[0] == "copy" else "new-derived"
                    info["from"] = hit[1]
                    info["from_region"] = hit[2]
                    info["similarity"] = round(hit[3], 3)
                    origins[hit[2]] += 1
                else:
                    origins["original"] += 1
            elif g:
                origins["original"] += 1
            info["status"] = status
        elif in_b and not in_m:
            g = _geo(gb_p)
            if g:
                geo_changed += g[0] * g[1]
                geo_total += g[0] * g[1]
                info["size"] = [g[0], g[1]]
            info["status"] = "removed"
        else:
            gfrac = 0.0
            if gm_p == gb_p:
                g = _geo(gm_p)
                if g:
                    geo_total += g[0] * g[1]
            else:
                ga, gb = _geo(gm_p), _geo(gb_p)
                if ga and gb:
                    ch, tot = _geo_diff(ga, gb)
                    geo_changed += ch
                    geo_total += tot
                    gfrac = ch / tot if tot else 0.0
            pm, pb = mod["map"].get(n), base["map"].get(n)
            is_moved = False
            if pm and pb:
                both_mapped += 1
                if (abs(pm[0] - pb[0] - off_x) > 0.5 or abs(pm[1] - pb[1] - off_y) > 0.5
                        or pm[2] != pb[2]):
                    moved += 1
                    is_moved = True
            cr_m, cr_b = mod_cr.get(n, Counter()), base_cr.get(n, Counter())
            content = []
            if cr_m != cr_b:
                content.append("creatures")
            if om != ob:
                content.append("objects")
            if mod["rooms"][n] != base["rooms"][n]:
                content.append("connections" if mod["rooms"][n][0] != base["rooms"][n][0] else "tag")
            if gfrac > 0:
                info["status"] = "geometry"
                info["geometry"] = round(gfrac, 4)
            elif is_moved:
                info["status"] = "moved"
            elif content:
                info["status"] = "content"
            else:
                info["status"] = "same"
            if content:
                info["content"] = content
            if cr_m != cr_b:
                d = {t: cr_m.get(t, 0) - cr_b.get(t, 0) for t in set(cr_m) | set(cr_b)}
                info["creatures"] = {t: v for t, v in sorted(d.items()) if v}
        rooms[n] = info

    dims = {
        "rooms": _set_dist(set(mod["rooms"]), set(base["rooms"])),
        "connections": _set_dist(mod["edges"], base["edges"]),
        "geometry": geo_changed / geo_total if geo_total else 0.0,
        "layout": moved / both_mapped if both_mapped else 0.0,
        "creatures": _multiset_dist(mod["creatures"], base["creatures"]),
        "objects": _multiset_dist(obj_mod, obj_base),
        "properties": _set_dist(mod["props"], base["props"]),
    }
    score = sum(WEIGHTS[k] * v for k, v in dims.items())
    if is_new:
        cls = "new"
    elif score == 0 and all(r["status"] == "same" for r in rooms.values()):
        cls = "same"
    elif score < TWEAKED_MAX:
        cls = "tweaked"
    else:
        cls = "reworked"
    counts = Counter(r["status"] for r in rooms.values())
    return {"region": region, "class": cls, "score": score, "dims": dims,
            "rooms": rooms, "removed": [n for n, r in rooms.items() if r["status"] == "removed"],
            "counts": dict(counts), "origins": dict(origins),
            "base_map": {n: base["map"][n] for n in rooms if rooms[n]["status"] == "removed"
                         and n in base["map"]},
            "base_size": {n: rooms[n].get("size") for n in rooms
                          if rooms[n]["status"] == "removed"}}


def room_tooltip(name, info):
    """Short hover text for an SVG room tint."""
    st = info["status"]
    parts = [f"{name}: {STATUS_STYLE.get(st, ('', st))[1]}"]
    if "from" in info:
        parts.append(f"from {info['from']} ({info['similarity']:.0%})")
    if "geometry" in info:
        parts.append(f"{info['geometry']:.0%} tiles")
    if info.get("content"):
        parts.append("changed: " + ", ".join(info["content"]))
    if info.get("creatures"):
        parts.append("creatures " + ", ".join(f"{t} {v:+d}" for t, v in info["creatures"].items()))
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

DIM_COLS = [("geometry", "GEO"), ("connections", "CONN"), ("creatures", "CREAT"),
            ("objects", "OBJ"), ("layout", "LAYOUT"), ("properties", "PROP")]


def format_report(diffs):
    """Console table + mod summary for one campaign's list of diff dicts."""
    diffs = [d for d in diffs if d]
    lines = []
    hdr = ["REGION", "CLASS", "SCORE", "ROOMS +new/-rem/~chg/=same"] + [c for _k, c in DIM_COLS] + ["NEW ROOM ORIGINS"]
    rows = []
    order = {"new": 0, "reworked": 1, "tweaked": 2, "removed": 3, "same": 4}
    for d in sorted(diffs, key=lambda d: (order.get(d["class"], 9), -d["score"], d["region"])):
        c = d["counts"]
        new = c.get("new", 0) + c.get("new-copy", 0) + c.get("new-derived", 0)
        chg = c.get("geometry", 0) + c.get("moved", 0) + c.get("content", 0)
        rooms = f"+{new}/-{c.get('removed', 0)}/~{chg}/={c.get('same', 0)}"
        dims = [f"{d['dims'].get(k, 0):.0%}" if d["dims"] else "-" for k, _c in DIM_COLS]
        org = ", ".join(f"{k}:{v}" for k, v in sorted(d["origins"].items(), key=lambda t: -t[1]))
        rows.append([d["region"], d["class"], f"{d['score']:.1%}", rooms] + dims + [org])
    widths = [max(len(str(r[i])) for r in rows + [hdr]) for i in range(len(hdr))]
    for r in [hdr] + rows:
        lines.append("  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)).rstrip())

    cls = Counter(d["class"] for d in diffs)
    tot = Counter()
    org = Counter()
    for d in diffs:
        tot.update(d["counts"])
        org.update(d["origins"])
    new_orig = org.get("original", 0)
    new_from = sum(v for k, v in org.items() if k != "original")
    modified = tot.get("geometry", 0) + tot.get("moved", 0) + tot.get("content", 0)
    all_rooms = sum(tot.values()) - tot.get("removed", 0)
    pct = lambda n: f"{n / all_rooms:.0%}" if all_rooms else "-"
    lines.append("")
    lines.append("summary: regions " + ", ".join(f"{k} {cls[k]}" for k in
                                                ("new", "reworked", "tweaked", "same", "removed") if cls[k]))
    lines.append(f"  rooms now: {all_rooms}  |  new original {new_orig} ({pct(new_orig)})"
                 f"  |  new from baseline rooms {new_from} ({pct(new_from)})"
                 f"  |  changed baseline {modified} ({pct(modified)})"
                 f"  |  untouched baseline {tot.get('same', 0)} ({pct(tot.get('same', 0))})"
                 f"  |  removed {tot.get('removed', 0)}")
    if new_from:
        src = ", ".join(f"{k}:{v}" for k, v in org.most_common() if k != "original")
        lines.append(f"  new rooms copied/derived from: {src}")
    return lines
