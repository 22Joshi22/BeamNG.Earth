"""Python model of the mod's runtime rules (protocol 4, docs/V1_3_SPEC.md over docs/V1_2_DESIGN.md),
for the integration drive (test_integration_drive.py) and the protocol agreement test.

Every threshold is read from the mod's Lua files (lua_consts), and the rules are line-by-line
ports of:
  follow.lua    target / decide (terrain-follow prepare/commit, commit gating hook)
  rebase.lua    decide, core.lua rebaseHint
  governor.lua  the per-frame streaming budget and heavy-slot isolation (Governor)
  crossover.lua the C1/C2 mode choice (in view at any distance, the cap where near in-view groups
                wait for a slot) and phase lengths; defaultErr from the server's hello.cells (M1)
  farfield.lua  the tile state machine (FarField): hidden preload, swap groups that run once
                every add is loaded, groups sharing a key in arrival order, far_remove ordering,
                the previous anchor's leftovers at far_done, cover state (edge ring sunk by the
                server's `sink`, interior hidden; nothing covered in mesh near-visual mode),
                onBlockCommit, uncoverReady, deletes (never in a frame that imported); mesh mode:
                nearest-first imports, previous-anchor tiles dropped once covered.
  satmat.lua    per-tile imagery materials: created (unit 'material') before the first tile using
                them, reference-counted, deleted (unit 'matDelete') after their last tile object.
  governor.lua  texKind: the unit kind of a textured import from far_tile.tex (FORMAT 4, M1:
                cdaeTex128/256/512; 'cdaeTex' without it).
test_modsim.py runs governor.lua and follow.lua under luajit against these ports.
Engine calls are replaced by a cost model (ms per unit kind) supplied by the caller.
"""
import math
import os
import re
import sys
from collections import deque
from pathlib import Path

import numpy as np

SERVER_DIR = Path(__file__).resolve().parents[1]
MOD_ROOT = SERVER_DIR.parent / "mod" / "beamng_earth"
MOD_DIR = MOD_ROOT / "lua" / "ge" / "extensions" / "beamngEarth"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
import farfield as ff  # noqa: E402


# --- Lua constants ---------------------------------------------------------------------
_ASSIGN = re.compile(r"^(?:M\.|local )([\w, .]+?)\s*=\s*(.+?)\s*(?:--.*)?$", re.M)
_REF = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")
_NUMERIC = re.compile(r"^[\d.\s+\-*/()e]+$")


def _eval(expr, own, env):
    """Value of a numeric Lua expression with M.X / <module>.X references, or None."""
    expr = expr.strip()
    if expr.startswith("{") and expr.endswith("}"):
        items = [s.strip() for s in expr[1:-1].split(",") if s.strip()]
        if items and all("=" in s for s in items):
            out = {}
            for s in items:
                k, v = (p.strip() for p in s.split("=", 1))
                out[k] = _eval(v, own, env)
            return None if None in out.values() else out
        vals = [_eval(s, own, env) for s in items]
        return None if None in vals else vals

    def ref(m):
        mod, name = m.group(1), m.group(2)
        table = own if mod == "M" else env.get(mod)
        if table is None or name not in table or not isinstance(table[name], (int, float)):
            raise KeyError(name)
        return repr(float(table[name]))
    try:
        expr = _REF.sub(ref, expr)
    except KeyError:
        return None
    if not _NUMERIC.match(expr):
        bare = own.get(expr)            # `local REACH = ...; x = REACH`
        return bare if isinstance(bare, (int, float)) else None
    try:
        return float(eval(expr, {"__builtins__": {}}))     # digits and operators only
    except (SyntaxError, ZeroDivisionError):
        return None


def lua_consts(name, env=None):
    """Numeric constants (and flat numeric tables) of a mod Lua file: `M.X = expr`,
    `local X = expr`, `M.A, M.B = a, b`. env: {moduleName: consts} for cross-file references
    (`M.SIZE = lattice.N`)."""
    text = (MOD_DIR / name).read_text()
    env = env or {}
    out = {}
    for m in _ASSIGN.finditer(text):
        names = [n.strip().removeprefix("M.") for n in m.group(1).split(",")]
        rhs = m.group(2)
        parts = [rhs] if len(names) == 1 else [p for p in rhs.split(",")]
        if len(parts) != len(names) or not all(re.fullmatch(r"\w+", n) for n in names):
            continue
        for n, p in zip(names, parts):
            v = _eval(p, out, env)
            if v is not None:
                out[n] = v
    return out


def lua_string(name, const):
    """String constant `M.X = '...'` of a mod Lua file."""
    m = re.search(rf"^M\.{const}\s*=\s*'([^']*)'", (MOD_DIR / name).read_text(), re.M)
    return m.group(1) if m else None


LATTICE = lua_consts("lattice.lua")
FOLLOW = lua_consts("follow.lua", {"lattice": LATTICE})
REBASE = lua_consts("rebase.lua")
CORE = lua_consts("core.lua")
GOV = lua_consts("governor.lua")
XO = lua_consts("crossover.lua")
FAR = lua_consts("farfield.lua")


# --- follow.lua / rebase.lua / core.lua rebaseHint ----------------------------------------
def snap(v):
    return math.floor(v / FOLLOW["GRID"] + 0.5) * FOLLOW["GRID"]


def cheb(ax, ay, bx, by):
    return max(abs(ax - bx), abs(ay - by))


def _clamp_axis(c, p):
    reach, g = FOLLOW["REACH"], FOLLOW["GRID"]
    if c > p + reach:
        return math.floor((p + reach) / g) * g
    if c < p - reach:
        return math.ceil((p - reach) / g) * g
    return c


def follow_target(px, py, vx, vy):
    t = FOLLOW["LOOKAHEAD_S"]
    return _clamp_axis(snap(px + vx * t), px), _clamp_axis(snap(py + vy * t), py)


def follow_decide(px, py, vx, vy, cx, cy, pending, now=0.0, retry_at=0.0, gate=None):
    """follow.decide: ('prepare', tx, ty) | ('commit',) | ('discard',) | None."""
    if cx is None:
        return None
    d = cheb(px, py, cx, cy)
    if pending:
        pd = cheb(px, py, pending["cx"], pending["cy"])
        if pending.get("ready") and pd < d and d > FOLLOW["COMMIT_DIST"]:
            if d > FOLLOW["HARD_COMMIT_DIST"] or gate is None or gate(pending["cx"], pending["cy"]):
                return ("commit",)
            return None
        if pd > FOLLOW["REACH"]:
            return ("discard",)
        return None
    if d <= FOLLOW["PREPARE_DIST"] or now < retry_at:
        return None
    tx, ty = follow_target(px, py, vx, vy)
    if (tx, ty) == (cx, cy):
        return None
    return ("prepare", tx, ty)


def rebase_decide(dist, pending, stale, distance, now=0.0, retry_at=0.0):
    if not pending:
        return "prefetch" if dist >= distance - REBASE["PREFETCH_MARGIN"] and now >= retry_at else None
    if pending["purpose"] != "rebase" or not pending.get("ready"):
        return None
    if dist < distance:
        return None
    if stale is not None and stale > REBASE["STALE_DIST"]:
        return "discard"
    return "commit"


def rebase_hint(vx, vy, dist, distance):
    rem = max(0.0, distance - dist)
    speed = math.hypot(vx, vy)
    px = py = 0.0
    if speed > 1:
        px, py = vx / speed * rem, vy / speed * rem
    return follow_target(px, py, vx, vy)


def cover_box(cx, cy):
    h = LATTICE["HALF"]
    return cx - h, cy - h, cx + h, cy + h


def ground_box(cx, cy):
    """lattice.groundBox: the block's collidable / rendered ground, [cx-1024, cx+1022]^2."""
    x0, y0 = cx - LATTICE["HALF"], cy - LATTICE["HALF"]
    return x0, y0, x0 + LATTICE["VALID_W"], y0 + LATTICE["VALID_W"]


# --- governor.lua ----------------------------------------------------------------------
class Governor:
    """governor.lua Gov, same state and arithmetic. The caller passes the previous frame's wall
    time to begin_frame (the Lua code measures it with hptimer)."""

    def __init__(self, target_ms=None):
        g = GOV
        self.target = g["DEFAULT_TARGET_MS"]
        self.loading = False
        self.b = self.b_prev = self.tokens = 0.0
        self.est = dict(g["PRIOR"])
        self.used = set()
        self.hist, self.hist_n, self.base = [0.0] * int(g["BASE_FRAMES"]), 0, None
        self.frame = 0
        self.w1 = self.w2 = None
        self.prev_heavy = False
        self.frame_spent, self.frame_units = 0.0, 0
        self.last_stream = -math.inf
        self.heavy_pending, self.heavy_since, self.heavy_req = False, None, None
        self.heavy_frame = -math.inf
        self.quiet = 0
        self.blocked = False
        if target_ms is not None:
            self.set_target(target_ms)

    def set_target(self, ms):
        self.target = max(GOV["MIN_TARGET_MS"], min(GOV["MAX_TARGET_MS"], ms))

    def target_now(self):
        return GOV["LOADING_TARGET_MS"] if self.loading else self.target

    def max_unit(self):
        return max((self.est.get(k, 0.0) for k in self.used), default=0.0)

    def begin_frame(self, has_work, wall_ms):
        g = GOV
        self.frame += 1
        if wall_ms is not None:
            if not self.prev_heavy:
                self.hist_n += 1
                self.hist[(self.hist_n - 1) % len(self.hist)] = wall_ms
                n = min(self.hist_n, len(self.hist))
                s = sorted(self.hist[:n])
                self.base = s[(n + 1) // 2 - 1] if n % 2 == 1 else 0.5 * (s[n // 2 - 1] + s[n // 2])
            self.w1, self.w2 = wall_ms, self.w1
        self.prev_heavy = False
        self.frame_spent, self.frame_units = 0.0, 0
        if self.heavy_pending and self.heavy_req < self.frame - 1:
            self.heavy_pending = False
        target = self.target_now()
        self.b_prev = self.b
        if self.quiet > 0:
            self.quiet -= 1
            self.b, self.tokens, self.blocked = 0.0, 0.0, True
            return
        self.blocked = False
        if self.heavy_pending:
            self.b = max(0.0, self.b - g["RAMP_DOWN_HEAVY"])
        elif has_work:
            if self.b < target:
                self.b = min(target, self.b + (g["RAMP_UP_LOADING"] if self.loading else g["RAMP_UP"]))
            else:
                self.b = max(target, self.b - g["RAMP_DOWN_IDLE"])
        else:
            self.b = max(0.0, self.b - g["RAMP_DOWN_IDLE"])
        self.tokens = min(self.tokens + self.b, 2 * self.max_unit())
        if self.heavy_pending or not has_work:
            self.tokens = min(self.tokens, self.b)

    def try_(self, kind):
        self.used.add(kind)
        if self.blocked or self.heavy_frame == self.frame:
            return False
        e = self.est.get(kind, 1.0)
        if self.tokens < e:
            return False
        if self.frame_spent + e > self.b + self.max_unit():
            return False
        return True

    def charge(self, kind, ms):
        ms = max(0.0, ms)
        self.tokens -= ms
        self.frame_spent += ms
        self.frame_units += 1
        self.last_stream = self.frame
        e = self.est.get(kind, ms)
        self.est[kind] = max(0.005, e + GOV["EST_ALPHA"] * (ms - e))

    def charge_motion(self, ms):
        ms = max(0.0, ms)
        self.tokens -= ms
        self.frame_spent += ms

    def _grant(self):
        self.heavy_pending, self.heavy_since = False, None
        self.heavy_frame = self.frame
        self.quiet = int(GOV["QUIET_AFTER"])
        self.b, self.tokens, self.blocked = 0.0, 0.0, True
        self.prev_heavy = True
        return True

    def request_heavy(self, urgent=False):
        g = GOV
        if self.heavy_frame == self.frame:
            return True
        if not self.heavy_pending:
            self.heavy_pending, self.heavy_since = True, self.frame
            if self.frame_units == 0 and not self.blocked:
                self.b = max(0.0, self.b_prev - g["RAMP_DOWN_HEAVY"])
        self.heavy_req = self.frame
        self.tokens = min(self.tokens, self.b)
        if urgent:
            return self._grant()
        if self.b > 0 or self.frame - self.last_stream <= g["QUIET_BEFORE"]:
            return False
        calm = (self.base is not None and self.w1 is not None and self.w2 is not None
                and self.w1 <= self.base + g["CALM_MARGIN_MS"] and self.w2 <= self.base + g["CALM_MARGIN_MS"])
        if calm or self.frame - self.heavy_since >= g["CALM_TIMEOUT"]:
            return self._grant()
        return False

    def heavy_now(self):
        return self.request_heavy(True)


# --- crossover.lua ---------------------------------------------------------------------
def smootherstep(t):
    return 0.0 if t <= 0 else 1.0 if t >= 1 else t * t * t * (t * (t * 6 - 15) + 10)


def cone_half_deg(fov_deg):
    f = math.radians(min(max(fov_deg or 60, 1), 179) / 2)
    return math.degrees(math.atan(math.tan(f) * math.sqrt(1 + XO["ASPECT_MAX"] ** 2)))


def sphere_in_view(cam, cx, cy, cz, r):
    dx, dy, dz = cx - cam["x"], cy - cam["y"], cz - cam["z"]
    d = math.sqrt(dx * dx + dy * dy + dz * dz)
    if d <= r:
        return True, 0.0
    fl = math.sqrt(cam["fx"] ** 2 + cam["fy"] ** 2 + cam["fz"] ** 2)
    if fl < 1e-9:
        return True, d - r
    c = (dx * cam["fx"] + dy * cam["fy"] + dz * cam["fz"]) / (d * fl)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, c))))
    spread = math.degrees(math.asin(min(1.0, r / d)))
    return angle - spread <= cone_half_deg(cam["fovDeg"]) + XO["MARGIN_DEG"], d - r


def tile_sphere(x0, y0, x1, y1, z, delta):
    s = max(x1 - x0, y1 - y0)
    return 0.5 * (x0 + x1), 0.5 * (y0 + y1), z, 0.7072 * s + max(50.0, 0.1 * s) + delta


def xo_delta(err_new, err_old):
    return 1.2 * (err_new + err_old) + 0.2


def default_err(level, cells=None):
    """crossover.defaultErr with the server's cell count (hello.cells; XO CELLS = 32 without it)."""
    return 256 * 2 ** level / (cells or XO["CELLS"]) / 4


def _tonumber(v):
    """Lua tonumber for the values a JSON message can carry: numbers, numeric strings, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return v if isinstance(v, (int, float)) else None


def _integral(n):
    """Lua n == math.floor(n) (true for +-inf, false for nan)."""
    return n is not None and (math.isinf(n) or (n == n and n == math.floor(n)))


def set_cells(n):
    """crossover.setCells: a positive integer (tonumber), else the FORMAT 3 default."""
    n = _tonumber(n)
    return n if n is not None and n >= 1 and _integral(n) else XO["CELLS"]


def exact_level_for(cells):
    """farfield.exactLevelFor: the level whose tile is `cells` lattice squares wide."""
    return math.floor(math.log((cells or 32) * LATTICE["SQUARE"] / LATTICE["L0"]) / math.log(2) + 0.5)


def tex_kind(px):
    """governor.texKind: cdaeTex<px> for a texture size with a prior, else 'cdaeTex'."""
    px = _tonumber(px)
    if _integral(px) and not math.isinf(px):      # (inf formats to no size with a prior in Lua)
        k = f"cdaeTex{int(px)}"
        if k in GOV["PRIOR"]:
            return k
    return "cdaeTex"


# --- farfield.lua ----------------------------------------------------------------------
class Inst:
    """One far tile instance (farfield.lua newInst). bbox0/aid: its footprint in its own
    anchor's frame (for exact coverage); x0..y1: the footprint in the current frame (AABB after
    a rebase transform, as the mod keeps it)."""
    __slots__ = ("key", "aid", "level", "bbox0", "x0", "y0", "x1", "y1", "z", "err", "sink", "seq",
                 "prefetch", "kind", "path", "queued", "loaded", "failed", "shown", "doomed", "removing",
                 "cover", "cover_hide", "anim_end", "grp", "material", "mat", "mat_failed")

    def __init__(self, msg, seq, path, cells=None):
        self.key, self.aid = str(msg["key"]), msg["anchorId"]
        self.level = int(msg.get("level", 0))
        size = FOLLOW["GRID"] * 2 ** self.level
        x, y, z = (float(v) for v in msg["pos"])
        self.bbox0 = (x, y, x + size, y + size)
        self.x0, self.y0, self.x1, self.y1 = self.bbox0
        self.z = z
        self.err = float(msg["err"]) if msg.get("err") is not None else default_err(self.level, cells)
        self.sink = float(msg["sink"]) if msg.get("sink") is not None else None
        self.seq = seq
        self.prefetch = msg.get("state") == "prefetch"
        mat = msg.get("material")
        self.material = mat if isinstance(mat, str) and mat else None   # per-tile imagery material
        self.mat, self.mat_failed = None, False                         # held reference
        if not msg["shape"].lower().endswith(".cdae"):
            self.kind = "dae"
        else:
            self.kind = tex_kind(msg.get("tex")) if self.material else "cdae"
        self.path = path
        self.queued, self.loaded, self.failed, self.shown = True, False, False, False
        self.doomed = self.removing = False
        self.cover, self.cover_hide, self.anim_end, self.grp = "none", False, None, None


class Group:
    __slots__ = ("id", "add", "remove", "state", "msg_in", "msg_out", "adds", "rems", "t0", "dur", "d_in",
                 "d_out", "mode")

    def __init__(self, msg):
        self.id = msg.get("id")
        self.add = [str(k) for k in msg.get("add") or []]
        self.remove = [str(k) for k in msg.get("remove") or []]
        self.state = "wait"
        self.msg_in = msg.get("dIn")
        self.msg_out = msg.get("dOut")
        self.adds, self.rems = [], []
        self.t0 = self.dur = 0.0
        self.d_in = self.d_out = 0.0
        self.mode = None


class FarField:
    """farfield.lua as a state machine over Inst records. costs: {kind: ms} used as the measured
    cost of each unit (import kinds 'cdae'/'dae', 'reveal', 'delete', 'motion' per moving tile);
    path_of: VFS shape path -> local file (an import fails if the file is gone, as FS:fileExists)."""

    # costs not given by the caller (ms): a per-tile Material (p_sat.lua 0.13-0.46 ms), a textured
    # .cdae import (2.5-3.5 ms incl. its texture), a Material delete (unmeasured)
    DEFAULT_COSTS = {"material": 0.3, "cdaeTex": 3.0, "matDelete": 0.2, "motion": 0.0}
    IMPORT_KINDS = ("cdae", "dae", "cdaeTex", "cdaeTex128", "cdaeTex256", "cdaeTex512")

    def __init__(self, costs, path_of, cost_scale=None):
        """cost_scale: None, or n -> factor applied to the cost of an import with n objects in the
        scene (live + old + deleting; docs/V1_4_SPEC.md R3: c(n) = c(n0) (1 + alpha (n - n0)))."""
        self.costs, self.path_of = {**self.DEFAULT_COSTS, **costs}, path_of
        self.cost_scale = cost_scale
        self.cells = XO["CELLS"]          # crossover.setCells(hello.cells)
        self.pending, self.live, self.old = {}, {}, {}
        self.main_q, self.pre_q = deque(), deque()
        self.n_queued = 0
        self.groups = []
        self.active_groups = 0
        self.removals = deque()           # (inst, seq, oldKey or None)
        self.doomed = []
        self.moving = set()
        self.lossy = set()
        self.block = None
        self.anchor_id = None
        self.done = False
        self.seq_n = 0
        self.clock = 0.0
        self.counters = dict(imports=0, failed=0, crossovers=0, instant=0, pops=0, deletes=0, late_tiles=0,
                             unknown_swap_keys=0, textured=0, materials=0, mat_deletes=0, cap_waits=0,
                             old_covered=0)
        self.dropped_old = set()          # (anchorId, key) dropped by drop_covered_old (diagnostics)
        self.near_visual = "terrain"
        self.mats = {}                    # satmat.lua: material name -> references
        self.mat_del = []                 # names whose references reached 0 (FIFO)
        self.mat_queued = set()
        self.unknown_removes = []
        self.frame = dict(units=0, imports=0, est=0.0, spent=0.0, excess=0.0)
        self.on_import = None             # test hook: called with each instance the "engine" loaded

    # --- cover ---
    def cover_of(self, inst):
        b = self.block
        if b is None or self.near_visual == "mesh":
            return "none"
        e = 0.01
        if inst.x0 < b[0] - e or inst.y0 < b[1] - e or inst.x1 > b[2] + e or inst.y1 > b[3] + e:
            return "none"
        d = min(inst.x0 - b[0], inst.y0 - b[1], b[2] - inst.x1, b[3] - inst.y1)
        return "edge" if d < FAR["EDGE_BAND"] else "interior"

    @staticmethod
    def cover_target_hide(inst, cover):
        if cover == "none":
            return False
        if inst.sink is not None:
            return cover == "interior"
        return True

    def animating(self, inst):
        if inst.anim_end is not None and self.clock >= inst.anim_end:
            inst.anim_end = None
        return inst.anim_end is not None

    def hidden(self, inst):
        """The object is hidden (or gone): it shows no ground."""
        if inst.doomed or not inst.shown:
            return True
        return inst.cover_hide and not self.animating(inst)

    def apply_cover(self, inst, animate):
        c = self.cover_of(inst)
        if c == inst.cover:
            return False
        was_visible = inst.cover == "none" and not self.hidden(inst)
        inst.cover = c
        inst.cover_hide = self.cover_target_hide(inst, c)
        if animate and was_visible and c != "none":
            inst.anim_end = self.clock + XO["COVER_SINK_S"]
            self.moving.add(inst)
        else:
            inst.anim_end = None
        return True

    def set_block(self, cx, cy):
        self.block = cover_box(cx, cy)

    def set_near_visual(self, mode):
        """farfield.setNearVisual: 'mesh' or 'terrain'; re-applies every shown tile's cover state."""
        mode = "mesh" if mode == "mesh" else "terrain"
        if mode == self.near_visual:
            return
        self.near_visual = mode
        for inst in list(self.live.values()) + list(self.old.values()):
            self.apply_cover(inst, False)

    def on_block_commit(self, cx, cy):
        self.set_block(cx, cy)
        n = 0
        for inst in list(self.live.values()) + list(self.old.values()):
            n += self.apply_cover(inst, True)
        return n

    def uncover_ready(self, ocx, ocy, ncx, ncy):
        if self.near_visual == "mesh":
            return True
        ox0, oy0, ox1, oy1 = cover_box(ocx, ocy)
        nx0, ny0, nx1, ny1 = cover_box(ncx, ncy)
        L, e = FOLLOW["GRID"], 0.01
        for inst in self.live.values():
            if (not inst.failed and inst.x0 < nx1 - e and nx0 + e < inst.x1 and inst.y0 < ny1 - e
                    and ny0 + e < inst.y1 and (inst.level > 0 or inst.x0 < nx0 - e or inst.y0 < ny0 - e
                                               or inst.x1 > nx1 + e or inst.y1 > ny1 + e)):
                return False
        idx = {(math.floor(i.x0 / L + 0.5), math.floor(i.y0 / L + 0.5))
               for i in self.live.values() if i.level == 0 and not i.failed}

        def any_covers(px, py, insts):
            return any(not i.failed and not i.doomed and i.x0 < px < i.x1 and i.y0 < py < i.y1 for i in insts)
        for gx in np.arange(ox0, ox1 - 1 + 1e-9, L):
            for gy in np.arange(oy0, oy1 - 1 + 1e-9, L):
                px, py = gx + L / 2, gy + L / 2
                if (not (nx0 < px < nx1 and ny0 < py < ny1)
                        and (math.floor(gx / L + 0.5), math.floor(gy / L + 0.5)) not in idx
                        and not any_covers(px, py, self.live.values())
                        and not any_covers(px, py, self.old.values())):
                    return False
        return True

    # --- groups ---
    def min_pending_seq(self):
        return min((i.seq for i in self.pending.values() if not i.prefetch), default=math.inf)

    def group_ready(self, g):
        for k in g.add:
            a = self.pending.get(k)
            if a is not None:
                if not a.loaded and not a.failed:
                    return False
            elif k not in self.live and not (self.done and self.n_queued == 0):
                return False
        return True

    # --- satmat.lua ---
    def mat_unref(self, name):
        if name not in self.mats:
            return
        self.mats[name] -= 1
        if self.mats[name] <= 0:
            self.mats[name] = 0
            if name not in self.mat_queued:
                self.mat_queued.add(name)
                self.mat_del.append(name)

    def mat_has_deletes(self):
        return any(self.mats.get(n) == 0 for n in self.mat_del)

    def mat_delete_next(self):
        while self.mat_del:
            name = self.mat_del.pop(0)
            self.mat_queued.discard(name)
            if self.mats.get(name) == 0:
                del self.mats[name]
                return True
        return False

    def release_mat(self, inst):
        if inst.mat is not None:
            self.mat_unref(inst.mat)
            inst.mat = None

    def doom(self, inst):
        if inst.doomed:
            return
        inst.doomed, inst.grp, inst.anim_end = True, None, None
        self.moving.discard(inst)
        if inst.queued:
            inst.queued = False
            self.n_queued -= 1
        if inst.loaded:
            self.doomed.append(inst)      # its material reference goes with the object's delete
        else:
            self.release_mat(inst)

    def in_view(self, insts, cam, delta):
        if cam is None:
            return True, 0.0
        anyv, nearest = False, math.inf
        for i in insts:
            vis, d = sphere_in_view(cam, *tile_sphere(i.x0, i.y0, i.x1, i.y1, i.z, delta))
            nearest = min(nearest, d)
            if vis:
                anyv = True
        return anyv, nearest

    def drop_covered_old(self, adds):
        """farfield.lua dropCoveredOld: mesh mode, a previous anchor's tile that settled
        current-anchor tiles cover completely goes at once (instead of waiting for its far_remove)."""
        if not self.old or self.near_visual != "mesh" or not adds:
            return
        drop = []
        for ok, o in self.old.items():
            hit = any(a.x0 < o.x1 and o.x0 < a.x1 and a.y0 < o.y1 and o.y0 < a.y1 for a in adds)
            if not hit or o.doomed or o.grp is not None:
                continue
            area = 0.0
            for lt in self.live.values():
                if lt.shown and not lt.failed and lt.grp is None:
                    w = min(lt.x1, o.x1) - max(lt.x0, o.x0)
                    h = min(lt.y1, o.y1) - max(lt.y0, o.y0)
                    if w > 0 and h > 0:
                        area += w * h
            if area >= (o.x1 - o.x0) * (o.y1 - o.y0) * (1 - 1e-9):
                drop.append((o.seq, ok, o))
        for _, ok, o in sorted(drop, key=lambda d: d[0]):     # in arrival order, as the Lua
            del self.old[ok]
            self.dropped_old.add(ok)
            self.doom(o)
            self.counters["old_covered"] += 1

    def finish_group(self, g):
        for a in g.adds:
            if a.grp is g:
                a.grp = None
                if not self.animating(a):
                    self.moving.discard(a)
        for r in g.rems:
            self.doom(r)
        if g.state == "run":
            self.active_groups -= 1
        g.state = "done"
        self.drop_covered_old(g.adds)

    def start_group(self, g, cam):
        """farfield.lua startGroup: False (nothing changed) when the group must wait for a crossover
        slot (in view within NEAR_POP_M at the cap)."""
        # 1. plan
        rems, seen = [], set()
        for k in g.remove:
            r = self.live.get(k)
            if r is not None and id(r) not in seen:
                rems.append(r)
                seen.add(id(r))
        adds, unknown = [], 0
        for k in g.add:
            a = self.pending.get(k)
            if a is not None:
                cur = self.live.get(k)
                if cur is not None and id(cur) not in seen:
                    rems.append(cur)
                    seen.add(id(cur))
                adds.append(a)
            elif k not in self.live:
                unknown += 1
        vis_adds, vis_rems, err_n, err_o = [], [], 0.0, 0.0
        for a in adds:
            if self.cover_of(a) == "none" and not a.failed:
                vis_adds.append(a)
                err_n = max(err_n, a.err)
        for r in rems:
            if r.cover == "none" and not self.hidden(r) and r.grp is None:
                vis_rems.append(r)
                err_o = max(err_o, r.err)
        mode, d_in, d_out = "instant", 0.0, 0.0
        if vis_adds or vis_rems:
            bound = xo_delta(err_n, err_o)
            if vis_adds and vis_rems and g.msg_in is not None and g.msg_out is not None:
                d_in, d_out = float(g.msg_in), float(g.msg_out)
            else:
                d_in = d_out = bound
            vis, nearest = self.in_view(vis_adds + vis_rems, cam, max(d_in, d_out))
            if not vis_rems and not self.old:
                mode = "instant"
            elif not vis:
                mode = "instant"
            elif self.active_groups >= XO["MAX_GROUPS"]:
                if nearest <= XO["NEAR_POP_M"]:
                    self.counters["cap_waits"] += 1
                    return False
                mode = "instant"
                self.counters["pops"] += 1          # in view, over the cap and far: a visible pop
            else:
                mode = "cross" if vis_rems else "rise"
        # 2. apply
        self.counters["unknown_swap_keys"] += unknown
        for a in adds:
            self.pending.pop(a.key, None)
            if a.queued:
                a.queued = False
                self.n_queued -= 1
            self.live[a.key] = a
            a.shown = True
            a.cover = self.cover_of(a)
            a.cover_hide = self.cover_target_hide(a, a.cover)
        for r in rems:
            if self.live.get(r.key) is r:
                del self.live[r.key]
        vis_ids = {id(r) for r in vis_rems}
        for r in rems:
            if id(r) not in vis_ids:
                self.doom(r)
        g.adds, g.rems, g.mode, g.d_in, g.d_out = vis_adds, vis_rems, mode, d_in, d_out
        if mode == "instant":
            for r in vis_rems:
                self.doom(r)
            g.state = "done"
            self.counters["instant"] += 1
            self.drop_covered_old(vis_adds)
            return True
        g.t0, g.state = self.clock, "run"
        g.dur = XO["PHASE1_S"] + (XO["PHASE2_S"] if mode == "cross" else 0.0)
        for a in vis_adds:
            a.grp = g
            self.moving.add(a)
        for r in vis_rems:
            r.grp = g
            self.moving.add(r)
        self.active_groups += 1
        self.counters["crossovers"] += 1
        return True

    def finish_all(self):
        for g in self.groups:
            if g.state == "run":
                self.finish_group(g)
        for inst in self.moving:
            inst.anim_end = None
        self.moving = set()

    # --- messages ---
    def on_tile(self, msg):
        if msg.get("anchorId") != self.anchor_id:
            if msg.get("anchorId") is not None:
                self.lossy.add(msg["anchorId"])
            self.counters["late_tiles"] += 1
            return
        key = str(msg["key"])
        prev = self.pending.get(key)
        if prev is not None:
            self.doom(prev)
        self.seq_n += 1
        inst = Inst(msg, self.seq_n, self.path_of(msg["shape"]), self.cells)
        if inst.material:
            self.counters["textured"] += 1
        self.pending[key] = inst
        self.n_queued += 1
        (self.pre_q if inst.prefetch else self.main_q).append(inst)

    def on_swap(self, msg):
        if msg.get("anchorId") != self.anchor_id:
            return
        g = Group(msg)
        for k in g.add:
            p = self.pending.get(k)
            if p is not None and p.prefetch:
                p.prefetch = False
                if p.queued:
                    self.main_q.append(p)
        self.groups.append(g)

    def on_remove(self, msg):
        key, aid = str(msg["key"]), msg.get("anchorId")
        if aid != self.anchor_id:
            ok = (aid, key)
            inst = self.old.get(ok)
            if inst is not None and not inst.removing and aid not in self.lossy:
                inst.removing = True
                self.removals.append((inst, self.seq_n, ok))
            elif inst is None and aid not in self.lossy and ok not in self.dropped_old:
                self.unknown_removes.append(ok)     # the mod never had it (silently ignored there)
            return
        p = self.pending.pop(key, None)
        if p is not None:
            self.doom(p)
            return
        live = self.live.get(key)
        self.unknown_removes.append((aid, key))     # the server never far_removes a shown tile
        if live is not None and not live.removing:
            live.removing = True
            self.removals.append((live, self.seq_n, None))

    def on_done(self, msg):
        if msg.get("anchorId") == self.anchor_id:
            self.done = True

    # --- anchors ---
    def transform(self, src, dst):
        """farfield.transform: every kept tile's footprint into the new frame (AABB)."""
        self.finish_all()
        for inst in list(self.live.values()) + list(self.old.values()):
            if not inst.doomed:
                inst.x0, inst.y0, inst.x1, inst.y1 = ff.transform_bbox(src, dst, (inst.x0, inst.y0, inst.x1, inst.y1))

    def switch_anchor(self, new_id, keep_old, blk):
        self.finish_all()
        keep = deque()
        if keep_old:
            due = self.min_pending_seq()
            for inst, seq, okey in self.removals:
                if seq < due and not inst.doomed:
                    keep.append((inst, 0, okey or (self.anchor_id, inst.key)))
            dropped = False
            for inst in self.pending.values():
                if not inst.prefetch:
                    dropped = True
                self.doom(inst)
            if dropped and self.anchor_id is not None:
                self.lossy.add(self.anchor_id)
            for key, inst in self.live.items():
                self.old[(self.anchor_id, key)] = inst
        else:
            for d in (self.live, self.old, self.pending):
                for inst in d.values():
                    self.doom(inst)
            self.old, self.lossy = {}, set()
        self.pending, self.live = {}, {}
        self.main_q, self.pre_q, self.n_queued = deque(), deque(), 0
        self.groups, self.active_groups, self.moving = [], 0, set()
        self.removals = keep
        self.anchor_id = new_id
        self.done = False
        if blk is not None:
            self.set_block(*blk)
            for inst in self.old.values():
                self.apply_cover(inst, False)

    # --- per frame ---
    def _peek(self, cam=None):
        """farfield.lua peekImport: group tiles in server order, then prefetch; mesh mode with a
        camera: the queued group tile nearest the camera (first of equals in queue order)."""
        q = self.main_q
        while q:
            inst = q[0]
            if self.pending.get(inst.key) is inst and inst.queued:
                break
            q.popleft()
        if q:
            if self.near_visual != "mesh" or cam is None:
                return q[0]
            best, bd = None, math.inf
            for inst in q:
                if self.pending.get(inst.key) is inst and inst.queued:
                    dx = max(inst.x0 - cam["x"], 0.0, cam["x"] - inst.x1)
                    dy = max(inst.y0 - cam["y"], 0.0, cam["y"] - inst.y1)
                    d = dx * dx + dy * dy
                    if d < bd:
                        best, bd = inst, d
            return best
        q = self.pre_q
        while q:
            inst = q[0]
            if self.pending.get(inst.key) is inst and inst.queued:
                return inst
            q.popleft()
        return None

    def has_work(self):
        return (self.n_queued > 0 or bool(self.doomed) or bool(self.removals)
                or any(g.state == "wait" for g in self.groups) or self.mat_has_deletes())

    def pending_main(self):
        return sum(1 for i in self.pending.values() if not i.prefetch)

    def objects(self):
        """Tile objects in the scene (live + old + deleting), the R3 cost term's n."""
        return len(self.live) + len(self.old) + len(self.doomed)

    def import_cost(self, kind):
        ms = self.costs[kind]
        return ms * self.cost_scale(self.objects()) if self.cost_scale is not None else ms

    def _unit(self, gov, kind, ms):
        e = gov.est.get(kind, 1.0)
        self.frame["est"] += e
        self.frame["excess"] = max(self.frame["excess"], ms - e)     # over the estimate it was granted on
        gov.charge(kind, ms)
        self.frame["units"] += 1
        self.frame["spent"] += ms

    def update(self, gov, dt, cam):
        """farfield.update for one frame; fills self.frame with its units and costs."""
        self.frame = dict(units=0, imports=0, est=0.0, spent=0.0, excess=0.0)
        self.clock += min(max(dt, 0.0), FAR["MAX_DT"])
        # 1. motion
        for g in self.groups:
            if g.state == "run" and self.clock - g.t0 >= g.dur:
                self.finish_group(g)
        n_moving = 0
        for inst in list(self.moving):
            n_moving += 1
            if inst.grp is None and not self.animating(inst):
                self.moving.discard(inst)
        motion = n_moving * self.costs["motion"]
        gov.charge_motion(motion)
        self.frame["spent"] += motion
        # 2. swap groups, in order; groups sharing a key wait for the earlier one
        busy = set()
        for g in self.groups:
            if g.state == "run":
                busy.update(g.add, g.remove)
        stop = False
        for g in self.groups:
            if g.state != "wait":
                continue
            blocked = stop or any(k in busy for k in g.add) or any(k in busy for k in g.remove)
            if not blocked and self.group_ready(g):
                if gov.try_("reveal"):
                    if self.start_group(g, cam):
                        self._unit(gov, "reveal", self.costs["reveal"])
                    # else: waits for a crossover slot; its keys stay busy, other groups go on
                else:
                    stop = True
            if g.state != "done":
                busy.update(g.add, g.remove)
        self.groups = [g for g in self.groups if g.state != "done"]
        # 3. far_removes whose earlier adds are all shown
        if self.removals:
            due = self.min_pending_seq()
            while self.removals and self.removals[0][1] < due:
                inst, _, okey = self.removals[0]
                if not inst.doomed:
                    if not gov.try_("reveal"):
                        break
                    if okey is not None:
                        if self.old.get(okey) is inst:
                            del self.old[okey]
                    elif self.live.get(inst.key) is inst:
                        del self.live[inst.key]
                    self.doom(inst)
                    self._unit(gov, "reveal", self.costs["reveal"])
                self.removals.popleft()
        # 4. imports (hidden); a tile's own imagery material first, as its own unit
        imported = False
        while True:
            inst = self._peek(cam)
            if inst is None:
                break
            if inst.material and inst.mat is None and not inst.mat_failed:
                if inst.material not in self.mats:
                    if not gov.try_("material"):
                        break
                    self.mats[inst.material] = 0
                    self.counters["materials"] += 1
                    self._unit(gov, "material", self.costs["material"])
                self.mats[inst.material] += 1
                inst.mat = inst.material
                continue
            if not gov.try_(inst.kind):
                break
            if not os.path.exists(inst.path):
                inst.failed = True
                self.counters["failed"] += 1
                self.release_mat(inst)
            else:
                inst.loaded = True
                self.counters["imports"] += 1
                if self.on_import is not None:
                    self.on_import(inst)
            inst.queued = False
            self.n_queued -= 1
            self.frame["imports"] += 1
            self._unit(gov, inst.kind, self.import_cost(inst.kind))
            imported = True
        # 5. the previous anchor's leftovers once far_done came and everything received is shown
        if self.done and self.old and not self.groups and self.pending_main() == 0:
            for inst in self.old.values():
                self.doom(inst)
            self.old = {}
        # 6. deletes (not in a frame that imported a tile, unless under pressure)
        if (self.n_queued == 0 and not imported) or len(self.doomed) > FAR["DELETE_PRESSURE"]:
            while self.doomed and gov.try_("delete"):
                self.release_mat(self.doomed.pop())
                self.counters["deletes"] += 1
                self._unit(gov, "delete", self.costs["delete"])
            while self.mat_has_deletes() and gov.try_("matDelete"):
                self.mat_delete_next()
                self.counters["mat_deletes"] += 1
                self._unit(gov, "matDelete", self.costs["matDelete"])

    def eager_keys(self):
        """Current-anchor keys shown once the received swaps have run (far_done's count)."""
        keys = set(self.live)
        for g in self.groups:
            keys = (keys - set(g.remove)) | set(g.add)
        return keys

    def visible(self):
        """(current-anchor footprints, {anchorId: [frame-own footprints]}) of shown, unhidden tiles."""
        cur = [i.bbox0 for i in self.live.values() if not self.hidden(i)]
        by_anchor = {}
        for i in self.old.values():
            if not self.hidden(i):
                by_anchor.setdefault(i.aid, []).append(i.bbox0)
        return cur, by_anchor
