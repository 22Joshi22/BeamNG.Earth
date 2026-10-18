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
import math

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
DECK_SIDE = "bne_bridge_concrete"  # the slab, girders, parapets, piers and tunnel tubes: plain concrete (the stock
                        # concrete_plain textures; West Coast USA's "sidewalk1" had drain covers in it, round marks
                        # along every parapet next to Mapillary's Queensboro Bridge, 2026-10-17)
# A bridge as the official maps build theirs (Italy's road bridges on tapered wall piers, East Coast's): girders under
# the slab, a parapet along each free edge, piers (roads.pier_sites); ours was a bare floating slab (the user,
# 2026-10-17: "Bridges need a lot of work in general")
GIRDER_D = 1.0          # m of girders under the slab
GIRDER_W = 0.6          # m wide each
GIRDER_EVERY = 3.0      # m between girders across the deck
PARAPET_W = 0.45        # m: the slab reaches this far past the road's edge, a parapet on it ...
PARAPET_H = 0.9         # ... this high over the road
PARAPET_COVER_DZ = 2.0  # m: another deck's surface within this height at an edge point: no parapet there
PARAPET_MIN_M = 8.0     # m: a parapet run shorter than this is left out (stubs stood in the Queensboro Bridge's lanes
                        # where its two directions' decks meet and the cover test missed a few points, 2026-10-17)
FOOTING_M = 2.0         # m a pier reaches into the ground
PIER_T = 1.8            # m: a wall pier's thickness along the deck (at its top; FOOTING tapers wider)
PIER_TAPER = 0.012      # its width grows this much per m down (each side)
PIER_CAP_H = 1.2        # m of pier cap under the girders, as wide as the deck
PIER_BENT_W = 16.0      # a deck wider than this stands on columns (a bent) rather than one wall
PIER_COLUMN = 1.6       # m square columns of a bent


def deck(chunks, origin, piers=(), context=()):
    """The bridge decks of a tile as one colliding mesh: per "deck" chunk a slab of opaque concrete
    (DECK_SIDE) whose top is at the chunk's fixed heights (the road surface, drawn DECK_SKIN above it
    by build()), with sides and underside DECK_DEPTH below. (positions, tris, uvs, materials, tri
    material index) or None. Pure."""
    dch = [c for c in chunks if c.get("deck")]
    rb = [(r, c) for r, c in zip(ribbons(dch), dch) if r[5] is not None]
    if not rb and not piers:
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

    if DECK_SIDE not in mats:
        mats.append(DECK_SIDE)
    side = mats.index(DECK_SIDE)                # opaque concrete; the asphalt is the road mesh's
    cr = [r for r in ribbons(list(context)) if r[5] is not None]
    covered = _parapet_cover([(r, c) for r, c in rb], [(r, c) for r, c in zip(cr, context)])
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        k = len(z)
        lat = left - right
        u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]     # unit left
        wide = 0.0 if c.get("tunnel") else PARAPET_W
        el, er = left + u * wide, right - u * wide                  # the slab's edges, past the road's
        L = np.c_[el, z] - origin
        R = np.c_[er, z] - origin
        Lb, Rb = L - [0, 0, DECK_DEPTH], R - [0, 0, DECK_DEPTH]
        out_l = np.c_[u, np.zeros(k)]
        quads(L, R, 0.0, 1.0, v, v, side, np.array([0.0, 0.0, 1.0]))
        quads(L, Lb, 0.0, 0.1, v, v, side, out_l)
        quads(R, Rb, 0.0, 0.1, v, v, side, -out_l)
        quads(Lb, Rb, 0.0, 1.0, v, v, side, np.array([0.0, 0.0, -1.0]))
        if c.get("tunnel"):                     # a tunnel's floor: the slab alone
            continue
        # girders under the slab
        half = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2
        ng = max(2, int(round(2 * half / GIRDER_EVERY)))
        for off in np.linspace(-(half - 0.6), half - 0.6, ng):
            mid = (left + right) / 2 + u * off
            gl, gr = mid + u * GIRDER_W / 2, mid - u * GIRDER_W / 2
            GL, GR = np.c_[gl, z - DECK_DEPTH] - origin, np.c_[gr, z - DECK_DEPTH] - origin
            GLb, GRb = GL - [0, 0, GIRDER_D], GR - [0, 0, GIRDER_D]
            quads(GL, GLb, 0.0, 0.1, v, v, side, out_l)
            quads(GR, GRb, 0.0, 0.1, v, v, side, -out_l)
            quads(GLb, GRb, 0.0, 0.05, v, v, side, np.array([0.0, 0.0, -1.0]))
        # a parapet on each free edge (not where another deck goes on beside it: a ramp leaving, a carriageway)
        for sgn, edge, inner, cov in ((1, el, left, covered[n][0]), (-1, er, right, covered[n][1])):
            i = 0
            while i < k:
                if cov[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < k and not cov[j + 1]:
                    j += 1
                run_len = float(np.hypot(*np.diff(edge[i:j + 1], axis=0).T).sum()) if j > i else 0.0
                if j > i and (run_len >= PARAPET_MIN_M or i == 0 or j == k - 1):    # (a piece's ends: tile seams)
                    sl = slice(i, j + 1)
                    zz = z[sl]
                    Eo, Ei = np.c_[edge[sl], zz] - origin, np.c_[inner[sl], zz] - origin
                    To, Ti = Eo + [0, 0, PARAPET_H], Ei + [0, 0, PARAPET_H]
                    o = out_l[sl] * sgn
                    quads(Eo, To, 0.0, 0.1, v[sl], v[sl], side, o)          # outside face
                    quads(Ei, Ti, 0.0, 0.1, v[sl], v[sl], side, -o)         # road face
                    quads(Ti, To, 0.0, 0.05, v[sl], v[sl], side, np.array([0.0, 0.0, 1.0]))
                i = j + 1
    for p in _piers_clear(piers, rb):
        _pier(p, origin, P, T, UV, M, side)
        nv = sum(len(a) for a in P)
    return (np.concatenate(P), np.concatenate(T), np.concatenate(UV), mats, np.concatenate(M))


def _piers_clear(piers, rb):
    """The piers that cross no other deck under their top: a pier of the Queensboro Bridge's upper roadway stood on
    the ground through the lower one, across its lanes (the user, 2026-10-17: "objects blocking the bridge road")."""
    if not piers or not rb:
        return list(piers)
    lines = [(((l + r) / 2), float(np.median(np.hypot(*(l - r).T))) / 2 + PARAPET_W, z) for (l, r, v, m, layer, z), c in rb
             if not c.get("tunnel")]
    out = []
    for p in piers:
        x, y, ux, uy, zt, zb, width = p
        q = np.array([x, y])
        reach = float(np.clip(0.7 * width, 2.5, 14.0)) / 2 + PIER_T
        hit = False
        for mid, hw, z in lines:
            d = np.hypot(*(mid - q).T)
            k = int(np.argmin(d))
            if d[k] < hw + reach and z[k] < zt - 1.0:       # a deck below the pier's top, across its footprint
                hit = True
                break
        if not hit:
            out.append(p)
    return out


def _parapet_cover(rb, context=()):
    """Per deck ribbon (left, right edge points) whether another deck's surface lies there (no parapet):
    earthfast.deck_edges_covered over every ribbon's centre line, its half width + PARAPET_W; Python else."""
    import fast
    out = []
    if not rb:
        return out
    pts, own, A, B, HW, OS = [], [], [], [], [], []
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        lat = left - right
        u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]
        mid = (left + right) / 2
        hw = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2 + PARAPET_W
        oid = hash(c.get("fid", n)) & 0x3FFFFFFF
        for e in (left + u * PARAPET_W / 2, right - u * PARAPET_W / 2):
            pts.append(np.c_[e, z])
            own.append(np.full(len(z), oid))
        A.append(np.c_[mid[:-1], z[:-1]])
        B.append(np.c_[mid[1:], z[1:]])
        HW.append(np.full(len(z) - 1, hw))
        OS.append(np.full(len(z) - 1, oid))
    for (left, right, v, m, layer, z), c in context:     # the decks around the tile: surfaces only
        lat = left - right
        mid = (left + right) / 2
        hw = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2 + PARAPET_W
        oid = hash(c.get("fid", "ctx")) & 0x3FFFFFFF
        A.append(np.c_[mid[:-1], z[:-1]])
        B.append(np.c_[mid[1:], z[1:]])
        HW.append(np.full(len(z) - 1, hw))
        OS.append(np.full(len(z) - 1, oid))
    P_, O_ = np.concatenate(pts), np.concatenate(own).astype(np.int64)
    A_, B_, H_, S_ = np.concatenate(A), np.concatenate(B), np.concatenate(HW), np.concatenate(OS).astype(np.int64)
    if fast.AVAILABLE and hasattr(fast._ef, "deck_edges_covered"):
        cov = np.asarray(fast._ef.deck_edges_covered(np.ascontiguousarray(P_), O_, np.ascontiguousarray(A_),
                                                      np.ascontiguousarray(B_), H_, S_, PARAPET_COVER_DZ))
    else:
        cov = np.zeros(len(P_), bool)
        ab = B_[:, :2] - A_[:, :2]
        L2 = np.maximum((ab * ab).sum(1), 1e-9)
        for i, p in enumerate(P_):
            t = np.clip(((p[:2] - A_[:, :2]) * ab).sum(1) / L2, 0.0, 1.0)
            d = np.hypot(*(p[:2] - (A_[:, :2] + t[:, None] * ab)).T)
            zz = A_[:, 2] + t * (B_[:, 2] - A_[:, 2])
            cov[i] = bool(((S_ != O_[i]) & (d < H_) & (np.abs(p[2] - zz) < PARAPET_COVER_DZ)).any())
    k0 = 0
    for (left, right, v, m, layer, z), c in rb:
        k = len(z)
        out.append((cov[k0:k0 + k], cov[k0 + k:k0 + 2 * k]))
        k0 += 2 * k
    return out


def _pier(p, origin, P, T, UV, M, mat):
    """One pier (roads.pier_sites) appended to P, T, UV, M: a tapered wall with a cap under the girders, or for a deck
    wider than PIER_BENT_W a bent: square columns under a cap beam."""
    x, y, ux, uy, zt, zb, width = p
    t = np.array([ux, uy])
    nrm = np.array([-uy, ux])
    c = np.array([x, y])
    boxes = []                                  # (centre, half along t, half across, z0, z1, widen per m down)
    cap_w = width / 2 + PARAPET_W
    boxes.append((c, PIER_T / 2 + 0.3, cap_w, zt - PIER_CAP_H, zt, 0.0))
    if width > PIER_BENT_W:
        nc = max(2, int(round(width / 9.0)))
        for off in np.linspace(-(width / 2 - 2.0), width / 2 - 2.0, nc):
            boxes.append((c + nrm * off, PIER_COLUMN / 2, PIER_COLUMN / 2, zb, zt - PIER_CAP_H, 0.0))
    else:
        wall = float(np.clip(0.7 * width, 2.5, 14.0)) / 2
        boxes.append((c, PIER_T / 2, wall, zb, zt - PIER_CAP_H, PIER_TAPER))
    for cc, ht, hn, z0, z1, taper in boxes:
        grow = taper * max(z1 - z0, 0.0)
        top = [cc + t * a * ht + nrm * b * hn for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        bot = [cc + t * a * (ht + grow * 0.5) + nrm * b * (hn + grow) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        nv = sum(len(a) for a in P)
        V = np.array([[*q, z1] for q in top] + [[*q, z0] for q in bot]) - np.asarray(origin, np.float64)
        P.append(V)
        UV.append(np.c_[np.r_[0, 1, 0, 1, 0, 1, 0, 1] * 0.25, np.r_[0, 0, 0, 0, 1, 1, 1, 1] * (z1 - z0) / 8.0])
        tri = []
        for i in range(4):                      # sides (outward: the corners go counter-clockwise from above)
            a, b = i, (i + 1) % 4
            tri += [(a, b + 4, b), (a, a + 4, b + 4)]
        tri += [(0, 1, 2), (0, 2, 3)]           # top
        tri = np.array(tri, np.int64)
        # outward winding: check one side's normal against the outward direction, flip all if wrong
        Vn = V[tri]
        nrm_ = np.cross(Vn[:, 1] - Vn[:, 0], Vn[:, 2] - Vn[:, 0])
        cen = V.mean(0)
        flip = (nrm_ * (Vn.mean(1) - cen)).sum(1) < 0
        tri[flip] = tri[flip][:, ::-1]
        T.append(tri + nv)
        M.append(np.full(len(tri), mat))


TUNNEL_SHOULDER = 1.2      # m of concrete floor between the road's edge and the wall
TUNNEL_WALL_H = 4.6        # m of upright wall before the vault
TUNNEL_H = 6.8             # m from the floor to the vault's top
PORTAL_FRAME = 6.0         # m of portal face around the opening (hides the terrain hole's 2 m steps)
TUNNEL_SIDE = DECK_SIDE    # the tube's concrete
TUNNEL_STONE = "bne_tunnel_stone"  # a low arch's portal faces and the retaining walls: grey coursed rubble (Central
                                    # Park's transverse walls and arches; the buildings' pale dressed stone read as
                                    # concrete, 2026-10-17)
TUNNEL_STONE_IN = "bne_tunnel_stone_in"   # the same stone darker: a low arch's vault (in its shade)
TUNNEL_SOIL = "bne_tunnel_soil"     # grass on the ground over the masonry: a retaining wall's top behind its coping,
                                    # a low arch's top (stone slabs on the grass, the user 2026-10-17: "blend the
                                    # stone and terrain better")
SOIL_UV_M = 1.0            # m per repeat of the grass (the terrain's detail texture: bngEarthGroundGrass, size 1)
WALL_COPING_M = 0.5        # m of a retaining wall's top in stone (its coping), the rest of it soil
STONE_UV_M = 3.0           # m per repeat of the stone on a low arch
LOW_PORTAL_UP = 1.4        # m a low arch's portal face reaches over the vault (to the ground over it) ...
LOW_PORTAL_SIDE = 3.0      # ... and beside the opening
LOW_RISE = 0.4             # a low arch's vault rises this share of its height from its walls (a segmental arch)
ARCH_CAP_OVER = 0.75       # m a low arch's top lies over its vault (roads.ARCH_SOIL_MIN of soil there, + a little)
LOW_CAP_M = 7.0            # m a low arch's stone top runs back from its portal face over the mouth: the terrain there
                           # dips to the floor and is cut away (roads.arch_raise, LOW_HOLE_M + LOW_HOLE_STEP): from
                           # above a row of dark terrain teeth behind the face (79th Street Transverse, 2026-10-17)
TUNNEL_OVERLAP = 0.25      # m a tube piece reaches past a tile edge
TUNNEL_LIGHT = "bne_tunnel_light"   # the lamps' emissive material (levels/beamng_earth/art/shapes/beamngEarth)
TUNNEL_LAMP_OFF = 0.35     # two lamp lines, this share of the half-width either side of the centre, ...
TUNNEL_LAMP_W = 0.25       # ... this wide, ...
TUNNEL_LAMP_DROP = 0.05    # ... this far under the vault, lit along every other profile step (2-4 m lamps and gaps)


def _tunnel_section(hw, th=TUNNEL_H, low=False):
    """The tube's cross-section from the left road edge's floor over the vault to the right one: (lateral offset
    from the centre line, positive = left; height over the floor) for half-width hw (centre to wall) and a vault
    th over the floor (its walls in proportion). low: a park arch's segmental vault, a shallow circular arc rising
    LOW_RISE of th from its walls (Central Park's transverse arches)."""
    if low:
        rise = LOW_RISE * th
        wall = th - rise
        r = (hw * hw + rise * rise) / (2 * rise)
        a0 = math.asin(min(1.0, hw / r))
        vault = [(r * math.sin(a), th - r + r * math.cos(a)) for a in np.linspace(a0, -a0, 9)]
    else:
        wall = TUNNEL_WALL_H * th / TUNNEL_H
        vault = [(np.cos(a) * hw, wall + np.sin(a) * (th - wall))
                 for a in np.linspace(0.0, np.pi, 9)]
    return np.array([(hw - TUNNEL_SHOULDER, 0.0), (hw, 0.0)] + vault + [(-hw, 0.0), (-hw + TUNNEL_SHOULDER, 0.0)],
                    np.float64)


WALL_THICK_M = 2.4         # a retaining wall's top: over the grading's flat strip behind it (grading.WALL_FLAT_M)


def _retaining_walls(walls, origin, P, T, UV, M, nv):
    """roads.retaining_walls' walls as stone (material index 2): the face toward the road, the top, the ends;
    appended to P, T, UV, M. Returns the new vertex count."""
    for w in walls:
        b = np.asarray(w["base"], np.float64)
        top, foot, tw = np.asarray(w["top"]), np.asarray(w["foot"]), np.asarray(w["toward"], np.float64)
        k = len(b)
        if k < 2:
            continue
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(b, axis=0).T))]
        back = b - tw * WALL_THICK_M
        face_lo = np.c_[b, foot] - origin
        face_hi = np.c_[b, top] - origin
        back_hi = np.c_[back, top] - origin
        cop_hi = np.c_[b - tw * WALL_COPING_M, top] - origin
        for lo_, hi_, vlo, vhi, mat in ((face_lo, face_hi, foot, top, 2), (face_hi, cop_hi, None, WALL_COPING_M, 2),
                                        (cop_hi, back_hi, None, WALL_THICK_M - WALL_COPING_M, 4)):
            P.append(np.concatenate([lo_, hi_]))
            if vlo is not None:
                UV.append(np.c_[np.r_[s, s] / STONE_UV_M, np.r_[vlo, vhi] / STONE_UV_M])
            else:
                sc = SOIL_UV_M if mat == 4 else STONE_UV_M
                UV.append(np.c_[np.r_[s, s] / sc, np.r_[np.zeros(k), np.full(k, vhi)] / sc])
            i = np.arange(k - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            pts = np.concatenate([lo_, hi_])
            nrm = np.cross(pts[tri[:, 1] - nv] - pts[tri[:, 0] - nv], pts[tri[:, 2] - nv] - pts[tri[:, 0] - nv])
            # the face looks toward the road, the top up
            if vlo is not None:
                look = np.c_[np.concatenate([tw[i], tw[i]]), np.zeros(2 * (k - 1))]
            else:
                look = np.tile([0.0, 0.0, 1.0], (2 * (k - 1), 1))
            flip = (nrm * look).sum(1) < 0
            tri[flip] = tri[flip][:, ::-1]
            T.append(tri)
            M.append(mat)
            nv += 2 * k
        for at in (0, k - 1):           # the ends: from the face over to the back, foot to top
            q = np.array([[*b[at], foot[at]], [*b[at], top[at]], [*back[at], top[at]], [*back[at], foot[at]]]) - origin
            P.append(q)
            UV.append(np.array([[0, 0], [0, 1], [0.25, 1], [0.25, 0]], np.float64))
            T.append(np.array([[nv, nv + 1, nv + 2], [nv, nv + 2, nv + 3], [nv, nv + 2, nv + 1], [nv, nv + 3, nv + 2]],
                              np.int64))
            M.append(2)
            nv += 4
    return nv


def tunnel(chunks, origin, walls=()):
    """The tunnels of a tile as one colliding mesh: around every "tunnel" chunk (its road surface: a deck, deck()
    makes its slab) a tube of concrete (TUNNEL_SIDE) faced inwards: shoulders, walls and a vault TUNNEL_H over the
    floor; at a chunk end that is a portal ("portal") a face around the opening, seen from both sides. (positions,
    tris, uvs, materials, tri material index) or None. Pure."""
    tc = [c for c in chunks if c.get("tunnel")]
    rb = [(r, c) for r, c in zip(ribbons(tc), tc) if r[5] is not None] if tc else []
    if not rb and not walls:
        return None
    P, T, UV, M = [], [], [], []           # M: per T entry its material index (0 concrete, 1 lamps, 2 stone, 3 dark stone,
                                            # 4 soil)
    nv = _retaining_walls(walls, np.asarray(origin, np.float64), P, T, UV, M, 0) if walls else 0
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
        th = float(c.get("tunnel_h") or TUNNEL_H)
        low = th < TUNNEL_H - 0.05                          # a low arch: stone, a short portal face
        mat = 3 if low else 0                               # (its portal faces: 2, the stone in daylight)
        sec = _tunnel_section(hw, th, low)
        k, m = len(mid), len(sec)
        rows = np.empty((m, k, 3))
        for j, (off, h) in enumerate(sec):
            rows[j, :, :2] = mid + u * off
            rows[j, :, 2] = z + h
        rows -= origin
        # inward: towards the tube's axis (a point th / 2 over the floor's centre)
        axis = np.c_[mid, z + th / 2] - origin
        for j in range(m - 1):
            a, b = rows[j], rows[j + 1]
            P.append(np.concatenate([a, b]))
            if low:                                         # the stone at its scale: around the section, along
                sj = float(np.hypot(*(sec[min(j + 1, m - 1)] - sec[j])))
                s0 = float(np.sum(np.hypot(*np.diff(sec[:j + 1], axis=0).T))) if j else 0.0
                along = np.r_[0.0, np.cumsum(np.hypot(*np.diff(mid, axis=0).T))]
                UV.append(np.c_[np.r_[np.full(k, s0), np.full(k, s0 + sj)] / STONE_UV_M, np.r_[along, along] / STONE_UV_M])
            else:
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
            # a low arch is seen from outside too (where its thin cover is cut away: its stone, not a hole)
            T.append(np.concatenate([tri, tri[:, ::-1]]) if low and j >= 2 and j < m - 3 else tri)
            M.append(mat)
            nv += 2 * k
        # the lamps: under the vault along both lines, every other step, faced down (a long tunnel's; not under an arch)
        if not low:
            nv = _tunnel_lamps(P, T, UV, M, nv, mid, u, z, hw, origin)
        pups = c.get("portal_up") or (None, None)
        for end, at, pup in zip(c.get("portal") or (False, False), (0, k - 1), pups):
            if not end:
                continue
            # the face: from the opening's outline out to a frame PORTAL_FRAME wider and higher, both sides
            inner = rows[:, at]
            o = sec.copy()
            up, side = (LOW_PORTAL_UP, LOW_PORTAL_SIDE) if low else (PORTAL_FRAME, PORTAL_FRAME)
            if pup is not None:     # to the ground over the portal (TunnelProfiles.portal_ups); a low arch's at least
                # LOW_PORTAL_UP (under a hill higher than its top, the ground dipping into its mouth showed over it)
                up = max(LOW_PORTAL_UP, float(pup)) if low else float(pup)
            o[:, 0] = np.sign(o[:, 0]) * (np.abs(o[:, 0]) + side)
            # its foot under the ground (2 m into a hillside; a low arch's mouth is flat: just under its floor, or the
            # face's bottom stood as a dark wedge beside the opening)
            if low:     # a flat top over the arch (a parapet), not following its curve
                o[:, 1] = np.where(o[:, 1] > 0.0, th + up, -0.3)
            else:
                o[:, 1] = np.where(o[:, 1] > 0.0, o[:, 1] + up, -2.0)
            outer = np.empty((m, 3))
            outer[:, :2] = mid[at] + u[at] * o[:, :1]
            outer[:, 2] = z[at] + o[:, 1]
            outer -= origin
            P.append(np.concatenate([inner, outer]))
            sc = STONE_UV_M if low else 4.0
            UV.append(np.c_[np.r_[sec[:, 0], o[:, 0]] / sc, np.r_[sec[:, 1], o[:, 1]] / sc])
            i = np.arange(m - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + m + i, nv + m + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            T.append(np.concatenate([tri, tri[:, ::-1]]))   # seen from outside and from in the tube
            M.append(2 if low else 0)
            nv += 2 * m
            if low:
                nv = _arch_cap(P, T, UV, M, nv, mid, u, z, at, k, float(np.max(o[:, 0])), float(o[:, 1].max()), th, origin)
    mats = [TUNNEL_SIDE, TUNNEL_LIGHT] + ([TUNNEL_STONE, TUNNEL_STONE_IN, TUNNEL_SOIL] if any(mm >= 2 for mm in M) else [])
    Tall = np.concatenate(T)
    Mall = np.concatenate([np.full(len(t), m, np.int64) for t, m in zip(T, M)]) if M else np.zeros(len(Tall), np.int64)
    return (np.concatenate(P), Tall, np.concatenate(UV), mats, Mall)


def _arch_cap(P, T, UV, M, nv, mid, u, z, at, k, side, top, th, origin):
    """A low arch's stone top over its mouth (LOW_CAP_M back from the portal face at row `at`, side either way of
    the centre, `top` over the floor): its top, its sides down to the floor's level (outside the tube's walls) and
    its back down to just over the vault (th), seen from both sides. Appended to P, T, UV, M; returns the new vertex count."""
    nb = 1 if at == 0 else k - 2
    t = mid[nb] - mid[at]
    t = t / max(float(np.hypot(*t)), 1e-9)              # into the tube
    a = mid[at]
    b = a + t * LOW_CAP_M
    ua = u[at]
    z0 = float(z[at]) + top - (LOW_PORTAL_UP - ARCH_CAP_OVER)   # the soil's level (the face stands over it)
    corners = np.array([[*(a + ua * side), z0], [*(a - ua * side), z0], [*(b - ua * side), z0], [*(b + ua * side), z0]])
    foot = corners.copy()
    foot[:, 2] = float(z[at])
    over = corners.copy()
    over[:, 2] = float(z[at]) + th + 0.1
    quads = [corners,                                   # the top
             np.array([corners[3], corners[2], over[2], over[3]]),     # its back, toward the soil over the vault
             np.array([corners[0], corners[3], foot[3], foot[0]]),     # its sides
             np.array([corners[1], corners[2], foot[2], foot[1]])]
    for qi, q in enumerate(quads):
        P.append(q - origin)
        w = float(np.hypot(*(q[1, :2] - q[0, :2]))) or float(abs(q[1, 2] - q[0, 2]))
        h = float(np.hypot(*(q[2, :2] - q[1, :2]))) + float(abs(q[2, 2] - q[1, 2]))
        UV.append(np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float64) / (SOIL_UV_M if qi == 0 else STONE_UV_M))
        tri = np.array([[nv, nv + 1, nv + 2], [nv, nv + 2, nv + 3]], np.int64)
        T.append(np.concatenate([tri, tri[:, ::-1]]))
        M.append(4 if qi == 0 else 2)                   # its top grass, its sides stone
        nv += 4
    return nv


def _tunnel_lamps(P, T, UV, M, nv, mid, u, z, hw, origin):
    """Two dashed lamp strips under a tube's vault (material TUNNEL_LIGHT, index 1), appended to P, T, UV, M; returns
    the new vertex count. Pure but for the lists."""
    a = math.acos(TUNNEL_LAMP_OFF)
    h = TUNNEL_WALL_H + math.sin(a) * (TUNNEL_H - TUNNEL_WALL_H) - TUNNEL_LAMP_DROP
    k = len(mid)
    for side in (-1.0, 1.0):
        c = side * TUNNEL_LAMP_OFF * hw
        for i in range(0, k - 1, 2):
            quad = []
            for j in (i, i + 1):
                for off in (c - TUNNEL_LAMP_W / 2, c + TUNNEL_LAMP_W / 2):
                    xy = mid[j] + u[j] * off
                    quad.append([xy[0], xy[1], z[j] + h])
            q = np.asarray(quad, np.float64) - origin
            P.append(q)
            UV.append(np.array([[0, 0], [1, 0], [0, 1], [1, 1]], np.float64))
            tri = np.array([[nv, nv + 1, nv + 3], [nv, nv + 3, nv + 2]], np.int64)
            nrm = np.cross(q[1] - q[0], q[3] - q[0])
            if nrm[2] > 0:                  # faced down, into the tube
                tri = tri[:, ::-1]
            T.append(tri)
            M.append(1)
            nv += 4
    return nv


def write(path, mesh):
    P, T, UV, mats, M = mesh
    return cdae.write(path, P, T, uvs=UV, material=mats, tri_materials=M, object_name="road")
