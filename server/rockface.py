"""Rock on steep ground in the far tiles' photos (2026-10-15, world tour: Lauterbrunnen's and Geiranger's
cliffs were the aerial photo stretched down the wall, vertical streaks of the trees and meadows at its top).

A far tile's texture is the photo seen from above, mapped straight down: a wall gets a few texels stretched
over its height. Where the ground is steeper than STEEP_DEG the photo's colour is drawn toward rock: its own
colour desaturated and darkened, with a value noise taken at the texel's 3D position (so it is even on the
wall, not streaked) and faint horizontal strata. Noise finer than a texel's height on the wall is left out
(it would alias into streaks again), so the steepest walls are a plain rock tint of the photo's colour.

    rgb = rock(rgb, frame, x0, y0, size, U, ux0, uy0, usp)
      rgb: (px, px, 3) uint8, row 0 north, covering [x0, x0 + size)^2 of the anchor frame
      U: the tile's height grid [j, i] (j north from uy0), spacing usp, origin (ux0, uy0)
"""
import math

import numpy as np

STEEP_DEG = (38.0, 58.0)    # rock from the first slope, fully at the second
OCTAVES = ((28.0, 0.55), (9.0, 0.3), (3.0, 0.15))  # (m, weight) of the noise
STRATA_M = 7.0              # the strata's spacing (m of height)
STRATA_W = 0.10
ROCK_TINT = np.array((1.04, 1.0, 0.94), np.float32)   # warm grey
DESAT = 0.7                 # how much of the photo's colour goes
DARKEN = 0.82


def _hash3(i, j, k):
    """A hashed float in [0, 1) per integer lattice point (uint64 mixing)."""
    x = (i.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15)) ^ (j.astype(np.uint64) * np.uint64(0xC2B2AE3D27D4EB4F))
    x ^= k.astype(np.uint64) * np.uint64(0x165667B19E3779F9)
    x ^= x >> np.uint64(31)
    x *= np.uint64(0xBF58476D1CE4E5B9)
    x ^= x >> np.uint64(29)
    return (x >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def value_noise3(x, y, z):
    """Smooth value noise in [0, 1) at float arrays x, y, z (lattice spacing 1)."""
    xi, yi, zi = np.floor(x), np.floor(y), np.floor(z)
    fx, fy, fz = x - xi, y - yi, z - zi
    fx, fy, fz = (f * f * (3 - 2 * f) for f in (fx, fy, fz))
    xi, yi, zi = (a.astype(np.int64) for a in (xi, yi, zi))
    out = 0.0
    for dx in (0, 1):
        wx = fx if dx else 1 - fx
        for dy in (0, 1):
            wy = fy if dy else 1 - fy
            for dz in (0, 1):
                wz = fz if dz else 1 - fz
                out = out + wx * wy * wz * _hash3(xi + dx, yi + dy, zi + dz)
    return out


def steepness(slope):
    """0 .. 1 from the slope (rise / run)."""
    a, b = (math.tan(math.radians(d)) for d in STEEP_DEG)
    t = np.clip((slope - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def rock(rgb, frame, x0, y0, size, U, ux0, uy0, usp):
    """rgb with the steep texels drawn toward rock (see the module doc); rgb itself when nothing is steep."""
    rgb = np.asarray(rgb)
    px = rgb.shape[0]
    t = size / px                                   # m per texel
    c = (np.arange(px) + 0.5) * t
    X = x0 + c[None, :]
    Y = y0 + size - c[:, None]                      # row 0 north
    # the heights at the texels (bilinear on the tile's grid, held at its edges)
    U = np.asarray(U, np.float64)
    ny, nx = U.shape
    fi = np.clip((X - ux0) / usp, 0, nx - 1.000001)
    fj = np.clip((Y - uy0) / usp, 0, ny - 1.000001)
    i0, j0 = np.floor(fi).astype(np.int64), np.floor(fj).astype(np.int64)
    wi, wj = fi - i0, fj - j0
    H = ((U[j0, i0] * (1 - wi) + U[j0, i0 + 1] * wi) * (1 - wj)
         + (U[j0 + 1, i0] * (1 - wi) + U[j0 + 1, i0 + 1] * wi) * wj)
    H = np.broadcast_to(H, (px, px))
    gy, gx = np.gradient(H, t)
    slope = np.hypot(gx, gy)
    k = steepness(slope)
    if not (k > 0.01).any():
        return rgb
    # only the steep texels (most tiles have few): their absolute positions (the noise is the same whatever
    # the anchor), east / north metres
    sel = k > 0.01
    lat, lon = frame.lat0, frame.lon0
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(lat))
    Xs = np.broadcast_to(X, (px, px))[sel]
    Ys = np.broadcast_to(Y, (px, px))[sel]
    Hs, ks, slope_s = H[sel], k[sel], slope[sel]
    ex, ny_ = lon * m_lon + Xs, lat * m_lat + Ys
    foot = t * np.maximum(1.0, slope_s)             # m of wall a texel spans
    n = np.zeros(len(Hs))
    wsum = 0.0
    for scale, w in OCTAVES:
        a = np.clip(1.0 - 1.5 * foot / scale, 0.0, 1.0) * w
        on = a > 0
        if on.any():
            n[on] += a[on] * (value_noise3(ex[on] / scale, ny_[on] / scale, Hs[on] / scale) - 0.5)
        wsum += w
    n /= wsum
    sa = np.clip(1.0 - 1.5 * foot / STRATA_M, 0.0, 1.0)
    strata = sa * np.sin(2 * math.pi * Hs / STRATA_M + 3.0 * n) * STRATA_W
    f = rgb[sel].astype(np.float32) / 255.0
    lum = (f @ np.array((0.299, 0.587, 0.114), np.float32))[:, None]
    rockc = (f * (1 - DESAT) + lum * DESAT) * ROCK_TINT * DARKEN
    rockc = rockc * (1.0 + 0.55 * n[:, None] + strata[:, None])
    mixed = f * (1 - ks[:, None]) + rockc * ks[:, None]
    out = rgb.copy()
    out[sel] = np.clip(np.rint(mixed * 255.0), 0, 255).astype(np.uint8)
    return out
