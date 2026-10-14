"""The sea's water tiles near the player: square WaterBlocks that follow the Earth's curve (seacarve.py,
the mod's sea.lua; docs/research/2026-10-13-water-probe.md).

    tiles = plan(frame, x, y, sea_at)     # [Tile]: the anchor frame's sea tiles around (x, y)

A quadtree on a lattice of TILE_MAX squares in the anchor frame: a square is split while the player is
closer than SPLIT_K times its size (down to TILE_MIN), so the tiles are small near the player and large
far away (<= 76 over open sea; 83 tiles cost 3-9 % fps at Sandy Point, 2026-10-13). A leaf is kept when sea_at
says it holds sea. Each tile's surface is the sea level (seacarve.SEA_E) at its centre, its normal the
local vertical there, so its surface is at most (size/2)^2 / 2R off the true sea: 5 mm for 512 m tiles,
1.3 m at the corners of 8192 m ones (which are >= 8 km away).
"""
import math
from dataclasses import dataclass

import numpy as np

TILE_MIN = 512.0
TILE_MAX = 8192.0
SPLIT_K = 0.75              # a square closer to the player than SPLIT_K * its size is split (<= 76 tiles)
RADIUS_M = 8192.0           # tiles within this of the player; beyond, the far tiles' photo
SAMPLES = 16                # sea_at is asked on a SAMPLES x SAMPLES grid per tile
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
