"""Near-field TerrainBlock heightmap (docs/V1_2_DESIGN.md §1, §12 P1).

1024 x 1024 samples at exactly s = 2.0 m, on a world-locked lattice: the block centre (cx, cy)
is a multiple of 256 m in the anchor's ENU plane, the block position (SW corner) is
(cx - 1024, cy - 1024, zBase), and sample (gx, gy) sits at (cx - 1024 + 2 gx, cy - 1024 + 2 gy).
Coordinates are computed as integer lattice index x 2.0, so the same world point yields
bit-identical lat/lon (and DEM value) in every block; a 256 m terrain-follow move is exactly
128 samples. Row/column 1023 hold real DEM data.

The engine (probe P1) computes h = zBase + q * maxHeight / 65536. With a fixed per-anchor
height quantum Q (a power of two), maxHeight = Q * 65536 and zBase a multiple of Q, every
stored height is Q * round(u / Q) whatever the block, so overlapping samples of two blocks are
identical. Physics covers [x0, x0 + (N-1) s] = 2046 m per axis (P1); the 2 m strip up to
x0 + 2048 on the N/E edges is left to the far tiles.

The value of a sample is the ENU z of the DEM surface point under it. Stored as a 16-bit
grayscale PNG, row 0 = north.
"""
import math
import os

import numpy as np
from PIL import Image

SIZE = 1024
SQUARE = 2.0                        # m; exactly representable, so the lattice is exact
HALF = 1024.0                       # block position = centre - HALF
VALID = (SIZE - 1) * SQUARE         # 2046 m: collidable extent from the SW corner (P1)
GRID = 256.0                        # block centres are multiples of this (= 128 samples)
CHUNK_ROWS = 256                    # bounds the geodesy temporaries
# Height quanta (m), finest first: 65536 Q = 256 ... 4096 m of range per block.
Q_LADDER = (1 / 256, 1 / 128, 1 / 64, 1 / 32, 1 / 16)
Q_DEFAULT = 1 / 64
ZBASE_STEP = 64.0                   # zBase is a multiple of this (and so of every Q)
ZBASE_MARGIN = 8.0                  # m below the lowest sample


def snap(v, grid=GRID):
    """Nearest multiple of grid, halves rounded up (Lua: floor(v/g + 0.5)*g)."""
    return float(math.floor(v / grid + 0.5) * grid) + 0.0


def lattice_index(c):
    """Lattice index of the block's first sample for a centre c (a multiple of 2 m)."""
    k = (c - HALF) / SQUARE
    if k != math.floor(k):
        raise ValueError(f"block centre {c} is not on the 2 m lattice")
    return int(k)


def lattice_coords(k0, n):
    """World coordinates of n lattice samples starting at index k0 (exact: index x 2.0)."""
    return (k0 + np.arange(n, dtype=np.int64)).astype(np.float64) * SQUARE


def sample_coords(cx, cy, size=SIZE):
    """World x of grid columns and y of grid rows (both length `size`)."""
    return lattice_coords(lattice_index(cx), size), lattice_coords(lattice_index(cy), size)


def surface_u(frame, elev_fn, xs, ys):
    """ENU z of the DEM surface under every plane point (xs[i], ys[j]), indexed [j, i].
    Elementwise, so a point gets the same value whichever grid it is part of."""
    return surface(frame, elev_fn, xs, ys)[1]


def surface(frame, elev_fn, xs, ys):
    """(DEM elevation, ENU z of the surface point) under every plane point, indexed [j, i]."""
    ny, nx = len(ys), len(xs)
    lat = np.empty((ny, nx))
    lon = np.empty((ny, nx))
    for r0 in range(0, ny, CHUNK_ROWS):
        e, n = np.meshgrid(xs, ys[r0:r0 + CHUNK_ROWS])
        lat[r0:r0 + CHUNK_ROWS], lon[r0:r0 + CHUNK_ROWS], _ = frame.enu_to_geodetic(e, n, 0.0)
    elev = elev_fn(lat, lon)
    u = np.empty((ny, nx))
    for r0 in range(0, ny, CHUNK_ROWS):
        s = slice(r0, r0 + CHUNK_ROWS)
        # Spec step 5: z of the surface point; its tiny horizontal shift is ignored.
        u[s] = frame.geodetic_to_enu(lat[s], lon[s], elev[s])[2]
    return elev, u


def grid_u(frame, elev_fn, cx=0.0, cy=0.0, size=SIZE):
    """ENU z for every grid sample, indexed [gy, gx] (gy = north), all from real data."""
    xs, ys = sample_coords(cx, cy, size)
    return surface_u(frame, elev_fn, xs, ys)


def quantise(u, q):
    """Global quantum index round(u / Q) (exact: Q is a power of two)."""
    return np.rint(np.asarray(u, np.float64) / q)


def choose_q(relief, ladder=Q_LADDER):
    """Smallest quantum whose block range (65535 Q) covers the relief plus 10 % and the zBase
    rounding (ZBASE_STEP + ZBASE_MARGIN)."""
    need = relief * 1.1 + ZBASE_STEP + ZBASE_MARGIN
    for q in ladder:
        if 65535 * q >= need:
            return q
    return ladder[-1]


def block_params(u_min, u_max, q):
    """(Q, zBase, maxHeight) for a block with this height range. zBase is a multiple of
    ZBASE_STEP (so of Q); if the block does not fit the anchor's Q, the next coarser quantum
    is used for this block only (a one-time difference of <= Q/2 at its seams)."""
    z_base = math.floor((u_min - ZBASE_MARGIN) / ZBASE_STEP) * ZBASE_STEP
    ladder = [x for x in Q_LADDER if x >= q] or [Q_LADDER[-1]]
    for qq in ladder:
        if u_max - z_base <= 65534 * qq:        # one quantum of headroom for the rounding
            q = qq
            break
    else:
        q = ladder[-1]
    return q, float(z_base), q * 65536


def write_png(path, u, q=Q_DEFAULT):
    """Quantise on the global Q lattice and write the PNG atomically.
    Returns (Q, zBase, maxHeight, qgrid)."""
    q, z_base, max_height = block_params(float(u.min()), float(u.max()), q)
    g = quantise(u, q) - round(z_base / q)          # integer arithmetic on exact floats
    qgrid = np.clip(g, 0, 65535).astype(np.uint16)
    img = Image.fromarray(np.ascontiguousarray(qgrid[::-1]))   # flip: row 0 = north (max gy)
    tmp = path + ".tmp"
    # Uncompressed: the game decodes it on the main thread during the terrain-follow commit,
    # and level 0 imports ~7 ms faster than level 1 (43 vs 50 ms for 1024², measured in-game).
    img.save(tmp, format="PNG", compress_level=0)
    os.replace(tmp, path)
    return q, z_base, max_height, qgrid


def heights(qgrid, z_base, max_height):
    """Stored heights of a quantised grid, as the engine computes them (float64 here)."""
    return z_base + qgrid.astype(np.float64) * (max_height / 65536)


def height_at(qgrid, z_base, max_height, cx, cy, x, y, square=SQUARE):
    """Bilinear height of the quantised grid at world (x, y), or None outside the valid
    (collidable) area. This is what the game's terrain reports there."""
    x0, y0 = cx - HALF, cy - HALF
    n = qgrid.shape[0] - 1
    fx, fy = (x - x0) / square, (y - y0) / square
    if not (0 <= fx <= n and 0 <= fy <= n):
        return None
    ix, iy = min(int(fx), n - 1), min(int(fy), n - 1)
    wx, wy = fx - ix, fy - iy
    h = heights(qgrid[iy:iy + 2, ix:ix + 2], z_base, max_height)
    return float((h[0, 0] * (1 - wx) + h[0, 1] * wx) * (1 - wy) + (h[1, 0] * (1 - wx) + h[1, 1] * wx) * wy)


def generate(frame, elev_fn, path, cx=0.0, cy=0.0, ground_at=(0.0, 0.0), q=Q_DEFAULT):
    """Build the block centred at (cx, cy) with height quantum q and write it to path.
    groundZ is the terrain height at ground_at (None if that point is outside the block)."""
    u = grid_u(frame, elev_fn, cx, cy)
    q, z_base, max_height, qgrid = write_png(path, u, q)
    gz = height_at(qgrid, z_base, max_height, cx, cy, *ground_at) if ground_at else None
    return {"cx": cx, "cy": cy, "zBase": z_base, "maxHeight": max_height, "q": q,
            "groundZ": None if gz is None else round(gz, 3),
            "minU": float(u.min()), "maxU": float(u.max())}
