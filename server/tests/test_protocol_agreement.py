"""Protocol 4 agreement between the server (earthd.py) and the mod (core.lua, farfield.lua,
lattice.lua, follow.lua, ...), checked from both sides against one table (docs/V1_3_SPEC.md E over
docs/V1_2_DESIGN.md §7 as amended by §12; server/README.md documents the same table):

  - every message the mod builds (net.send constructors in core.lua) has exactly the table's
    fields, and every field the server reads exists in the table;
  - every message the server sends in a real session (in-process server, synthetic DEM: teleport,
    far field, terrain-follow, errors, a second anchor with the previous one's far_removes) has
    the table's required fields with the table's types and nothing else, and every field the mod
    reads (msg.X / tr.X in its Lua) is in the table;
  - the constants both sides must share (protocol/format, lattice, quantum, terrain-follow rule,
    tile grid, material name and file, paths), and the material loading order in the mod;
  - the tile layouts (docs/V1_4_SPEC.md §3.1 M1): FORMAT 3 (32 cells) and 4 (64 cells) on both
    sides, hello.cells and far_tile.tex with FORMAT 4 only, crossover.defaultErr (under luajit) and
    the exact level for both cell counts, the governor kind of every texture size;
  - the terrain block sizes (docs/V1_4_SPEC.md §3.5 M4): both follow tables (1024 and 512, with the
    fast mode) and the staleness distances on both sides, hello.blockSize with --block 512 only,
    terrain.size the session's, the mod's layer / flat maps per size, setBlockSize first in hello;
  - the horizon (docs/V1_4_SPEC.md §3.6 M1h): hello.horizon and status.radius with --horizon only, the
    mod's visibleDistance margin and the sky's planet radius against the server's (horizon.lua), the
    mod reacting to both (core.lua);
  - protocol 4 (docs/V1_3_SPEC.md E): hello.nearVisual values; a textured far_tile's material
    name (bngEarthSat_<anchorId>_<key, '-' as 'm'>) is the one inside its .cdae (the mod's
    Material maps onto it by mapTo), its texture is a DXT1 DDS with a full mip chain next to the
    shape (same stem), UV0 spans the footprint with v flipped as the engine stores it (row 0 of
    the texture = north), UV1 is world-locked (x, y) / DETAIL_M (the mod's detail map runs on UV1
    at detailScale 1, so one repeat per DETAIL_M), and the mod's detail texture is a stock asset;
  - real weather (server/weather.py, docs/research/2026-10-10-real-weather.md): the 'weather'
    message (additive, no protocol bump: both sides ignore unknown types), sent in the session by an
    injected weather service; its nested cover / wind / aloft fields; the mod reads it in weather.lua;
  - real gravity (server/gravity.py Provider / Tracker): the 'gravity' message (additive the same
    way), sent in the session by a provider on the degree-36 EGM2008 fixture; weather.lua reads it.
"""
import json
import math
import os
import struct
import re
import shutil
import subprocess
import zipfile
import sys
import types
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdae  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import heightmap  # noqa: E402
import imagery  # noqa: E402
import modsim  # noqa: E402
import test_server  # noqa: E402
import colormatch as colormatchmod  # noqa: E402
import deshadow as deshadowmod  # noqa: E402
import gravity as gravitymod  # noqa: E402
import test_weather  # noqa: E402
import weather  # noqa: E402

NUM = (int, float)
OPT = "optional"            # may be absent
NULL = "nullable"           # may be JSON null


def F(types, *flags):
    return (types if isinstance(types, tuple) else (types,), set(flags))


# message type -> {field: F(types, flags)}; "type" itself is implied.
MOD_TO_SERVER = {
    "hello": {"protocol": F(int), "format": F(int), "modVersion": F(str)},
    "prepare_anchor": {"requestId": F(int), "lat": F(NUM), "lon": F(NUM), "cx": F(NUM, OPT), "cy": F(NUM, OPT)},
    "commit_anchor": {"anchorId": F(int)},
    "prepare_terrain": {"requestId": F(int), "anchorId": F(int), "cx": F(NUM), "cy": F(NUM)},
    "commit_terrain": {"anchorId": F(int), "cx": F(NUM), "cy": F(NUM)},
    "pose": {"anchorId": F(int), "x": F(NUM), "y": F(NUM), "z": F(NUM), "vx": F(NUM), "vy": F(NUM), "lat": F(NUM),
             "lon": F(NUM), "speed": F(NUM), "cam": F(dict, OPT),
             # previous anchors whose tile objects the mod still has (an empty Lua table may encode as {})
             "held": F((list, dict), OPT)},
}
CAM = {"x", "y", "z", "fx", "fy", "fz", "fovDeg"}
TERRAIN = {"cx": F(NUM), "cy": F(NUM), "heightmap": F(str), "size": F(int), "squareSize": F(NUM),
           "zBase": F(NUM), "maxHeight": F(NUM), "q": F(NUM)}
SERVER_TO_MOD = {
    "hello": {"protocol": F(int), "format": F(int), "serverVersion": F(str), "sessionId": F(str),
              "farFormat": F(str), "material": F(str),
              "nearVisual": F(str), "attribution": F(str),          # protocol 4
              "cells": F(int, OPT),             # v1.4 M1: cells per tile edge, FORMAT 4 only
              "blockSize": F(int, OPT),         # v1.4 M4: terrain block size, only when not 1024 (--block 512)
              "horizon": F(bool, OPT),          # v1.4 M1h: true with --horizon only
              "deshadow": F(str, OPT),          # --deshadow lambert|full only (the settings page shows it)
              "colormatch": F(str, OPT)},       # --colormatch s2 only (likewise)
    "status": {"text": F(str),
               "backlog": F(int),           # v1.4 M0: desired - shown far tiles of the current anchor
               "radius": F(NUM, OPT)},      # v1.4 M1h: the far-field radius (m), --horizon only
    "anchor_ready": {"requestId": F(int), "anchorId": F(int), "lat": F(NUM), "lon": F(NUM),
                     "groundZ": F(NUM, NULL), "terrain": F(dict),
                     "anchorKey": F(str, OPT)},            # phase 5 (lattice anchors): not sent yet
    "anchor_error": {"requestId": F(int, NULL), "error": F(str)},
    "terrain_ready": {"requestId": F(int), "anchorId": F(int), **TERRAIN},
    "terrain_error": {"requestId": F(int, NULL), "anchorId": F(int, NULL), "error": F(str)},
    "far_tile": {"anchorId": F(int), "key": F(str), "level": F(int), "shape": F(str), "pos": F(list),
                 "err": F(NUM), "skirt": F(NUM), "sink": F(NUM, OPT), "state": F(str),   # sink: exactly on L0
                 # protocol 4, satellite imagery: the tile's own material (all three, or none)
                 "material": F(str, OPT), "texture": F(str, OPT), "detail": F(bool, OPT),
                 "tex": F(int, OPT)},           # v1.4 M1: texture px (FORMAT 4, with the material)
    "far_swap": {"anchorId": F(int), "id": F(int), "add": F(list), "remove": F(list), "dIn": F(NUM),
                 "dOut": F(NUM)},
    "far_remove": {"anchorId": F(int), "key": F(str)},
    "far_done": {"anchorId": F(int), "count": F(int)},
    # real weather at the player (weather.py; additive, protocol 4)
    "weather": {"v": F(int), "t": F(int), "lat": F(NUM), "lon": F(NUM), "code": F(int), "isDay": F(bool),
                "cover": F(dict), "baseM": F(NUM), "baseAglM": F(NUM), "rainMmH": F(NUM), "snowCmH": F(NUM),
                "visM": F(NUM, NULL), "fog": F(bool), "thunder": F(bool), "wind": F(dict),
                "aloft": F(dict, OPT), "tempC": F(NUM), "dewC": F(NUM), "elevM": F(NUM), "slTempC": F(NUM),
                "pressureHpa": F(NUM), "utcOffsetS": F(int, OPT), "tz": F(str, OPT), "credit": F(str)},
    # real gravity at the player (gravity.py; additive, protocol 4): |g| in m/s^2 at (lat, lon, h m MSL)
    "gravity": {"v": F(int), "g": F(NUM), "lat": F(NUM), "lon": F(NUM), "h": F(NUM), "source": F(str)},
}
WEATHER_NESTED = {"cover": {"total", "low", "mid", "high"}, "wind": {"ms", "fromDeg", "gustMs"},
                  "aloft": {"ms", "fromDeg"}}
# events the mod's net.lua makes up itself (not on the wire)
MOD_INTERNAL = {"_connected": set(), "_disconnected": {"error"}}

LUA = modsim.MOD_DIR
GRAVITY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "egm2008_n36.npz"     # degree 36 (test_gravity)
SHAPE_RE = r"^/temp/beamngEarth/[^/]+/far_\d+_-?\d+_-?\d+_-?\d+\.cdae$"     # levels down to -2 (mesh mode)
HM_RE = r"^/temp/beamngEarth/[^/]+/hm_\d+_-?\d+_-?\d+\.png$"


def lua_tables(text, start):
    """Text of the Lua table constructor that opens at text[start] == '{' (balanced braces)."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unbalanced table")


def top_keys(table):
    """Keys `name = ...` of a constructor, and of the constructors nested one level down
    ({outer key: keys})."""
    keys, nested, depth, last = set(), {}, 0, None
    for m in re.finditer(r"\{|\}|(\w+)\s*=(?!=)", table):
        tok = m.group(0)
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        elif depth == 1:
            keys.add(m.group(1))
            last = m.group(1)
        elif depth == 2 and last:
            nested.setdefault(last, set()).add(m.group(1))
    return keys, nested


def mod_sends():
    """{type: (keys, nested)} of every net.send({...}) in core.lua."""
    text = (LUA / "core.lua").read_text()
    out = {}
    for m in re.finditer(r"net\.send\(\{", text):
        t = lua_tables(text, m.end() - 1)
        keys, nested = top_keys(t)
        kind = re.search(r"type\s*=\s*'(\w+)'", t).group(1)
        out.setdefault(kind, []).append((keys - {"type"}, nested))
    return out


def mod_reads():
    """Fields the mod reads from server messages: msg.X / p.msg.X / tr.X (terrain) in its Lua."""
    out = set()
    for name in ("core.lua", "farfield.lua", "lattice.lua", "weather.lua"):
        text = (LUA / name).read_text()
        out |= set(re.findall(r"\bmsg\.(\w+)", text))
        out |= {"terrain." + f for f in re.findall(r"\btr\.(\w+)", text)}
    return out


def server_reads():
    text = (Path(earthd.__file__)).read_text()
    out = set(re.findall(r"msg\.get\(\"(\w+)\"", text)) | set(re.findall(r"msg\[\"(\w+)\"\]", text))
    out |= set(re.findall(r"_num\(msg, \"(\w+)\"", text))
    return out


def lua_function(text, name):
    """Body of `local function name(` / `function M.name(` up to the next top-level `end`."""
    m = re.search(rf"^(?:local )?function (?:M\.)?{name}\(.*?^end", text, re.S | re.M)
    return m.group(0) if m else ""


class AgreementTableTest(unittest.TestCase):
    def test_mod_sends_match_table(self):
        sends = mod_sends()
        self.assertEqual(set(sends), set(MOD_TO_SERVER), "message types the mod sends")
        for kind, variants in sends.items():
            want = MOD_TO_SERVER[kind]
            required = {f for f, (_, flags) in want.items() if OPT not in flags}
            for keys, nested in variants:
                self.assertLessEqual(required, keys, (kind, keys))
                self.assertLessEqual(keys, set(want), (kind, keys))
                if "cam" in nested:
                    self.assertEqual(nested["cam"], CAM)

    def test_server_reads_are_in_table(self):
        fields = {f for spec in MOD_TO_SERVER.values() for f in spec} | {"type"}
        self.assertLessEqual(server_reads(), fields)

    def test_mod_reads_are_in_table(self):
        sent = {f for spec in SERVER_TO_MOD.values() for f in spec} | {"type"}
        sent |= {f for spec in MOD_INTERNAL.values() for f in spec}
        sent |= {"terrain." + f for f in TERRAIN}
        unknown = mod_reads() - sent
        self.assertFalse(unknown, f"the mod reads fields no server message has: {sorted(unknown)}")


class HorizonAgreementTest(unittest.TestCase):
    def test_horizon_constants_and_mod(self):
        """M1h (docs/V1_4_SPEC.md §3.6): the sky's planet radius is the tiles' sphere, the mod's
        visibleDistance margin is the server's (radius + 10 %), and core.lua reads hello.horizon first
        thing after the block size and status.radius on every status; horizon.lua re-applies
        LevelInfo with postApply (in-game: without it the engine keeps the level's value)."""
        H = modsim.lua_consts("horizon.lua")
        self.assertEqual(H["PLANET_RADIUS_KM"] * 1000.0, ff.R_EARTH)
        self.assertEqual(H["VISIBLE_MARGIN"], ff.VISIBLE_MARGIN)
        core = (LUA / "core.lua").read_text()
        on_msg = lua_function(core, "onMessage")
        hello = on_msg[on_msg.index("t == 'hello'"):on_msg.index("t == 'status'")]
        self.assertIn("M.setHorizon(msg.horizon == true)", hello)
        status = on_msg[on_msg.index("t == 'status'"):on_msg.index("t == 'anchor_ready'")]
        self.assertIn("M.setFarRadius(msg.radius)", status)
        text = (LUA / "horizon.lua").read_text()
        setter = lua_function(text, "setVisibleDistance")
        self.assertLess(setter.index("li.visibleDistance = v"), setter.index("li:postApply()"))
        self.assertIn("s.atmoPlanetRadius = M.PLANET_RADIUS_KM", lua_function(text, "applySky"))
        self.assertIn("horizon.restoreSky()", lua_function(core, "onClientEndMission"))
        self.assertIn("horizon.restoreSky()", lua_function(core, "onExtensionUnloaded"))


class ConstantsTest(unittest.TestCase):
    def test_protocol_and_format(self):
        """Protocol 4; the mod reads FORMAT 3 and 4 and says the newest in its hello; the server's
        format is its layout's (32 cells: 3, the default; 64: 4)."""
        self.assertEqual(modsim.CORE["PROTOCOL"], earthd.PROTOCOL)
        self.assertEqual(modsim.CORE["FORMAT"], ff.Layout(64).format)
        self.assertEqual(modsim.CORE["FORMAT"], max(modsim.CORE["FORMATS"]))
        self.assertEqual(set(modsim.CORE["FORMATS"]), set(ff.CONTENT_FORMAT.values()))
        self.assertEqual((ff.V131.format, ff.Layout(64).format), (3, 4))
        self.assertEqual(ff.Layout(earthd.CELLS_DEFAULT).format, 3)            # default: v1.3.1 until P8
        core = (LUA / "core.lua").read_text()
        on_msg = lua_function(core, "onMessage")
        self.assertIn("crossover.setCells(msg.cells)", on_msg)
        self.assertIn("farfield.setExactLevel(farfield.exactLevelFor(cells))", on_msg)
        self.assertIn("not formatKnown(msg.format)", on_msg)

    def test_lattice_and_quantum(self):
        L = modsim.LATTICE
        self.assertEqual((L["N"], L["SQUARE"], L["HALF"], L["GRID"]),
                         (heightmap.SIZE, heightmap.SQUARE, heightmap.HALF, heightmap.GRID))
        self.assertEqual(L["VALID_W"], heightmap.VALID)                     # 2046 m (§12 P1)
        self.assertEqual(tuple(L["Q_LADDER"]), heightmap.Q_LADDER)
        for q in heightmap.Q_LADDER:
            self.assertEqual(heightmap.block_params(1000.0, 1100.0, q)[2], q * L["Q_SCALE"])
        self.assertEqual(L["L0"], ff.BASE)
        self.assertEqual(2 * L["HALF"], 8 * ff.BASE)                        # a block sits on 8 x 8 L0 tiles
        # M4: lattice.configure(size) against the server's per-size geometry
        for size in ff.BLOCK_SIZES:
            t = modsim.lattice_table(size)
            self.assertEqual((t["N"], t["HALF"], t["VALID_W"], t["GRID"]),
                             (size, heightmap.half_extent(size), heightmap.valid(size), heightmap.grid(size)))
            self.assertEqual(ff.Layout(64, size).grid, t["GRID"])
            # the SW corner of a block on its grid is an even lattice index (Torque diagonal parity)
            self.assertEqual((t["GRID"] - t["HALF"]) / heightmap.SQUARE % 2, 0)
        self.assertEqual(modsim.XO["CELLS"], ff.V131.cells)             # without hello.cells (FORMAT 3)
        self.assertLess(modsim.FAR["EDGE_BAND"], ff.BASE)                   # the edge ring is one L0 tile deep

    def test_exact_level(self):
        """The mod's exactMiss metric (farfield.lua, V1_4_SPEC 3.0 M0): the exact level is the finest
        mesh level, whose vertices are the physics lattice, and the lookup reaches the coarsest
        level. (M1: the 64-cell layout makes L-1 exact; the mod's setExactLevel(-1) from hello.cells.)"""
        self.assertEqual(modsim.FAR["EXACT_LEVEL"], ff.MESH_MIN_LEVEL[32])
        self.assertEqual(modsim.FAR["MAX_LEVEL"], ff.MAX_LEVEL)
        for cells in ff.CELL_COUNTS:
            lay = ff.Layout(cells)
            self.assertEqual(ff.level_size(lay.exact_level), cells * heightmap.SQUARE)
            self.assertEqual(modsim.exact_level_for(cells), lay.exact_level)      # farfield.exactLevelFor
            self.assertEqual(lay.exact_level, lay.min_level)
        far = (LUA / "farfield.lua").read_text()
        # the lookup key is the server's tile key (Tile.key: L_i_j, i = floor(x / S))
        self.assertIn("local k = l .. '_' .. floor(x / s) .. '_' .. floor(y / s)", lua_function(far, "shownAt"))
        self.assertEqual(ff.Tile(-2, 3, -1).key, "-2_3_-1")
        core = (LUA / "core.lua").read_text()
        self.assertIn("farfield.update(gov, (gov.w1 or dtReal * 1000) / 1000, st.cam, st.player)", core)

    @unittest.skipIf(shutil.which("luajit") is None, "luajit not installed")
    def test_default_err_and_exact_level_in_lua(self):
        """crossover.defaultErr after setCells (from hello.cells) and farfield.exactLevelFor agree
        with the server's layout for both cell counts: a quarter of the layout's vertex spacing,
        and the level on the physics lattice."""
        lua = (f"package.path = '{modsim.MOD_DIR.parent}/?.lua;' .. package.path\n"
               f"dofile('{modsim.MOD_ROOT.parent / 'tests' / 'stubs.lua'}')\n"
               "local X = require('beamngEarth/crossover')\n"
               "local F = require('beamngEarth/farfield')\n"
               "for _, c in ipairs({ 32, 64 }) do\n"
               "  X.setCells(c)\n"
               "  for l = -2, 9 do print(c, l, string.format('%.9f', X.defaultErr(l))) end\n"
               "  print(c, 'exact', F.exactLevelFor(c))\n"
               "end\n"
               "X.setCells(nil)\n"
               "print(0, 0, string.format('%.9f', X.defaultErr(0)))\n")
        out = subprocess.run(["luajit", "-e", lua], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = [ln.split("\t") for ln in out.stdout.splitlines()]
        for c, lvl, v in rows:
            c = int(c)
            if lvl == "exact":
                self.assertEqual(int(v), ff.Layout(c).exact_level)
            elif c == 0:                            # no hello.cells: the FORMAT 3 layout
                self.assertAlmostEqual(float(v), ff.V131.spacing(0) / 4, places=9)
            else:
                lay = ff.Layout(c)
                self.assertAlmostEqual(float(v), lay.spacing(int(lvl)) / 4, places=9)
                self.assertAlmostEqual(float(v), modsim.default_err(int(lvl), c), places=9)
        self.assertEqual(len(rows), 2 * 13 + 1)

    def test_terrain_follow_rule(self):
        F = modsim.FOLLOW
        self.assertEqual((F["PREPARE_DIST"], F["LOOKAHEAD_S"], F["REACH"]),
                         (earthd.FOLLOW_PREPARE, earthd.FOLLOW_LEAD, earthd.FOLLOW_REACH))
        # the player stays >= EDGE_MARGIN inside the collidable [cx-1024, cx+1022] on both sides
        self.assertLessEqual(F["REACH"], L_VALID_EDGE - F["EDGE_MARGIN"])
        self.assertLessEqual(earthd.ANCHOR_CENTRE_MAX, modsim.REBASE["MAX_DISTANCE"] + 5000)

    def test_follow_tables_per_block_size(self):
        """M4 (docs/V1_4_SPEC.md §3.5): the server's predicted follow rule per block size is the mod's
        table (flat *_1024 / *_512 constants) on both sides, including the 512 fast mode; each table
        keeps the player EDGE_MARGIN inside the collidable edge; the default stays 1024."""
        F = modsim.FOLLOW
        self.assertEqual(set(earthd.FOLLOW_RULES), set(ff.BLOCK_SIZES))
        self.assertEqual(earthd.BLOCK_DEFAULT, heightmap.SIZE)
        self.assertEqual(earthd.FOLLOW_RULES[1024], earthd.FollowRule(256.0, earthd.FOLLOW_PREPARE, earthd.FOLLOW_LEAD,
                                                                     earthd.FOLLOW_REACH, earthd.PREFETCH_START))
        for size, rule in earthd.FOLLOW_RULES.items():
            T = modsim.follow_table(size)
            self.assertEqual((rule.grid, rule.prepare, rule.lead, rule.reach),
                             (T["GRID"], T["PREPARE_DIST"], T["LOOKAHEAD_S"], T["REACH"]), size)
            self.assertEqual(rule.grid, heightmap.grid(size))
            edge = heightmap.valid(size) - heightmap.half_extent(size)            # +1022 / +510 m
            self.assertLessEqual(T["REACH"], edge - T["EDGE_MARGIN"])
            self.assertEqual(rule.fast_v, math.inf if T["FAST_MODE_V"] >= 1e9 else T["FAST_MODE_V"])
            self.assertLess(rule.prefetch_start, rule.prepare)
            self.assertLessEqual(T["PREPARE_DIST"], T["COMMIT_DIST"])
            self.assertLess(T["COMMIT_DIST"], T["HARD_COMMIT_DIST"])
        fast = earthd.FOLLOW_RULES[512]
        self.assertEqual((fast.fast_v, fast.fast_lead, fast.fast_reach, fast.fast_ahead),
                         (F["FAST_V"], F["LOOKAHEAD_FAST_S"], F["REACH_FAST"], F["PREP_AHEAD_FAST_S"]))
        self.assertLessEqual(F["REACH_FAST"], 510 - F["EDGE_MARGIN_FAST"])
        self.assertEqual(earthd.FOLLOW_RULES[512].prefetch_start, earthd.FOLLOW_RULES[512].prepare - 28)
        # the predicted request is the mod's target at the prepare point (both modes), 512 table
        for vx, vy, px, py in ((30.0, 0.0, 129.0, 0.0), (134.0, 0.0, 129.0, 0.0), (200.0, 0.0, 0.0, 0.0),
                               (-90.0, 60.0, -140.0, 40.0), (170.0, 120.0, 10.0, -20.0)):
            want = modsim.follow_decide(px, py, vx, vy, 0.0, 0.0, None, size=512)
            self.assertEqual(want[0], "prepare", (vx, vy))
            self.assertEqual(earthd.predict_centre(px, py, vx, vy, *fast.mode(vx, vy)[:2], fast.grid), want[1:])

    def test_stale_dist_per_block_size(self):
        """rebase.lua STALE_DIST per block size equals that table's commit distance (a rebase never
        commits a block the follow rule would already replace), and the stale-anchor rule is 512² only."""
        for size in ff.BLOCK_SIZES:
            self.assertEqual(modsim.rebase_table(size)["STALE_DIST"], modsim.follow_table(size)["COMMIT_DIST"])
        self.assertEqual(modsim.REBASE["STALE_DIST"], modsim.REBASE["STALE_DIST_1024"])      # the default
        self.assertEqual((modsim.rebase_table(1024)["KEEP_EDGE"], modsim.rebase_table(512)["KEEP_EDGE"]), (0, 150))

    def test_block_size_in_the_mod(self):
        """core.lua sets the block size from hello.blockSize before anything reads the lattice, every
        module's configure is called, and the mod ships the layer / flat maps of each size."""
        core = (LUA / "core.lua").read_text()
        on_msg = lua_function(core, "onMessage")
        hello = on_msg[on_msg.index("t == 'hello'"):]
        self.assertLess(hello.index("M.setBlockSize(msg.blockSize or 1024)"), hello.index("requestAnchor("))
        setter = lua_function(core, "setBlockSize")
        for call in ("lattice.configure(", "follow.configure(n)", "terrain.configure(n)", "rebase.configure(n)"):
            self.assertIn(call, setter)
        terrain = (LUA / "terrain.lua").read_text()
        for size in ff.BLOCK_SIZES:
            for kind, mode in (("layer", "RGB"), ("flat", "I;16")):
                name = f"beamngEarth_{kind}_{size}.png"
                self.assertIn(f"[{size}] = TERRAIN_DIR .. '{name}'", terrain)
                with Image.open(modsim.MOD_ROOT / "levels" / "beamng_earth" / "art" / "terrains" / name) as im:
                    self.assertEqual((im.size, im.mode), ((size, size), mode))

    def test_material(self):
        name = modsim.lua_string("farfield.lua", "MATERIAL")
        self.assertEqual(name, ff.MATERIAL)
        vfs = modsim.lua_string("farfield.lua", "MATERIALS_FILE")
        self.assertEqual(vfs, "/" + "/".join(ff.MOD_SHAPES) + "/main.materials.json")
        local = modsim.MOD_ROOT / vfs.lstrip("/")
        mats = json.loads(local.read_text())
        self.assertEqual(mats[name]["mapTo"], name)                        # P2: mapTo overrides the shape's own
        self.assertEqual(mats[name]["class"], "Material")
        palette = mats[name]["Stages"][0]["baseColorMap"]
        self.assertTrue((modsim.MOD_ROOT / palette.lstrip("/")).is_file(), palette)
        # the level loader picks up every *materials.json under the level folder, so the file must
        # live inside levels/beamng_earth
        self.assertTrue(vfs.startswith("/levels/beamng_earth/"))

    def test_material_loaded_before_first_tile(self):
        text = (LUA / "farfield.lua").read_text()
        spawn = lua_function(text, "spawn")
        self.assertLess(spawn.index("ensureMaterial()"), spawn.index("createObject('TSStatic')"))
        self.assertIn("ensureMaterial()", lua_function(text, "switchAnchor"))
        ensure = lua_function(text, "ensureMaterial")
        self.assertIn("scenetree.findObject(M.MATERIAL)", ensure)          # loaded by the level already?
        self.assertIn("loadJsonMaterialsFile(M.MATERIALS_FILE)", ensure)
        terrain = (LUA / "terrain.lua").read_text()
        create = lua_function(terrain, "getOrCreate")
        self.assertLess(create.index("ensureMaterials()"), create.index("TerrainBlock()"))

    def test_near_visual_values(self):
        """hello.nearVisual: the mod draws the ground with tiles for 'mesh' and with the
        TerrainBlock for anything else (an older server sends none: the v1.2 look)."""
        self.assertEqual(set(earthd.NEAR_VISUALS), {"mesh", "terrain"})
        core = (LUA / "core.lua").read_text()
        self.assertIn("nearVisual = (mode == 'mesh') and 'mesh' or 'terrain'", lua_function(core, "setNearVisual"))
        far = (LUA / "farfield.lua").read_text()
        self.assertIn("mode = (mode == 'mesh') and 'mesh' or 'terrain'", lua_function(far, "setNearVisual"))

    def test_imagery_material(self):
        """satmat.lua builds the Material the way the server's shapes and textures need: mapTo =
        the far_tile's material (the name in the shape), colorMap = its texture, the detail map on
        UV1 at detailScale 0.5 (UV1 = metres / DETAIL_M, so one repeat per 2 * DETAIL_M); the
        detail texture ships with the mod (tools/make_detail_texture.py: mean 0.5 grey, fading
        mips)."""
        text = (LUA / "satmat.lua").read_text()
        create = lua_function(text, "create")
        self.assertIn("m:setField('mapTo', 0, name)", create)
        self.assertIn("m:setField('colorMap', 0, texture)", create)
        self.assertIn("m:registerObject(name)", create)
        self.assertIn("m:setField('detailMap', 0, M.DETAIL_MAP)", create)
        self.assertRegex(text, r"\{ 'detailMapUseUV', '1' \}")
        self.assertRegex(text, r"\{ 'detailScale', '0.5 0.5' \}")
        # farfield passes the far_tile's fields straight through
        far = (LUA / "farfield.lua").read_text()
        self.assertIn("satmat.create, inst.material, inst.texture, inst.detail", far)
        detail = modsim.lua_string("satmat.lua", "DETAIL_MAP")
        mod_file = modsim.MOD_ROOT / detail.lstrip("/")      # mod root = the VFS root
        self.assertTrue(mod_file.is_file(), mod_file)
        data = mod_file.read_bytes()
        self.assertEqual(data[:4], b"DDS ")
        self.assertGreater(struct.unpack_from("<I", data, 28)[0], 1)   # has mips (the fade)

    def test_shape_kind_from_extension(self):
        text = (LUA / "farfield.lua").read_text()
        self.assertIn("%.cdae$", lua_function(text, "kindOf"))
        self.assertLessEqual({"cdae", "dae"}, set(modsim.GOV["PRIOR"]))
        self.assertEqual(set(ff.FORMATS), {"cdae", "dae"})

    def test_texture_kinds(self):
        """M1: far_tile.tex picks the governor kind; every texture size a layout uses by default has
        its own prior (cdaeTex128/256/512, measured in-game by P8: the shape dominates, so the priors
        are close and grow with the texture), and a FORMAT 3 tile (no tex) stays 'cdaeTex'."""
        far = (LUA / "farfield.lua").read_text()
        self.assertIn("inst.material and governor.texKind(inst.tex) or 'cdae'", lua_function(far, "kindOf"))
        self.assertIn("tex = tonumber(msg.tex)", far)
        prior = modsim.GOV["PRIOR"]
        for px in sorted({p for t in ff.TEX_DEFAULT.values() for p in t} | {128}):
            self.assertEqual(modsim.tex_kind(px), f"cdaeTex{px}")
            self.assertGreater(prior[f"cdaeTex{px}"], 0)
        self.assertLess(prior["cdaeTex128"], prior["cdaeTex256"])
        self.assertLess(prior["cdaeTex256"], prior["cdaeTex512"])
        self.assertEqual(modsim.tex_kind(None), "cdaeTex")


L_VALID_EDGE = modsim.LATTICE["VALID_W"] - modsim.LATTICE["HALF"]      # +1022 m from the centre


class ServerMessagesTest(test_server.ServerHarness):
    """Everything the server sends in a session, checked against SERVER_TO_MOD."""

    def setUp(self):
        super().setUp()
        self.server.weather = weather.Service(fetch=test_weather.FakeFetch())     # no network
        self.server.gravity = gravitymod.Provider(model=gravitymod.GravityModel.from_npz(GRAVITY_FIXTURE),
                                                  download=False)               # no network either

    def check(self, m):
        kind = m["type"]
        self.assertIn(kind, SERVER_TO_MOD, m)
        spec = SERVER_TO_MOD[kind]
        fields = set(m) - {"type"}
        self.assertLessEqual(fields, set(spec), m)
        for f, (types, flags) in spec.items():
            if f not in m:
                self.assertTrue(OPT in flags, (f, m))
                continue
            if m[f] is None:
                self.assertIn(NULL, flags, (f, m))
                continue
            self.assertIsInstance(m[f], types, (f, m))
            if isinstance(m[f], float):
                self.assertTrue(math.isfinite(m[f]), (f, m))
        if kind == "far_tile":
            self.assertRegex(m["shape"], SHAPE_RE)
            self.assertIn(m["state"], ("group", "prefetch"))
            self.assertEqual(len(m["pos"]), 3)
            # sink: on L0 in terrain near-visual mode only (mesh mode covers nothing)
            self.assertEqual(m["level"] == 0 and self.near_visual == "terrain", "sink" in m, m)
            # imagery: every tile carries its material, unless imagery is off
            self.assertEqual("material" in m, self.with_imagery, m)
            # M1: the texture size with FORMAT 4 (the governor kind), never with FORMAT 3
            self.assertEqual("tex" in m, self.with_imagery and self.cells == 64, m)
            if "tex" in m:
                self.assertEqual(m["tex"], self.server.tex_size(m["level"]))
                self.assertGreaterEqual(m["level"], ff.MESH_MIN_LEVEL[64])
            self.assertEqual(m["key"], ff.tile_of(m["key"]).key)
        if kind in ("anchor_ready", "terrain_ready"):
            t = m["terrain"] if kind == "anchor_ready" else {f: m[f] for f in TERRAIN}
            self.assertEqual(set(t), set(TERRAIN))
            for f, (types, _) in TERRAIN.items():
                self.assertIsInstance(t[f], types, (f, t))
            self.assertRegex(t["heightmap"], HM_RE)
            # M4: the session's block size (hello.blockSize), on its grid
            self.assertEqual((t["size"], t["squareSize"]), (self.server.layout.block, modsim.LATTICE["SQUARE"]))
            grid = modsim.lattice_table(t["size"])["GRID"]
            self.assertEqual((t["cx"] % grid, t["cy"] % grid), (0, 0))
            self.assertEqual(t["maxHeight"], t["q"] * modsim.LATTICE["Q_SCALE"])
            self.assertEqual(t["zBase"] % t["q"], 0)
        if kind == "far_swap":
            self.assertTrue(all(isinstance(k, str) for k in m["add"] + m["remove"]))
        if kind == "status":                    # M1h: the radius with --horizon only, at least 80 km
            self.assertEqual("radius" in m, self.horizon, m)
            if self.horizon:
                self.assertTrue(ff.HORIZON_MIN <= m["radius"] <= ff.HORIZON_MAX, m)
        if kind == "far_tile":
            self.assertLessEqual(m["level"], ff.HORIZON_MAX_LEVEL if self.horizon else ff.MAX_LEVEL)
        if kind == "weather":
            for k, fields in WEATHER_NESTED.items():
                if k in m:
                    self.assertEqual(set(m[k]), fields, (k, m))
                    self.assertTrue(all(isinstance(v, NUM) for v in m[k].values()), (k, m))
            self.assertEqual((m["v"], m["credit"]), (weather.VERSION, weather.CREDIT))
        if kind == "gravity":
            self.assertEqual((m["v"], m["source"]), (gravitymod.MSG_VERSION, "egm2008"))
            self.assertTrue(9.75 < m["g"] < 9.85, m)
        if kind == "far_tile" and "material" in m:
            self.assertTrue("texture" in m and "detail" in m, m)
            self.assertRegex(m["texture"], r"^/temp/beamngEarth/[^/]+/[^/]+\.dds$")
            # the name the shape's material slot carries: a plain identifier, unique per anchor+key
            self.assertEqual(m["material"], earthd.sat_material(m["anchorId"], m["key"]))
            self.assertRegex(m["material"], r"^bngEarthSat_\d+_m?\d+_m?\d+_m?\d+$")
            self.assertEqual(m["texture"], m["shape"].rsplit(".", 1)[0] + ".dds")      # next to the shape
            self.assertIs(m["detail"], True)
        if kind == "hello":
            self.assertIn(m["nearVisual"], ("mesh", "terrain"))
            self.assertEqual(m["format"], self.server.layout.format)
            self.assertIn(m["format"], modsim.CORE["FORMATS"])
            self.assertEqual(m.get("cells"), 64 if self.cells == 64 else None)
            self.assertEqual(m.get("blockSize"), None if self.block == 1024 else self.block)       # M4
            self.assertEqual(m.get("horizon"), True if self.horizon else None)                    # M1h
            self.assertEqual(m["material"], modsim.lua_string("farfield.lua", "MATERIAL"))
            self.assertIn(m["farFormat"], ff.FORMATS)

    def test_session_messages(self):
        c = self.c
        seen = []

        def until(pred, timeout=60):
            m, before = c.until(pred, timeout)
            seen.extend(before + [m])
            return m

        c.send({"type": "hello", "protocol": modsim.CORE["PROTOCOL"], "format": modsim.CORE["FORMAT"],
                "modVersion": "0.4.0"})
        until(lambda m: m["type"] == "hello")
        c.send({"type": "prepare_anchor", "requestId": 1, "lat": test_server.LAT, "lon": test_server.LON,
                "cx": 0, "cy": 0})
        a = until(lambda m: m["type"] == "anchor_ready")
        aid = a["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0, 0)
        until(lambda m: m["type"] == "far_done" and m["anchorId"] == aid, 120)
        self.pose(aid, 600, 0, vx=30)                   # LOD swaps (both sides) + the predicted block
        until(lambda m: m["type"] == "far_done" and m["anchorId"] == aid, 120)
        c.send({"type": "prepare_terrain", "requestId": 2, "anchorId": aid, "cx": 512, "cy": 0})
        until(lambda m: m["type"] == "terrain_ready")
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 512, "cy": 0})
        seen.extend(c.drain(0.3))                       # a commit changes no far tile: nothing to wait for
        c.send({"type": "prepare_terrain", "requestId": 3, "anchorId": 999, "cx": 0, "cy": 0})
        until(lambda m: m["type"] == "terrain_error")
        c.send({"type": "prepare_anchor", "requestId": 4, "lat": 123.0, "lon": 0})
        until(lambda m: m["type"] == "anchor_error")
        # a second anchor 2 km east (a rebase): the previous anchor's tiles get far_removes
        lat, lon, _ = self.server.anchors[aid].frame.enu_to_geodetic(2000.0, 0.0, 0.0)
        c.send({"type": "prepare_anchor", "requestId": 5, "lat": float(lat), "lon": float(lon), "cx": 0, "cy": 0})
        b = until(lambda m: m["type"] == "anchor_ready")
        c.send({"type": "commit_anchor", "anchorId": b["anchorId"]})
        c.send({"type": "commit_terrain", "anchorId": b["anchorId"], "cx": 0, "cy": 0})
        self.pose(b["anchorId"], 0, 0)
        until(lambda m: m["type"] == "far_done" and m["anchorId"] == b["anchorId"], 120)
        seen += c.drain(0.3)
        kinds = {m["type"] for m in seen}
        self.assertEqual(kinds, set(SERVER_TO_MOD), "a message type was not exercised or is not in the table")
        for m in seen:
            self.check(m)
        old_removes = [m for m in seen if m["type"] == "far_remove" and m["anchorId"] == aid]
        self.assertTrue(old_removes)
        textured = [m for m in seen if m["type"] == "far_tile" and "material" in m]
        self.assertEqual(bool(textured), self.with_imagery)
        for m in textured[::max(1, len(textured) // 12)]:
            self.check_textured(m)

    def check_textured(self, m):
        """The files a textured far_tile names, as the mod and the engine use them."""
        shape = cdae.read(self.fs(m["shape"]))
        self.assertEqual([x["name"] for x in shape["materials"]], [m["material"]])   # mapTo target
        mesh = cdae.mesh_arrays(shape["meshes"][0])
        v, uv0, uv1 = mesh["verts"], mesh["tverts"], mesh["tverts2"]
        size = ff.tile_of(m["key"]).size
        self.assertEqual(len(uv1), len(v))
        with open(self.fs(m["texture"]), "rb") as f:
            px = imagery.read_dds_info(f.read())[0]
        # UV0: the footprint inside the texture's gutter, v stored flipped (the engine's
        # convention): u = gutter_uv(x / S), 1 - v = gutter_uv(y / S), so texture row 0 is north
        self.assertTrue(np.allclose(uv0[:, 0], imagery.gutter_uv(v[:, 0] / size, px), atol=1e-6))
        self.assertTrue(np.allclose(uv0[:, 1], 1 - imagery.gutter_uv(v[:, 1] / size, px), atol=1e-6))
        # UV1: tile-local metres / DETAIL_M, flipped the same way; world-locked because the tile
        # origin is a multiple of DETAIL_M
        self.assertTrue(np.allclose(uv1[:, 0], v[:, 0] / ff.DETAIL_M, atol=1e-4))
        self.assertTrue(np.allclose(uv1[:, 1], 1 - v[:, 1] / ff.DETAIL_M, atol=1e-4))
        self.assertEqual(m["pos"][0] % ff.DETAIL_M, 0)
        self.assertEqual(m["pos"][1] % ff.DETAIL_M, 0)
        with open(self.fs(m["texture"]), "rb") as f:
            data = f.read()
        w, h, mips, fourcc, sizes = imagery.read_dds_info(data)
        self.assertEqual((w, h, fourcc), (self.server.tex_size(m["level"]),) * 2 + (b"DXT1",))
        self.assertEqual(mips, w.bit_length())                 # full chain down to 1 x 1
        self.assertEqual(len(data), imagery.DDS_HEADER + sum(sizes))


class DeshadowHelloTest(test_server.ServerHarness):
    """hello.deshadow: absent with --deshadow off (the default hello is unchanged), the mode otherwise."""

    def test_hello_deshadow(self):
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertNotIn("deshadow", m)
        self.server.deshadow = types.SimpleNamespace(mode="full", prepare=lambda lat, lon: None)
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertEqual(m["deshadow"], "full")
        self.assertIn("deshadow", SERVER_TO_MOD["hello"])
        self.assertIn(m["deshadow"], deshadowmod.MODES)


class ColormatchHelloTest(test_server.ServerHarness):
    """hello.colormatch: absent with --colormatch off (the default hello is unchanged), the mode otherwise."""

    def test_hello_colormatch(self):
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertNotIn("colormatch", m)
        self.server.colormatch = types.SimpleNamespace(mode="s2", prepare=lambda lat, lon: None)
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertEqual(m["colormatch"], "s2")
        self.assertIn("colormatch", SERVER_TO_MOD["hello"])
        self.assertIn(m["colormatch"], colormatchmod.MODES)


class MeshImageryMessagesTest(ServerMessagesTest):
    """The same session with the v1.3 defaults: --near-visual mesh, imagery on (synthetic)."""
    near_visual = "mesh"
    with_imagery = True


class MeshImagery64MessagesTest(MeshImageryMessagesTest):
    """The same with --cells 64 (docs/V1_4_SPEC.md §3.1 M1): FORMAT 4, hello.cells, far_tile.tex."""
    cells = 64


class MeshImagery64Block512MessagesTest(MeshImagery64MessagesTest):
    """The same with --block 512 (docs/V1_4_SPEC.md §3.5 M4): hello.blockSize, terrain.size 512 on the
    128 m grid."""
    block = 512



class MeshImagery64HorizonMessagesTest(MeshImagery64MessagesTest):
    """The same with --horizon (docs/V1_4_SPEC.md §3.6 M1h): hello.horizon, status.radius, tiles up to
    L10 with the coarse textures."""
    horizon = True


if __name__ == "__main__":
    unittest.main()
