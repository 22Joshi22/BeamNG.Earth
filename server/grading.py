"""Ground graded under roads (docs/ROADMAP.md v1.6; docs/research/2026-10-11-official-road-study.md §3, §5.3):
the physics block and the near mesh tiles sample one graded elevation, so tyres, DecalRoads and the
visible ground agree.

Per road (roads.road_specs; bridges, tunnels and off-ground segments are left out by it):
- along: the centreline height is the DEM sampled every PROFILE_STEP m and smoothed (Gaussian,
  SIGMA_M m; dirt roads DIRT_SIGMA_M), so grades stay real and small bumps go (the official maps:
  |2nd difference| per 10 m 0.02-0.1 m);
- across: flat over the core (the carriageway as a city street would have it, parking included, plus
  SHOULDER_M, or the sidewalk plus SIDEWALK_MARGIN_M on the classes that get sidewalks in towns, so a
  raised sidewalk slab sits on flat ground), then back to the natural ground over a transition of 2x the height difference (a
  1:2 cut or fill slope), clamped to [T_MIN, T_MAX] m;
- several roads at one point (junctions, divided carriageways) blend their targets by inverse distance
  from each centreline (BLEND_R), so they meet in a gradual slope.
The core width never depends on the land cover (it is taken as urban for every built road), and
each map cell's plan is made once per anchor and cached, so the block and the tiles built later from
the same anchor always agree.

GradedDem(dem, grader) is a drop-in for dem.Dem where only sample() is graded; use it for the block
and the levels <= 0 tiles only (coarser tiles sink under the block and cover far more ground).
"""
import logging
import math
import threading
import time

import numpy as np

import mapcells
import roads as roadsmod

log = logging.getLogger("earthd.grading")

VERSION = 1
PROFILE_STEP = 2.0          # m between profile samples
SIGMA_M = 12.0              # m, profile smoothing of built roads
DIRT_SIGMA_M = 5.0          # m, profile smoothing of gravel / dirt roads
SHOULDER_M = 1.0            # m of flat shoulder beyond the carriageway, each side
SIDEWALK_MARGIN_M = 0.5     # m of flat ground beyond a sidewalk (roads.SIDEWALK_CLASSES)
T_MIN, T_MAX = 5.0, 12.0    # m, transition back to the natural ground (5: roads close together meet gently)
SLOPE_RUN = 2.0             # transition = SLOPE_RUN x the height difference (1:2 slope)
VERTEX_STEP = 16.0          # m between the plan's vertices (segments for the grading pass)
BUCKET = 32.0               # m, query-point buckets
PAIR_BATCH = 2_000_000      # (segment, point) pairs graded at once (bounds the temporaries, ~200 MB)
ROAD_LIFT = 0.04            # m the physics ground is raised under roads (see Grader.apply)
BLEND_R = 4.0               # m: roads near each other blend their targets by 1 / (1 + (d / BLEND_R)^2)
PROFILE_ZOOM = 15           # DEM zoom of the profiles (one zoom for every caller)
RETRY_S = 60.0              # s before a cell whose road data failed is asked for again


def smooth_profile(z, step, sigma):
    """Gaussian smoothing of a profile sampled every `step` m (reflected at the ends). Pure."""
    z = np.asarray(z, np.float64)
    if len(z) < 3 or sigma <= 0:
        return z.copy()
    r = int(math.ceil(3 * sigma / step))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) * step / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(z, r, mode="reflect") if len(z) > r else np.pad(z, r, mode="edge")
    return np.convolve(pad, k, mode="valid")


def resample(xy, step):
    """Points every `step` m along a polyline (xy (k, 2)), the last point kept, and their arc lengths."""
    seg = np.hypot(*np.diff(xy, axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] <= 0:
        return xy[:1].copy(), np.zeros(1)
    t = np.r_[np.arange(0.0, s[-1], step), s[-1]]
    return np.stack([np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])], axis=1), t


class Plan:
    """One road's grading: vertices (n, 2) in the frame's ENU metres, their target heights, core half width."""
    __slots__ = ("xy", "z", "half", "bbox")

    def __init__(self, xy, z, half):
        self.xy, self.z, self.half = xy, z, float(half)
        pad = half + T_MAX
        self.bbox = (xy[:, 0].min() - pad, xy[:, 1].min() - pad, xy[:, 0].max() + pad, xy[:, 1].max() + pad)


def plan_road(frame, lonlat, spec, elev):
    """The Plan of one road part (lonlat (k, 2)); elev(lat, lon) -> m at PROFILE_ZOOM. Pure given elev."""
    return plan_roads(frame, [(lonlat, spec)], elev)[0]


def plan_roads(frame, parts, elev):
    """plan_road for many (lonlat, spec) parts with one elev call (its cost is per call). Pure given elev."""
    if not parts:
        return []
    ll = np.concatenate([lonlat for lonlat, _ in parts])
    E, N, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    prof, k = [], 0
    for lonlat, spec in parts:
        m = len(lonlat)
        xy, _ = resample(np.stack([E[k:k + m], N[k:k + m]], axis=1), PROFILE_STEP)
        k += m
        prof.append(xy if len(xy) >= 2 else None)
    pts = [xy for xy in prof if xy is not None]
    if not pts:
        return [None] * len(parts)
    allxy = np.concatenate(pts)
    la, lo, _ = frame.enu_to_geodetic(allxy[:, 0], allxy[:, 1], 0.0)
    zall = np.asarray(elev(la, lo), np.float64)
    out, k = [], 0
    for (lonlat, spec), xy in zip(parts, prof):
        if xy is None:
            out.append(None)
            continue
        z = zall[k:k + len(xy)]
        k += len(xy)
        out.append(_plan(xy, z, spec))
    return out


def _plan(xy, z, spec):
    built = spec.kind in roadsmod.BUILT
    zs = smooth_profile(z, PROFILE_STEP, SIGMA_M if built else DIRT_SIGMA_M)
    # thin to VERTEX_STEP (the profile stays smooth between: linear in the grading pass)
    every = max(1, int(round(VERTEX_STEP / PROFILE_STEP)))
    keep = np.r_[np.arange(0, len(xy) - 1, every), len(xy) - 1]
    # the carriageway as a city street would have it (never from the land cover), or the measured one
    width = max(spec.width, roadsmod.carriageway(spec.cls, spec.kind, True, None, spec.oneway)) if built else spec.width
    return Plan(xy[keep], zs[keep], width / 2 + shoulder(spec))


def shoulder(spec):
    """m of flat ground beyond the carriageway edge, each side. Like the core width, never from the land
    cover: a road class that gets sidewalks in towns keeps a sidewalk-wide verge everywhere."""
    if spec.kind not in roadsmod.BUILT:
        return 0.0
    if spec.cls in roadsmod.SIDEWALK_CLASSES or (spec.cls == "trunk" and (not spec.oneway
                                                                          or roadsmod._arterial(spec))):
        return spec.walk + SIDEWALK_MARGIN_M
    return SHOULDER_M


class Grader:
    """The road plans of one anchor frame, per map cell, made once (worker threads; it fetches the road
    cells it lacks)."""

    def __init__(self, frame, mapdata, elev, widths=None):
        self.frame, self.md, self.elev, self.widths = frame, mapdata, elev, widths
        self.cells = {}
        self.failed = {}        # cell key -> monotonic time of the last failed fetch
        self.lock = threading.Lock()
        self.key_locks = {}     # cell key -> Lock: one thread plans a cell, the others wait for it

    def _cell_plans(self, key):
        with self.lock:
            got = self.cells.get(key)
            kl = self.key_locks.setdefault(key, threading.Lock())
        if got is not None:
            return got
        with kl:
            return self._plan_cell(key)

    def _plan_cell(self, key):
        with self.lock:
            got = self.cells.get(key)
            failed = self.failed.get(key)
        if got is not None:
            return got
        if failed is not None and time.monotonic() - failed < RETRY_S:
            return []               # offline: no block waits on the same failing fetch again
        try:
            feats = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        except Exception as e:      # no road data: ungraded (asked again after RETRY_S)
            log.debug("grading: roads of %s: %s: %s", key, type(e).__name__, e)
            with self.lock:
                self.failed[key] = time.monotonic()
            return []
        plans = []
        if feats is not None and len(feats):
            specs = roadsmod.road_specs(feats, None, self.widths)
            parts, ids = [], []
            for i in range(len(feats)):
                if specs[i] is None:
                    continue
                for ring in roadsmod.ground_parts(feats, i):     # not bridges / tunnels
                    parts.append((np.asarray(ring, np.float64), specs[i]))
                    ids.append(str(feats.ids[i]))
            for fid, p in zip(ids, plan_roads(self.frame, parts, self.elev)):
                if p is not None:
                    plans.append((fid, p))
        with self.lock:
            self.cells.setdefault(key, plans)
            return self.cells[key]

    def planned(self, key):
        """True once cell key's plans exist (made, or failed recently)."""
        with self.lock:
            f = self.failed.get(key)
            return key in self.cells or (f is not None and time.monotonic() - f < RETRY_S)

    def warm(self, key):
        """Make cell key's plans now (worker threads: the lookahead ahead of the player)."""
        self._cell_plans(key)

    def plans(self, lat, lon):
        """The Plans touching the points' area (each road once)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        pad = (T_MAX + 40.0) / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lat)))))
        seen, out = set(), []
        for key in mapcells.cells_for_bbox(float(lat.min()) - pad, float(lon.min()) - padlon,
                                           float(lat.max()) + pad, float(lon.max()) + padlon):
            for fid_part, p in self._cell_plans(key):
                k = (fid_part, p.xy[0, 0], p.xy[0, 1])
                if k not in seen:
                    seen.add(k)
                    out.append(p)
        return out

    def apply(self, lat, lon, base, lift=0.0):
        """base (the ungraded heights at the points) graded under the roads. Same shape as base.
        lift: m added under the roads (tapering with the grading's weight): the physics block and the
        road mesh use ROAD_LIFT, the visible ground tiles 0, so the road mesh lies exactly on the
        ground the tyres touch while the visible ground stays a little below it (no z-fighting;
        2026-10-11 the user saw the tyres sink into a road mesh lifted above the physics ground)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        base = np.asarray(base, np.float64)
        shape = base.shape
        if base.size == 0:
            return base
        plans = self.plans(lat, lon)
        if not plans:
            return base
        e, n, _ = self.frame.geodetic_to_enu(lat.ravel(), lon.ravel(), 0.0)
        return grade_points(np.asarray(e), np.asarray(n), base.ravel(), plans, lift).reshape(shape)


def grade_points(px, py, base, plans, lift=0.0):
    """base at the points (px, py) graded under the plans (see the module doc). Pure.
    All segments at once, in batches of about PAIR_BATCH (segment, point) pairs (a Python loop per
    segment was the server's largest CPU cost while driving, 2026-10-12)."""
    px = np.asarray(px, np.float64)
    py = np.asarray(py, np.float64)
    base = np.asarray(base, np.float64)
    out = base.copy()
    if not len(px):
        return out
    x0, y0, x1, y1 = px.min(), py.min(), px.max(), py.max()
    bx = np.floor((px - x0) / BUCKET).astype(np.int64)
    by = np.floor((py - y0) / BUCKET).astype(np.int64)
    nbx = int(bx.max()) + 1
    key = by * nbx + bx
    order = np.argsort(key, kind="stable")
    sk = key[order]
    # every segment of the plans near the points: ends, end heights, reach (core half width + T_MAX)
    A, B, ZA, ZB, H = [], [], [], [], []
    for p in plans:
        if p.bbox[2] < x0 or p.bbox[0] > x1 or p.bbox[3] < y0 or p.bbox[1] > y1 or len(p.xy) < 2:
            continue
        A.append(p.xy[:-1])
        B.append(p.xy[1:])
        ZA.append(p.z[:-1])
        ZB.append(p.z[1:])
        H.append(np.full(len(p.xy) - 1, p.half))
    if not A:
        return out
    A, B, ZA, ZB, H = (np.concatenate(v) for v in (A, B, ZA, ZB, H))
    reach = H + T_MAX
    lo_x = np.minimum(A[:, 0], B[:, 0]) - reach
    hi_x = np.maximum(A[:, 0], B[:, 0]) + reach
    lo_y = np.minimum(A[:, 1], B[:, 1]) - reach
    hi_y = np.maximum(A[:, 1], B[:, 1]) + reach
    near = ~((hi_x < x0) | (lo_x > x1) | (hi_y < y0) | (lo_y > y1))
    A, B, ZA, ZB, H = A[near], B[near], ZA[near], ZB[near], H[near]
    lo_x, hi_x, lo_y, hi_y = lo_x[near], hi_x[near], lo_y[near], hi_y[near]
    cx0 = np.maximum(0, np.floor((lo_x - x0) / BUCKET)).astype(np.int64)
    cx1 = np.minimum(nbx - 1, np.floor((hi_x - x0) / BUCKET)).astype(np.int64)
    cy0 = np.maximum(0, np.floor((lo_y - y0) / BUCKET)).astype(np.int64)
    cy1 = np.floor((hi_y - y0) / BUCKET).astype(np.int64)
    # (segment, bucket row) pairs -> the sorted points' range of that row's buckets cx0..cx1
    nrow = np.maximum(cy1 - cy0 + 1, 0)
    seg = np.repeat(np.arange(len(A)), nrow)
    row = np.repeat(cy0, nrow) + (np.arange(nrow.sum()) - np.repeat(np.cumsum(nrow) - nrow, nrow))
    i0 = np.searchsorted(sk, row * nbx + cx0[seg], "left")
    i1 = np.searchsorted(sk, row * nbx + cx1[seg], "right")
    cnt = np.maximum(i1 - i0, 0)
    keep = cnt > 0
    seg, i0, cnt = seg[keep], i0[keep], cnt[keep]
    num = np.zeros(len(px))
    den = np.zeros(len(px))
    wmax = np.zeros(len(px))
    ends = np.cumsum(cnt)
    start = 0
    while start < len(seg):
        stop = int(np.searchsorted(ends, (ends[start - 1] if start else 0) + PAIR_BATCH, "right"))
        stop = max(stop, start + 1)
        c = cnt[start:stop]
        s_ = np.repeat(seg[start:stop], c)
        idx = order[np.repeat(i0[start:stop], c) + (np.arange(c.sum()) - np.repeat(np.cumsum(c) - c, c))]
        a = A[s_]
        ab = B[s_] - a
        qx, qy = px[idx] - a[:, 0], py[idx] - a[:, 1]
        L2 = ab[:, 0] * ab[:, 0] + ab[:, 1] * ab[:, 1]
        long_ = L2 > 1e-12
        t = np.where(long_, np.clip((qx * ab[:, 0] + qy * ab[:, 1]) / np.where(long_, L2, 1.0), 0.0, 1.0), 0.0)
        d = np.hypot(qx - t * ab[:, 0], qy - t * ab[:, 1])
        target = ZA[s_] + (ZB[s_] - ZA[s_]) * t
        T = np.clip(SLOPE_RUN * np.abs(target - base[idx]), T_MIN, T_MAX)
        u = np.clip((d - H[s_]) / T, 0.0, 1.0)
        w = 1.0 - u * u * (3.0 - 2.0 * u)           # 1 on the core, smooth to 0 over T
        m = w > 0
        i = idx[m]
        # the blend between roads by inverse distance from each centreline (times w), so two roads
        # near each other (divided carriageways, junctions) meet in a gradual slope; with w alone
        # the target jumped at a core's edge (2026-10-11, the user's car flipped at speed:
        # centreline 2nd difference p99 0.17 m per metre, worst 0.5 m)
        wb = w[m] / (1.0 + (d[m] / BLEND_R) ** 2)
        num += np.bincount(i, wb * target[m], len(px))
        den += np.bincount(i, wb, len(px))
        np.maximum.at(wmax, i, w[m])
        start = stop
    g = den > 0
    out[g] = base[g] + wmax[g] * (num[g] / den[g] - base[g] + lift)
    return out


class GradedDem:
    """dem.Dem with sample() graded under the roads; everything else passes through."""

    def __init__(self, dem, grader):
        self._dem, self._grader = dem, grader

    def sample(self, lat, lon, zoom, progress=None):
        base = self._dem.sample(lat, lon, zoom, progress) if progress is not None else self._dem.sample(lat, lon, zoom)
        return self._grader.apply(lat, lon, base)

    def __getattr__(self, name):
        return getattr(self._dem, name)
