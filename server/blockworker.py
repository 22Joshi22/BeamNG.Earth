"""Physics-block builds in a process of their own (2026-10-12).

The server is one Python process: its threads share one interpreter lock, so while three far-tile workers
built, a physics block took 3-10x longer than alone (land cover 1.4 s instead of 0.1 s), too long to follow a
car at 58 m/s, where the 512² block moves every 2.2 s. This process builds the blocks the follow and the
prefetch ask for on a core of its own. It keeps its own DEM reader, road plans (grading.Grader per anchor),
street widths, height chunks and land-cover reader over the same disk caches. All of them are deterministic,
so a block built here is the one the server would build (the far tiles, graded in the server process, agree
with it).

init(...) runs once in the process (ProcessPoolExecutor initializer); build(...), base_entry(...) and
far_geometry(...) (the far-tile workers, the same state) are its jobs. warm_q (a multiprocessing queue) carries the road cells to plan ahead of the player: (anchor id, lat,
lon, [mapcells.CellKey]), the latest list replacing the earlier one.
"""
import logging
import os
import queue
import threading
import time

import dem as demmod
import farfield
import geodesy
import grading as gradingmod
import heightmap
import mapdata as mapdatamod
import netfetch
import roadwidth as roadwidthmod
import surface as surfacemod
import terraincache

log = logging.getLogger("earthd.blockworker")

_s = None       # this process's state (init)


class _State:
    def __init__(self, cache_dir, offline, roads, grading, road_widths, surface):
        net = netfetch.Fetcher(offline=offline)
        self.dem = demmod.Dem(os.path.join(cache_dir, "dem"), fetcher=net)
        md = mapdatamod.MapData(cache_dir, fetcher=net) if (roads or surface) else None
        self.md = md if roads else None
        self.grading = bool(grading) and self.md is not None
        self.widths = (roadwidthmod.RoadWidths(
            lambda key: md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings")).get
            if road_widths and self.md is not None else None)
        self.surface = surfacemod.Surface(md) if surface else None
        self.graders = {}           # anchor id -> grading.Grader
        self.chunks = {}            # anchor id -> heightmap.ChunkCache
        self.bridges = {}           # anchor id -> roads.BridgeProfiles (roads_tile)
        self.lock = threading.Lock()
        self.want = []              # (grader, cell key) to plan, most urgent first
        self.cv = threading.Condition()

    def anchor(self, aid, lat, lon):
        """(Grader or None, ChunkCache) of an anchor; only the last few anchors are kept."""
        with self.lock:
            if aid not in self.chunks:
                frame = geodesy.EnuFrame(lat, lon)
                self.chunks[aid] = heightmap.ChunkCache()
                self.graders[aid] = (gradingmod.Grader(
                    frame, self.md, lambda la, lo: self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM), self.widths)
                    if self.grading else None)
                for old in sorted(self.chunks)[:-ANCHORS_KEPT]:
                    del self.chunks[old], self.graders[old]
            return self.graders[aid], self.chunks[aid]


ANCHORS_KEPT = 2


def init(cache_dir, offline, roads, grading, road_widths, surface, warm_q, log_level=logging.INFO):
    global _s
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _s = _State(cache_dir, offline, roads, grading, road_widths, surface)
    threading.Thread(target=_watch_parent, args=(os.getppid(),), daemon=True, name="parent-watch").start()
    threading.Thread(target=_read_warm, args=(warm_q,), daemon=True, name="warm-read").start()
    threading.Thread(target=_warm_loop, daemon=True, name="roads-ahead").start()


def _watch_parent(ppid):
    """Exit with the server: a killed server (SIGTERM) left its block processes running."""
    while os.getppid() == ppid:
        time.sleep(PARENT_POLL_S)
    os._exit(0)


PARENT_POLL_S = 2.0


def _read_warm(warm_q):
    while True:
        try:
            aid, lat, lon, keys = warm_q.get()
        except (EOFError, OSError):
            return
        try:
            while True:         # only the latest list matters
                aid, lat, lon, keys = warm_q.get_nowait()
        except queue.Empty:
            pass
        g, _ = _s.anchor(aid, lat, lon)
        if g is None:
            continue
        with _s.cv:
            _s.want = [(g, k) for k in keys]
            _s.cv.notify()


def _warm_loop():
    while True:
        with _s.cv:
            job = None
            while job is None:
                job = next(((g, k) for g, k in _s.want if not g.planned(k)), None)
                if job is None:
                    _s.cv.wait()
        try:
            t0 = time.perf_counter()
            job[0].warm(job[1])
            log.debug("road plans ahead: %s in %.2f s", job[1], time.perf_counter() - t0)
        except Exception:
            log.exception("road plans ahead for %s", job[1])
            with _s.cv:
                _s.want = [w for w in _s.want if w[1] != job[1]]


def build(aid, lat, lon, hm_zoom, q, cx, cy, size, out_dir, name, ter_name=None):
    """Build and write the block (heightmap PNG name in out_dir, its surface masks beside it, and the
    .ter ter_name if given). Returns the info dict of earthd's make_heightmap (without seconds / zoom /
    base)."""
    t0 = time.perf_counter()
    g, chunks = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    keep = {}
    grade_s = [0.0]

    def elev(la, lo):
        z = _s.dem.sample(la, lo, hm_zoom)
        if g is None:
            return z
        t1 = time.perf_counter()
        z = g.apply(la, lo, z, gradingmod.ROAD_LIFT)
        grade_s[0] += time.perf_counter() - t1
        return z
    info = heightmap.generate(frame, elev, os.path.join(out_dir, name), cx, cy, q=q, size=size, keep=keep,
                              chunks=chunks)
    stages = {"heights": time.perf_counter() - t0, "grading": grade_s[0]}
    idx = None
    if _s.surface is not None:
        t1 = time.perf_counter()
        classes, complete = _s.surface.classes(keep["lat"], keep["lon"])
        idx = surfacemod.classify(classes, keep["u"], heightmap.SQUARE)
        road_feats, roads_complete = _s.surface.roads(keep["lat"], keep["lon"])
        stages["landcover"] = time.perf_counter() - t1
        t1 = time.perf_counter()
        if road_feats:
            idx = surfacemod.paint_roads(idx, keep["lat"], keep["lon"], road_feats, heightmap.SQUARE,
                                         _s.surface.urban, _s.widths)
        stages["roadPaint"] = time.perf_counter() - t1
        info["layers"] = surfacemod.write_masks(idx[::-1], out_dir, name[:-4])
        info["surfaceComplete"] = complete and roads_complete
    if ter_name is not None:
        t1 = time.perf_counter()
        info["ter"] = surfacemod.write_ter(out_dir, ter_name, keep["qgrid"], idx)
        stages["ter"] = time.perf_counter() - t1
    info["stages"] = stages
    return info


def roads_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift):
    """earthd.Server.build_roads_tile here (road-tile pool): the anchor's road tile (tx, ty) built and
    written; returns roads.write_tile's (vfs path, chunks). In the server's process its Python made up
    ~80 % of the main process's busy threads (in-game profile 2026-10-13): the block photos and far
    textures waited for the GIL."""
    import roadmesh as roadmeshmod
    import roads as roadsmod
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    cells = [_s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
             for k in roadsmod.cells_of_tile(frame, tx, ty)]

    def elev(la, lo):           # the physics ground (lifted under roads: grading.ROAD_LIFT)
        z = _s.dem.sample(la, lo, zoom)
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    with _s.lock:
        bridges = _s.bridges.get(aid)
        if bridges is None:
            bridges = _s.bridges[aid] = roadsmod.BridgeProfiles(
                frame, lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"), elev)
            for old in sorted(_s.bridges)[:-ANCHORS_KEPT]:
                del _s.bridges[old]
    walks = []
    urban = _s.surface.urban if _s.surface is not None else None
    chunks = roadsmod.build_tile(frame, tx, ty, cells, elev, urban, walks=walks, bridges=bridges, widths=_s.widths)
    x0, y0, _, _ = roadsmod.tile_bbox(tx, ty)
    mesh = roadmeshmod.build(frame, chunks, elev, (x0, y0, 0.0), lift)
    dmesh = roadmeshmod.deck(chunks, (x0, y0, 0.0))
    return roadsmod.write_tile(out_dir, vfs_dir, aid, tx, ty, chunks, walks if sidewalks else None, mesh, dmesh)


def base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value):
    """terraincache.base_entry here (its BC7 encode is the base photo's main cost)."""
    return terraincache.base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value)


def far_geometry(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout):
    """farfield.build_tile here (far-tile workers: the geometry, graded at levels <= 0 like the block, is
    most of a far tile's cost; its texture is rendered in the server, where the imagery corrections live).
    Returns the Built."""
    g, _ = _s.anchor(aid, lat, lon)
    src = gradingmod.GradedDem(_s.dem, g) if (g is not None and tile.level <= 0) else _s.dem
    return farfield.build_tile(geodesy.EnuFrame(lat, lon), src, tile, zoom, q, ref_zoom, with_sink=with_sink,
                               layout=layout)
