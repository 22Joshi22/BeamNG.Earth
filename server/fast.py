"""The world server's Rust kernels (native/earthfast, built by tools/build_native.py) with their Python fallbacks.

The road tiles' hot loops were Python over tiny numpy arrays (a 2026-10-15 profile: the islands' curb ray march alone
was ~20 % of a road tile, 0.9 s of ~4 s); the compiled module does them in one call. Without it (no binary for this
platform) every caller keeps its Python code: fast.AVAILABLE says which.
"""
import numpy as np

try:
    import earthfast as _ef
except ImportError:         # no binary for this platform / Python: the Python code
    _ef = None

AVAILABLE = _ef is not None


def segments(lines_hw):
    """(segs (m, 4) ax ay bx by, hw (m,)) of [(polyline (k, 2), half width)]."""
    segs, hws = [], []
    for pts, hw in lines_hw:
        p = np.asarray(pts, np.float64)[:, :2]
        if len(p) < 2:
            continue
        segs.append(np.hstack([p[:-1], p[1:]]))
        hws.append(np.full(len(p) - 1, float(hw)))
    if not segs:
        return np.zeros((0, 4)), np.zeros(0)
    return np.ascontiguousarray(np.vstack(segs)), np.ascontiguousarray(np.concatenate(hws))


def island_ring_rays(poly, lines_hw, c, rays, step, rmax):
    """(rays, 2) or None without the module."""
    if _ef is None:
        return None
    segs, hw = segments(lines_hw)
    return _ef.island_ring_rays(np.ascontiguousarray(poly[:, :2], np.float64), segs, hw, float(c[0]), float(c[1]),
                                int(rays), float(step), float(rmax))
