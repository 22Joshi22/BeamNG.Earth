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


class Others:
    """furniture's (fid, half width, polyline) roads as segment arrays once, for clear() at every candidate."""

    def __init__(self, others):
        self.others = others
        if _ef is None:
            return
        ids = {}
        segs, half, owner = [], [], []
        for fid, h, line in others:
            p = np.asarray(line, np.float64)[:, :2]
            if len(p) < 2:
                continue
            k = ids.setdefault(fid, len(ids))
            segs.append(np.hstack([p[:-1], p[1:]]))
            half.append(np.full(len(p) - 1, float(h)))
            owner.append(np.full(len(p) - 1, k, np.int64))
        self.ids = ids
        self.segs = np.ascontiguousarray(np.vstack(segs)) if segs else np.zeros((0, 4))
        self.half = np.ascontiguousarray(np.concatenate(half)) if half else np.zeros(0)
        self.owner = np.ascontiguousarray(np.concatenate(owner)) if owner else np.zeros(0, np.int64)

    def clear(self, p, own, margin):
        """None without the module (the caller's Python check then)."""
        if _ef is None:
            return None
        return _ef.clear_of(self.segs, self.half, self.owner, float(p[0]), float(p[1]), self.ids.get(own, -1),
                            float(margin))
