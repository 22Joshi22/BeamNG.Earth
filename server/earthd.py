#!/usr/bin/env python3
"""BeamNG.Earth companion server (protocol 4). See docs/V1_3_SPEC.md, docs/V1_2_DESIGN.md
(and V1_1_SPEC.md).

    python3 server/earthd.py [--port 47613] [--user-dir DIR] [--cache-dir DIR] [--far-format cdae|dae]
                             [--imagery usgs,eox2023,eox2016|off] [--near-visual mesh|terrain]
                             [--cells 32|64] [--tex 256,256,256,512] [--deshadow off|lambert|full]

Listens on 127.0.0.1 for the mod, builds terrain-block heightmaps (1024² on a world-locked
2 m lattice, 256 m-aligned centres; in mesh near-visual mode the block is physics only) and a
dynamic-LOD tile field (down to 64 m tiles on the physics lattice in mesh mode) with per-tile
satellite textures, streamed as swap groups, and writes them into
<user-dir>/temp/beamngEarth/<sessionId>/.

Tile layout (docs/V1_4_SPEC.md §3.1, M1): --cells 64 (the default in mesh mode since probe P8
passed) makes every tile 64 x 64 cells, FORMAT 4, with the exact level L-1, hello.cells and
far_tile.tex; --cells 32 (and terrain mode) is the v1.3.1 layout, FORMAT 3.
"""
import os

# Heavy numpy work must not compete with the game's physics threads (§4.5): one BLAS/OpenMP
# thread per worker. Set before numpy is imported.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import glob  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections import OrderedDict, deque  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import numpy as np  # noqa: E402

import cdae  # noqa: E402
import dem as demmod  # noqa: E402
import deshadow as deshadowmod  # noqa: E402
import farfield  # noqa: E402
import geodesy  # noqa: E402
import heightmap  # noqa: E402
import imagery as imagerymod  # noqa: E402
import protocol  # noqa: E402

PROTOCOL = 4
# Tile/heightmap content format (hello.format) is the layout's: 3 = v1.3.1 (2 m lattice, fixed Q,
# 32-cell tiles), 4 = 64-cell tiles down to L-1 (farfield.CONTENT_FORMAT). A mod's hello.format is
# the newest format it reads (it reads every older one from 3: core.lua FORMATS), so a mod older
# than the server's format gets the mismatch status.
# Server(cells=...) default: the v1.3.1 layout (library and tests; test_v131_default pins it).
# The command line defaults to 64 cells in mesh mode since the in-game gate P8 passed
# (2026-10-10: 64-cell textured imports 4.0 ms at 256 px, 4.2 ms at 512 px; at 90 m/s the exact
# level was under the car 100 % of frames, queue max 35, vs 26 % / 2442 with 32 cells;
# docs/research/m1_d*); terrain mode stays frozen at 32.
CELLS_DEFAULT = 32
CLI_CELLS_MESH = 64


def resolve_cells(cells, near_visual):
    """The --cells value to use: the given one, else 64 in mesh mode and 32 in terrain mode."""
    if cells is not None:
        return cells
    return CLI_CELLS_MESH if near_visual == "mesh" else 32
SERVER_VERSION = "0.4.0"
NEAR_VISUALS = ("mesh", "terrain")
TEX_NEAR, TEX_FAR = 256, 256    # 32 cells: texture edge (px) of the two finest levels / of the others (512
                                 # near was 15x finer than USGS: no visible gain for 2x build time and VRAM);
                                 # --tex gives one per level (64 cells: farfield.TEX_DEFAULT)
SESSION_MARKER = ".earthd-session"
# The engine caches every loaded .dae as a .cdae under <user-dir>/temp/<VFS path, lower-cased>
# (seen in-game: temp/beamngEarth/<sid>/far_*.dae -> temp/temp/beamngearth/<sid>/far_*.cdae,
# 1.8 GB / 24k files after one v1 session). Those caches are deleted along with their sources.
# Server-written .cdae tiles (the default) have no such duplicate.
CDAE_ROOT = ("temp", "temp", "beamngearth")

HM_CACHE = 6                # heightmaps kept beyond the protected ones
HM_KEEP_COMMITTED = 3       # the current terrain and the last two
HM_KEEP_SENT = 2            # terrains handed to the mod but maybe not committed yet
# Generated far-tile files kept per server, by layout: a 64-cell tile is ~3.7x the bytes (348 KB)
# and a pose needs ~300 tiles instead of ~811 (docs/V1_4_SPEC.md §3.1).
TILE_CACHE = {32: 6000, 64: 2500}
PREFETCH_START = 200.0      # m (Chebyshev) from the block centre before prefetching
# The mod's terrain-follow rule (mod follow.lua): it asks once the player is more than
# FOLLOW_PREPARE (Chebyshev) from the block centre, for snap256(player + v * FOLLOW_LEAD),
# clamped so the player stays within FOLLOW_REACH of the new centre on both axes
# (1020 - 600 m margin, §1.2; with the 2046 m collidable width of §12 P1 the N/E edge is at
# +1022, so this keeps >= 602 m there). tests/test_protocol_agreement.py checks these
# against follow.lua.
FOLLOW_PREPARE = 300.0
FOLLOW_LEAD = 4.0
FOLLOW_REACH = 420.0
FRAME_S = 0.05              # the mod decides per frame, so it asks just past the threshold
# |cx|,|cy| of an anchor's first terrain. A rebase hint stays within ~512 m of the new origin, but
# a resync (same origin, after a server restart) asks for the block around the player, which can be
# up to the mod's maximum rebase distance (16 km) away, plus however far the player got past it.
# Clamping that to ±768 m made the mod reject every answer as stale and re-request forever.
ANCHOR_CENTRE_MAX = 21000.0
# A superseded anchor's files are deleted this long after the new anchor's far_done: the mod
# still shows (and deletes, time-sliced) old tiles after far_done, and the engine watches
# loaded files (ge/main.lua onFileChanged), so a shape must not vanish under a live TSStatic.
PRUNE_DELAY = 30.0
# A tile the server swapped out (retired) keeps its files this long: the mod shows it until the swap
# has run there, and with an import backlog (in-game 2026-10-10, ball drive at ~38 m/s: ~1000 swap
# groups waiting) that took minutes; evicting at 30 s deleted shapes under shown tiles, which then
# rendered as nothing (the world went invisible, collision stayed). v1.4 replaces this guess with
# the mod reporting its deletes.
RETIRED_KEEP = 600.0
PRUNE_RECHECK = 2.0          # s; a prune waiting for the mod to let go of an anchor's tiles re-checks this often
# The game watches /temp: deleting ~400 tile files in one burst stalled it for 200-340 ms
# (in-game 2026-10-10, ~30 s after every rebase). Old files are deleted gradually instead.
DELETE_INTERVAL = 0.2           # seconds between file deletions (5 files/s)
# Mesh mode with imagery writes ~5x as many files per km (64/128 m tiles and a texture per tile:
# ~400 tiles = 800 files per km; a 10 km anchor leaves ~11k), more than 5 files/s can ever delete.
# There the queue drains faster when it is long, still one file at a time and evenly spaced:
# rate = backlog / DELETE_DRAIN_S, between 5 and DELETE_MAX_RATE files/s.
DELETE_DRAIN_S = 60.0
DELETE_MAX_RATE = 40.0
# Anchor height quantum: relief survey of +-SURVEY_HALF around the anchor at DEM zoom SURVEY_ZOOM.
SURVEY_HALF = 15000.0
SURVEY_N = 31
SURVEY_ZOOM = 10
DELTA_MARGIN = 0.1          # m added to measured swap-group deltas (crossings between vertices)
NICE = 10                   # default niceness of the server process (§4.5)
IMAGERY_PREFETCH = 12       # queued tiles whose imagery is fetched ahead of their build
# status.backlog (docs/V1_4_SPEC.md §3.0, §4): every status message carries the number of desired
# tiles of the current anchor not shown yet. Status messages only go out on events (v1.3.1); with
# --status-interval S > 0 the last status is also re-sent from on_pose at most every S seconds
# while the backlog changed, so tools/drive.py can sample it (M0 harness runs use 1).
STATUS_INTERVAL = 0.0
# Shown in the mod's UI (V1_3_SPEC §C), after the imagery credits.
ELEVATION_CREDIT = "Elevation: Mapterhorn (see mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen"

log = logging.getLogger("earthd")


@dataclass
class Anchor:
    id: int
    lat: float
    lon: float
    frame: geodesy.EnuFrame
    hm_zoom: int = 15
    zooms: dict = field(default_factory=dict)      # far level -> DEM zoom (L0 = hm_zoom)
    first_terrain: tuple = None                    # (cx, cy) of the anchor_ready terrain
    q: float = None                                # height quantum, from the relief survey
    q_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class TileInfo:
    """A built far tile (server-wide cache entry)."""
    pos: list
    err: float
    sink: float | None
    skirt: float
    grid: np.ndarray            # (cells + 1)² absolute vertex heights (float32), for swap deltas
    bytes: int
    seconds: float
    material: str | None = None     # per-tile imagery material (None: the static palette material)
    texture: str | None = None      # file name of its texture (next to the shape)
    tex_bytes: int = 0
    tex_seconds: float = 0.0


def predict_centre(px, py, vx, vy, lead, reach=FOLLOW_REACH):
    """snap256(p + v*lead), clamped so p stays within `reach` of the centre on both axes."""
    out = []
    for p, v in ((px, vx), (py, vy)):
        c = heightmap.snap(p + v * lead)
        lo = math.ceil((p - reach) / heightmap.GRID) * heightmap.GRID
        hi = math.floor((p + reach) / heightmap.GRID) * heightmap.GRID
        out.append(float(min(max(c, lo), hi)) + 0.0)
    return tuple(out)


def predict_request(px, py, vx, vy, cx, cy):
    """The centre the mod will ask for (V1_1_SPEC 4 prefetch): where a player moving
    straight at (vx, vy) leaves the FOLLOW_PREPARE box around the block centre, then the
    mod's own target rule there. (Spec 4 suggests snap256(p + v*6 s); predicting the mod's
    exact request instead avoids builds that are never asked for.)"""
    t = 0.0
    if max(abs(px - cx), abs(py - cy)) <= FOLLOW_PREPARE:
        exits = [((c + math.copysign(FOLLOW_PREPARE, v)) - p) / v
                 for p, v, c in ((px, vx, cx), (py, vy, cy)) if abs(v) > 0.5]
        t = min(exits) + FRAME_S if exits else 0.0
    return predict_centre(px + vx * t, py + vy * t, vx, vy, FOLLOW_LEAD)


def _num(msg, name, default=None):
    v = msg.get(name, default)
    if v is None:
        return default
    v = float(v)
    if not math.isfinite(v):
        raise ValueError(f"{name} is not finite")
    return v


def survey_q(frame, dem):
    """Height quantum of an anchor (§1.3): from the relief of a +-15 km survey at DEM z10, the
    smallest ladder step whose block range covers it plus 10 %."""
    xs = np.linspace(-SURVEY_HALF, SURVEY_HALF, SURVEY_N)
    u = heightmap.surface_u(frame, lambda la, lo: dem.sample(la, lo, SURVEY_ZOOM), xs, xs)
    return heightmap.choose_q(float(u.max() - u.min()))


def sat_material(aid, key):
    """Per-tile imagery material name (V1_3_SPEC §A): bngEarthSat_<anchorId>_<key>, with the
    key's minus signs as 'm' so the name is a plain identifier (keys never contain an 'm')."""
    return f"bngEarthSat_{aid}_{key.replace('-', 'm')}"


class Server:
    def __init__(self, user_dir, cache_dir, tile_workers=3, kfun=None,
                 hysteresis=farfield.HYSTERESIS, radius=farfield.RADIUS, far_format="cdae",
                 imagery=imagerymod.DEFAULT_CHAIN, near_visual="mesh", tex_near=TEX_NEAR, tex_far=TEX_FAR,
                 tone=1.0, near_radii=None, status_interval=STATUS_INTERVAL, cells=CELLS_DEFAULT, tex=None,
                 deshadow="off"):
        """kfun: K(L) for every level (default: the layout's K table); near_radii: the min-level
        radii (default: the layout's); cells: 32 (v1.3.1) or 64 (M1, mesh mode only); tex: texture
        px per level from the finest (default: tex_near, tex_near, tex_far with 32 cells,
        farfield.TEX_DEFAULT[64] with 64)."""
        self.sid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        root = os.path.join(user_dir, "temp", "beamngEarth")
        self._clean_old_sessions(root, os.path.join(user_dir, *CDAE_ROOT))
        self.out_dir = os.path.join(root, self.sid)
        self.cdae_dir = os.path.join(user_dir, *CDAE_ROOT, self.sid.lower())
        self.vfs_dir = f"/temp/beamngEarth/{self.sid}"
        os.makedirs(self.out_dir, exist_ok=True)
        open(os.path.join(self.out_dir, SESSION_MARKER), "w").close()
        if far_format not in farfield.FORMATS:
            raise ValueError(f"unknown far-tile format {far_format!r}")
        self.far_format = far_format
        if near_visual not in NEAR_VISUALS:
            raise ValueError(f"unknown near-visual mode {near_visual!r}")
        # mesh: the TerrainBlock is physics only, the ground is drawn by the finest tiles (§B)
        self.near_visual = near_visual
        if tex is None and cells == 32:
            tex = (tex_near, tex_near, tex_far)         # the v1.3.1 rule: the two finest levels are near
        # Every layout-dependent rule reads self.layout (terrain mode: frozen at 32 cells)
        self.layout = farfield.Layout(cells, heightmap.SIZE, near_visual == "mesh", tex)
        self.min_level = self.layout.min_level
        self.near_radii = dict(self.layout.near_radii if near_radii is None else near_radii)
        self.tile_cache = TILE_CACHE[cells]
        self.dem = demmod.Dem(os.path.join(cache_dir, "dem"))
        # Satellite imagery (§A); None = --imagery off (v3-style palette tiles).
        # De-shadowing of the USGS tiles (--deshadow lambert|full; off = the imagery as served).
        self.deshadow = None
        if deshadow != "off" and imagery and deshadowmod.SOURCE in [getattr(s, "name", s) for s in imagery]:
            self.deshadow = deshadowmod.Deshadow(cache_dir, self.dem, deshadow)
        self.imagery = (imagerymod.Imagery(os.path.join(cache_dir, "imagery"), imagery, tone=tone,
                                           deshadow=self.deshadow)
                        if imagery else None)
        self.imagery_failures = 0           # tiles that fell back to the palette (network trouble)
        # Two heightmap workers so a mod request never waits behind a prefetch.
        self.hm_pool = ThreadPoolExecutor(2, thread_name_prefix="heightmap")
        self.tile_pool = ThreadPoolExecutor(tile_workers, thread_name_prefix="far")
        self.tile_workers = tile_workers
        self.kfun, self.hysteresis, self.radius = kfun or self.layout.k, hysteresis, radius
        self.status_interval = status_interval
        self.anchors = {}
        self._next_anchor = 1
        self.connections = set()
        # Heightmaps: (anchorId, cx, cy) -> info, LRU order.
        self.hm = OrderedDict()
        self.hm_inflight = {}
        self.hm_committed = deque(maxlen=HM_KEEP_COMMITTED)
        self.hm_sent = deque(maxlen=HM_KEEP_SENT)
        # Far tiles: (anchorId, key) -> TileInfo, LRU order; files are far_<anchorId>_<key>.<fmt>.
        self.tiles = OrderedDict()
        self.tile_inflight = {}
        self.build_stats = deque(maxlen=500)   # seconds per built tile, for the log
        self.level_stats = {}                  # level -> deque of (build s, texture s, shape B, texture B)
        self.prune_delay = PRUNE_DELAY
        self.prune_recheck = PRUNE_RECHECK
        self.retired_keep = RETIRED_KEEP
        self.delete_interval = DELETE_INTERVAL
        self.delete_max_rate = DELETE_MAX_RATE if near_visual == "mesh" else 1.0 / DELETE_INTERVAL
        self.delete_queue = deque()     # (path, tile key or None) waiting for a rate-limited delete
        self._deleting = False
        self._prune_scheduled = set()

    @staticmethod
    def _clean_old_sessions(root, cdae_root):
        """Remove session folders left by earlier runs (only ones we marked),
        and the engine's .cdae caches of their shapes."""
        for d in glob.glob(os.path.join(root, "*", SESSION_MARKER)):
            sid = os.path.basename(os.path.dirname(d))
            shutil.rmtree(os.path.dirname(d), ignore_errors=True)
            shutil.rmtree(os.path.join(cdae_root, sid.lower()), ignore_errors=True)

    def new_anchor(self, lat, lon):
        a = Anchor(self._next_anchor, lat, lon, geodesy.EnuFrame(lat, lon))
        a.hm_zoom = demmod.zoom_for(lat, 2.5)       # ~2 m pixels where the data exists
        a.zooms = {lvl: self.layout.dem_zoom(lat, lvl) for lvl in range(1, farfield.MAX_LEVEL + 1)}
        a.zooms[0] = a.hm_zoom                      # L0 shares the terrain's DEM (§1.4)
        if self.deshadow is not None:
            self.deshadow.prepare(lat, lon)         # the regions around it, in the background
        self._next_anchor += 1
        return a

    def anchor_q(self, anchor):
        """The anchor's height quantum, surveyed once (worker threads)."""
        with anchor.q_lock:
            if anchor.q is None:
                anchor.q = survey_q(anchor.frame, self.dem)
                log.info("anchor %d height quantum 1/%d m", anchor.id, round(1 / anchor.q))
            return anchor.q

    @staticmethod
    def _remove(path):
        for p in (path, path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def delete_gap(self):
        """Seconds until the next queued delete: delete_interval, or shorter for a long queue
        (up to delete_max_rate files/s, see DELETE_DRAIN_S)."""
        rate = min(self.delete_max_rate, len(self.delete_queue) / DELETE_DRAIN_S)
        return min(self.delete_interval, 1.0 / rate) if rate > 0 else self.delete_interval

    def _delete_one(self):
        path, ck = self.delete_queue.popleft()
        # an evicted tile built again since (same file name) keeps its new file
        if ck is None or (ck not in self.tiles and ck not in self.tile_inflight):
            self._remove(path)

    def queue_delete(self, paths, ck=None):
        """Delete files gradually (one at a time, delete_gap() apart) so the game's file watcher
        never sees a burst. ck: the far tile (anchorId, key) the files belong to, if it may be
        built again meanwhile (LRU eviction): then they are left alone. Needs a running event
        loop; falls back to deleting at once."""
        self.delete_queue.extend((p, ck) for p in paths)
        if self._deleting:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            while self.delete_queue:
                self._delete_one()
            return
        self._deleting = True

        def step():
            if self.delete_queue:
                self._delete_one()
            if self.delete_queue:
                loop.call_later(self.delete_gap(), step)
            else:
                self._deleting = False
        loop.call_later(self.delete_gap(), step)

    # --- heightmaps ------------------------------------------------------
    def hm_name(self, key):
        aid, cx, cy = key
        return f"hm_{aid}_{int(cx)}_{int(cy)}.png"

    def hm_vfs(self, key):
        return f"{self.vfs_dir}/{self.hm_name(key)}"

    def make_heightmap(self, anchor, cx, cy, progress):
        """Worker thread: build and write one 1024² terrain."""
        t0 = time.perf_counter()
        q = self.anchor_q(anchor)
        info = heightmap.generate(anchor.frame,
                                  lambda la, lo: self.dem.sample(la, lo, anchor.hm_zoom, progress),
                                  os.path.join(self.out_dir, self.hm_name((anchor.id, cx, cy))), cx, cy, q=q,
                                  size=self.layout.block)
        info["seconds"] = time.perf_counter() - t0
        info["zoom"] = anchor.hm_zoom
        return info

    def terrain_cached(self, key):
        return key in self.hm or key in self.hm_inflight

    async def terrain(self, anchor, cx, cy, progress=None):
        """Info of the (anchorId, cx, cy) terrain, generating it once (deduplicated)."""
        key = (anchor.id, cx, cy)
        if key in self.hm:
            self.hm.move_to_end(key)
            return self.hm[key]
        fut = self.hm_inflight.get(key)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = self.hm_inflight[key] = loop.run_in_executor(
                self.hm_pool, self.make_heightmap, anchor, cx, cy, progress)
            fut.add_done_callback(lambda f: self._hm_done(key, f))
        return await asyncio.shield(fut)

    def _hm_done(self, key, fut):
        self.hm_inflight.pop(key, None)
        if fut.cancelled() or fut.exception() is not None:
            return
        info = fut.result()
        log.info("terrain %s z%d in %.2f s: zBase %.0f maxHeight %.0f (Q 1/%d)", key, info["zoom"],
                 info["seconds"], info["zBase"], info["maxHeight"], round(1 / info["q"]))
        self.hm[key] = info
        self.evict_heightmaps(fresh=key)

    def evict_heightmaps(self, fresh=None):
        """Keep at most HM_CACHE entries, never deleting the current terrain, the
        last two committed ones, the last two handed to the mod, or `fresh` (the terrain
        just built: a request about to be answered, or the prefetch the mod asks for next)."""
        keep = set(self.hm_committed) | set(self.hm_sent)
        if fresh is not None:
            keep.add(fresh)
        # The first terrain of an anchor no connection has committed yet: anchor_ready
        # handed it out and the rebase may commit it only after more terrain-follow requests
        # in the old frame. (Once committed it is in hm_committed while it matters; keeping
        # every anchor's first terrain made the protected set larger than HM_CACHE after a
        # rebase, so each new prefetch was deleted as soon as it was built, and rebuilt on
        # every pose.)
        newest = max((c.active for c in self.connections if c.active is not None), default=None)
        keep |= {(aid, *a.first_terrain) for aid, a in sorted(self.anchors.items())[-2:]
                 if a.first_terrain is not None and (newest is None or aid > newest)}
        for key in [k for k in self.hm if k not in keep][:max(0, len(self.hm) - HM_CACHE)]:
            del self.hm[key]
            self._remove(os.path.join(self.out_dir, self.hm_name(key)))

    # --- far tiles -------------------------------------------------------
    def tile_name(self, aid, key):
        return f"far_{aid}_{key}.{self.far_format}"

    def attribution(self):
        """The credit line the mod shows: imagery (if on) and elevation sources."""
        parts = [self.imagery.attribution()] if self.imagery is not None else []
        return "; ".join(parts + [ELEVATION_CREDIT])

    def level_summary(self):
        """'L<level> <geometry ms>/<texture ms> <shape KB>/<texture KB>' medians, for the log."""
        out = []
        for lvl, st in sorted(self.level_stats.items()):
            # list() copies the deque in one step: tile workers append to it meanwhile, and a
            # Python-level iteration over it could raise "deque mutated during iteration" here
            # (in the connection's streamer, which would stop streaming).
            st = list(st)
            med = [sorted(v[k] for v in st)[len(st) // 2] for k in range(4)]
            out.append(f"L{lvl} {1000 * med[0]:.1f}/{1000 * med[1]:.1f} {med[2] / 1024:.0f}/{med[3] / 1024:.0f}")
        return ", ".join(out)

    def texture_name(self, aid, key):
        return f"far_{aid}_{key}.dds"

    def tile_files(self, name):
        """A far tile's files: the shape, its texture and the engine's .cdae cache of it (DAE tiles)."""
        out = [os.path.join(self.out_dir, name), os.path.join(self.out_dir, os.path.splitext(name)[0] + ".dds")]
        if name.endswith(".dae"):
            out.append(os.path.join(self.cdae_dir, os.path.splitext(name)[0].lower() + ".cdae"))
        return out

    def tex_size(self, level):
        """Texture edge of a level (the layout's per-level table: with 32 cells the two finest
        levels of the mode get tex_near, the others tex_far)."""
        return self.layout.tex_size(level)

    def prefetch_imagery(self, anchor, tile):
        """Start downloading a queued tile's imagery (fire and forget), so tile workers rarely
        wait on the network. Only the first source of the chain; errors surface at build time."""
        if self.imagery is not None:
            x0, y0, _, _ = tile.bbox
            px = self.tex_size(tile.level)
            self.imagery.prefetch(anchor.frame, *imagerymod.gutter_square(x0, y0, tile.size, px), px)

    def make_tile(self, anchor, tile):
        """Worker thread: build and write one far tile (and its texture). Returns its TileInfo."""
        t0 = time.perf_counter()
        lvl = tile.level
        q = self.anchor_q(anchor) if lvl <= 0 else None
        ref_zoom = anchor.zooms[lvl - 1] if lvl > 0 else None
        path = os.path.join(self.out_dir, self.tile_name(anchor.id, tile.key))
        built = farfield.build_tile(anchor.frame, self.dem, tile, anchor.zooms[max(lvl, 0)], q,
                                    ref_zoom, with_sink=self.near_visual == "terrain", layout=self.layout)
        t1 = time.perf_counter()
        material = texture = None
        tex_bytes = 0
        if self.imagery is not None:
            x0, y0, _, _ = tile.bbox
            try:
                px = self.tex_size(lvl)
                rgb, _ = self.imagery.render(anchor.frame, *imagerymod.gutter_square(x0, y0, tile.size, px), px)
                texture = self.texture_name(anchor.id, tile.key)
                tex_bytes = imagerymod.write_dds(os.path.join(self.out_dir, texture), rgb)
                material = sat_material(anchor.id, tile.key)
            except imagerymod.ImageryError as e:
                # Network trouble: this tile keeps the palette look (the mod's static material).
                self.imagery_failures += 1
                if self.imagery_failures % 100 == 1:
                    log.warning("tile %s of anchor %d: no imagery (%s), palette material (%d tiles so far)",
                                tile.key, anchor.id, e, self.imagery_failures)
        t2 = time.perf_counter()
        size = farfield.write_built(path, built, self.far_format, material or farfield.MATERIAL,
                                    textured=material is not None, tex_px=self.tex_size(lvl))
        dt = time.perf_counter() - t0
        self.build_stats.append(dt)
        self.level_stats.setdefault(lvl, deque(maxlen=200)).append((dt - (t2 - t1), t2 - t1, size, tex_bytes))
        return TileInfo(built.pos, built.err, built.sink, built.skirt, built.grid, size, dt,
                        material, texture, tex_bytes, t2 - t1)

    def tile(self, anchor, tile):
        """asyncio future for the tile's TileInfo; generated at most once per (anchorId, key)."""
        ck = (anchor.id, tile.key)
        loop = asyncio.get_running_loop()
        if ck in self.tiles:
            self.tiles.move_to_end(ck)
            f = loop.create_future()
            f.set_result(self.tiles[ck])
            return f
        fut = self.tile_inflight.get(ck)
        if fut is None:
            fut = self.tile_inflight[ck] = loop.run_in_executor(self.tile_pool, self.make_tile, anchor, tile)
            fut.add_done_callback(lambda f: self._tile_done(ck, f))
        return fut

    def in_use(self):
        """(anchorId, key) of every tile a connection shows or holds, or let go of less than
        prune_delay ago: after a far_swap / far_remove the mod still shows the tile until the
        swap has run (its adds load within the governor budget, then a 0.5 s crossover) and
        deletes it time-sliced, and a shape file must not vanish under a live TSStatic."""
        out = set()
        now = time.monotonic()
        for c in self.connections:
            out |= set(c.shown)
            out |= {(c.active, k) for k in c.sent}
            # built for the active anchor but not sent yet: evicting these deleted the files of
            # tiles about to be sent (in-game 2026-10-10, after a 200 mph run had left thousands
            # of held previous-anchor tiles in the cache: 150 tiles failed with "No Mesh")
            out |= {(c.active, k) for k in c.desired}
            out |= set(c.inflight)
            out |= {ck for ck, t in c.retired.items() if now - t < self.retired_keep}
        held = self.held_anchors()
        if held:
            out |= {ck for ck in self.tiles if ck[0] in held}
        return out

    def held_anchors(self):
        """Previous anchors whose tile objects some mod still has (pose.held): their files stay."""
        out = set()
        for c in self.connections:
            out |= c.held
        return out

    def _tile_done(self, ck, fut):
        self.tile_inflight.pop(ck, None)
        if fut.cancelled() or fut.exception() is not None:
            return
        self.tiles[ck] = fut.result()
        if len(self.tiles) > self.tile_cache:
            used = self.in_use()
            for old in [k for k in self.tiles if k not in used][:len(self.tiles) - self.tile_cache]:
                del self.tiles[old]
                # through the rate-limited queue: the mod loaded most of these files once (P9)
                self.queue_delete((p for p in self.tile_files(self.tile_name(*old)) if os.path.exists(p)), old)

    def schedule_prune(self, keep_from):
        """prune(keep_from) after prune_delay seconds (once per keep_from)."""
        if keep_from in self._prune_scheduled or not any(a < keep_from for a in self.anchors):
            return
        self._prune_scheduled.add(keep_from)

        loop = asyncio.get_running_loop()

        def run():
            # the mod may still show a previous anchor's tiles long after far_done (it applies
            # far_removes only once every tile received before them is shown; a rebase reload
            # can take minutes at speed): a file deleted under a live TSStatic gets reloaded
            if any(a < keep_from for a in self.held_anchors()):
                loop.call_later(self.prune_recheck, run)
                return
            self._prune_scheduled.discard(keep_from)
            self.prune(keep_from)
        loop.call_later(self.prune_delay, run)

    def prune(self, keep_from):
        """Forget anchors older than keep_from and delete their files."""
        for aid in [a for a in self.anchors if a < keep_from]:
            del self.anchors[aid]
            for ck in [k for k in self.tiles if k[0] == aid]:
                del self.tiles[ck]
            for fut in [f for k, f in self.tile_inflight.items() if k[0] == aid]:
                fut.cancel()
            for key in [k for k in self.hm if k[0] == aid]:
                if key not in self.hm_committed and key not in self.hm_sent:
                    del self.hm[key]
            keep = {self.hm_name(k) for k in self.hm}
            doomed = []
            for p in glob.glob(os.path.join(self.out_dir, f"far_{aid}_*")):
                name = os.path.basename(p)
                doomed.append(p)
                if name.endswith(".dae"):
                    doomed.append(os.path.join(self.cdae_dir, os.path.splitext(name)[0].lower() + ".cdae"))
            doomed += glob.glob(os.path.join(self.cdae_dir, f"far_{aid}_*"))  # caches whose source went earlier
            doomed += [p for p in glob.glob(os.path.join(self.out_dir, f"hm_{aid}_*"))
                       if os.path.basename(p) not in keep]
            self.queue_delete(p for p in dict.fromkeys(doomed) if os.path.exists(p))


class Connection:
    """One mod connection. Far-field state for the active anchor A:
        desired  D: the selected tiles (farfield.select)
        shown    C: tiles of executed swaps, (anchorId, key) -> bbox in A's frame; also holds
                    previous anchors' tiles after a rebase until they are covered
        sent     S: far_tile sent but not yet part of a far_swap (the mod keeps them hidden)
        groups:     farfield.swap_groups(C, D), recomputed whenever D changes
    A group is emitted (far_swap) once all its adds are sent; C then becomes C - remove + add.
    Sent tiles that no group needs any more are evicted with far_remove."""

    def __init__(self, server, reader, writer):
        self.s = server
        self.reader, self.writer = reader, writer
        self.loop = asyncio.get_running_loop()
        self.peer = writer.get_extra_info("peername")
        self.tasks = set()
        self.active = None              # committed anchor id
        self.block = None               # farfield.Block of the committed terrain
        self.pending_block = None       # the last prepare_terrain of the active anchor
        self.predicted_block = None     # the terrain the prefetch expects next
        self.focus = (0.0, 0.0)         # player x, y in the active frame
        self.vel = (0.0, 0.0)
        self.cam = None                 # pose.cam (protocol 3), kept for diagnostics
        self.desired = {}               # key -> Tile (active anchor)
        self.refined = set()            # nodes refined by the last selection (hysteresis)
        self.node_changed = {}          # node -> monotonic time of its last state change
        self.shown = {}
        self.sent = {}                  # key -> Tile (active anchor)
        self.retired = {}               # (anchorId, key) -> monotonic time it left shown / sent
        self.held = set()               # pose.held: previous anchors whose tile objects the mod still has
        self.groups = []                # [(adds, removes)] still to emit
        self.queue = []                 # tiles to build, by priority
        self.qi = 0
        self.next_group = 1
        self.inflight = {}              # (anchorId, key) -> (asyncio future, Tile)
        self.failed = set()
        self.dirty = False              # far_done owed once every group is emitted
        self.batch = [0.0, 0, 0]        # [start, tiles sent, swaps sent] of the current change, for the log
        self.prefetching = False
        self.wake = asyncio.Event()
        self.streamer = None
        self.last_status = ""           # text of the last status message (re-sent with a new backlog)
        self.status_sent = (0.0, None)  # (monotonic time, backlog) of the last status message

    def send(self, msg):
        if not self.writer.is_closing():
            self.writer.write(protocol.encode(msg))

    def backlog(self):
        """status.backlog: desired tiles of the current anchor that are not shown yet (V1_4_SPEC §4)."""
        aid = self.active
        if aid is None:
            return 0
        return sum(1 for k in self.desired if (aid, k) not in self.shown)

    def status(self, text):
        n = self.backlog()
        self.last_status, self.status_sent = text, (time.monotonic(), n)
        self.send({"type": "status", "text": text, "backlog": n})

    def refresh_status(self):
        """--status-interval: re-send the last status when the backlog changed, at most every
        status_interval seconds (off by default: v1.3.1 sends status on events only)."""
        iv = self.s.status_interval
        if iv <= 0 or time.monotonic() - self.status_sent[0] < iv:
            return
        if self.backlog() != self.status_sent[1]:
            self.status(self.last_status)

    async def run(self):
        log.info("client connected %s", self.peer)
        self.s.connections.add(self)
        self.streamer = asyncio.create_task(self.stream())
        try:
            while True:
                msg = await protocol.read_message(self.reader)
                if msg is None:
                    break
                try:
                    self.dispatch(msg)
                except (KeyError, TypeError, ValueError) as e:  # a malformed field must not drop the connection
                    log.warning("client %s: bad %r message: %s: %s", self.peer, msg.get("type"), type(e).__name__, e)
                except Exception:
                    log.exception("client %s: message %r failed", self.peer, msg.get("type"))
                await self.writer.drain()
        except (protocol.ProtocolError, ConnectionError) as e:
            log.warning("client %s: %s", self.peer, e)
        finally:
            self.s.connections.discard(self)
            self.streamer.cancel()
            for t in list(self.tasks):
                t.cancel()
            self.writer.close()
            log.info("client disconnected %s", self.peer)

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def dispatch(self, msg):
        kind = msg["type"]
        if kind == "hello":
            lay = self.s.layout
            fmt = msg.get("format", lay.format)
            if msg.get("protocol") != PROTOCOL:
                self.status(f"protocol mismatch: mod {msg.get('protocol')}, server {PROTOCOL}")
            elif not isinstance(fmt, (int, float)) or fmt < lay.format:
                # the mod's format is the newest it reads (a FORMAT 4 mod reads 3 too)
                self.status(f"format mismatch: mod {msg.get('format')}, server {lay.format}")
            m = {"type": "hello", "protocol": PROTOCOL, "format": lay.format, "serverVersion": SERVER_VERSION,
                 "sessionId": self.s.sid, "farFormat": self.s.far_format, "material": farfield.MATERIAL,
                 "nearVisual": self.s.near_visual, "attribution": self.s.attribution()}
            if lay.format >= 4:         # M1: the tiles' cell count (crossover.defaultErr, the exact level)
                m["cells"] = lay.cells
            self.send(m)
        elif kind == "prepare_anchor":
            self.spawn(self.prepare_anchor(msg))
        elif kind == "prepare_terrain":
            self.spawn(self.prepare_terrain(msg))
        elif kind == "commit_anchor":
            self.commit_anchor(msg.get("anchorId"))
        elif kind == "commit_terrain":
            self.commit_terrain(msg)
        elif kind == "pose":
            self.on_pose(msg)
        else:
            log.debug("ignoring message type %r", kind)

    # --- anchors and terrain ------------------------------------------------
    def _progress(self):
        last = [0.0]

        def progress(done, total):      # called from DEM worker threads
            now = time.monotonic()
            if done == total or now - last[0] > 0.25:
                last[0] = now
                self.loop.call_soon_threadsafe(self.status, f"Elevation tiles {done}/{total}")
        return progress

    def terrain_msg(self, key, info):
        return {"cx": key[1], "cy": key[2], "heightmap": self.s.hm_vfs(key), "size": self.s.layout.block,
                "squareSize": heightmap.SQUARE, "zBase": info["zBase"], "maxHeight": info["maxHeight"],
                "q": info["q"]}

    async def prepare_anchor(self, msg):
        req = msg.get("requestId")
        try:
            lat, lon = float(msg["lat"]), float(msg["lon"])
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90):
                raise ValueError
            lim = ANCHOR_CENTRE_MAX
            cx = min(max(heightmap.snap(_num(msg, "cx", 0.0)), -lim), lim)
            cy = min(max(heightmap.snap(_num(msg, "cy", 0.0)), -lim), lim)
        except (KeyError, TypeError, ValueError):
            self.send({"type": "anchor_error", "requestId": req, "error": "invalid lat/lon/cx/cy"})
            return
        lon = (lon + 180.0) % 360.0 - 180.0
        anchor = self.s.new_anchor(lat, lon)
        self.status(f"Building terrain at {lat:.5f}, {lon:.5f}...")
        t0 = time.perf_counter()
        try:
            info = await self.s.terrain(anchor, cx, cy, self._progress())
        except Exception as e:
            log.exception("terrain for anchor request %s failed", req)
            self.send({"type": "anchor_error", "requestId": req, "error": f"{type(e).__name__}: {e}"})
            return
        key = (anchor.id, cx, cy)
        anchor.first_terrain = (cx, cy)
        self.s.anchors[anchor.id] = anchor
        self.s.hm_sent.append(key)
        # groundZ is None when (0, 0) lies outside the block (a resync around a distant player)
        gz = info["groundZ"]
        log.info("anchor %d (%.5f, %.5f) ready in %.2f s, terrain at (%.0f, %.0f), groundZ %s; dem %s",
                 anchor.id, lat, lon, time.perf_counter() - t0, cx, cy,
                 "n/a" if gz is None else f"{gz:.1f}",
                 self.s.dem.stats_snapshot())
        self.send({"type": "anchor_ready", "requestId": req, "anchorId": anchor.id, "lat": lat, "lon": lon,
                   "groundZ": info["groundZ"], "terrain": self.terrain_msg(key, info)})
        self.status(f"Terrain ready ({time.perf_counter() - t0:.1f} s)")

    async def prepare_terrain(self, msg):
        req = msg.get("requestId")
        try:
            aid = msg["anchorId"]
            anchor = self.s.anchors[aid]
            cx, cy = heightmap.snap(_num(msg, "cx")), heightmap.snap(_num(msg, "cy"))
        except (KeyError, TypeError, ValueError) as e:
            self.send({"type": "terrain_error", "requestId": req, "anchorId": msg.get("anchorId"),
                       "error": f"invalid request: {type(e).__name__} {e}"})
            return
        if aid == self.active and farfield.Block(cx, cy) != self.block:
            # The pending block's L0 tiles stream first, so the commit never waits (§2.3).
            self.pending_block = farfield.Block(cx, cy)
            self.update_far()
        t0 = time.perf_counter()
        try:
            info = await self.s.terrain(anchor, cx, cy)
        except Exception as e:
            log.exception("terrain request %s failed", req)
            self.send({"type": "terrain_error", "requestId": req, "anchorId": aid,
                       "error": f"{type(e).__name__}: {e}"})
            if aid == self.active and self.pending_block == farfield.Block(cx, cy):
                self.pending_block = None   # the mod will not commit it: stop forcing L0 there
                self.update_far()
            return
        key = (aid, cx, cy)
        self.s.hm_sent.append(key)
        log.info("terrain_ready %s after %.0f ms", key, (time.perf_counter() - t0) * 1000)
        self.send({"type": "terrain_ready", "requestId": req, "anchorId": aid, **self.terrain_msg(key, info)})

    def commit_anchor(self, aid):
        if aid == self.active:
            return
        anchor = self.s.anchors.get(aid)
        if anchor is None:
            log.warning("commit of unknown anchor %r ignored", aid)
            return
        old = self.s.anchors.get(self.active)
        if old is not None:
            # Tiles of the old anchor stay visible (the mod moves them); track their
            # footprints in the new frame so they are removed once covered.
            self.shown = {k: farfield.transform_bbox(old.frame, anchor.frame, bb) for k, bb in self.shown.items()}
            fx, fy, _ = anchor.frame.ecef_to_enu(*old.frame.enu_to_ecef(*self.focus, 0.0))
            self.focus = (float(fx), float(fy))
            if anchor.first_terrain is not None:
                bx0, by0, bx1, by1 = farfield.Block(*anchor.first_terrain).bbox
                if not (bx0 <= fx <= bx1 and by0 <= fy <= by1):
                    # Not a rebase but a teleport: the mod places the player on the new
                    # anchor's first terrain, and its next pose may come only after that.
                    self.focus, self.vel = (float(anchor.first_terrain[0]), float(anchor.first_terrain[1])), (0.0, 0.0)
        else:
            self.shown = {k: bb for k, bb in self.shown.items() if k[0] in self.s.anchors}
            # No previous frame to map the player from (first anchor, or a resync after a server
            # restart, whose terrain is around the player): start at the anchor's first terrain.
            self.focus = tuple(float(v) for v in anchor.first_terrain) if anchor.first_terrain else (0.0, 0.0)
            self.vel = (0.0, 0.0)
        for ck, (fut, _) in self.inflight.items():
            if ck[0] != aid:
                fut.cancel()            # only stops jobs that have not started
        # Tiles sent but not swapped in are dropped by the mod at the anchor switch.
        self._retire((self.active, k) for k in self.sent)
        self.active = aid
        self.desired, self.sent, self.groups, self.queue, self.qi = {}, {}, [], [], 0
        self.refined, self.node_changed = set(), {}
        self.failed.clear()
        self.block = self.pending_block = self.predicted_block = None
        if anchor.first_terrain is not None:
            self._set_block(aid, *anchor.first_terrain)
        log.info("commit anchor %d", aid)
        self.update_far()

    def _set_block(self, aid, cx, cy):
        self.block = farfield.Block(cx, cy)
        if self.pending_block == self.block:
            self.pending_block = None
        key = (aid, cx, cy)
        if key in self.s.hm_committed:
            self.s.hm_committed.remove(key)
        self.s.hm_committed.append(key)
        self.s.evict_heightmaps()

    def commit_terrain(self, msg):
        aid = msg.get("anchorId")
        cx, cy = heightmap.snap(_num(msg, "cx")), heightmap.snap(_num(msg, "cy"))
        if aid != self.active:
            if aid in self.s.anchors and (self.active is None or aid > self.active):
                self.commit_anchor(aid)
            else:
                log.warning("commit_terrain for anchor %r (active %r) ignored", aid, self.active)
                return
        if self.block == farfield.Block(cx, cy):
            return
        # Far-tile keys do not depend on the block; only the forced L0 area moves.
        self._set_block(aid, cx, cy)
        self.update_far()

    def on_pose(self, msg):
        held = msg.get("held")
        if isinstance(held, dict):      # an empty Lua table may encode as {}
            held = list(held.values())
        if isinstance(held, list):
            self.held = {int(a) for a in held if isinstance(a, (int, float)) and not isinstance(a, bool)}
        aid = msg.get("anchorId")
        # The latest committed anchor is the one the mod reports in its pose
        # (ids only grow, so never switch back to an older one).
        if aid in self.s.anchors and (self.active is None or aid > self.active):
            self.commit_anchor(aid)
        x, y = _num(msg, "x", 0.0), _num(msg, "y", 0.0)
        vx, vy = _num(msg, "vx", 0.0), _num(msg, "vy", 0.0)
        if aid == self.active:
            self.focus = (x, y)
        elif aid in self.s.anchors and self.active in self.s.anchors:
            # A late pose of the previous anchor: move it into the active frame.
            src, dst = self.s.anchors[aid].frame, self.s.anchors[self.active].frame
            fx, fy, _ = dst.ecef_to_enu(*src.enu_to_ecef(x, y, 0.0))
            self.focus = (float(fx), float(fy))
        else:
            return
        self.vel = (vx, vy)
        if isinstance(msg.get("cam"), dict):
            self.cam = msg["cam"]
        self.predicted_block = None
        # The mod asks for its next terrain relative to the block it will have committed by
        # then: the pending one if it asked already (it commits that one first), else the
        # committed one. Predicting from the committed block while a request is pending built
        # terrains nobody asks for whenever the heading changed.
        base = self.pending_block or self.block
        if base is not None:
            px, py = self.focus
            if max(abs(px - base.cx), abs(py - base.cy)) >= PREFETCH_START:
                c = predict_request(px, py, *self.vel, base.cx, base.cy)
                if c != (base.cx, base.cy) and farfield.Block(*c) != self.block:
                    self.predicted_block = farfield.Block(*c)
        self.update_far()
        self.maybe_prefetch()
        self.refresh_status()

    def maybe_prefetch(self):
        """Speculatively build the terrain the mod is about to ask for."""
        anchor = self.s.anchors.get(self.active)
        if anchor is None or self.predicted_block is None or self.prefetching:
            return
        cx, cy = self.predicted_block.cx, self.predicted_block.cy
        if self.s.terrain_cached((anchor.id, cx, cy)):
            return
        self.prefetching = True

        async def run():
            try:
                await self.s.terrain(anchor, cx, cy)
                log.debug("prefetched terrain (%d, %.0f, %.0f)", anchor.id, cx, cy)
            except Exception:
                log.exception("terrain prefetch failed")
            finally:
                self.prefetching = False
        self.spawn(run())

    # --- far field ----------------------------------------------------------
    def forced(self):
        """Boxes refined to L0 (terrain mode: the mod covers L0 under the block). None in mesh
        mode: the block is invisible, so tiles never depend on it (V1_3_SPEC §B)."""
        if self.s.near_visual == "mesh":
            return []
        return [b.bbox for b in (self.block, self.pending_block, self.predicted_block) if b is not None]

    def _retire(self, cks):
        now = time.monotonic()
        for ck in cks:
            self.retired[ck] = now

    def update_far(self):
        """Recompute the desired set, the swap groups and the build queue; wake the streamer."""
        if self.active is None:
            return
        now = time.monotonic()
        self.retired = {ck: t for ck, t in self.retired.items() if now - t < self.s.retired_keep}
        self.node_changed = {n: t for n, t in self.node_changed.items() if now - t < farfield.HOLD_S}
        new, refined = farfield.select(*self.focus, self.forced(), self.refined, self.s.kfun,
                                       self.s.hysteresis, self.s.radius, hold=set(self.node_changed),
                                       min_level=self.s.min_level, radii=self.s.near_radii, layout=self.s.layout)
        if self.desired:                # the first selection of an anchor is not a change
            for n in refined ^ self.refined:
                self.node_changed[n] = now
        self.refined = refined
        changed = new.keys() != self.desired.keys()
        self.desired = new
        self._regroup()
        if changed and not self.dirty:
            self.dirty = True
            self.batch = [time.perf_counter(), 0, 0]
        self.wake.set()

    def _regroup(self):
        aid = self.active
        cur = {k for a, k in self.shown if a == aid}
        self.groups = farfield.swap_groups(cur, self.desired)
        # Sent tiles no group needs any more: the mod drops them.
        needed = {k for adds, _ in self.groups for k in adds}
        for k in [k for k in self.sent if k not in needed]:
            del self.sent[k]
            self._retire([(aid, k)])
            self.send({"type": "far_remove", "anchorId": aid, "key": k})
        # Build order: L0 under the committed / pending block (the mod's cover and commit
        # gating need them), then groups that are partly sent, then the rest; nearest first.
        gate = [b.bbox for b in (self.block, self.pending_block) if b is not None and self.s.near_visual == "terrain"]
        started = {k for adds, _ in self.groups if any(a in self.sent for a in adds) for k in adds}
        fx, fy = self.focus
        todo = [self.desired[k] for k in needed if k not in self.sent]

        def prio(t):
            cls = 2
            if t.level == 0 and any(farfield.overlaps(t.bbox, b) for b in gate):
                cls = 0
            elif t.key in started:
                cls = 1
            return (cls, t.distance(fx, fy), t.level, t.key)
        self.queue = sorted(todo, key=prio)
        self.qi = 0
        self.failed.clear()

    def _next_tile(self):
        aid = self.active
        while self.qi < len(self.queue):
            t = self.queue[self.qi]
            self.qi += 1
            ck = (aid, t.key)
            if t.key not in self.sent and ck not in self.inflight and t.key in self.desired:
                return t
        return None

    def emit_groups(self):
        """far_swap every group whose adds are all sent."""
        aid = self.active
        rest = []
        for adds, removes in self.groups:
            if not all(k in self.sent for k in adds):
                rest.append((adds, removes))
                continue
            info = self.s.tiles
            try:
                d_in, d_out = farfield.group_deltas(
                    ([(farfield.tile_of(k), info[(aid, k)].grid) for k in adds],
                     [(farfield.tile_of(k), info[(aid, k)].grid) for k in removes]))
                d_in, d_out = d_in + DELTA_MARGIN, d_out + DELTA_MARGIN
            except KeyError:            # a tile record was evicted: fall back to the err bound
                e = [info[(aid, k)].err for k in adds + removes if (aid, k) in info]
                d_in = d_out = 1.2 * sum(e) + 0.2 if e else 0.0
            gid = self.next_group
            self.next_group += 1
            self.send({"type": "far_swap", "anchorId": aid, "id": gid, "add": adds, "remove": removes,
                       "dIn": round(d_in, 2), "dOut": round(d_out, 2)})
            for k in removes:
                self.shown.pop((aid, k), None)
            self._retire((aid, k) for k in removes)
            for k in adds:
                self.shown[(aid, k)] = self.sent.pop(k).bbox
            self.batch[2] += 1
        self.groups = rest

    def flush_old(self):
        """far_remove every shown tile of a previous anchor once no desired tile that is not
        swapped in yet overlaps it (so nothing ever leaves a hole)."""
        aid = self.active
        cands = [ck for ck in self.shown if ck[0] != aid]
        if not cands:
            return
        pending = [t.bbox for k, t in self.desired.items() if (aid, k) not in self.shown]
        if pending:
            c = np.array([self.shown[ck] for ck in cands])
            u = np.array(pending)
            blocked = ((c[:, None, 0] < u[None, :, 2]) & (u[None, :, 0] < c[:, None, 2]) &
                       (c[:, None, 1] < u[None, :, 3]) & (u[None, :, 1] < c[:, None, 3])).any(1)
        else:
            blocked = [False] * len(cands)
        for ck, b in zip(cands, blocked):
            if not b:
                del self.shown[ck]
                self._retire([ck])
                self.send({"type": "far_remove", "anchorId": ck[0], "key": ck[1]})

    def _blocked(self):
        """Groups that cannot complete now (an add failed to build)."""
        return [g for g in self.groups if any(k in self.failed for k in g[0])]

    async def stream(self):
        try:
            await self._stream()
        except ConnectionError:
            pass
        except asyncio.CancelledError:
            for fut, _ in self.inflight.values():
                fut.cancel()
            raise

    async def _stream(self):
        """Build the needed tiles by priority, send them, emit swap groups as they complete;
        one task per connection."""
        waker = None
        while True:
            # Clear before reading the state, so an update from here on wakes us again.
            self.wake.clear()
            anchor = self.s.anchors.get(self.active)
            while anchor is not None and len(self.inflight) < self.s.tile_workers:
                t = self._next_tile()
                if t is None:
                    break
                self.inflight[(anchor.id, t.key)] = (self.s.tile(anchor, t), t)
            if anchor is not None and self.s.imagery is not None:
                for t in self.queue[self.qi:self.qi + IMAGERY_PREFETCH]:
                    if (anchor.id, t.key) not in self.s.tiles:
                        self.s.prefetch_imagery(anchor, t)
            if anchor is not None:
                self.emit_groups()
                self.flush_old()
            if (not self.inflight and self.dirty and anchor is not None
                    and len(self.groups) == len(self._blocked())):
                self.dirty = False
                n = sum(1 for k in self.desired if (anchor.id, k) in self.shown)
                self.send({"type": "far_done", "anchorId": anchor.id, "count": n})
                self.status(f"Far field ready ({n} tiles)")
                bs = sorted(self.s.build_stats)
                log.info("anchor %d far field done: %d tiles (%d sent, %d swaps in %.2f s; build median %.1f ms)",
                         anchor.id, n, self.batch[1], self.batch[2], time.perf_counter() - self.batch[0],
                         1000 * bs[len(bs) // 2] if bs else 0.0)
                log.info("tile build ms (geometry+write / texture) and KB (shape / texture) by level: %s",
                         self.s.level_summary())
                if self.s.imagery is not None:
                    log.info("imagery: %s", self.s.imagery.summary())
                if not any(ck[0] != anchor.id for ck in self.shown):
                    self.s.schedule_prune(anchor.id)
            await self.writer.drain()
            if waker is None:
                waker = asyncio.ensure_future(self.wake.wait())
            futs = [f for f, _ in self.inflight.values()]
            done, _ = await asyncio.wait(futs + [waker], return_when=asyncio.FIRST_COMPLETED)
            if waker in done:
                waker = None
            for ck in [ck for ck, (f, _) in self.inflight.items() if f.done()]:
                fut, t = self.inflight.pop(ck)
                if fut.cancelled():
                    continue
                if fut.exception() is not None:
                    log.error("far tile %s of anchor %d failed: %r", t.key, ck[0], fut.exception())
                    self.failed.add(t.key)
                    continue
                if ck[0] != self.active or t.key not in self.desired or ck in self.shown or t.key in self.sent:
                    continue            # superseded meanwhile; the file stays cached
                info = fut.result()
                self.sent[t.key] = t
                self.batch[1] += 1
                m = {"type": "far_tile", "anchorId": ck[0], "key": t.key, "level": t.level,
                     "shape": f"{self.s.vfs_dir}/{self.s.tile_name(*ck)}",
                     "pos": [round(v, 3) for v in info.pos], "err": round(info.err, 3),
                     "skirt": round(info.skirt, 2), "state": "group"}
                if info.sink is not None:
                    m["sink"] = round(info.sink, 2)
                if info.material is not None:   # protocol 4: the tile's own imagery material
                    m["material"] = info.material
                    m["texture"] = f"{self.s.vfs_dir}/{info.texture}"
                    m["detail"] = True          # the shape has UV1 (world-locked detail map)
                    if self.s.layout.format >= 4:   # M1: texture px, the mod's governor unit kind
                        m["tex"] = self.s.tex_size(t.level)
                self.send(m)
                if self.batch[1] % 50 == 0:
                    left = sum(1 for g in self.groups for k in g[0] if k not in self.sent)
                    # this change's tiles (sent / sent + still to send), not the whole far field
                    self.status(f"Far field loading: {self.batch[1]}/{left + self.batch[1]} tiles")


async def main_async(args):
    fmt = args.far_format
    if fmt == "cdae":
        v = cdae.engine_version(args.user_dir)
        if v is not None and v != cdae.VERSION_WORD:
            log.warning("the game writes shape version 0x%08x, this server 0x%08x: using DAE far tiles",
                        v, cdae.VERSION_WORD)
            fmt = "dae"
    kfun = None if args.lod_k is None else (lambda lvl: args.lod_k)
    radii = None
    if args.near_radii is not None:
        ml = farfield.MESH_MIN_LEVEL[args.cells]
        radii = {ml: args.near_radii[0], ml + 1: args.near_radii[1]}
    server = Server(args.user_dir, args.cache_dir, args.workers, kfun, args.hysteresis, args.far_radius, fmt,
                    imagery=args.imagery, near_visual=args.near_visual, tex_near=args.tex_near,
                    tex_far=args.tex_far, tone=args.tone, near_radii=radii,
                    status_interval=args.status_interval, cells=args.cells, tex=args.tex,
                    deshadow=args.deshadow)
    srv = await asyncio.start_server(
        lambda r, w: Connection(server, r, w).run(), "127.0.0.1", args.port)
    port = srv.sockets[0].getsockname()[1]
    print(f"earthd listening on 127.0.0.1:{port} session {server.sid} -> {server.out_dir} "
          f"(far tiles: {fmt}, near visual: {server.near_visual}, cells: {server.layout.cells} "
          f"(format {server.layout.format}), imagery: {','.join(args.imagery) if args.imagery else 'off'}"
          f"{'' if server.deshadow is None else ', deshadow: ' + server.deshadow.mode})",
          flush=True)
    async with srv:
        await srv.serve_forever()


def parse_tex_px(text):
    """--tex-near / --tex-far: a power of two from 16 to 2048 (render() maps the texture in
    MESH_CELLS equal cells, and the DXT1 mip chain halves it down to 1 x 1)."""
    px = int(text)
    if px < 16 or px > 2048 or px & (px - 1):
        raise argparse.ArgumentTypeError("texture size must be a power of two from 16 to 2048")
    return px


def parse_tex_list(text):
    """--tex: texture px per level from the finest, the last value repeating: '256,256,256,512'."""
    return tuple(parse_tex_px(v) for v in text.split(","))


def parse_radii(text):
    """'150,400' -> (150.0, 400.0): the radii of the exact level and the next one (32 cells: L-2,
    L-1; 64 cells: L-1, L0)."""
    r2, r1 = (float(v) for v in text.split(","))
    if not 0 <= r2 <= r1:
        raise argparse.ArgumentTypeError("need 0 <= exact-level radius <= next-level radius")
    return r2, r1


def main():
    home = os.path.expanduser("~")
    ap = argparse.ArgumentParser(description="BeamNG.Earth companion server")
    ap.add_argument("--port", type=int, default=47613, help="TCP port on 127.0.0.1 (0 = any free port)")
    ap.add_argument("--user-dir", default=os.path.join(home, ".local/share/BeamNG/BeamNG.drive/current"),
                    help="BeamNG user folder; output goes to <user-dir>/temp/beamngEarth/<sessionId>/")
    ap.add_argument("--cache-dir", default=os.path.join(home, ".cache/beamng-earth"),
                    help="download cache root (DEM tiles go to <cache-dir>/dem/)")
    ap.add_argument("--workers", type=int, default=3, help="far-tile worker threads (keep <= 3: the game needs the cores)")
    ap.add_argument("--far-format", choices=farfield.FORMATS, default="cdae",
                    help="far-tile shape format: binary .cdae (default, ~5x faster to load) or Collada .dae")
    ap.add_argument("--lod-k", type=float, default=None,
                    help="far-field LOD: one K for every level (default: K(L1,L2)=3, K(L3+)=2.5)")
    ap.add_argument("--hysteresis", type=float, default=farfield.HYSTERESIS,
                    help="a refined tile stays refined until the player is (1 + h) x its switch distance away")
    ap.add_argument("--far-radius", type=float, default=farfield.RADIUS, help="far-field radius (m)")
    ap.add_argument("--nice", type=int, default=NICE, help="process niceness increment (0 = leave as is)")
    ap.add_argument("--imagery", type=imagerymod.parse_chain, default=imagerymod.DEFAULT_CHAIN,
                    help="satellite imagery source chain, first with data wins: any of usgs,eox2023,eox2016, "
                         "or 'off' for the v1.2 palette look (default: usgs,eox2023,eox2016)")
    ap.add_argument("--near-visual", choices=NEAR_VISUALS, default="mesh",
                    help="mesh: the ground near the player is drawn by 64/128 m tiles and the terrain block is "
                         "physics only (default); terrain: the v1.2 look (the terrain block is drawn)")
    ap.add_argument("--cells", type=int, choices=farfield.CELL_COUNTS, default=None,
                    help="tile cells per edge: 64 = docs/V1_4_SPEC.md M1 (FORMAT 4, exact level L-1; the "
                         "default in mesh mode), 32 = the v1.3.1 layout (FORMAT 3; terrain mode always)")
    ap.add_argument("--tex", type=parse_tex_list, default=None,
                    help="texture size (px) per level from the finest, the last value repeating "
                         "(default: 64 cells 256,256,256,512; 32 cells from --tex-near / --tex-far)")
    ap.add_argument("--tex-near", type=parse_tex_px, default=TEX_NEAR,
                    help="32 cells: texture size (px) of the two finest levels")
    ap.add_argument("--tex-far", type=parse_tex_px, default=TEX_FAR, help="32 cells: texture size (px) of the other levels")
    ap.add_argument("--tone", type=float, default=1.0,
                    help="strength of the imagery tone curve (0 = the imagery as downloaded)")
    ap.add_argument("--deshadow", choices=deshadowmod.MODES, default="off",
                    help="undo the capture sun's shading in the USGS imagery (docs/research/"
                         "2026-10-10-deshadow-prototype.md): lambert = slopes only, full = slopes and cast "
                         "shadows of the terrain; off = the imagery as served (default)")
    ap.add_argument("--near-radii", type=parse_radii, default=None,
                    help="mesh mode: radii in m around the player of the exact level and the next one "
                         "(L-2, L-1 with 32 cells; L-1, L0 with 64; default 150,400)")
    ap.add_argument("--status-interval", type=float, default=STATUS_INTERVAL,
                    help="re-send the last status with the current far-tile backlog at most every S seconds "
                         "while it changes (0 = status on events only, as v1.3.1; tools/drive.py runs use 1)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    args.cells = resolve_cells(args.cells, args.near_visual)
    if args.cells != 32 and args.near_visual != "mesh":
        ap.error("--cells 64 needs --near-visual mesh (terrain mode is frozen at the v1.3.1 layout)")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.nice:
        try:
            os.nice(args.nice)          # all threads: below the game's main and physics threads
        except OSError as e:
            log.warning("could not lower the process priority: %s", e)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
