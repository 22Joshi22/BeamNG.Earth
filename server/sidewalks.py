"""Raised sidewalks with curbs as one mesh per road tile (docs/ARCHITECTURE.md "Raised sidewalks").

How West Coast USA does it (2026-10-11 study of its level files, after the user found our first
curbs "invisible" and plain): no MeshRoads anywhere. Each district's sidewalks are one modelled
mesh (64 `sidewalk_<district>.dae`, 231k triangles in all) with a curb profile textured from the
`curbs` trim sheet (8 horizontal bands: plain, red / yellow / white / green painted; in each band the
light upper part is the curb top and the darker lower part the face) and slab tops (`sidewalk1`,
a 4096 x 1024 sheet of 5 sidewalk strips with panel joints along them), plus a collision mesh. One
draw call per material per district instead of three per sidewalk piece.

Here: the sidewalk runs of a road tile (roads.build_tile, after the cuts at crossings) are swept
along a fixed profile into one shape (cdae.py, two materials). The profile, across from the curb
line (offset 0) outwards, heights above the road:
    face   (0, -SINK) -> (BATTER, h - NOSE)   curbs, the band's dark part
    nose   -> (NOSE_W, h)                     curbs, light
    top    -> (CURB_W, h)                     curbs, light
    slab   -> (W, h + W * CROSS_SLOPE)        sidewalk1 (rising away from the road, as built)
    back   -> (W, -SINK)                      curbs, dark
with flat end caps (curbs). Heights follow the road's (graded) centreline z, carried by the runs.
The mod shows it as a TSStatic that collides with its visible mesh. Pure functions; deterministic.
"""
import numpy as np

import cdae

CURB_H = 0.15           # m above the road (US curbs: 0.15)
SINK = 0.10             # m the faces reach below the road / ground
BATTER = 0.02           # m the face leans back over its height
NOSE = 0.025            # m: the bevelled nose, down from the top ...
NOSE_W = 0.045          # ... and in from the face
CURB_W = 0.15           # m of curb top band
CROSS_SLOPE = 0.015     # sidewalk cross slope, rising away from the road
CURB_REPEAT_M = 9.0     # m of curb per texture width (3 joints of 3 m)
SLAB_REPEAT_M = 45.0    # m of sidewalk per texture width (4096 px, a 205 px strip ~ 2.25 m)
MATERIALS = ("curbs", "sidewalk1")      # West Coast USA (art/shapes/roads), loaded by the mod

# curbs sheet (1024 x 512, 8 bands of 64 px; Collada v up, so band 0 is v in [7/8, 1]):
# rows 0-40 of a band are the light curb top, 40-64 the darker face
V_BAND_TOP, V_LIGHT_LOW, V_BAND_LOW = 1.0 - 1 / 512, 1.0 - 40 / 512, 1.0 - 63 / 512
# sidewalk1 sheet: strip 0 (the top row) across the slab
V_SLAB_IN, V_SLAB_OUT = 1.0 - 0.195, 1.0 - 0.005


def _orient(tris, P, want):
    """tris (m, 3) flipped where their normal points against want (m, 3) or (3,)."""
    a, b, c = P[tris[:, 0]], P[tris[:, 1]], P[tris[:, 2]]
    n = np.cross(b - a, c - a)
    flip = (n * want).sum(-1) < 0
    tris = tris.copy()
    tris[flip] = tris[flip][:, ::-1]
    return tris


def _normals_2d(xy):
    """Unit left normals at each point of a polyline (mitred like roads.offset_polyline, clamped)."""
    d = np.diff(xy, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])
    L[L == 0] = 1.0
    t = d / L[:, None]
    nseg = np.stack([-t[:, 1], t[:, 0]], axis=1)
    n = np.empty((len(xy), 2))
    n[0], n[-1] = nseg[0], nseg[-1]
    if len(xy) > 2:
        m = nseg[:-1] + nseg[1:]
        ml = np.hypot(m[:, 0], m[:, 1])
        ml[ml < 1e-9] = 1.0
        m = m / ml[:, None]
        cos = np.clip((m * nseg[1:]).sum(1), 0.5, 1.0)          # miter at most 2x
        n[1:-1] = m / cos[:, None]
    return n


def profile(h=CURB_H, w=2.5):
    """The cross-section points (offset from the curb line outwards, height above the road), in
    order face bottom, face top, nose end, top end, slab outer top, back bottom."""
    return np.array([[0.0, -SINK], [BATTER, h - NOSE], [NOSE_W, h], [CURB_W, h],
                     [w, h + w * CROSS_SLOPE], [w, -SINK]])


# strips: (from profile point, to point, material index, v at from, v at to)
STRIPS = ((0, 1, 0, V_BAND_LOW, V_LIGHT_LOW), (1, 2, 0, V_LIGHT_LOW, V_BAND_TOP - 0.02),
          (2, 3, 0, V_BAND_TOP - 0.02, V_BAND_TOP), (3, 4, 1, V_SLAB_IN, V_SLAB_OUT),
          (4, 5, 0, V_LIGHT_LOW, V_BAND_LOW))


class Builder:
    """Accumulates sidewalk runs into one mesh (positions relative to `origin`)."""

    def __init__(self, origin):
        self.origin = np.asarray(origin, np.float64)
        self.P, self.UV, self.T, self.M = [], [], [], []
        self.n = 0

    def _add(self, P, UV):
        k = self.n
        self.P.append(P)
        self.UV.append(UV)
        self.n += len(P)
        return k

    def add_run(self, curb, toward_out, w, h=CURB_H, caps=(True, True)):
        """One sidewalk run: curb (k, 3) the curb line (z = the road there), toward_out (k, 2) unit
        vectors from the curb outwards (scaled by the miter), w its width; caps: end caps at the
        (start, end) (not where the run goes on in the next tile)."""
        curb = np.asarray(curb, np.float64)
        k = len(curb)
        if k < 2:
            return
        prof = profile(h, w)
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(curb[:, :2], axis=0).T))]
        ring = np.empty((k, len(prof), 3))
        for j, (off, dz) in enumerate(prof):
            ring[:, j, :2] = curb[:, :2] + toward_out * off
            ring[:, j, 2] = curb[:, 2] + dz
        ring -= self.origin
        for a, b, mat, va, vb in STRIPS:
            rep = CURB_REPEAT_M if mat == 0 else SLAB_REPEAT_M
            P = np.concatenate([ring[:, a], ring[:, b]])
            UV = np.concatenate([np.stack([s / rep, np.full(k, va)], 1), np.stack([s / rep, np.full(k, vb)], 1)])
            base = self._add(P, UV)
            i = np.arange(k - 1)
            A, A1, B1, B = base + i, base + i + 1, base + k + i + 1, base + k + i
            tris = np.concatenate([np.stack([A, B1, A1], 1), np.stack([A, B, B1], 1)])
            # counter-clockwise seen from outside (dae convention): the profile runs clockwise
            # (offset out, z up), so an edge's outward normal is its left normal (-dz, doff)
            doff, dz = prof[b] - prof[a]
            o3 = np.concatenate([toward_out[i], np.zeros((k - 1, 1))], 1)
            want = -dz * o3 + doff * np.array([0.0, 0.0, 1.0])
            self.T.append(base + _orient(tris - base, P, np.concatenate([want, want])))
            self.M.append(np.full(2 * (k - 1), mat))
        for end, sgn, cap in ((0, -1, caps[0]), (k - 1, 1, caps[1])):   # end caps: a fan over the profile
            if not cap:
                continue
            P = ring[end]
            UV = np.stack([prof[:, 0] / CURB_REPEAT_M, V_BAND_LOW + (prof[:, 1] + SINK) / (h + SINK + 0.05)
                           * (V_LIGHT_LOW - V_BAND_LOW)], 1)
            base = self._add(P, UV)
            fan = [(base, base + j, base + j + 1) for j in range(1, len(prof) - 1)]
            fan = np.array(fan)
            t = curb[min(end + 1, k - 1), :2] - curb[max(end - 1, 0), :2]
            t = np.r_[t / max(np.hypot(*t), 1e-9), 0.0] * sgn
            self.T.append(base + _orient(fan - base, P, t))
            self.M.append(np.full(len(fan), 0))

    def empty(self):
        return self.n == 0

    def arrays(self):
        P = np.concatenate(self.P)
        UV = np.concatenate(self.UV)
        T = np.concatenate(self.T)
        M = np.concatenate(self.M)
        return P, T, UV, M

    def write(self, path):
        P, T, UV, M = self.arrays()
        return cdae.write(path, P, T, uvs=UV, material=list(MATERIALS), tri_materials=M, object_name="walk")


def run_geometry(walk_line, side, w):
    """The curb line and outward vectors of a sidewalk run given its centreline (k, 3) and the side of
    the road it is on (+1: the road's left, as roads.offset_polyline). Pure."""
    xy = np.asarray(walk_line, np.float64)[:, :2]
    n = _normals_2d(xy)
    out = n * side                      # left of the road's direction is +n for side +1
    curb = np.array(walk_line, np.float64)
    curb[:, :2] = xy - out * (w / 2)
    return curb, out
