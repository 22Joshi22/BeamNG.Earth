"""Buildings (v1.9; the user, 2026-10-13: "make the world look alive"): Overture footprints extruded into
walls with a window band per floor and a flat, gabled or hipped roof, one mesh per road tile (roads.TILE),
written next to the tile's road mesh and shown as a colliding TSStatic by the mod (roads.lua, "bld").

Overture rarely knows a building's height (Fells Point, 2026-10-13: 2 % of 12458; towns 50-80 %) and
almost never its roof shape or facade, so most of it is predicted from the footprint (area, narrowness,
class), deterministically per building (ROADMAP "deterministic generation": a hash of the footprint's
position, so it is the same building whichever way the player came).

Facades use the stock window sheets (asset survey 2026-10-13): m_highrise_plaster's four 256 px bands are
four floor styles (tall windows on plaster, square windows on panels, sash windows on plaster, sash windows
on red brick), m_highrise_metal's are office curtain wall and a shopfront; each floor of a wall is one band,
its windows a whole number of bays along every wall (no window cut at a corner). Pure functions of the
footprints and a ground-height function; no game state.
"""
import hashlib
import math

import numpy as np

FLOOR_M = 3.0               # m per storey (residential)
GROUND_FLOOR_SHOP_M = 4.2   # m, a shop's ground floor
FOUNDATION_M = 0.6          # m the walls reach under the lowest ground of the footprint
MIN_AREA_M2 = 12.0          # footprints smaller than this are skipped (kiosks, sheds' slivers)
MAX_VERTS = 64              # footprints with more vertices are simplified until under this
SIMPLIFY_M = 0.35           # m: vertices closer than this to the line of their neighbours are dropped
TILE_PX_M = 12.0            # m of wall per 1024 px of the window sheets (5 sash bays of 2.4 m)
ROOF_UV_M = 6.0             # m per repeat of the roof textures
SIDING_UV_M = 3.0           # m per repeat of the plain wall textures (sheds, garages)
PITCH = {"gabled": 32.0, "hipped": 26.0}     # degrees
EAVE_M = 0.35               # m a pitched roof overhangs its walls
CORNICE_OUT_M = 0.28        # a flat roof's cornice: how far it stands out, its depth below and above the eave
CORNICE_DOWN_M = 0.45
CORNICE_UP_M = 0.2

# Materials (mod/.../art/shapes/beamngEarth/buildings.materials.json): the window sheets in a few tints, the
# shop / office sheet, roofs, plain walls.
FACADE_TINTS = ("bne_bld_fac_white", "bne_bld_fac_cream", "bne_bld_fac_sand", "bne_bld_fac_grey")
PAINT_TINTS = ("bne_bld_fac_blue", "bne_bld_fac_green", "bne_bld_fac_rose", "bne_bld_fac_yellow")   # painted houses
BRICK_TINTS = ("bne_bld_fac_white", "bne_bld_fac_brickdark", "bne_bld_fac_brickwarm")   # the brick band's
SHOP = "bne_bld_shop"
OFFICE = "bne_bld_office"
ROOF_FLAT = "bne_bld_roof_flat"
ROOF_SLATE = "bne_bld_roof_slate"
ROOF_SHINGLE = "bne_bld_roof_shingle"
SIDING = "bne_bld_siding"
FACADE = "bne_bld_facade"    # the plaster window sheet, tinted per building by vertex colour
# The mesh's materials (vertColor: each building's colours are its vertices'); the names above are the
# looks plan() picks, each one of these with a colour (LOOKS).
MATERIALS = (FACADE, SHOP, OFFICE, ROOF_FLAT, ROOF_SLATE, ROOF_SHINGLE, SIDING)
LOOKS = {
    "bne_bld_fac_white": (FACADE, (1.0, 1.0, 1.0)), "bne_bld_fac_cream": (FACADE, (1.0, 0.93, 0.80)),
    "bne_bld_fac_sand": (FACADE, (0.92, 0.82, 0.68)), "bne_bld_fac_grey": (FACADE, (0.80, 0.82, 0.84)),
    "bne_bld_fac_brickdark": (FACADE, (0.78, 0.72, 0.70)), "bne_bld_fac_brickwarm": (FACADE, (1.0, 0.88, 0.80)),
    "bne_bld_fac_blue": (FACADE, (0.80, 0.87, 0.96)), "bne_bld_fac_green": (FACADE, (0.84, 0.92, 0.82)),
    "bne_bld_fac_rose": (FACADE, (0.99, 0.86, 0.82)), "bne_bld_fac_yellow": (FACADE, (1.0, 0.95, 0.72)),
    SHOP: (SHOP, (1.0, 1.0, 1.0)), OFFICE: (OFFICE, (1.0, 1.0, 1.0)), SIDING: (SIDING, (0.93, 0.92, 0.88)),
}
ROOF_TINTS = {               # per roof material: the colours a building's roof takes (hashed)
    ROOF_FLAT: ((0.85, 0.85, 0.85), (0.62, 0.62, 0.64), (0.95, 0.93, 0.88), (0.45, 0.45, 0.46), (0.75, 0.70, 0.64)),
    ROOF_SLATE: ((1.0, 1.0, 1.0), (0.80, 0.66, 0.56), (0.76, 0.80, 0.90), (0.62, 0.62, 0.62)),
    ROOF_SHINGLE: ((0.75, 0.72, 0.70), (0.55, 0.50, 0.48), (0.70, 0.56, 0.48), (0.62, 0.66, 0.72)),
}
for _m, _cs in ROOF_TINTS.items():
    for _k, _c in enumerate(_cs):
        LOOKS[f"{_m}|{_k}"] = (_m, _c)
JITTER = 0.06               # a building's colour varies this much (one factor, hashed)


class _Looks:
    """name -> (material index, RGBA bytes) for one building (its hashed brightness jitter)."""

    def __init__(self, seed):
        self.f = 1.0 + JITTER * (2.0 * _hash01(seed, "jit") - 1.0)

    def __getitem__(self, name):
        mat, c = LOOKS[name]
        return MATERIALS.index(mat), tuple(int(max(0, min(255, round(255 * v * self.f)))) for v in c) + (255,)

# The window sheets' bands, top of the image first (v in Collada convention: v = 1 at the image top).
# (sheet, band index, bays per 1024 px)
BAND_TALL = (0, 3)          # m_highrise_plaster: tall windows on plaster (3 bays of uneven width)
BAND_PANEL = (1, 4)         # square windows on panels
BAND_SASH = (2, 5, (0, 2, 3))   # sash windows on plaster (bays 1 and 4 are black "open" windows: skipped)
BAND_BRICK = (3, 5)         # sash windows on red brick
BAND_OFFICE = (0, 5)        # m_highrise_metal: curtain wall
BAND_OFFICE2 = (1, 4)
BAND_SHOP = (2, 2)          # shopfront with awnings
BANDS = 4

SHED_CLASSES = {"shed", "garage", "garages", "carport", "roof", "hut", "cabin", "barn", "farm_auxiliary",
                "greenhouse", "service", "storage_tank", "toilets", "kiosk", "parking"}
SHOP_CLASSES = {"retail", "commercial", "supermarket", "kiosk", "office", "hotel", "civic", "restaurant"}
HOUSE_CLASSES = {"house", "detached", "semidetached_house", "bungalow", "residential", "farm", "static_caravan",
                 "cabin", "dormitory"}
ROW_CLASSES = {"terrace"}
INDUSTRIAL_CLASSES = {"industrial", "warehouse", "manufacture", "factory", "hangar"}


def _hash01(*vals):
    """A number in [0, 1) from the values (stable across runs and machines)."""
    h = hashlib.blake2b(repr(vals).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


# ------------------------------------------------------------------------------------------- polygons

def _area2(p):
    """Twice the signed area of the ring p (n, 2), counter-clockwise positive."""
    x, y = p[:, 0], p[:, 1]
    return float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def clean_ring(p):
    """The ring (n, 2) without its closing point, duplicate and nearly collinear vertices, counter-clockwise;
    None if fewer than 3 are left."""
    p = np.asarray(p, np.float64)[:, :2]
    if len(p) > 1 and np.allclose(p[0], p[-1]):
        p = p[:-1]
    tol = SIMPLIFY_M
    for _ in range(8):
        if len(p) < 3:
            return None
        prev, nxt = np.roll(p, 1, 0), np.roll(p, -1, 0)
        d = nxt - prev
        L = np.hypot(d[:, 0], d[:, 1])
        off = np.abs(d[:, 0] * (p[:, 1] - prev[:, 1]) - d[:, 1] * (p[:, 0] - prev[:, 0])) / np.maximum(L, 1e-9)
        drop = (off < tol) | (np.hypot(*(p - prev).T) < 1e-3)
        if not drop.any():
            if len(p) <= MAX_VERTS:
                break
            tol *= 1.6
            continue
        # drop every other candidate at most, so a run of near-collinear points is not removed whole
        idx = np.nonzero(drop)[0]
        keep = np.ones(len(p), bool)
        keep[idx[::2]] = False
        p = p[keep]
    if len(p) < 3:
        return None
    if _area2(p) < 0:
        p = p[::-1]
    return p


def triangulate(p):
    """Ear clipping of the counter-clockwise simple ring p (n, 2): (n-2, 3) indices into p (fewer if the
    ring is not simple: the rest is left out)."""
    n = len(p)
    idx = list(range(n))
    out = []

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    guard = 0
    while len(idx) > 3 and guard < 4 * n * n:
        guard += 1
        m = len(idx)
        found = False
        for k in range(m):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % m]
            a, b, c = p[i0], p[i1], p[i2]
            if cross(a, b, c) <= 1e-12:
                continue
            ear = True
            for j in idx:
                if j in (i0, i1, i2):
                    continue
                q = p[j]
                if cross(a, b, q) >= 0 and cross(b, c, q) >= 0 and cross(c, a, q) >= 0:
                    ear = False
                    break
            if ear:
                out.append((i0, i1, i2))
                del idx[k]
                found = True
                break
        if not found:
            break
    if len(idx) == 3:
        out.append(tuple(idx))
    return np.array(out, np.int64).reshape(-1, 3)


def oriented_box(p):
    """The minimum-area rectangle around ring p along one of its edges: (centre (2,), axis u (2,) along the
    long side, half length, half width)."""
    best = None
    for i in range(len(p)):
        e = p[(i + 1) % len(p)] - p[i]
        L = math.hypot(*e)
        if L < 1e-6:
            continue
        u = e / L
        v = np.array([-u[1], u[0]])
        a, b = p @ u, p @ v
        area = (a.max() - a.min()) * (b.max() - b.min())
        if best is None or area < best[0]:
            best = (area, u, v, a, b)
    _, u, v, a, b = best
    c = u * (a.max() + a.min()) / 2 + v * (b.max() + b.min()) / 2
    hl, hw = (a.max() - a.min()) / 2, (b.max() - b.min()) / 2
    if hw > hl:
        u, hl, hw = v, hw, hl
    return c, u, hl, hw


# ------------------------------------------------------------------------------------------- the model

class Spec:
    """What is built for one footprint (decided by plan())."""
    __slots__ = ("ring", "floors", "floor_h", "height", "roof", "roof_mat", "facade", "band", "shop", "plain",
                 "seed")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def brick_share(lat, lon):
    """The share of brick facades in a region (old East Coast / Midwest towns are mostly brick; the South and
    the West plaster, siding and stucco). A coarse stand-in until a facade-material map."""
    if lat is None:
        return 0.4
    if lon > -95.0 and lat > 36.0:
        return 0.75
    if lon > -95.0:
        return 0.35
    return 0.15


def plan(ring, attrs, neighbours=0, lat=None, lon=None):
    """The Spec of a footprint (local ring (n, 2) in metres, counter-clockwise) from its Overture attributes
    (dict: class, height, num_floors, min_height, roof_shape, roof_height) and the number of buildings
    sharing a wall with it. Height and roof are predicted when not given."""
    area = _area2(ring) / 2
    c = ring.mean(0)
    seed = _hash01(round(float(c[0]) * 2), round(float(c[1]) * 2), round(area))
    _, _, hl, hw = oriented_box(ring)
    box_fill = area / max(4 * hl * hw, 1e-6)
    cls = (attrs.get("class") or "").lower()
    narrow = hw < 4.2 and hl > 2.2 * hw                 # a row house: ~4-8 m wide, deep
    row = cls in ROW_CLASSES or (narrow and neighbours >= 1)
    shed = cls in SHED_CLASSES or (area < 40 and not row)
    shop = cls in SHOP_CLASSES
    industrial = cls in INDUSTRIAL_CLASSES or (area > 2500 and cls in ("", "commercial"))

    h = attrs.get("height")
    nf = attrs.get("num_floors")
    h = None if h is None or not np.isfinite(h) or h <= 0 else float(h)
    nf = None if nf is None or not np.isfinite(nf) or nf <= 0 else int(round(nf))
    floor_h = FLOOR_M
    if industrial:
        floor_h = 4.5
    if h is None and nf is None:
        if shed:
            nf = 1
        elif row:
            nf = 2 if seed < 0.25 else (3 if seed < 0.85 else 4)
        elif cls in HOUSE_CLASSES or (area < 220 and not shop):
            nf = 1 if seed < 0.3 else 2
        elif industrial:
            nf = 2 if area > 6000 else 1
        elif cls == "apartments":
            nf = 3 + int(seed * 3)
        elif area < 900:
            nf = 2 + int(seed * 2)
        else:
            nf = 2 + int(seed * 3)
    roof_given = (attrs.get("roof_shape") or "").lower()
    if roof_given in ("gabled", "hipped", "flat"):
        roof = roof_given
    elif roof_given in ("pyramidal", "half-hipped", "mansard", "gambrel"):
        roof = "hipped"
    elif roof_given in ("skillion", "dome", "onion", "round", "saltbox"):
        roof = "gabled"
    else:
        detached_house = (cls in HOUSE_CLASSES or (area < 260 and not shop and not row)) and not industrial
        if neighbours >= 1:
            roof = "flat"                           # attached: a pitched roof would show its gable to the neighbour
        elif shed and area < 80:
            roof = "gabled" if seed > 0.4 else "flat"
        elif detached_house:
            roof = "gabled" if seed < 0.6 else "hipped"
        else:
            roof = "flat"
    if roof != "flat" and (box_fill < 0.8 or hw < 2.0 or hw > 14.0):
        roof = "flat"                               # pitched roofs only on near-rectangular footprints
    if h is None:
        h = nf * floor_h + (GROUND_FLOOR_SHOP_M - floor_h if shop and nf > 1 else 0.0) + (0.6 if roof == "flat" else 0.2)
    else:
        if attrs.get("roof_height") and np.isfinite(attrs["roof_height"]) and roof != "flat":
            h = max(2.5, h - float(attrs["roof_height"]))
        elif roof != "flat":
            h = max(2.5, h - hw * math.tan(math.radians(PITCH[roof])))
        if nf is None:
            nf = max(1, int(round(h / floor_h)))
    nf = max(1, min(nf, 120))
    floor_h = h / nf
    if shed:
        facade, band, plain = SIDING, None, True
    else:
        tall = nf >= 7
        if tall or (industrial and not shed):
            facade = OFFICE if tall else FACADE_TINTS[3]
            band = (BAND_OFFICE if seed < 0.5 else BAND_OFFICE2) if tall else BAND_PANEL
        else:
            u_brick = _hash01(seed, "brick")
            brick = u_brick < brick_share(lat, lon) * (1.15 if row else 1.0)
            if brick:
                band = BAND_BRICK
                facade = BRICK_TINTS[int(seed * 7919) % len(BRICK_TINTS)]
            else:
                facade = FACADE_TINTS[int(seed * 7919) % len(FACADE_TINTS)]
                if (row or cls in HOUSE_CLASSES) and _hash01(seed, "paint") < 0.3:
                    facade = PAINT_TINTS[int(_hash01(seed, "pc") * len(PAINT_TINTS))]
                if row or cls in HOUSE_CLASSES or roof != "flat":
                    band = BAND_SASH if seed < 0.7 else BAND_PANEL
                else:
                    band = (BAND_SASH, BAND_TALL, BAND_PANEL)[int(seed * 104729) % 3]
        plain = False
    roof_mat = ROOF_FLAT if roof == "flat" else (ROOF_SLATE if seed < 0.5 else ROOF_SHINGLE)
    roof_mat = f"{roof_mat}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[roof_mat]))}"
    return Spec(ring=ring, floors=nf, floor_h=floor_h, height=h, roof=roof, roof_mat=roof_mat, facade=facade,
                band=band, shop=shop and not shed and nf >= 1, plain=plain, seed=seed)


class Mesh:
    """Positions, uvs, colours, triangles and their material index, appended to. A material is a token
    (index, RGBA) as _Looks gives."""

    def __init__(self):
        self.P, self.UV, self.C, self.T, self.M = [], [], [], [], []
        self.n = 0

    def quad(self, a, b, c, d, uva, uvb, uvc, uvd, mat):
        """Corners counter-clockwise seen from outside."""
        i = self.n
        self.P += [a, b, c, d]
        self.UV += [uva, uvb, uvc, uvd]
        self.C += [mat[1]] * 4
        self.T += [(i, i + 1, i + 2), (i, i + 2, i + 3)]
        self.M += [mat[0], mat[0]]
        self.n += 4

    def tri(self, a, b, c, uva, uvb, uvc, mat):
        i = self.n
        self.P += [a, b, c]
        self.UV += [uva, uvb, uvc]
        self.C += [mat[1]] * 3
        self.T.append((i, i + 1, i + 2))
        self.M.append(mat[0])
        self.n += 3

    def poly(self, pts3, uv, mat):
        """A flat counter-clockwise (seen from above) polygon."""
        tris = triangulate(np.asarray(pts3)[:, :2])
        i = self.n
        self.P += [tuple(p) for p in pts3]
        self.UV += [tuple(u) for u in uv]
        self.C += [mat[1]] * len(pts3)
        self.T += [(i + a, i + b, i + c) for a, b, c in tris]
        self.M += [mat[0]] * len(tris)
        self.n += len(pts3)

    def arrays(self):
        return (np.array(self.P, np.float64).reshape(-1, 3), np.array(self.UV, np.float64).reshape(-1, 2),
                np.array(self.T, np.int64).reshape(-1, 3), np.array(self.M, np.int64),
                np.array(self.C, np.uint8).reshape(-1, 4))


def _band_v(band, k):
    """v range (bottom, top) of band k (0 = the image top) of a 4-band sheet; a hair inside it."""
    eps = 1.5 / 1024
    return 1.0 - (k + 1) / BANDS + eps, 1.0 - k / BANDS - eps


def _walls(m, spec, ring, z0, z_eave, mat_idx, lite=False):
    """Each wall of the ring, floor by floor: one band of the window sheet, a whole number of bays."""
    n = len(ring)
    fac = mat_idx[spec.facade]
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        L = float(math.hypot(*(b - a)))
        if L < 0.05:
            continue
        if spec.plain:
            u1 = L / SIDING_UV_M
            m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z_eave), (a[0], a[1], z_eave),
                   (0, z0 / SIDING_UV_M), (u1, z0 / SIDING_UV_M), (u1, z_eave / SIDING_UV_M), (0, z_eave / SIDING_UV_M),
                   fac)
            continue
        bays_per_tile = spec.band[1]
        bay = TILE_PX_M / bays_per_tile
        nb = int(round(L / bay))
        u1 = (nb / bays_per_tile) if nb >= 1 else 0.02 * L          # a short wall: the plain strip at the sheet's edge
        # the floors stand on the footprint's highest ground; below it a plain plinth (a slope's basement)
        base = z_eave - spec.floors * spec.floor_h
        za = z0
        if base - z0 > FOUNDATION_M + 0.4:
            pv0, pv1 = _band_v(spec.band, spec.band[0])
            m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], base - 0.3), (a[0], a[1], base - 0.3),
                   (0.002, pv0), (0.012, pv0), (0.012, pv0 + 0.01), (0.002, pv0 + 0.01), fac)
            za = base - 0.3
        for f in range(spec.floors):
            zb = z_eave if f == spec.floors - 1 else base + (f + 1) * spec.floor_h
            if f == 0 and spec.shop:
                mat, (_, k_bpt) = mat_idx[SHOP], BAND_SHOP
                band_k = BAND_SHOP[0]
                nbs = max(1, int(round(L / (TILE_PX_M / k_bpt))))
                uu = nbs / k_bpt if L > 2.0 else 0.02 * L
            else:
                mat, band_k, uu = fac, spec.band[0], u1
                if len(spec.band) > 2 and nb >= 1 and not lite:     # the good bays only, one quad each
                    v0, v1 = _band_v(spec.band, band_k)
                    good = spec.band[2]
                    for k in range(nb):
                        g = good[int(_hash01(spec.seed, i, f, k) * len(good))]
                        pa, pb = a + (b - a) * (k / nb), a + (b - a) * ((k + 1) / nb)
                        m.quad((pa[0], pa[1], za), (pb[0], pb[1], za), (pb[0], pb[1], zb), (pa[0], pa[1], zb),
                               (g / bays_per_tile, v0), ((g + 1) / bays_per_tile, v0), ((g + 1) / bays_per_tile, v1),
                               (g / bays_per_tile, v1), mat)
                    za = zb
                    continue
            v0, v1 = _band_v(spec.band, band_k)
            # the foundation and the parapet stretch the band a little; the windows stay a floor's height
            m.quad((a[0], a[1], za), (b[0], b[1], za), (b[0], b[1], zb), (a[0], a[1], zb),
                   (0, v0), (uu, v0), (uu, v1), (0, v1), mat)
            za = zb


def _cornice(m, spec, ring, z_eave, mat_idx):
    """A box moulding round a flat roof's edge (row houses' cornices, parapets), the plain white plaster."""
    mat = mat_idx["bne_bld_fac_white"]
    o, z0, z1 = CORNICE_OUT_M, z_eave - CORNICE_DOWN_M, z_eave + CORNICE_UP_M
    uv = ((0.002, 0.995), (0.012, 0.995), (0.012, 0.999), (0.002, 0.999))
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        e = b - a
        L = float(math.hypot(*e))
        if L < 0.3:
            continue
        d = e / L
        nn = np.array([d[1], -d[0]])                  # outward (counter-clockwise ring)
        a2, b2 = a + nn * o - d * o, b + nn * o + d * o
        m.quad((a2[0], a2[1], z0), (b2[0], b2[1], z0), (b2[0], b2[1], z1), (a2[0], a2[1], z1), *uv, mat)
        m.quad((a[0], a[1], z1), (a2[0], a2[1], z1), (b2[0], b2[1], z1), (b[0], b[1], z1), *uv, mat)      # top
        m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b2[0], b2[1], z0), (a2[0], a2[1], z0), *uv, mat)      # under


def _roof(m, spec, ring, z_eave, mat_idx):
    rm = mat_idx[spec.roof_mat]
    if spec.roof == "flat":
        pts = [(p[0], p[1], z_eave) for p in ring]
        uv = [(p[0] / ROOF_UV_M, p[1] / ROOF_UV_M) for p in ring]
        m.poly(pts, uv, rm)
        return
    c, u, hl, hw = oriented_box(ring)
    v = np.array([-u[1], u[0]])
    hl2, hw2 = hl + EAVE_M, hw + EAVE_M
    rise = hw2 * math.tan(math.radians(PITCH[spec.roof]))
    ze = z_eave - EAVE_M * math.tan(math.radians(PITCH[spec.roof]))
    zr = ze + rise

    def at(s, t, z):
        p = c + u * s + v * t
        return (float(p[0]), float(p[1]), float(z))
    slope = math.hypot(hw2, rise)

    def uv(s, t):            # along the ridge, up the slope (m / ROOF_UV_M)
        return (s / ROOF_UV_M, (hw2 - abs(t)) / hw2 * slope / ROOF_UV_M)
    if spec.roof == "gabled":
        r = hl2
        m.quad(at(-hl2, -hw2, ze), at(hl2, -hw2, ze), at(r, 0, zr), at(-r, 0, zr),
               uv(-hl2, -hw2), uv(hl2, -hw2), uv(r, 0), uv(-r, 0), rm)
        m.quad(at(hl2, hw2, ze), at(-hl2, hw2, ze), at(-r, 0, zr), at(r, 0, zr),
               uv(hl2, hw2), uv(-hl2, hw2), uv(-r, 0), uv(r, 0), rm)
        # the gable ends: wall triangles from the eave line up to the ridge, the facade's plain colour
        fac = mat_idx[spec.facade]
        for sgn in (-1, 1):
            s = sgn * hl
            a, b, top = at(s, -sgn * hw, z_eave), at(s, sgn * hw, z_eave), at(s, 0, zr - EAVE_M * 0.2)
            if sgn > 0:
                m.tri(a, b, top, (0, 0.995), (0.02, 0.995), (0.01, 0.999), fac)
            else:
                m.tri(a, b, top, (0, 0.995), (0.02, 0.995), (0.01, 0.999), fac)
    else:                    # hipped
        r = max(hl2 - hw2, 0.0)
        m.quad(at(-hl2, -hw2, ze), at(hl2, -hw2, ze), at(r, 0, zr), at(-r, 0, zr),
               uv(-hl2, -hw2), uv(hl2, -hw2), uv(r, 0), uv(-r, 0), rm)
        m.quad(at(hl2, hw2, ze), at(-hl2, hw2, ze), at(-r, 0, zr), at(r, 0, zr),
               uv(hl2, hw2), uv(-hl2, hw2), uv(-r, 0), uv(r, 0), rm)
        m.tri(at(hl2, -hw2, ze), at(hl2, hw2, ze), at(r, 0, zr),
              (-hw2 / ROOF_UV_M, 0), (hw2 / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)
        m.tri(at(-hl2, hw2, ze), at(-hl2, -hw2, ze), at(-r, 0, zr),
              (-hw2 / ROOF_UV_M, 0), (hw2 / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)


def build(specs_and_ground, lite=False):
    """The tile's mesh from [(Spec, ground z min, ground z max)] with rings in the tile's local frame:
    (positions, uvs, tris, tri material indices, MATERIALS) or None if empty. lite (far tiles): no
    cornices, whole walls instead of one quad per bay."""
    m = Mesh()
    for spec, gmin, gmax in specs_and_ground:
        mat_idx = _Looks(spec.seed)
        ring = spec.ring
        if spec.roof != "flat":
            c, u, hl, hw = oriented_box(ring)
            v = np.array([-u[1], u[0]])
            ring = np.array([c - u * hl - v * hw, c + u * hl - v * hw, c + u * hl + v * hw, c - u * hl + v * hw])
        z0 = gmin - FOUNDATION_M
        z_eave = gmax + spec.height
        _walls(m, spec, ring, z0, z_eave, mat_idx, lite)
        _roof(m, spec, ring, z_eave, mat_idx)
        if spec.roof == "flat" and not spec.plain and spec.floors <= 6 and not lite:
            _cornice(m, spec, ring, z_eave, mat_idx)
    if m.n == 0:
        return None
    P, UV, T, M, C = m.arrays()
    return P, UV, T, M, MATERIALS, C


# ------------------------------------------------------------------------------------------- a tile

def _shared_walls(rings, tol=0.6):
    """Per ring, the number of other rings with a vertex within tol of one of its edges (row houses)."""
    n = len(rings)
    cnt = np.zeros(n, np.int64)
    if n < 2:
        return cnt
    cents = np.array([r.mean(0) for r in rings])
    rads = np.array([np.hypot(*(r - r.mean(0)).T).max() for r in rings])
    order = np.argsort(cents[:, 0])
    xs = cents[order, 0]
    for oi, i in enumerate(order):
        lo = np.searchsorted(xs, cents[i, 0] - rads[i] - 30.0)
        hi = np.searchsorted(xs, cents[i, 0] + rads[i] + 30.0)
        for j in order[lo:hi]:
            if j == i or np.hypot(*(cents[j] - cents[i])) > rads[i] + rads[j] + tol:
                continue
            ri, rj = rings[i], rings[j]
            a, b = ri, np.roll(ri, -1, 0)
            d = b - a
            L2 = np.maximum((d * d).sum(1), 1e-9)
            t = np.clip(((rj[:, None, :] - a[None]) * d[None]).sum(2) / L2[None], 0, 1)
            q = a[None] + t[..., None] * d[None]
            dist = np.hypot(*(rj[:, None, :] - q).transpose(2, 0, 1))
            if (dist < tol).sum() >= 2:
                cnt[i] += 1
    return cnt


def tile_buildings(frame, x0, y0, x1, y1, layers, elev, water=None):
    """[(Spec, gmin, gmax)] of the buildings whose footprint centroid is in the tile [x0, x1) x [y0, y1)
    (anchor frame), rings relative to (x0, y0). layers: mapgeom.Features of buildings (one per map cell; a
    building in two cells is taken once, by id); elev(lat, lon) the physics ground."""
    seen = set()
    rings, attrs = [], []
    for f in layers:
        if f is None or len(f.kind) == 0:
            continue
        a = f.attrs
        ids = getattr(f, "ids", None)
        for i in range(len(f.kind)):
            if a.get("is_underground") is not None and bool(a["is_underground"][i]):
                continue
            key = ids[i] if ids is not None else None
            if key is not None and key in seen:
                continue
            parts = f.parts(i)
            if not parts or not parts[0]:
                continue
            outer = np.asarray(parts[0][0], np.float64)
            if len(outer) < 4:
                continue
            e, n, _ = frame.geodetic_to_enu(outer[:, 1], outer[:, 0], np.zeros(len(outer)))
            cx, cy = float(np.mean(e[:-1])), float(np.mean(n[:-1]))
            if not (x0 <= cx < x1 and y0 <= cy < y1):
                continue
            ring = clean_ring(np.stack([e - x0, n - y0], 1))
            if ring is None or _area2(ring) / 2 < MIN_AREA_M2:
                continue
            if key is not None:
                seen.add(key)
            rings.append(ring)
            attrs.append({k: (a[k][i] if k in a else None) for k in
                          ("class", "height", "num_floors", "min_height", "roof_shape", "roof_height")})
    if not rings:
        return []
    shared = _shared_walls(rings)
    clat, clon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
    clat, clon = float(clat), float(clon)
    out = []
    for ring, at, sh in zip(rings, attrs, shared):
        mh = at.get("min_height")
        if mh is not None and np.isfinite(mh) and mh > 2.0:
            continue                                  # a bridge / skyway part; not drawn yet
        spec = plan(ring, at, int(sh), clat, clon)
        e = ring[:, 0] + x0
        n = ring[:, 1] + y0
        la, lo, _ = frame.enu_to_geodetic(e, n, np.zeros(len(e)))
        g = np.asarray(elev(la, lo), np.float64)
        if not np.all(np.isfinite(g)):
            continue
        if water is not None:
            c = ring.mean(0)
            cla, clo, _ = frame.enu_to_geodetic(c[0] + x0, c[1] + y0, 0.0)
            if water(float(cla), float(clo)):
                continue
        out.append((spec, float(g.min()), float(g.max())))
    return out


def collider_boxes(items, x0, y0):
    """Boxes [x0, y0, z0, x1, y1, z1] (anchor frame) around each building: the mod does not make the tile's
    mesh solid while the player's car is inside one (roads.lua playerIn)."""
    out = []
    for spec, gmin, gmax in items:
        r = spec.ring
        out.append([round(float(r[:, 0].min() + x0), 2), round(float(r[:, 1].min() + y0), 2),
                    round(gmin - FOUNDATION_M, 2), round(float(r[:, 0].max() + x0), 2),
                    round(float(r[:, 1].max() + y0), 2), round(gmax + spec.height + 6.0, 2)])
    return out


def write(path, mesh):
    """The tile's .cdae (cdae.encode with one primitive per material)."""
    import cdae
    P, UV, T, M, mats, C = mesh
    return cdae.write(path, P, T, uvs=UV, material=list(mats), tri_materials=M, object_name="buildings", colors=C)


DOOR_ROAD_M = 30.0          # a front door on the wall facing the nearest road within this
DOOR_CLASSES_SKIP = SHED_CLASSES | INDUSTRIAL_CLASSES
G_DOOR = "door"
FAR_SEND_M = 1600.0         # far building tiles (render-only, the lite mesh) sent within this (Chebyshev)
FAR_DROP_M = 1900.0         # ... dropped beyond
FAR_PER_POSE = 4


def doors(items, lines, x0, y0):
    """Prop rows (trees.lua rows, group "door") of a front door per house: on the wall whose middle is
    nearest a road and faces it, a row house's to one side, others' in the middle. lines: [(centreline
    (k, 2) in the anchor frame, half width)]."""
    if not lines:
        return []
    segs = []
    for pts, half in lines:
        p = np.asarray(pts, np.float64)
        if len(p) >= 2:
            segs.append(np.concatenate([p[:-1], p[1:]], 1))
    if not segs:
        return []
    S = np.concatenate(segs)                             # (n, 4) x0 y0 x1 y1
    A, B = S[:, :2], S[:, 2:]
    D = B - A
    L2 = np.maximum((D * D).sum(1), 1e-12)
    rows = []
    for spec, gmin, gmax in items:
        if spec.plain or spec.shop or spec.floors >= 7 or spec.band in (BAND_OFFICE, BAND_OFFICE2):
            continue
        ring = spec.ring + np.array([x0, y0])
        best = None
        for i in range(len(ring)):
            a, b = ring[i], ring[(i + 1) % len(ring)]
            e = b - a
            Le = float(math.hypot(*e))
            if Le < 2.5:
                continue
            mid = (a + b) / 2
            t = np.clip(((mid - A) * D).sum(1) / L2, 0, 1)
            q = A + t[:, None] * D
            dist = np.hypot(*(q - mid).T)
            k = int(dist.argmin())
            if dist[k] > DOOR_ROAD_M:
                continue
            out = np.array([e[1], -e[0]]) / Le
            if float(np.dot(q[k] - mid, out)) <= 0:
                continue                                 # the wall faces away from that road
            if best is None or dist[k] < best[0]:
                best = (dist[k], a, e / Le, Le, out)
        if best is None:
            continue
        _, a, d, Le, out = best
        narrow = Le < 9.0
        f = (0.22 if _hash01(spec.seed, "door") < 0.5 else 0.78) if narrow else 0.5
        p = a + d * (Le * f) + out * 0.03
        base = gmax                                      # the ground floor (_walls: floors from the highest ground)
        rows.append([G_DOOR, round(float(p[0]), 2), round(float(p[1]), 2), round(base, 2),
                     round(math.degrees(math.atan2(d[1], d[0])) % 360.0, 1), 1.0, 2.2,
                     round(_hash01(spec.seed, "dv"), 3)])
    return rows


def road_tile(frame, tx, ty, md, ground, lines=None, props=None):
    """The buildings of road tile (tx, ty) (roads.TILE squares of the anchor frame): (mesh, collider boxes) or
    None. md: mapdata.MapData; ground(lat, lon): the physics ground without the road lift. lines (the road
    centrelines, roads.build_tile) and props (its prop rows): the houses' front doors are added to props."""
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    layers = [md.cell(k, layers=("buildings",), kind="demand").layers.get("buildings")
              for k in roads.cells_of_tile(frame, tx, ty, margin=0.0)]
    items = tile_buildings(frame, x0, y0, x1, y1, layers, ground)
    if props is not None and lines:
        props.extend(doors(items, lines, x0, y0))
    mesh = build(items)
    if mesh is None:
        return None
    return mesh, collider_boxes(items, x0, y0)


def far_tile(frame, tx, ty, md, ground, out_dir, vfs_dir, aid):
    """A far (render-only) building tile written to the session folder: (vfs path of its JSON, building
    count) or None. The lite mesh of the same buildings as road_tile's."""
    import json
    import os
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    layers = [md.cell(k, layers=("buildings",), kind="demand").layers.get("buildings")
              for k in roads.cells_of_tile(frame, tx, ty, margin=0.0)]
    items = tile_buildings(frame, x0, y0, x1, y1, layers, ground)
    mesh = build(items, lite=True)
    data = {"v": 1}
    if mesh is not None:
        shape = f"bldfar_{aid}_{tx}_{ty}.cdae"
        write(os.path.join(out_dir, shape), mesh)
        data["shape"] = f"{vfs_dir}/{shape}"
        data["pos"] = [x0, y0, 0.0]
    name = f"bldfar_{aid}_{tx}_{ty}.json"
    path = os.path.join(out_dir, name)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)
    return f"{vfs_dir}/{name}", len(items)
