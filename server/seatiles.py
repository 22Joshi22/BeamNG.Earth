"""The sea's water tiles near the player: square WaterBlocks that follow the Earth's curve (seacarve.py,
the mod's sea.lua; docs/research/2026-10-13-water-probe.md).

    tiles = plan(frame, x, y, sea_at)     # [Tile]: the anchor frame's sea tiles around (x, y)

A quadtree on a lattice of TILE_MAX squares in the anchor frame: a square is split while the player is
closer than SPLIT_K times its size (down to TILE_MIN), so the tiles are small near the player and large
far away (<= 34 over open sea; 83 tiles cost 3-9 % fps at Sandy Point, 2026-10-13). A leaf is kept when sea_at
says it holds sea. Each tile's surface is the sea level (seacarve.SEA_E) at its centre, its normal the
local vertical there, so its surface is at most (size/2)^2 / 2R off the true sea: 0.17 m at the corners
of 2048 m tiles, 1.3 m at the corners of 8192 m ones (which are >= 6 km away).
"""
import math
from dataclasses import dataclass

import numpy as np

TILE_MIN = 2048.0           # the sea's tiles: 6 inches off the curve at the corners of the smallest
PLAN_SQUARE = 512.0         # the server plans again when the player enters another square of this size
TILE_MAX = 8192.0
SPLIT_K = 0.75              # a square closer to the player than SPLIT_K * its size is split (<= 34 tiles)
RADIUS_M = 8192.0           # tiles within this of the player; beyond, the far tiles' photo
SAMPLES = 64                # sea_at is asked on a SAMPLES x SAMPLES grid per tile (32-128 m apart)
EARTH_R = 6371000.0


@dataclass(frozen=True)
class Tile:
    key: str                # "<size>_<i>_<j>": the square's lattice index at its size
    x: float                # centre in the anchor frame (m)
    y: float
    z: float                # the sea level there
    size: float
    nx: float               # the local vertical's tilt (its x and y; z = sqrt(1 - nx^2 - ny^2))
    ny: float

    def to_msg(self):
        return [self.key, round(self.x, 2), round(self.y, 2), round(self.z, 3), self.size,
                round(self.nx, 6), round(self.ny, 6)]


def _dist(x, y, x0, y0, s):
    """Distance from (x, y) to the square [x0, x0 + s] x [y0, y0 + s]."""
    dx = max(x0 - x, 0.0, x - (x0 + s))
    dy = max(y0 - y, 0.0, y - (y0 + s))
    return math.hypot(dx, dy)


def square_of(x, y):
    """The PLAN_SQUARE the player is in: the plans are the same anywhere inside it."""
    return math.floor(x / PLAN_SQUARE), math.floor(y / PLAN_SQUARE)


def squares(x, y, radius=None):
    """The quadtree leaves around (x, y): [(x0, y0, size)], every point within `radius` (default
    RADIUS_M) of the centre of (x, y)'s TILE_MIN square in exactly one of them."""
    r = RADIUS_M if radius is None else radius
    # planned from the centre of the player's TILE_MIN square: the set only changes when the player
    # crosses into another square (no tiles made and deleted every few metres)
    x = (math.floor(x / TILE_MIN) + 0.5) * TILE_MIN
    y = (math.floor(y / TILE_MIN) + 0.5) * TILE_MIN
    out = []
    i0, i1 = math.floor((x - r) / TILE_MAX), math.floor((x + r) / TILE_MAX)
    j0, j1 = math.floor((y - r) / TILE_MAX), math.floor((y + r) / TILE_MAX)

    def visit(x0, y0, s):
        d = _dist(x, y, x0, y0, s)
        if d > r:
            return
        if s > TILE_MIN and d < SPLIT_K * s:
            h = s / 2
            for a in (0, 1):
                for b in (0, 1):
                    visit(x0 + a * h, y0 + b * h, h)
        else:
            out.append((x0, y0, s))
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            visit(i * TILE_MAX, j * TILE_MAX, TILE_MAX)
    return out


def plan(frame, x, y, sea_at, radius=None):
    """The sea tiles around (x, y) in `frame` (a geodesy.EnuFrame). sea_at(lat, lon, size) -> bool array:
    which sample points of a tile of that size are sea."""
    leaves = squares(x, y, radius)
    if not leaves:
        return []
    f = (np.arange(SAMPLES) + 0.5) / SAMPLES
    out = []
    by_size = {}
    for x0, y0, s in leaves:
        by_size.setdefault(s, []).append((x0, y0))
    for s, corners in sorted(by_size.items()):
        c = np.asarray(corners, np.float64)
        gx = (c[:, 0:1] + f[None, :] * s)                  # [n, SAMPLES]
        gy = (c[:, 1:2] + f[None, :] * s)
        E = np.repeat(gx[:, None, :], SAMPLES, axis=1)     # [n, j, i]
        N = np.repeat(gy[:, :, None], SAMPLES, axis=2)
        lat, lon, _ = frame.enu_to_geodetic(E.ravel(), N.ravel(), 0.0)
        sea = np.asarray(sea_at(lat, lon, s), bool).reshape(len(c), -1).any(axis=1)
        for (x0, y0), keep in zip(corners, sea):
            if not keep:
                continue
            cx, cy = x0 + s / 2, y0 + s / 2
            la, lo, _ = frame.enu_to_geodetic(cx, cy, 0.0)
            z = float(np.asarray(frame.geodetic_to_enu(la, lo, 0.0)[2]))
            # the local vertical at (cx, cy): the sphere's normal, the frame's up tilted by the offset
            nx, ny = cx / EARTH_R, cy / EARTH_R
            out.append(Tile(f"{int(s)}_{int(x0 // s)}_{int(y0 // s)}", cx, cy, z, s, nx, ny))
    return out


# --- lakes and rivers ----------------------------------------------------------------------------------------
LAKE_RADIUS_M = 2500.0      # lake tiles within this of the player (the far tiles carved: L <= 2)
LAKE_TILE_MAX = 512.0
LAKE_TILE_MIN = 16.0
LAKE_SAMPLES = 24           # samples per tile edge when checking a lake tile
LAKE_FLOOD_M = 1.0          # land inside a lake tile lower than the lake by more than this splits it
                            # (a valley below a dam; the banks themselves are ~ at the lake's level)
LAKE_MAX_TILES = 60         # nearest first
LAKE_DEM_ZOOM = 15
LEVEL_TOL_M = 0.15           # a tile's water is at most this far from its plane (else it is split)


def _plane(u, v, z):
    """Least-squares plane z = a + b u + c v through the points (a level plane when they are too few or in
    a line)."""
    if len(z) < 3:
        return float(np.median(z)), 0.0, 0.0
    A = np.stack([np.ones_like(u), u, v], -1)
    sol, _, rank, _ = np.linalg.lstsq(A, z, rcond=None)
    if rank < 3:
        return float(np.median(z)), 0.0, 0.0
    return float(sol[0]), float(sol[1]), float(sol[2])


def plan_lakes(frame, x, y, lake_at, ground_at, radius=None):
    """Water tiles for the lakes and rivers around (x, y): lake_at(lat, lon) -> the inland water's level at
    the points (NaN away from it, seacarve.SeaCarver.lake_at), ground_at(lat, lon) -> the DEM there. A square of the
    LAKE_TILE_MAX lattice holding lake water becomes one tile at that level when every other point in it
    is land no lower than the water's plane by more than LAKE_FLOOD_M and the water is within LEVEL_TOL_M
    of that plane (a least-squares fit: a river falls across a tile, which is tilted to match), else it
    is split, down to LAKE_TILE_MIN (kept there when at least half of
    it is water; at the median level). So a reservoir's tile never floods the valley below its dam. Tilted to
    the local vertical like the sea's tiles."""
    r = LAKE_RADIUS_M if radius is None else radius
    x = (math.floor(x / PLAN_SQUARE) + 0.5) * PLAN_SQUARE
    y = (math.floor(y / PLAN_SQUARE) + 0.5) * PLAN_SQUARE
    s0 = LAKE_TILE_MAX
    i0, i1 = math.floor((x - r) / s0), math.floor((x + r) / s0)
    j0, j1 = math.floor((y - r) / s0), math.floor((y + r) / s0)
    roots = [(i * s0, j * s0) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)
             if _dist(x, y, i * s0, j * s0, s0) <= r]
    if not roots:
        return []
    f = (np.arange(LAKE_SAMPLES) + 0.5) / LAKE_SAMPLES
    out = []

    def samples(sq, s):
        c = np.asarray(sq, np.float64)
        E = np.repeat((c[:, 0:1] + f[None, :] * s)[:, None, :], LAKE_SAMPLES, axis=1)
        N = np.repeat((c[:, 1:2] + f[None, :] * s)[:, :, None], LAKE_SAMPLES, axis=2)
        lat, lon, _ = frame.enu_to_geodetic(E.ravel(), N.ravel(), 0.0)
        return lat, lon

    todo, s = roots, s0
    while todo and s >= LAKE_TILE_MIN:
        lat, lon = samples(todo, s)
        lv = np.asarray(lake_at(lat, lon), np.float64).reshape(len(todo), -1)
        has = np.isfinite(lv).any(axis=1)
        todo = [sq for sq, h in zip(todo, has) if h]
        lv = lv[has]
        if not todo:
            break
        lat, lon = samples(todo, s)
        g = np.asarray(ground_at(lat, lon), np.float64).reshape(len(todo), -1)
        nxt = []
        u = (f - 0.5) * s                               # sample offsets from the tile's centre
        U = np.tile(u, LAKE_SAMPLES)                    # [j, i] raveled: i fastest
        V = np.repeat(u, LAKE_SAMPLES)
        for (x0, y0), l, gr in zip(todo, lv, g):
            wet = np.isfinite(l)
            # the water as a plane over the tile (a river falls across it): level at the centre, slope
            a, bx, by = _plane(U[wet], V[wet], l[wet])
            fit = np.abs(l[wet] - (a + bx * U[wet] + by * V[wet])).max() <= LEVEL_TOL_M
            plane = a + bx * U + by * V
            clear = np.all(gr[~wet] >= plane[~wet] - LAKE_FLOOD_M)
            if (fit and clear) or (s <= LAKE_TILE_MIN and wet.mean() >= 0.5 and fit):
                out.append((x0, y0, s, a, bx, by))
            elif s > LAKE_TILE_MIN:
                h = s / 2
                nxt += [(x0 + a * h, y0 + b * h) for a in (0, 1) for b in (0, 1)]
        todo, s = nxt, s / 2
    out.sort(key=lambda t: _dist(x, y, t[0], t[1], t[2]))
    tiles = []
    for x0, y0, s, level, bx, by in out[:LAKE_MAX_TILES]:
        cx, cy = x0 + s / 2, y0 + s / 2
        la, lo, _ = frame.enu_to_geodetic(cx, cy, 0.0)
        z = float(np.asarray(frame.geodetic_to_enu(la, lo, level)[2]))
        # the tilt: the local vertical, then the water's own slope (a surface rising by b per metre in x
        # leans its normal by -b in x)
        tiles.append(Tile(f"L{int(s)}_{int(x0 // s)}_{int(y0 // s)}_{int(round(level * 10))}", cx, cy, z, s,
                          cx / EARTH_R - bx, cy / EARTH_R - by))
    return tiles
