"""Road surfaces and markings as one render-only mesh per road tile (docs/ARCHITECTURE.md "Roads").

Why not DecalRoads (2026-10-11, the user's drive at speed): a DecalRoad re-clips itself to the
terrain whenever the TerrainBlock changes, and ours moves every few seconds at speed. With 3,740
DecalRoads one block move cost a 140-148 ms frame (9 ms without them; ~37 us per decal), so the
game stuttered on every block commit. The official maps never move their terrain.

Here every chunk of roads.build_tile (asphalt, cobble strips, lines, gutters, drains) becomes a
ribbon (the DecalRoad's own layout: u 0..1 across, v = distance / texture length along) draped on the
graded ground: the ribbon is cut every DRAPE_STEP m and both edges sampled from the same elevation
function as the physics block (raised ROAD_LIFT under roads over the visible ground tiles, so the
mesh lies on what the tyres touch and above what is drawn), in one call per tile, plus its layer's
LIFT (markings over the asphalt). The stock decal
materials render on a mesh (probe 2026-10-11: they are translucent with zwrite, so overlapping
ribbons in one mesh draw in a fixed order and do not flicker). No collision: the tyres stay on the
terrain, whose surface masks carry the asphalt / dirt ground models. Pure given elev.
"""
import numpy as np

import cdae

DRAPE_STEP = 2.0        # m between ribbon cross-sections (the physics block's lattice)
LIFT = {"base": 0.0, "gutter": 0.005, "mark": 0.01}   # m above the physics ground (lifted under roads
                                                        # by grading.ROAD_LIFT over the visible ground)


def layer_of(chunk):
    m = chunk["m"]
    if m.startswith("line_") or m.startswith("crossing") or m == "m_metal_grid_drain":
        return "mark"
    if m == "gutter1":
        return "gutter"
    return "base"


def _resample(pts, step):
    """Points every `step` m along a polyline (k, 3), ends kept: (xy (m, 2), distance (m,), z (m,))."""
    seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] <= 1e-6:
        return pts[:1, :2], s[:1], pts[:1, 2]
    n = max(1, int(np.ceil(s[-1] / step)))
    t = np.linspace(0.0, s[-1], n + 1)
    return (np.stack([np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])], axis=1), t,
            np.interp(t, s, pts[:, 2]))


def _normals(xy):
    d = np.diff(xy, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])
    L[L < 1e-9] = 1e-9
    t = d / L[:, None]
    ns = np.stack([-t[:, 1], t[:, 0]], axis=1)
    n = np.empty((len(xy), 2))
    n[0], n[-1] = ns[0], ns[-1]
    if len(xy) > 2:
        m = ns[:-1] + ns[1:]
        ml = np.hypot(m[:, 0], m[:, 1])
        ml[ml < 1e-9] = 1.0
        m = m / ml[:, None]
        cos = np.clip((m * ns[1:]).sum(1), 0.5, 1.0)
        n[1:-1] = m / cos[:, None]
    return n


def ribbons(chunks):
    """Per chunk: (left (k, 2), right (k, 2), v (k,), material, layer, z (k,) or None) in the anchor
    frame; z: the chunk's own heights where it is "fixed" (bridge decks), else None (draped). Pure."""
    out = []
    for c in chunks:
        pts = np.asarray(c["n"], np.float64)
        if len(pts) < 2:
            continue
        xy, s, z = _resample(pts, DRAPE_STEP)
        if len(xy) < 2:
            continue
        n = _normals(xy) * (float(c["w"]) / 2)
        out.append((xy + n, xy - n, s / float(c.get("tl") or 10.0), c["m"], layer_of(c),
                    z if c.get("fixed") else None))
    return out


def build(frame, chunks, elev, origin):
    """The road mesh of a tile (render only; on bridges it lies DECK_SKIN over deck()'s slab): (positions
    relative to origin, tris, uvs, materials, tri material index), or None. elev(lat, lon) -> m (graded,
    as the physics block). One elev call."""
    rb = ribbons(chunks)                        # deck surfaces too: their asphalt over deck()'s concrete
    if not rb:
        return None
    draped = [r for r in rb if r[5] is None]
    if draped:
        edges = np.concatenate([np.concatenate([l, r]) for l, r, _, _, _, _ in draped])
        la, lo, _ = frame.enu_to_geodetic(edges[:, 0], edges[:, 1], 0.0)
        h = np.asarray(elev(la, lo), np.float64)
        _, _, u = frame.geodetic_to_enu(la, lo, h)
    mats = []
    P, UV, T, M = [], [], [], []
    k0 = nverts = 0
    for left, right, v, m, layer, fz in rb:
        k = len(left)
        if fz is None:
            z = u[k0:k0 + 2 * k] + LIFT[layer]
            k0 += 2 * k
        else:                                   # on a deck: over its opaque concrete top
            z = np.r_[fz, fz] + LIFT[layer] + DECK_SKIN
        if m not in mats:
            mats.append(m)
        mi = mats.index(m)
        P.append(np.c_[np.concatenate([left, right]), z])
        UV.append(np.c_[np.r_[np.zeros(k), np.ones(k)], np.r_[v, v]])
        i = np.arange(k - 1)
        A, A1, B, B1 = nverts + i, nverts + i + 1, nverts + k + i, nverts + k + i + 1
        # left edge to the left of the direction: (A, B, B1) is counter-clockwise seen from above
        T.append(np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)]))
        M.append(np.full(2 * (k - 1), mi))
        nverts += 2 * k
    P = np.concatenate(P) - np.asarray(origin, np.float64)
    return P, np.concatenate(T), np.concatenate(UV), mats, np.concatenate(M)


DECK_DEPTH = 1.2        # m of deck slab under the surface
DECK_SKIN = 0.01        # m the road mesh lies over a deck's top (the decal materials are translucent:
                        # 2026-10-11 a deck topped with road_asphalt_2lane was see-through)
DECK_SIDE = "sidewalk1" # the slab's sides and underside (West Coast USA concrete)


def deck(chunks, origin):
    """The bridge decks of a tile as one colliding mesh: per "deck" chunk a slab of opaque concrete
    (DECK_SIDE) whose top is at the chunk's fixed heights (the road surface, drawn DECK_SKIN above it
    by build()), with sides and underside DECK_DEPTH below. (positions, tris, uvs, materials, tri
    material index) or None. Pure."""
    rb = [r for r in ribbons([c for c in chunks if c.get("deck")]) if r[5] is not None]
    if not rb:
        return None
    mats, P, UV, T, M = [], [], [], [], []
    nv = 0

    def quads(a, b, ua, ub, va, vb, mi, up):
        """A strip between point rows a and b (k, 3); `up`: the side its faces look to (3,) or (k, 3)."""
        nonlocal nv
        k = len(a)
        P.append(np.concatenate([a, b]))
        UV.append(np.c_[np.r_[np.full(k, ua), np.full(k, ub)], np.r_[va, vb]])
        i = np.arange(k - 1)
        A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
        tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
        pts = np.concatenate(P)
        nrm = np.cross(pts[tri[:, 1]] - pts[tri[:, 0]], pts[tri[:, 2]] - pts[tri[:, 0]])
        want = np.broadcast_to(np.concatenate([up[:-1], up[:-1]]) if np.ndim(up) == 2 else up, nrm.shape)
        flip = (nrm * want).sum(1) < 0
        tri[flip] = tri[flip][:, ::-1]
        T.append(tri)
        M.append(np.full(len(tri), mi))
        nv += 2 * k

    for left, right, v, m, layer, z in rb:
        if DECK_SIDE not in mats:
            mats.append(DECK_SIDE)
        top = side = mats.index(DECK_SIDE)       # opaque concrete; the asphalt is the road mesh's
        L = np.c_[left, z] - origin
        R = np.c_[right, z] - origin
        Lb, Rb = L - [0, 0, DECK_DEPTH], R - [0, 0, DECK_DEPTH]
        out_l = np.c_[left - right, np.zeros(len(z))]
        quads(L, R, 0.0, 1.0, v, v, top, np.array([0.0, 0.0, 1.0]))
        quads(L, Lb, 0.0, 0.1, v, v, side, out_l)
        quads(R, Rb, 0.0, 0.1, v, v, side, -out_l)
        quads(Lb, Rb, 0.0, 1.0, v, v, side, np.array([0.0, 0.0, -1.0]))
    return (np.concatenate(P), np.concatenate(T), np.concatenate(UV), mats, np.concatenate(M))


def write(path, mesh):
    P, T, UV, mats, M = mesh
    return cdae.write(path, P, T, uvs=UV, material=mats, tri_materials=M, object_name="road")
