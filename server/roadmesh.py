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


def build(frame, chunks, elev, origin, lift=0.0):
    """The road mesh of a tile (render only; on bridges it lies DECK_SKIN over deck()'s slab): (positions
    relative to origin, tris, uvs, materials, tri material index), or None. elev(lat, lon) -> m (graded,
    as the physics block). One elev call. lift: m more for the draped surfaces (detail mode: the physics
    block is drawn, so the asphalt must sit over it, not in it; earthd DETAIL_ROAD_LIFT)."""
    rb = ribbons(chunks)                        # deck surfaces too: their asphalt over deck()'s concrete
    polys = [c for c in chunks if c.get("poly")]
    if not rb and not polys:
        return None
    draped = [r for r in rb if r[5] is None]
    pv = [np.asarray(c["poly"], np.float64)[:, :2] for c in polys]
    if draped or pv:
        edges = np.concatenate([np.concatenate([l, r]) for l, r, _, _, _, _ in draped] + pv)
        la, lo, _ = frame.enu_to_geodetic(edges[:, 0], edges[:, 1], 0.0)
        h = np.asarray(elev(la, lo), np.float64)
        _, _, u = frame.geodetic_to_enu(la, lo, h)
    mats = []
    P, UV, T, M = [], [], [], []
    k0 = nverts = 0
    for left, right, v, m, layer, fz in rb:
        k = len(left)
        if fz is None:
            z = u[k0:k0 + 2 * k] + LIFT[layer] + lift
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
        tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
        # where a ribbon folds over itself (a mitred joint at a sharp merge pulls its edges across: the user saw
        # black triangles in President St, 2026-10-14) its triangles turn over: dropped, the neighbours cover it
        xy = np.concatenate([left, right])[:, :2]
        loc = tri - nverts
        e1, e2 = xy[loc[:, 1]] - xy[loc[:, 0]], xy[loc[:, 2]] - xy[loc[:, 0]]
        cz = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
        keep = cz * np.sign(np.median(cz) or 1.0) > 1e-6
        T.append(tri[keep])
        M.append(np.full(int(keep.sum()), mi))
        nverts += 2 * k
    for c in polys:                             # filled polygons (corner asphalt): a fan from vertex 0
        q = np.asarray(c["poly"], np.float64)
        k = len(q)
        z = u[k0:k0 + k] + LIFT[layer_of(c)] + lift
        k0 += k
        if c["m"] not in mats:
            mats.append(c["m"])
        P.append(np.c_[q[:, :2], z])
        UV.append(np.c_[q[:, 0] / 8.0, q[:, 1] / float(c.get("tl") or 10.0)])
        fan = np.array([(nverts, nverts + a, nverts + a + 1) for a in range(1, k - 1)])
        pts = np.c_[q[:, :2], z]
        nrm = np.cross(pts[fan[:, 1] - nverts] - pts[fan[:, 0] - nverts], pts[fan[:, 2] - nverts] - pts[fan[:, 0] - nverts])
        flip = nrm[:, 2] < 0
        fan[flip] = fan[flip][:, ::-1]
        T.append(fan)
        M.append(np.full(len(fan), mats.index(c["m"])))
        nverts += k
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


TUNNEL_SHOULDER = 1.2      # m of concrete floor between the road's edge and the wall
TUNNEL_WALL_H = 4.6        # m of upright wall before the vault
TUNNEL_H = 6.8             # m from the floor to the vault's top
PORTAL_FRAME = 6.0         # m of portal face around the opening (hides the terrain hole's 2 m steps)
TUNNEL_SIDE = DECK_SIDE    # the tube's concrete
TUNNEL_OVERLAP = 0.25      # m a tube piece reaches past a tile edge


def _tunnel_section(hw):
    """The tube's cross-section from the left road edge's floor over the vault to the right one: (lateral offset
    from the centre line, positive = left; height over the floor) for half-width hw (centre to wall)."""
    vault = [(np.cos(a) * hw, TUNNEL_WALL_H + np.sin(a) * (TUNNEL_H - TUNNEL_WALL_H))
             for a in np.linspace(0.0, np.pi, 9)]
    return np.array([(hw - TUNNEL_SHOULDER, 0.0), (hw, 0.0)] + vault + [(-hw, 0.0), (-hw + TUNNEL_SHOULDER, 0.0)],
                    np.float64)


def tunnel(chunks, origin):
    """The tunnels of a tile as one colliding mesh: around every "tunnel" chunk (its road surface: a deck, deck()
    makes its slab) a tube of concrete (TUNNEL_SIDE) faced inwards: shoulders, walls and a vault TUNNEL_H over the
    floor; at a chunk end that is a portal ("portal") a face around the opening, seen from both sides. (positions,
    tris, uvs, materials, tri material index) or None. Pure."""
    tc = [c for c in chunks if c.get("tunnel")]
    rb = [(r, c) for r, c in zip(ribbons(tc), tc) if r[5] is not None]
    if not rb:
        return None
    P, T, UV = [], [], []
    nv = 0
    for (left, right, v, _, _, z), c in rb:
        # a piece ending at a tile edge reaches TUNNEL_OVERLAP past it: the next tile's piece meets it there without a
        # hairline of daylight (2026-10-17, inside Eisenhower)
        portal = c.get("portal") or (False, False)
        left, right, z = left.copy(), right.copy(), z.copy()
        for at, nb in ((0, 1), (-1, -2)):
            if portal[0 if at == 0 else 1] or len(left) < 2:
                continue
            for arr in (left, right):
                t = arr[at] - arr[nb]
                arr[at] += t / max(float(np.hypot(*t)), 1e-9) * TUNNEL_OVERLAP
        mid = (left + right) / 2
        lat = left - right
        lw = np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)
        u = lat / lw[:, None]                               # unit left
        hw = float(np.median(lw)) / 2 + TUNNEL_SHOULDER
        sec = _tunnel_section(hw)
        k, m = len(mid), len(sec)
        rows = np.empty((m, k, 3))
        for j, (off, h) in enumerate(sec):
            rows[j, :, :2] = mid + u * off
            rows[j, :, 2] = z + h
        rows -= origin
        # inward: towards the tube's axis (a point TUNNEL_H / 2 over the floor's centre)
        axis = np.c_[mid, z + TUNNEL_H / 2] - origin
        for j in range(m - 1):
            a, b = rows[j], rows[j + 1]
            P.append(np.concatenate([a, b]))
            UV.append(np.c_[np.r_[np.full(k, j / 4.0), np.full(k, (j + 1) / 4.0)], np.r_[v, v]])
            i = np.arange(k - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            pts = np.concatenate(P)
            nrm = np.cross(pts[tri[:, 1]] - pts[tri[:, 0]], pts[tri[:, 2]] - pts[tri[:, 0]])
            cen = (pts[tri[:, 0]] + pts[tri[:, 1]] + pts[tri[:, 2]]) / 3
            ax = np.concatenate([axis[i], axis[i]])
            flip = (nrm * (ax - cen)).sum(1) < 0
            tri[flip] = tri[flip][:, ::-1]
            T.append(tri)
            nv += 2 * k
        for end, at in zip(c.get("portal") or (False, False), (0, k - 1)):
            if not end:
                continue
            # the face: from the opening's outline out to a frame PORTAL_FRAME wider and higher, both sides
            inner = rows[:, at]
            o = sec.copy()
            o[:, 0] = np.sign(o[:, 0]) * (np.abs(o[:, 0]) + PORTAL_FRAME)
            o[:, 1] = np.where(o[:, 1] > 0.0, o[:, 1] + PORTAL_FRAME, -2.0)
            outer = np.empty((m, 3))
            outer[:, :2] = mid[at] + u[at] * o[:, :1]
            outer[:, 2] = z[at] + o[:, 1]
            outer -= origin
            P.append(np.concatenate([inner, outer]))
            UV.append(np.c_[np.r_[sec[:, 0], o[:, 0]] / 4.0, np.r_[sec[:, 1], o[:, 1]] / 4.0])
            i = np.arange(m - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + m + i, nv + m + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            T.append(np.concatenate([tri, tri[:, ::-1]]))   # seen from outside and from in the tube
            nv += 2 * m
    mats = [TUNNEL_SIDE]
    Tall = np.concatenate(T)
    return (np.concatenate(P), Tall, np.concatenate(UV), mats, np.zeros(len(Tall), np.int64))


def write(path, mesh):
    P, T, UV, mats, M = mesh
    return cdae.write(path, P, T, uvs=UV, material=mats, tri_materials=M, object_name="road")
