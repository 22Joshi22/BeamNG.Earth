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
import itertools
import math

import numpy as np

import fast
import mapgeom

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
STUCCO = "bne_bld_stucco"   # plain stucco walls (windows laid over them from the sheet)
ROOF_METAL = "bne_bld_roof_metal"   # standing-seam metal (barns, sheds, farm buildings)
PANEL = "bne_bld_panel"     # precast concrete panels (big-box stores, strip malls)
METALWALL = "bne_bld_metalwall"     # corrugated metal walls (warehouses, factories)
STONE = "bne_bld_stone"     # dressed stone (old villages and towns of southern and central Europe: Gordes came out as
                            # grey plaster, 2026-10-15)
STONE_FLOORS = 4            # stone walls on buildings this low at most
BIG_BOX_M2 = 1200.0         # m2: a low commercial building this big is a big box (blank walls, one storefront)
CHURCH_CLASSES = ("church", "chapel", "cathedral")
BARN_CLASSES = {"barn", "farm_auxiliary", "stable", "cowshed"}
# The mesh's materials (vertColor: each building's colours are its vertices'); the names above are the
# looks plan() picks, each one of these with a colour (LOOKS).
MATERIALS = (FACADE, SHOP, OFFICE, ROOF_FLAT, ROOF_SLATE, ROOF_SHINGLE, SIDING, STUCCO, ROOF_METAL, PANEL, METALWALL,
             STONE)
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
    # 4..: clay tiles (terracotta, red-brown, faded orange) and dark grey / anthracite / brown-black (regional)
    ROOF_SLATE: ((1.0, 1.0, 1.0), (0.80, 0.66, 0.56), (0.76, 0.80, 0.90), (0.62, 0.62, 0.62),
                 (0.95, 0.52, 0.36), (0.78, 0.40, 0.30), (1.0, 0.66, 0.46), (0.42, 0.43, 0.46), (0.30, 0.31, 0.33),
                 (0.48, 0.38, 0.32)),
    ROOF_SHINGLE: ((0.75, 0.72, 0.70), (0.55, 0.50, 0.48), (0.70, 0.56, 0.48), (0.62, 0.66, 0.72),
                   (0.95, 0.52, 0.36), (0.78, 0.40, 0.30), (0.42, 0.43, 0.46), (0.36, 0.42, 0.52)),
    ROOF_METAL: ((0.85, 0.86, 0.88), (0.55, 0.57, 0.60), (0.45, 0.58, 0.48), (0.62, 0.30, 0.26), (0.40, 0.42, 0.48)),
}
WALL_TINTS = {               # houses with plain walls and laid-over windows: their colours (hashed)
    SIDING: ((0.96, 0.95, 0.92), (0.90, 0.86, 0.76), (0.78, 0.84, 0.90), (0.95, 0.92, 0.74), (0.78, 0.84, 0.74),
             (0.82, 0.78, 0.72), (0.70, 0.72, 0.75), (0.62, 0.20, 0.17), (0.66, 0.24, 0.20)),   # the last: barn red
    STUCCO: ((1.0, 0.97, 0.90), (0.96, 0.88, 0.74), (0.93, 0.80, 0.68), (0.98, 0.95, 0.88), (0.88, 0.84, 0.78),
             # 5..: bright paints (Latin America, the Mediterranean, the tropics): ochre, terracotta, sky blue,
             # mint, pink, yellow
             (1.0, 0.78, 0.45), (0.92, 0.58, 0.44), (0.66, 0.82, 0.95), (0.70, 0.90, 0.78), (0.98, 0.72, 0.76),
             (1.0, 0.92, 0.52)),
    PANEL: ((0.96, 0.94, 0.89), (0.86, 0.86, 0.86), (0.92, 0.85, 0.74), (0.78, 0.80, 0.82), (0.88, 0.78, 0.68),
            (0.62, 0.62, 0.60), (0.56, 0.54, 0.50)),    # the last two: weathered concrete (parking decks)
    METALWALL: ((0.88, 0.89, 0.90), (0.72, 0.74, 0.77), (0.84, 0.80, 0.72), (0.66, 0.72, 0.78)),
    # limestone, honey, ochre and grey stone (the texture is a light ashlar: tinted down)
    STONE: ((1.0, 0.93, 0.80), (0.98, 0.86, 0.66), (0.95, 0.80, 0.60), (0.86, 0.85, 0.82), (0.92, 0.88, 0.80)),
    # curtain walls (the user, 2026-10-14: "the skyscrapers all use the same texture and look the same colour"):
    # silver, blue, green, bronze, charcoal and steel-blue glass, the common tints of real towers
    OFFICE: ((1.0, 1.0, 1.0), (0.74, 0.84, 0.98), (0.76, 0.92, 0.86), (0.98, 0.84, 0.66), (0.56, 0.58, 0.62),
             (0.66, 0.74, 0.86), (0.88, 0.90, 0.92)),
}
for _m, _cs in list(ROOF_TINTS.items()) + list(WALL_TINTS.items()):
    for _k, _c in enumerate(_cs):
        LOOKS[f"{_m}|{_k}"] = (_m, _c)
# stained wood boards (Alpine chalets; kept apart from the siding tints' indices): larch brown, dark brown, grey-brown
WOOD_TINTS = ((0.62, 0.44, 0.30), (0.46, 0.33, 0.24), (0.58, 0.52, 0.46))
for _k, _c in enumerate(WOOD_TINTS):
    LOOKS[f"{SIDING}|w{_k}"] = (SIDING, _c)
# Shop signs (Bishop's and Moab's main streets: a painted sign band over every shopfront, each shop its own
# colour): boxes over the ground floor of a shop's street wall, a colour per shop width (hashed).
SIGN_TINTS = ((0.62, 0.16, 0.14), (0.16, 0.22, 0.42), (0.18, 0.36, 0.24), (0.95, 0.90, 0.75), (0.42, 0.30, 0.22),
              (0.85, 0.65, 0.20), (0.13, 0.13, 0.14), (0.96, 0.96, 0.96), (0.15, 0.45, 0.48))
for _k, _c in enumerate(SIGN_TINTS):
    LOOKS[f"sign|{_k}"] = (FACADE, _c)
SIGN_H = 0.9                # m, the band's height (its top 0.15 m under the ground floor's top)
SIGN_OUT = 0.18             # m out from the wall
SIGN_SHOP_M = (6.0, 11.0)   # one shop's width (hashed in this range)
BARN_REDS = 2               # the last siding tints are barn red (barns only)
US_WALL_TINTS = {STUCCO: 5}  # the first tints of these kinds are the US's (later ones: regional paints)
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
GLASS_FLOORS = (20, 40)     # towers from 20 floors: glass, but for OLD_TOWER_GLASS of the old ones below 40 ...
OLD_TOWER_GLASS = 0.65      # ... (Midtown's pre-war skyscrapers are stone)
MIDRISE_GLASS_OFFICE = 0.45 # offices / hotels of 7+ floors: this share glass (modern), the rest masonry
TOWER_SHOP_FLOORS = 6       # a building this tall in a dense core: a shop / lobby ground floor whatever its class
SHOP_CLASSES = {"retail", "commercial", "supermarket", "kiosk", "office", "hotel", "civic", "restaurant"}
HOUSE_CLASSES = {"house", "detached", "semidetached_house", "bungalow", "residential", "farm", "static_caravan",
                 "cabin", "dormitory"}
ROW_CLASSES = {"terrace"}
GARAGE_MIN_M2 = 400.0       # a "parking" building this big is a parking structure
INDUSTRIAL_CLASSES = {"industrial", "warehouse", "manufacture", "factory", "hangar"}


def _hash01(*vals):
    """A number in [0, 1) from the values (stable across runs and machines)."""
    h = hashlib.blake2b(repr(vals).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


# ------------------------------------------------------------------------------------------- polygons

def _area2(p):
    """Twice the signed area of the ring p (n, 2), counter-clockwise positive."""
    x, y = p[:, 0], p[:, 1]
    # np.roll(v, -1) by hand: the same arrays without roll's axis handling (~28k calls a 9-tile set)
    return float(np.dot(x, np.concatenate((y[1:], y[:1]))) - np.dot(np.concatenate((x[1:], x[:1])), y))


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
        prev, nxt = np.concatenate((p[-1:], p[:-1])), np.concatenate((p[1:], p[:1]))     # np.roll by hand
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
    __slots__ = ("ring", "floors", "floor_h", "height", "roof", "roof_mat", "facade", "band", "shop", "plain", "min_h", "wall", "door", "windows", "front", "steeple", "stoop",
                 "sign", "porch", "cls", "seed", "deck", "pitch")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Style:
    """A region's building habits (2026-10-15, the user: "the other parts of the world need more love": Swiss
    villages came out as clapboard houses with porches). Shares are of buildings whose data does not say:
    brick: of facades; walls: (kind, share) for detached houses' plain walls (SIDING, STUCCO; the rest the
    plaster window sheet), wall_tints: the tint indices the plain walls take (None: all but barn red);
    paint: plaster houses painted (PAINT_TINTS); porch; flat: detached houses with flat roofs; hipped: of the
    pitched; roofs: (roof material, (tint indices)) to pick from; pitch: the roof pitch in degrees (None: PITCH);
    stone: the share of low older buildings (houses and attached ones, up to STONE_FLOORS, no shops) with dressed stone
    walls; wood: siding walls are stained wood (WOOD_TINTS, barns too); barn_red: the share of barns painted barn red
    (the US's and Scandinavia's; elsewhere their siding colours: a Lauterbrunnen garage row came out a 150 m red
    wall, 2026-10-15); attached: low attached buildings (terraces, old town
    blocks) with a pitched roof (the US: flat; Amsterdam's canal houses, Europe's old towns: gables).
    Coarse boxes by region until a facade-material map; the US ones keep their earlier behaviour."""

    def __init__(self, name, brick, walls, porch, flat, hipped, roofs, pitch=None, paint=0.3, wall_tints=None,
                 wood=False, attached=0.0, barn_red=0.0, stone=0.0):
        self.name, self.brick, self.walls, self.porch, self.flat = name, brick, walls, porch, flat
        self.barn_red, self.stone = barn_red, stone
        self.hipped, self.roofs, self.pitch, self.paint, self.wall_tints = hipped, roofs, pitch, paint, wall_tints
        self.wood, self.attached = wood, attached


_US_ROOFS = ((ROOF_SLATE, (0, 1, 2, 3)), (ROOF_SHINGLE, (0, 1, 2, 3)))
_CLAY = (ROOF_SLATE, (4, 5, 6))
_DARK = (ROOF_SLATE, (7, 8, 9))
STYLES = {
    "us_east": Style("us_east", 0.75, ((SIDING, 0.75),), 0.55, 0.0, 0.4, _US_ROOFS, barn_red=0.5),
    "us_south": Style("us_south", 0.35, ((SIDING, 0.75),), 0.55, 0.0, 0.4, _US_ROOFS, barn_red=0.5),
    "us_plains": Style("us_plains", 0.15, ((SIDING, 0.75),), 0.55, 0.0, 0.4, _US_ROOFS, barn_red=0.5),
    "us_west": Style("us_west", 0.15, ((STUCCO, 0.65), (SIDING, 0.25)), 0.25, 0.0, 0.4, _US_ROOFS, barn_red=0.5),
    # Alpine villages: white plaster ground floors, dark wooden chalets, low wide gables of dark or brown roofs
    "alpine": Style("alpine", 0.05, ((SIDING, 0.45),), 0.0, 0.03, 0.15, (_DARK, (ROOF_SLATE, (9, 5))), pitch=24.0,
                    paint=0.05, wood=True, attached=0.85, stone=0.15),
    # north-west / central Europe: brick (Low Countries, northern Germany, Britain) or plaster, steep tiled or
    # slate roofs
    "europe_north": Style("europe_north", 0.5, (), 0.02, 0.05, 0.3, (_CLAY, _DARK, (ROOF_SLATE, (5,))), pitch=42.0,
                          paint=0.15, attached=0.75, stone=0.12),
    # Scandinavia and Finland: painted wooden houses (siding), little brick, dark or tiled roofs, falu-red barns
    "nordic": Style("nordic", 0.12, ((SIDING, 0.65),), 0.0, 0.08, 0.2, (_DARK, _CLAY), pitch=35.0, paint=0.2,
                    attached=0.3, barn_red=0.7),
    "mediterranean": Style("mediterranean", 0.08, ((STUCCO, 0.85),), 0.0, 0.35, 0.5, (_CLAY,), pitch=22.0,
                           paint=0.1, wall_tints=(0, 1, 2, 3, 4, 5), attached=0.8, stone=0.4),
    "latin_america": Style("latin_america", 0.25, ((STUCCO, 0.9),), 0.02, 0.7, 0.3, (_CLAY, (ROOF_METAL, (0, 1))),
                           pitch=22.0, paint=0.0, wall_tints=(0, 1, 2, 3, 5, 6, 7, 8, 9, 10)),
    "east_asia": Style("east_asia", 0.08, ((SIDING, 0.5), (STUCCO, 0.3)), 0.0, 0.35, 0.5,
                       (_DARK, (ROOF_SHINGLE, (6, 7)), (ROOF_METAL, (1, 4))), pitch=25.0, paint=0.0,
                       wall_tints=(0, 1, 6, 5), attached=0.3),
    "oceania": Style("oceania", 0.55, ((STUCCO, 0.5), (SIDING, 0.3)), 0.25, 0.05, 0.7, (_CLAY, (ROOF_METAL, (0, 1, 4))),
                     pitch=24.0, paint=0.1),
    # elsewhere (South Asia, Africa, the Middle East, ...): concrete and plaster, mostly flat roofs
    "warm": Style("warm", 0.15, ((STUCCO, 0.85),), 0.0, 0.75, 0.4, (_CLAY, (ROOF_METAL, (0, 1, 3))), pitch=20.0,
                  paint=0.0, wall_tints=(0, 1, 2, 3, 4, 5, 6, 8)),
    "cool": Style("cool", 0.3, ((SIDING, 0.4),), 0.05, 0.1, 0.3, (_DARK, (ROOF_METAL, (0, 1, 2, 3))), pitch=30.0,
                  paint=0.3, barn_red=0.5),
}


def region_style(lat, lon):
    """The Style of the region around (lat, lon) (None: the US East's)."""
    if lat is None or lon is None:
        return STYLES["us_east"]
    if 24.0 <= lat <= 72.0 and -170.0 <= lon <= -52.0:          # North America (and Canada)
        if lon < -100.0:
            return STYLES["us_west"]
        if lon <= -95.0:
            return STYLES["us_plains"]
        return STYLES["us_east"] if lat > 36.0 else STYLES["us_south"]
    if -56.0 <= lat < 24.0 and -120.0 <= lon <= -30.0:          # Mexico, the Caribbean, Central and South America
        return STYLES["latin_america"]
    if 45.4 <= lat <= 48.0 and 5.8 <= lon <= 16.5:              # the Alps
        return STYLES["alpine"]
    if 56.0 <= lat <= 72.0 and 4.5 <= lon <= 32.0:              # Norway, Sweden, Finland (brick Copenhagen, Malmo: not)
        return STYLES["nordic"]
    if 46.0 <= lat <= 72.0 and -11.0 <= lon <= 32.0:
        return STYLES["europe_north"]
    if 30.0 <= lat < 46.0 and -10.0 <= lon <= 40.0:             # southern Europe, North Africa's coast, Levant
        return STYLES["mediterranean"]
    if 20.0 <= lat <= 46.0 and 100.0 <= lon <= 146.0:           # China, Korea, Japan, Taiwan
        return STYLES["east_asia"]
    if -50.0 <= lat <= -10.0 and 110.0 <= lon <= 180.0:         # Australia, New Zealand
        return STYLES["oceania"]
    if abs(lat) >= 50.0:
        return STYLES["cool"]
    return STYLES["warm"]


def brick_share(lat, lon):
    """The share of brick facades in a region (old East Coast / Midwest towns are mostly brick; the South and
    the West plaster, siding and stucco; region_style elsewhere). A coarse stand-in until a facade-material map."""
    if lat is None:
        return 0.4
    return region_style(lat, lon).brick


def roof_pitch(spec):
    """The roof pitch (degrees) of a Spec: its region's, else PITCH."""
    return spec.pitch or PITCH.get(spec.roof, PITCH["gabled"])


def plan(ring, attrs, neighbours=0, lat=None, lon=None, look=None, town=False, dense=False, west_town=False):
    """The Spec of a footprint (local ring (n, 2) in metres, counter-clockwise) from its Overture attributes
    (dict: class, height, num_floors, min_height, roof_shape, roof_height) and the number of buildings
    sharing a wall with it. Height and roof are predicted when not given. west_town: a small town of the
    arid West (district_town): single-storey main streets and ranch houses."""
    area = _area2(ring) / 2
    c = ring.mean(0)
    seed = _hash01(round(float(c[0]) * 2), round(float(c[1]) * 2), round(area)) if look is None else look
    style = region_style(lat, lon)
    _, _, hl, hw = oriented_box(ring)
    box_fill = area / max(4 * hl * hw, 1e-6)
    cls = (attrs.get("class") or "").lower()
    name = str(attrs.get("name") or "").lower()
    terminal = any(w in name for w in ("terminal", "concourse"))
    church = cls in CHURCH_CLASSES or any(w in name for w in ("church", "chapel", "cathedral"))
    if "hangar" in name:
        cls = "hangar"
    # a parking structure (a small "parking" building is a carport or a booth: a shed); "parking" is also a
    # shed class, which made big garages barns in barn-red siding (Columbia, 2026-10-14)
    garage = (cls in ("parking", "parking_garage") and area >= GARAGE_MIN_M2) or "parking garage" in name or (
        "garage" in name and area > 800)
    narrow = hw < 4.2 and hl > 2.2 * hw                 # a row house: ~4-8 m wide, deep
    # in a dense core small buildings are row houses even where the footprints leave gaps (Fells Point)
    row = cls in ROW_CLASSES or (not west_town and ((narrow and (neighbours >= 1 or dense)) or (
        dense and area < 260 and cls not in SHOP_CLASSES)))
    shed = (cls in SHED_CLASSES and not garage) or (area < 40 and not row)
    shop = cls in SHOP_CLASSES
    industrial = cls in INDUSTRIAL_CLASSES or (area > 2500 and cls in ("", "commercial"))
    industrial = industrial and not (garage or terminal)

    h = attrs.get("height")
    nf = attrs.get("num_floors")
    if terminal and (h is None or not np.isfinite(h)) and (nf is None or not np.isfinite(nf)):
        h, nf = 13.0, 2                                 # an airport terminal: two tall glass storeys
    if garage and (h is None or not np.isfinite(h)) and (nf is None or not np.isfinite(nf)):
        nf = 4 + int(seed * 2)
    if cls == "hangar" and (h is None or not np.isfinite(h)):
        h, nf = 12.0, 1
    if church and (h is None or not np.isfinite(h)) and (nf is None or not np.isfinite(nf)) and area < 2500:
        nf = 1                                          # a nave: one tall storey under a steep gable
    h = None if h is None or not np.isfinite(h) or h <= 0 else float(h)
    nf = None if nf is None or not np.isfinite(nf) or nf <= 0 else int(round(nf))
    floor_h = FLOOR_M
    if industrial:
        floor_h = 4.5
    if church:
        floor_h = 7.5
    # (a big shed is a barn; a row of garages, a carport or a canopy is not)
    barn = cls in BARN_CLASSES or (shed and area >= 150 and cls not in ("garage", "garages", "carport", "roof"))
    if barn:
        floor_h = 5.5 if area >= 150 else 3.5          # one tall storey; the gable above it
    if h is None and nf is None:
        if shed:
            nf = 1
        elif row:
            nf = 2 if seed < 0.25 else (3 if seed < 0.85 else 4)
        elif west_town and not industrial and cls != "apartments":
            # Bishop's and Moab's photos (2026-10-14): one-storey shops with false fronts, ranch houses
            nf = 1 if seed < WEST_TOWN_ONE_STOREY else 2
        elif cls in HOUSE_CLASSES or (area < 220 and not shop):
            nf = 1 if seed < 0.3 else 2
        elif industrial:
            nf = 2 if area > 6000 else 1
        elif cls == "apartments":
            nf = 3 + int(seed * 3)
        elif town:
            nf = 1 if seed < 0.6 else 2                 # small towns: mostly single-storey shops and offices
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
        detached_house = (cls in HOUSE_CLASSES or (area < 260 and not shop)) and not industrial and not row
        if church and neighbours == 0:
            roof = "gabled"
        elif neighbours >= 1:
            # attached: flat in the US (a pitched roof would show its gable to the neighbour); old European towns
            # are gabled (the ridge on the long axis: a narrow deep house shows its gable to the street)
            low = (nf or (h or 0) / FLOOR_M or 3) <= 6
            roof = "gabled" if (style.attached and low and not industrial and not shop
                                and _hash01(seed, "attached roof") < style.attached) else "flat"
        elif barn:
            roof = "gabled"
        elif shed and area < 80:
            roof = "gabled" if seed > 0.4 else "flat"
        elif detached_house:
            if style.flat and _hash01(seed, "flat roof") < style.flat:
                roof = "flat"
            else:
                roof = "gabled" if seed < 1.0 - style.hipped else "hipped"
        else:
            roof = "flat"
    # pitched roofs only on near-rectangular footprints (a church's nave keeps its gable beside a tower or an apse)
    if roof != "flat" and (box_fill < (0.6 if church else 0.8) or hw < 2.0
                           or hw > (18.0 if barn else 20.0 if church else 14.0)):
        roof = "flat"
    # a city tower's ground floor is shops or a lobby (Midtown, 2026-10-14: most towers have no class, and their
    # curtain wall came down to the pavement)
    tall = (nf or (h or 0) / FLOOR_M) >= TOWER_SHOP_FLOORS
    if dense and tall and cls not in HOUSE_CLASSES and not (shed or barn or garage or terminal or church):
        shop = True
    if church:
        shop = False                                # (no shopfronts on a church, Cluj 2026-10-15)
    if h is None:
        h = nf * floor_h + (GROUND_FLOOR_SHOP_M - floor_h if shop and nf > 1 else 0.0) + (0.6 if roof == "flat" else 0.2)
        if west_town and roof == "flat" and nf == 1 and area >= 80 and not (shed or barn):
            h += FALSE_FRONT_M                      # a main-street shop's tall parapet
    else:
        if attrs.get("roof_height") and np.isfinite(attrs["roof_height"]) and roof != "flat":
            h = max(2.5, h - float(attrs["roof_height"]))
        elif roof != "flat":
            h = max(2.5, h - hw * math.tan(math.radians(style.pitch or PITCH[roof])))
        if nf is None:
            # a church: one tall storey (Cluj's St. Michael's, 25 m, came out as three 8 m floors)
            nf = 1 if church else max(1, int(round(h / floor_h)))
    nf = max(1, min(nf, 120))
    floor_h = h / nf
    if shed or barn:
        facade, band, plain = SIDING, None, True
    else:
        # glass curtain walls on towers; the mid-rises of old city cores (and a share of the older towers) are
        # masonry with punched windows (Midtown's street photos, 2026-10-14: the theatre district's 5-15 storey
        # brick and stone buildings had all come out as glass towers)
        u_glass = _hash01(seed, "glass")
        # a building drawn as its parts: the style of the whole building on every part (NYC 2026-10-14: a brick
        # part on a glass tower, each part decided by its own height)
        bh = attrs.get("building_height")
        snf = max(nf, int(bh / FLOOR_M)) if bh is not None and np.isfinite(bh) else nf
        modern = cls in ("office", "commercial", "hotel") and u_glass < MIDRISE_GLASS_OFFICE
        tall = snf >= GLASS_FLOORS[1] or (snf >= GLASS_FLOORS[0] and (modern or u_glass < OLD_TOWER_GLASS)) \
            or (snf >= 7 and modern)
        if snf >= 7 and not tall and not industrial:
            if _hash01(seed, "brick") < brick_share(lat, lon) * 1.2:
                facade, band = BRICK_TINTS[int(seed * 7919) % len(BRICK_TINTS)], BAND_BRICK
            else:
                facade = FACADE_TINTS[int(seed * 7919) % len(FACADE_TINTS)]
                band = BAND_TALL if seed < 0.6 else BAND_SASH
        elif tall or (industrial and not shed):
            facade = f"{OFFICE}|{int(_hash01(seed, 'glass tint') * len(WALL_TINTS[OFFICE]))}" if tall \
                else FACADE_TINTS[3]
            band = (BAND_OFFICE if seed < 0.5 else BAND_OFFICE2) if tall else BAND_PANEL
        else:
            u_brick = _hash01(seed, "brick")
            detached = (cls in HOUSE_CLASSES or (area < 260 and not shop)) and not row and neighbours == 0
            brick = u_brick < brick_share(lat, lon) * (1.15 if row else 0.35 if detached else 1.0)
            if brick:
                band = BAND_BRICK
                facade = BRICK_TINTS[int(seed * 7919) % len(BRICK_TINTS)]
            else:
                facade = FACADE_TINTS[int(seed * 7919) % len(FACADE_TINTS)]
                if (row or cls in HOUSE_CLASSES) and _hash01(seed, "paint") < style.paint:
                    facade = PAINT_TINTS[int(_hash01(seed, "pc") * len(PAINT_TINTS))]
                if row or cls in HOUSE_CLASSES or roof != "flat":
                    band = BAND_SASH if seed < 0.7 else BAND_PANEL
                else:
                    band = (BAND_SASH, BAND_TALL, BAND_PANEL)[int(seed * 104729) % 3]
        plain = False
    wall = None
    house = (cls in HOUSE_CLASSES or (area < 260 and not shop and not industrial)) and not row and not shed \
        and neighbours == 0 and area < 300         # a long "residential" block is not a house (Fells Point)
    if house and band is not BAND_BRICK and nf <= 3:
        # detached houses: siding in the East and South, stucco in the West; windows laid over the wall
        u = _hash01(seed, "wall")
        kind, acc = None, 0.0
        for k, share in style.walls:            # (the US East: siding 75 %; the West: stucco 65 %, siding 25 %)
            acc += share
            if u < acc:
                kind = k
                break
        if kind is not None:
            tints = style.wall_tints
            if style.wood and kind == SIDING:   # chalets: stained wood
                wall = f"{SIDING}|w{int(_hash01(seed, 'wc') * len(WOOD_TINTS))}"
            elif tints is not None:
                tints = [t for t in tints if t < len(WALL_TINTS[kind]) - (BARN_REDS if kind == SIDING else 0)] or [0]
                wall = f"{kind}|{tints[int(_hash01(seed, 'wc') * len(tints))]}"
            else:
                n = len(WALL_TINTS[kind]) - (BARN_REDS if kind == SIDING else 0)     # houses: not barn red
                n = min(n, US_WALL_TINTS.get(kind, n))
                wall = f"{kind}|{int(_hash01(seed, 'wc') * n)}"
            facade = wall
    # dressed stone: a regional share of low older buildings, attached ones too (their plaster sheet otherwise)
    if wall is None and style.stone and nf <= STONE_FLOORS and band is not BAND_BRICK \
            and not (shop or industrial or terminal or garage or shed or barn or church) \
            and cls not in ("apartments", "office", "commercial", "hotel", "retail") \
            and _hash01(seed, "stone") < style.stone:
        wall = f"{STONE}|{int(_hash01(seed, 'stc') * len(WALL_TINTS[STONE]))}"
        facade = wall
    if terminal:
        facade, band, plain = OFFICE, BAND_OFFICE, False
    elif garage:
        facade, band, plain = FACADE_TINTS[3], BAND_PANEL, False
    windows = True
    if not (shed or barn or row or house or terminal or garage or church) and nf <= 2 and roof == "flat" \
            and (area >= BIG_BOX_M2 or industrial) \
            and cls not in HOUSE_CLASSES and cls != "apartments":
        kind = METALWALL if industrial else PANEL
        wall = f"{kind}|{int(_hash01(seed, 'wc') * len(WALL_TINTS[kind]))}"
        facade, windows = wall, False
    if roof == "flat" or barn or shed:
        roof_mat = ROOF_FLAT if roof == "flat" else ROOF_METAL
        roof_mat = f"{roof_mat}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[roof_mat]))}"
    else:       # the region's pitched roofs (the US: slate or shingle by the seed, their first four tints)
        mat, tints = style.roofs[min(int(seed * len(style.roofs)), len(style.roofs) - 1)]
        roof_mat = f"{mat}|{tints[int(_hash01(seed, 'roof') * len(tints))]}"
    if barn or shed:                    # plain walls tinted: barns the region's barn red or wood, sheds the siding colours
        tints = WALL_TINTS[SIDING]
        if barn and _hash01(seed, "barn") < style.barn_red:
            facade = f"{SIDING}|{len(tints) - 1 - int(_hash01(seed, 'red') * 2)}"
        elif style.wood:
            facade = f"{SIDING}|w{int(_hash01(seed, 'wc') * len(WOOD_TINTS))}"
        else:
            facade = f"{SIDING}|{int(_hash01(seed, 'sc') * (len(tints) - 2))}"
    # a front porch (the photos of Lancaster, Ocean City, Frederick, Moab: most houses have one): pitched-roof
    # houses, a regional share; doors() puts it on the front door's wall
    porch = (house and roof != "flat" and not barn and not shed and area < 300
             and _hash01(seed, "porch") < style.porch)
    if porch:   # pitched roofs draw the walls on the oriented box: an L-shaped house's door wall can be inside it
        _, _, hl, hw = oriented_box(ring)
        porch = area >= 0.9 * 4 * hl * hw
    return Spec(ring=ring, floors=nf, floor_h=floor_h, height=h, roof=roof, roof_mat=roof_mat, facade=facade,
                band=band, shop=shop and not shed and nf >= 1, plain=plain, seed=seed, min_h=0.0, wall=wall,
                windows=windows, steeple=church and area < 2500, porch=porch, cls=cls,
                deck=garage and roof == "flat", pitch=style.pitch)


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
        """A flat polygon facing up when counter-clockwise seen from above, down when clockwise (its underside:
        the ear clipping wants counter-clockwise rings and gave a clockwise one no triangles at all, so the
        parking decks' and floating parts' undersides were missing, the user's report 2026-10-14)."""
        xy = np.asarray(pts3)[:, :2]
        if _area2(xy) < 0:
            k = len(xy)
            tris = np.array([(k - 1 - c, k - 1 - b, k - 1 - a) for a, b, c in triangulate(xy[::-1])],
                            np.int64).reshape(-1, 3)
        else:
            tris = triangulate(xy)
        i = self.n
        self.P += [tuple(p) for p in pts3]
        self.UV += [tuple(u) for u in uv]
        self.C += [mat[1]] * len(pts3)
        self.T += [(i + a, i + b, i + c) for a, b, c in tris]
        self.M += [mat[0]] * len(tris)
        self.n += len(pts3)

    def arrays(self):
        # fromiter over the flattened tuples: the same arrays, ~2x faster than np.array of a list of tuples
        def flat(L, k, dt):
            return np.fromiter(itertools.chain.from_iterable(L), dt, len(L) * k).reshape(-1, k)
        return (flat(self.P, 3, np.float64), flat(self.UV, 2, np.float64), flat(self.T, 3, np.int64),
                np.array(self.M, np.int64), flat(self.C, 4, np.uint8))


def _band_v(band, k):
    """v range (bottom, top) of band k (0 = the image top) of a 4-band sheet; a hair inside it."""
    eps = 1.5 / 1024
    return 1.0 - (k + 1) / BANDS + eps, 1.0 - k / BANDS - eps


# A sash window cut from the plaster sheet (band 2, bay 0, its sill included): u, v ranges (Collada v up)
WIN_UV = (53 / 1024, 154 / 1024, 1.0 - (512 + 238) / 1024, 1.0 - (512 + 45) / 1024)
WIN_W, WIN_H, WIN_SILL = 1.05, 1.75, 0.85   # m
HOUSE_BAY_M = 3.4           # m between a house's windows
DOOR_GAP_M = 0.75           # m each side of a front door kept free of the ground floor's windows
WIN_OUT_M = 0.03            # m the window stands proud of its wall


def _plain_wall_with_windows(m, spec, a, b, L, z0, z_eave, wallmat, mat_idx, edge=None):
    """A wall of a plain tileable material (world-scaled UV) with sash windows laid over it, a whole
    number per floor centred in their bays."""
    su = SIDING_UV_M
    m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z_eave), (a[0], a[1], z_eave),
           (0, z0 / su), (L / su, z0 / su), (L / su, z_eave / su), (0, z_eave / su), wallmat)
    if spec.windows is False:
        if spec.front is not None and spec.front == edge and L > 4.0:
            # a big box's storefront: the shopfront band over the wall, a whole number of its bays
            d = (b - a) / L
            out = np.array([d[1], -d[0]]) * WIN_OUT_M
            base = z_eave - spec.floors * spec.floor_h
            zt = min(base + GROUND_FLOOR_SHOP_M, z_eave - 0.6)
            nbs = max(1, int(round(L * 0.7 / (TILE_PX_M / BAND_SHOP[1]))))
            v0, v1 = _band_v(BAND_SHOP, BAND_SHOP[0])
            p, q = a + d * (L * 0.15) + out, a + d * (L * 0.85) + out
            m.quad((p[0], p[1], base), (q[0], q[1], base), (q[0], q[1], zt), (p[0], p[1], zt),
                   (0, v0), (nbs / BAND_SHOP[1], v0), (nbs / BAND_SHOP[1], v1), (0, v1), mat_idx[SHOP])
        return
    nb = int(L // HOUSE_BAY_M)
    if nb < 1 or L < 2.0:
        return
    d = (b - a) / L
    out = np.array([d[1], -d[0]]) * WIN_OUT_M
    win = mat_idx["bne_bld_fac_white"]
    u0, u1, v0, v1 = WIN_UV
    base = z_eave - spec.floors * spec.floor_h
    for f in range(spec.floors):
        zf = base + f * spec.floor_h + WIN_SILL
        if zf + WIN_H > z_eave - 0.2:
            continue
        for k in range(nb):
            if f == 0 and nb % 2 == 1 and k == nb // 2:
                continue                                # where a front door may stand (doors(): the middle)
            c = a + d * (L * (k + 0.5) / nb) + out
            p, q = c - d * WIN_W / 2, c + d * WIN_W / 2
            m.quad((p[0], p[1], zf), (q[0], q[1], zf), (q[0], q[1], zf + WIN_H), (p[0], p[1], zf + WIN_H),
                   (u0, v0), (u1, v0), (u1, v1), (u0, v1), win)


def _walls(m, spec, ring, z0, z_eave, mat_idx, lite=False):
    """Each wall of the ring, floor by floor: one band of the window sheet, a whole number of bays."""
    n = len(ring)
    fac = mat_idx[spec.facade]
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        L = float(math.hypot(*(b - a)))
        if L < 0.05:
            continue
        if spec.wall:
            _plain_wall_with_windows(m, spec, a, b, L, z0, z_eave, fac, mat_idx, i)
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
                    dk = None
                    if f == 0 and spec.door and spec.door[0] == i and spec.roof == "flat":
                        dk = min(nb - 1, int(spec.door[1] / L * nb))      # the bay behind the front door
                    for k in range(nb):
                        g = good[int(_hash01(spec.seed, i, f, k) * len(good))]
                        pa, pb = a + (b - a) * (k / nb), a + (b - a) * ((k + 1) / nb)
                        if k == dk:
                            m.quad((pa[0], pa[1], za), (pb[0], pb[1], za), (pb[0], pb[1], zb), (pa[0], pa[1], zb),
                                   (0.002, v0), (0.012, v0), (0.012, v0 + 0.01), (0.002, v0 + 0.01), mat)
                            continue
                        m.quad((pa[0], pa[1], za), (pb[0], pb[1], za), (pb[0], pb[1], zb), (pa[0], pa[1], zb),
                               (g / bays_per_tile, v0), ((g + 1) / bays_per_tile, v0), ((g + 1) / bays_per_tile, v1),
                               (g / bays_per_tile, v1), mat)
                    za = zb
                    continue
            v0, v1 = _band_v(spec.band, band_k)
            door = spec.door if (f == 0 and spec.roof == "flat" and spec.door and spec.door[0] == i) else None
            if door is not None and DOOR_GAP_M < door[1] < L - DOOR_GAP_M:
                # the band either side of the door; a plain strip of the sheet's wall behind it
                s0, s1 = door[1] - DOOR_GAP_M, door[1] + DOOR_GAP_M
                for ta, tb in ((0.0, s0), (s1, L)):
                    pa, pb = a + (b - a) * (ta / L), a + (b - a) * (tb / L)
                    m.quad((pa[0], pa[1], za), (pb[0], pb[1], za), (pb[0], pb[1], zb), (pa[0], pa[1], zb),
                           (uu * ta / L, v0), (uu * tb / L, v0), (uu * tb / L, v1), (uu * ta / L, v1), mat)
                pa, pb = a + (b - a) * (s0 / L), a + (b - a) * (s1 / L)
                m.quad((pa[0], pa[1], za), (pb[0], pb[1], za), (pb[0], pb[1], zb), (pa[0], pa[1], zb),
                       (0.002, v0), (0.012, v0), (0.012, v0 + 0.01), (0.002, v0 + 0.01), mat)
                za = zb
                continue
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
        # the lip's inner face, above the roof (seen from on the roof it was missing: the user's report, 2026-10-14)
        m.quad((b[0], b[1], z_eave), (a[0], a[1], z_eave), (a[0], a[1], z1), (b[0], b[1], z1), *uv, mat)


def _inside(ring, p):
    """Point p inside the ring (even-odd)."""
    x, y = p
    if fast._ef is not None and ring.dtype == np.float64 and ring.ndim == 2:
        return fast._ef.point_inside(ring, float(x), float(y))
    xs, ys = ring[:, 0], ring[:, 1]
    xj, yj = np.roll(xs, 1), np.roll(ys, 1)
    cross = ((ys > y) != (yj > y)) & (x < (xj - xs) * (y - ys) / np.where(yj - ys == 0, 1e-12, yj - ys) + xs)
    return bool(np.count_nonzero(cross) % 2)


def _box(m, c, u, hx, hy, z0, z1, mat):
    """An axis-u box (half sizes hx along u, hy across) from z0 to z1: four sides and a top."""
    v = np.array([-u[1], u[0]])
    k = [c + u * sx * hx + v * sy * hy for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    uv = ((0.002, 0.995), (0.012, 0.995), (0.012, 0.999), (0.002, 0.999))
    for i in range(4):
        a, b = k[i], k[(i + 1) % 4]
        m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1), *uv, mat)
    m.quad(*[(p[0], p[1], z1) for p in k], *uv, mat)


def _signs(m, spec, ring, z_top, mat_idx):
    """The sign band over a shop's street wall (spec.sign: its edge), shop by shop, a few without one."""
    edge = spec.sign
    a, b = ring[edge], ring[(edge + 1) % len(ring)]
    L = float(math.hypot(*(b - a)))
    if L < 3.0:
        return
    d = (b - a) / L
    out = np.array([d[1], -d[0]])
    t, k = 0.3, 0
    while t < L - 1.5:
        w = SIGN_SHOP_M[0] + (SIGN_SHOP_M[1] - SIGN_SHOP_M[0]) * _hash01(spec.seed, "signw", k)
        t1 = min(L - 0.3, t + w)
        if L - 0.3 - t1 < 2.5:                         # no sliver of a shop at the end
            t1 = L - 0.3
        if _hash01(spec.seed, "signon", k) < 0.85:
            c = a + d * ((t + t1) / 2) + out * (SIGN_OUT / 2)
            look = f"sign|{int(_hash01(spec.seed, 'signc', k) * len(SIGN_TINTS))}"
            _box(m, c, d, (t1 - t) / 2 - 0.15, SIGN_OUT / 2, z_top - 0.15 - SIGN_H, z_top - 0.15, mat_idx[look])
        t, k = t1, k + 1


PORCH_DEPTH_M = 2.2
PORCH_H_M = 2.7             # the porch roof's underside above the floor
PORCH_DECK_M = 0.35         # the deck's top above the ground


def _porch(m, spec, base, mat_idx):
    """A front porch on the front door's wall (spec.door: (edge, distance along it) of spec.ring): a deck, three
    posts and a shallow roof, the house's trim white and its roof's colour."""
    edge, sd = spec.door
    ring = spec.ring
    a, b = ring[edge], ring[(edge + 1) % len(ring)]
    L = float(math.hypot(*(b - a)))
    if L < 4.0:
        return
    d = (b - a) / L
    out = np.array([d[1], -d[0]])
    w = min(L * 0.75, 7.5)
    c0 = float(np.clip(sd, w / 2, L - w / 2))
    c = a + d * c0
    white = mat_idx["bne_bld_fac_white"]
    D = PORCH_DEPTH_M
    _box(m, c + out * (D / 2), d, w / 2, D / 2, base - 0.3, base + PORCH_DECK_M, white)              # deck
    top = base + PORCH_DECK_M + PORCH_H_M
    for t in (-w / 2 + 0.15, 0.0, w / 2 - 0.15):                                                   # posts
        _box(m, c + d * t + out * (D - 0.15), d, 0.08, 0.08, base + PORCH_DECK_M, top, white)
    roof = mat_idx[spec.roof_mat] if spec.roof_mat else white
    _box(m, c + out * (D / 2 + 0.1), d, w / 2 + 0.15, D / 2 + 0.1, top, top + 0.18, roof)          # roof


def _stoop(m, spec, ring, base, mat_idx):
    """Two white steps out from the front door (spec.stoop: (edge, distance along it))."""
    edge, sd = spec.stoop
    a, b = ring[edge], ring[(edge + 1) % len(ring)]
    L = float(math.hypot(*(b - a)))
    if L < 1e-6:
        return
    d = (b - a) / L
    out = np.array([d[1], -d[0]])
    c = a + d * sd
    white = mat_idx["bne_bld_fac_white"]
    for k, (depth, z1) in enumerate(((0.9, STOOP_H), (0.45, STOOP_H / 2))):
        p = c + out * (0.9 + depth / 2) if k == 1 else c + out * depth / 2
        _box(m, p, d, 0.8, depth / 2, base - 0.3, base + z1, white)


def _steeple(m, spec, z_eave, mat_idx):
    """A church's square tower with a pyramid spire, at one end of its nave (hashed)."""
    c, u, hl, hw = oriented_box(spec.ring)
    if _hash01(spec.seed, "steeple") < 0.5:
        u = -u
    side = float(np.clip(1.1 * hw, 3.0, 6.0)) / 2
    p = c + u * (hl - side)
    for k in range(1, 12):              # on the footprint (an irregular one's box end can be outside it)
        if _inside(spec.ring, p):
            break
        p = c + u * (hl - side) * (1 - k / 12)
    rise = (min(hw, 12.0) + EAVE_M) * math.tan(math.radians(spec.pitch or PITCH["gabled"]))
    top = z_eave + rise + 6.0
    _box(m, p, u, side, side, z_eave - 1.0, top, mat_idx[spec.facade])
    v = np.array([-u[1], u[0]])
    k = [p + u * sx * side + v * sy * side for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    apex = (float(p[0]), float(p[1]), top + 2.6 * side * 2)
    rm = mat_idx[f"{ROOF_SLATE}|3"]
    for i in range(4):
        a, b = k[i], k[(i + 1) % 4]
        m.tri((a[0], a[1], top), (b[0], b[1], top), apex, (0, 0), (1, 0), (0.5, 2.0), rm)


ROOF_CLUTTER_M2 = 250.0     # flat roofs this large get boxes: air-conditioning units, stair bulkheads


def _clutter(m, spec, ring, z_eave, mat_idx):
    area = _area2(ring) / 2
    if spec.roof != "flat" or spec.plain or area < ROOF_CLUTTER_M2 or spec.cls in CHURCH_CLASSES:
        return
    c, u, hl, hw = oriented_box(ring)
    v = np.array([-u[1], u[0]])
    n = min(6, 1 + int(area / 600.0))
    mat = mat_idx["bne_bld_fac_grey"]
    for k in range(n):
        s = (_hash01(spec.seed, "cx", k) * 2 - 1) * (hl - 3.0)
        t = (_hash01(spec.seed, "cy", k) * 2 - 1) * (hw - 3.0)
        p = c + u * s + v * t
        if not _inside(ring, p):
            continue
        big = _hash01(spec.seed, "cb", k) < 0.25
        hx, hy, h = (2.0, 1.6, 2.6) if big else (1.1, 0.7, 1.1)
        _box(m, p, u, hx, hy, z_eave, z_eave + h, mat)


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
    rise = hw2 * math.tan(math.radians(roof_pitch(spec)))
    ze = z_eave - EAVE_M * math.tan(math.radians(roof_pitch(spec)))
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


# The engine draws a shape's mesh with 16-bit indices: past 65,535 vertices they wrap and triangles join far-apart
# vertices (the user's report 2026-10-14: slabs across a Fells Point street; dense tiles had up to 81k vertices).
MAX_MESH_VERTS = 60000


def build_parts(specs_and_ground, lite=False, max_verts=MAX_MESH_VERTS):
    """build() split into meshes of at most max_verts vertices each (whole buildings per mesh): a list, [] if empty."""
    parts, cur, n = [], [], 0
    for item in specs_and_ground:
        one = build([item], lite)
        k = 0 if one is None else len(one[0])
        if cur and n + k > max_verts:
            mesh = build(cur, lite)
            if mesh is not None:
                parts.append(mesh)
            cur, n = [], 0
        cur.append(item)
        n += k
    if cur:
        mesh = build(cur, lite)
        if mesh is not None:
            parts.append(mesh)
    return parts


# A parking structure you can drive into (the user, 2026-10-14: "garage buildings ... are solid"): open storeys of
# concrete decks on perimeter columns, a parapet round each upper deck, and a two-lane switchback ramp along the
# longest wall (one lane up per storey, a U-turn landing at each end). Only where the ramp fits inside the footprint
# and the ground under it is near level; otherwise the closed box as before. The tile's mesh collides as drawn.
DECK_LANE_M = 3.6           # a ramp lane
DECK_EDGE_M = 0.8           # the ramp lanes start this far in from the wall (the columns stand there)
DECK_LANDING_M = 7.0        # flat deck at each end of the ramps (the U-turn)
DECK_SLOPE = 0.17           # the steepest ramp
DECK_SLAB_M = 0.3
DECK_PARAPET_M = 1.0
DECK_COLUMN_M = 9.0         # perimeter column spacing
DECK_LEVEL_M = 3.5          # ground this uneven (max - min) under the footprint: no deck (up to it: the ground deck
                            # level at the highest ground on a plinth, driven into from the uphill side)
DECK_HEADROOM_M = 2.6       # the lowest storey height that still drives


def _deck_layout(ring, storey):
    """The ramp's frame on the longest edge of the counter-clockwise ring: (edge index i, A, along unit u, inward
    unit n, s0, s1), the ramps between arc lengths s0 and s1; None when they do not fit (too short for DECK_SLOPE,
    or the well outside the ring)."""
    i = max(range(len(ring)), key=lambda k: float(np.hypot(*(ring[(k + 1) % len(ring)] - ring[k]))))
    a, b = ring[i], ring[(i + 1) % len(ring)]
    L = float(np.hypot(*(b - a)))
    u = (b - a) / L
    n = np.array([-u[1], u[0]])                 # the inside is on the left
    s0, s1 = 0.5 + DECK_LANDING_M, L - 0.5 - DECK_LANDING_M
    if s1 - s0 < storey / DECK_SLOPE:
        return None
    depth = DECK_EDGE_M + 2 * DECK_LANE_M
    for s in np.linspace(0.5, L - 0.5, 9):
        for d in (0.3, depth / 2, depth + 0.5, depth + DECK_LANDING_M / 2):
            if not _inside(ring, a + u * s + n * d):
                return None
    return i, a, u, n, s0, s1


def _deck_wall(m, p, q, z0, z1, mat, t=0.2):
    """A thin wall (both faces and a top) from p to q, z0 to z1."""
    d = q - p
    L = float(np.hypot(*d))
    if L > 0.05:
        _box(m, (p + q) / 2, d / L, L / 2, t / 2, z0, z1, mat)


def deck_plan(spec, gmin, gmax):
    """(counter-clockwise ring, floors, storey height, _deck_layout) of a parking structure drawn as open decks, or
    None (not a garage, ground too uneven, storeys too low, the ramp does not fit: a closed box)."""
    if not spec.deck or gmax - gmin > DECK_LEVEL_M or not spec.height or (spec.min_h or 0.0) > 0.5:
        return None
    floors = max(1, int(spec.floors or 1))
    storey = spec.height / floors
    if storey < DECK_HEADROOM_M:
        return None
    ring = np.asarray(spec.ring, np.float64)
    if _area2(ring) < 0:
        ring = ring[::-1]
    lay = _deck_layout(ring, storey)
    return None if lay is None else (ring, floors, storey, lay)


def _parking_deck(m, spec, gmin, gmax, mat_idx):
    """The open parking structure (see DECK_*); False when it does not apply (the caller draws the box)."""
    plan_ = deck_plan(spec, gmin, gmax)
    if plan_ is None:
        return False
    ring, floors, storey, (i, A, u, n, s0, s1) = plan_
    # weathered concrete (Baltimore's street photos: the white decks read as a skeleton; city garages are grey)
    mat = mat_idx[f"{PANEL}|{len(WALL_TINTS[PANEL]) - 1 - int(_hash01(spec.seed, 'deck') * 2)}"]
    W, e = DECK_LANE_M, DECK_EDGE_M
    depth = e + 2 * W
    zs = [gmax + 0.03 + k * storey for k in range(floors + 1)]

    def at(s, d):
        return A + u * s + n * d

    def uvp(p):
        return (float(p[0]) / 4.0, float(p[1]) / 4.0)

    k_ring = len(ring)
    m.poly([(p[0], p[1], zs[0]) for p in ring], [uvp(p) for p in ring], mat)          # the ground deck, whole
    if gmax - gmin > 0.05:                      # its plinth down to the lowest ground (a sloping street)
        for j in range(k_ring):
            p, q = ring[j], ring[(j + 1) % k_ring]
            m.quad((p[0], p[1], gmin - FOUNDATION_M), (q[0], q[1], gmin - FOUNDATION_M), (q[0], q[1], zs[0]),
                   (p[0], p[1], zs[0]), (0, 0), (1, 0), (1, 0.1), (0, 0.1), mat)
    # the upper decks: the ring with the ramp well (both lanes, s0..s1) cut in from the longest edge
    well = [at(s0, 0.0), at(s0, depth), at(s1, depth), at(s1, 0.0)]
    pts = [A] + well + [ring[(i + 1 + j) % k_ring] for j in range(k_ring - 1)]
    for k in range(1, floors + 1):
        z = zs[k]
        m.poly([(p[0], p[1], z) for p in pts], [uvp(p) for p in pts], mat)
        m.poly([(p[0], p[1], z - DECK_SLAB_M) for p in pts[::-1]], [uvp(p) for p in pts[::-1]], mat)
        for j in range(len(pts)):
            p, q = pts[j], pts[(j + 1) % len(pts)]
            m.quad((p[0], p[1], z - DECK_SLAB_M), (q[0], q[1], z - DECK_SLAB_M), (q[0], q[1], z), (p[0], p[1], z),
                   (0, 0), (1, 0), (1, 0.1), (0, 0.1), mat)
            if not (1 <= j <= 3):               # the parapet, not round the well (below)
                _deck_wall(m, p, q, z, z + DECK_PARAPET_M, mat)
        # round the well: its inner side and the end away from the ramps' landing (ramp k - 1 comes up at s1 when
        # k - 1 is even, and ramp k leaves from that same end); the top deck has no ramp leaving: its lane closed
        land = s1 if (k - 1) % 2 == 0 else s0
        far = s0 if land == s1 else s1
        _deck_wall(m, at(s0, depth), at(s1, depth), z, z + DECK_PARAPET_M, mat)
        _deck_wall(m, at(far, 0.0), at(far, depth), z, z + DECK_PARAPET_M, mat)
        if k == floors:
            d_free = e + (k % 2) * W
            _deck_wall(m, at(land, d_free), at(land, d_free + W), z, z + DECK_PARAPET_M, mat)
    # the ramps: ramp k from deck k to deck k + 1 on lane k % 2, even ones climbing from s0 to s1, walled both sides
    for k in range(floors):
        d0 = e + (k % 2) * W
        sa, sb = (s0, s1) if k % 2 == 0 else (s1, s0)
        za, zb = zs[k], zs[k + 1]
        top = [(at(sa, d0), za), (at(sb, d0), zb), (at(sb, d0 + W), zb), (at(sa, d0 + W), za)]
        if sb < sa:                             # counter-clockwise seen from above
            top = top[::-1]
        quad = [(p[0], p[1], z) for p, z in top]
        m.quad(*quad, (0, 0), (1, 0), (1, 1), (0, 1), mat)
        m.quad(*[(x, y, z - DECK_SLAB_M) for x, y, z in quad[::-1]], (0, 0), (1, 0), (1, 1), (0, 1), mat)
        for d in (d0, d0 + W):
            steps = 6
            for j in range(steps):
                f0, f1 = j / steps, (j + 1) / steps
                ca, cb = at(sa + (sb - sa) * f0, d), at(sa + (sb - sa) * f1, d)
                _deck_wall(m, ca, cb, za + (zb - za) * f0, za + (zb - za) * f1 + DECK_PARAPET_M, mat, 0.15)
    # the columns: round the outline every DECK_COLUMN_M, from the foundation to the top deck
    for j in range(k_ring):
        p, q = ring[j], ring[(j + 1) % k_ring]
        L = float(np.hypot(*(q - p)))
        if L < 0.5:
            continue
        d = (q - p) / L
        inward = np.array([-d[1], d[0]])
        for t in np.arange(0.0, L, DECK_COLUMN_M):
            _box(m, p + d * t + inward * 0.35, d, 0.25, 0.25, zs[0] - FOUNDATION_M, zs[-1], mat)
    return True


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
        mh = spec.min_h or 0.0
        if spec.deck and not lite and _parking_deck(m, spec, gmin, gmax, mat_idx):
            continue
        z0 = gmin - FOUNDATION_M if mh <= 0.5 else gmax + mh
        z_eave = gmax + mh + spec.height
        _walls(m, spec, ring, z0, z_eave, mat_idx, lite)
        if mh > 0.5:                                  # a part standing on another / over a street: its underside
            m.poly([(p[0], p[1], z0) for p in ring[::-1]], [(0.002, 0.995)] * len(ring), mat_idx[spec.facade])
        _roof(m, spec, ring, z_eave, mat_idx)
        if spec.roof == "flat" and not spec.plain and spec.floors <= 6 and not lite:
            _cornice(m, spec, ring, z_eave, mat_idx)
        if not lite:
            _clutter(m, spec, ring, z_eave, mat_idx)
        if spec.steeple:
            _steeple(m, spec, z_eave, mat_idx)
        if spec.stoop and not lite:
            _stoop(m, spec, ring, gmax, mat_idx)
        if spec.porch and spec.door and not lite:
            _porch(m, spec, gmax, mat_idx)
        if spec.sign is not None and spec.roof == "flat" and not lite:
            if spec.windows is False:           # a big box: over its storefront band (_plain_wall_with_windows)
                z_top = min(gmax + GROUND_FLOOR_SHOP_M + 1.15, z_eave)
            else:
                z_top = z_eave - (spec.floors - 1) * spec.floor_h if spec.floors > 1 else z_eave
            _signs(m, spec, ring, z_top, mat_idx)
    if m.n == 0:
        return None
    P, UV, T, M, C = m.arrays()
    return P, UV, T, M, MATERIALS, C


def collision_mesh(specs_and_ground):
    """The tile's buildings for physics only: one quad per wall and a flat lid at the eaves (pitched roofs on their
    oriented box), parking decks as drawn; (positions, uvs, tris, tri materials, MATERIALS, colours) or None.
    The drawn mesh (windows a quad per bay and floor, cornices, signs) cost the game's collision rebuild ~30 ms of
    a 40 ms rebuild in Fells Point (2026-10-15), every few seconds while driving; this one is ~1/20 of its triangles."""
    m = Mesh()
    tok = (MATERIALS.index(PANEL), (128, 128, 128, 255))
    zero = (0.0, 0.0)
    for spec, gmin, gmax in specs_and_ground:
        if deck_plan(spec, gmin, gmax) is not None:
            _parking_deck(m, spec, gmin, gmax, _Looks(spec.seed))
            continue
        ring = np.asarray(spec.ring, np.float64)
        if spec.roof != "flat":
            c, u, hl, hw = oriented_box(ring)
            v = np.array([-u[1], u[0]])
            ring = np.array([c - u * hl - v * hw, c + u * hl - v * hw, c + u * hl + v * hw, c - u * hl + v * hw])
        if _area2(ring) < 0:
            ring = ring[::-1]
        mh = spec.min_h or 0.0
        z0 = gmin - FOUNDATION_M if mh <= 0.5 else gmax + mh
        z1 = gmax + mh + spec.height
        for i in range(len(ring)):
            a, b = ring[i], ring[(i + 1) % len(ring)]
            m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1), zero, zero, zero, zero, tok)
        m.poly([(p[0], p[1], z1) for p in ring], [zero] * len(ring), tok)
        if mh > 0.5:                # over a street: its underside
            m.poly([(p[0], p[1], z0) for p in ring[::-1]], [zero] * len(ring), tok)
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
    if fast._ef is not None and hasattr(fast._ef, "shared_walls"):     # the Rust loop: the same counts
        offs = np.zeros(n + 1, np.int64)
        offs[1:] = np.cumsum([len(r) for r in rings])
        coords = np.ascontiguousarray(np.concatenate(rings)[:, :2], np.float64)
        return np.asarray(fast._ef.shared_walls(coords, offs, np.ascontiguousarray(cents, np.float64),
                                                np.ascontiguousarray(rads, np.float64), order.astype(np.int64),
                                                float(tol)), np.int64)
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


def _part_heights(a, i):
    """A building part's (own height, its bottom) from its attributes, or None without a height."""
    top = a["height"][i] if "height" in a else np.nan
    mh = a["min_height"][i] if "min_height" in a else np.nan
    mh = float(mh) if np.isfinite(mh) and mh > 0 else 0.0
    if not np.isfinite(top) or top <= mh + 1.0:
        nf, f0 = (a[k][i] if k in a else np.nan for k in ("num_floors", "min_floor"))
        if np.isfinite(nf) and nf > 0:
            f0 = float(f0) if np.isfinite(f0) and f0 > 0 else 0.0
            mh = mh or f0 * FLOOR_M
            return max(FLOOR_M, (float(nf) - f0) * FLOOR_M), mh
        return None
    return float(top) - mh, mh


ROAD_THROUGH_M = 15.0       # a footprint that public roads run through this far is a structure over them
OVER_ROAD_CLEAR_M = 5.5     # ... lifted this high (an expressway's or a station's slab over the streets: Baltimore's
                            # 17,500 m2 one by I-83, the AI drove into its wall at 35 mph, 2026-10-14)
THROUGH_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
                   "living_street"}


def tunnel_lines(frame, road_layers, x0, y0, margin=50.0, size=256.0):
    """Polylines (k, 2) relative to (x0, y0) of the road tunnels near the tile (roads.tunnel_parts; for
    road_through: a building a tunnel runs into is its portal building, its floor over the tube)."""
    import roads as roadsmod
    out = []
    for f in road_layers:
        if f is None or len(f.kind) == 0:
            continue
        ts = roadsmod.tunnel_specs(f)
        for i in range(len(f.kind)):
            if ts[i] is None:
                continue
            for ring in roadsmod.tunnel_parts(f, i):
                e, n = mapgeom.enu_xy(f, frame, ring)
                xy = np.stack([e - x0, n - y0], 1)
                if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                        or xy[:, 1].min() > size + margin:
                    continue
                out.append(xy)
    return out


TUNNEL_THROUGH_M = 5.0      # a footprint a road tunnel runs into this far: its portal building, lifted over the tube
OVER_TUNNEL_CLEAR_M = 7.5   # (the tube's vault is roadmesh.TUNNEL_H = 6.8 m over the floor; 2026-10-17: Eisenhower's
                            # east portal building closed the tunnel)


def road_lines(frame, road_layers, x0, y0, margin=50.0, size=256.0):
    """Polylines (k, 2) relative to (x0, y0) of the public roads on the ground near the tile (for road_through)."""
    import roads as roadsmod
    out = []
    for f in road_layers:
        if f is None or len(f.kind) == 0:
            continue
        cls = f.attrs.get("class")
        for i in range(len(f.kind)):
            if cls is None or str(cls[i]) not in THROUGH_CLASSES or roadsmod.level_ranges(f, i):
                continue
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
            ll = f.lonlat[a:b]
            if len(ll) < 2:
                continue
            e, n = mapgeom.enu_xy(f, frame, ll)
            xy = np.stack([e - x0, n - y0], 1)
            if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                    or xy[:, 1].min() > size + margin:
                continue
            out.append(xy)
    return out


def road_through(ring, lines, step=2.0):
    """Metres of the polylines lines (k, 2) inside ring (sampled every step m)."""
    lo, hi = ring.min(0), ring.max(0)
    total = 0.0
    for ln in lines:
        if ln[:, 0].max() < lo[0] or ln[:, 0].min() > hi[0] or ln[:, 1].max() < lo[1] or ln[:, 1].min() > hi[1]:
            continue
        for p, q in zip(ln[:-1], ln[1:]):
            L = float(np.hypot(*(q - p)))
            k = max(1, int(L / step))
            for t in (np.arange(k) + 0.5) / k:
                if _inside(ring, p + (q - p) * t):
                    total += L / k
    return total


CENTRE_PREFILTER_M = 1.0       # m of slack for Features.outer_centres (cumulative sums: within ~1e-5 m of the exact mean)


def tile_buildings(frame, x0, y0, x1, y1, layers, elev, water=None, part_layers=(), west_town=False, roads_xy=None,
                   tunnels_xy=None):
    """[(Spec, gmin, gmax)] of the buildings whose footprint centroid is in the tile [x0, x1) x [y0, y1)
    (anchor frame), rings relative to (x0, y0). layers: mapgeom.Features of buildings (one per map cell; a
    building in two cells is taken once, by id); elev(lat, lon) the physics ground. part_layers: the
    building parts of the same cells: a building whose parts all have heights is drawn as its parts
    (each from its bottom to its top, the building's look)."""
    parts_of = {}                       # building id -> [(ring in the anchor frame, own height, bottom)]
    pseen = set()
    for f in part_layers:
        if f is None or len(f.kind) == 0:
            continue
        a = f.attrs
        for i in range(len(f.kind)):
            pid = f.ids[i]
            if pid in pseen:
                continue
            pseen.add(pid)
            bid = str(a["building_id"][i]) if "building_id" in a else ""
            if not bid:
                continue
            pp = f.parts(i)
            if not pp or not pp[0]:
                continue
            outer = np.asarray(pp[0][0], np.float64)
            if len(outer) < 4:
                continue
            e, n = mapgeom.enu_xy(f, frame, outer)
            parts_of.setdefault(bid, []).append((np.stack([e, n], 1), _part_heights(a, i)))
    seen = set()
    rings, attrs, looks, mins = [], [], [], []
    for f in layers:
        if f is None or len(f.kind) == 0:
            continue
        a = f.attrs
        ids = getattr(f, "ids", None)
        rows = range(len(f.kind))
        if isinstance(f, mapgeom.Features):     # only the buildings whose centre may be in the tile (the exact test below)
            cx_, cy_ = f.outer_centres(frame)
            m = CENTRE_PREFILTER_M
            rows = np.flatnonzero((cx_ >= x0 - m) & (cx_ < x1 + m) & (cy_ >= y0 - m) & (cy_ < y1 + m)).tolist()
        for i in rows:
            if a.get("is_underground") is not None and bool(a["is_underground"][i]):
                continue
            lv = a["level"][i] if a.get("level") is not None else None
            if lv is not None and np.isfinite(lv) and lv < 0:
                continue                # below ground (Shot Tower Metro, level -2, stood across a Baltimore street)
            key = ids[i] if ids is not None else None
            if key is not None and key in seen:
                continue
            parts = f.parts(i)
            if not parts or not parts[0]:
                continue
            outer = np.asarray(parts[0][0], np.float64)
            if len(outer) < 4:
                continue
            e, n = mapgeom.enu_xy(f, frame, outer)
            k = len(e) - 1             # the same reduction as np.mean, without its wrapper (36k calls a tile set)
            cx, cy = float(e[:-1].sum() / k), float(n[:-1].sum() / k)
            if not (x0 <= cx < x1 and y0 <= cy < y1):
                continue
            at = {k: (a[k][i] if k in a else None) for k in
                  ("class", "height", "num_floors", "min_height", "roof_shape", "roof_height", "name")}
            bparts = parts_of.get(str(key)) if key is not None else None
            if bparts and any(h is not None for _, h in bparts):
                # drawn as its parts, with the building's class and one look for all of them; a part
                # without a height takes the building's (else its lowest known part's)
                look = _hash01(str(key), "look")
                bh = at.get("height")
                fill = float(bh) if bh is not None and np.isfinite(bh) and bh > 0 else \
                    min(h[0] + h[1] for _, h in bparts if h is not None)
                top = max([fill] + [h[0] + h[1] for _, h in bparts if h is not None])
                for pring, hh in bparts:
                    ph, pmh = hh if hh is not None else (fill, 0.0)
                    r = clean_ring(pring - np.array([x0, y0]))
                    if r is None or _area2(r) / 2 < 4.0:
                        continue
                    rings.append(r)
                    attrs.append({"class": at["class"], "height": ph, "num_floors": None, "roof_shape": "flat",
                                  "building_height": top})
                    looks.append(look)
                    mins.append(pmh)
                if key is not None:
                    seen.add(key)
                continue
            ring = clean_ring(np.stack([e - x0, n - y0], 1))
            if ring is None or _area2(ring) / 2 < MIN_AREA_M2:
                continue
            if key is not None:
                seen.add(key)
            rings.append(ring)
            mh = at.get("min_height")
            mh = float(mh) if mh is not None and np.isfinite(mh) and mh > 0 else 0.0
            if mh <= 0.5 and roads_xy and road_through(ring, roads_xy) >= ROAD_THROUGH_M:
                mh = OVER_ROAD_CLEAR_M
                h = at.get("height")
                if h is None or not np.isfinite(h) or h < mh + 3.0:
                    at = dict(at, height=mh + 3.0)      # keeps its top; at least a slab's worth over the clearance
            if mh < OVER_TUNNEL_CLEAR_M and tunnels_xy and road_through(ring, tunnels_xy) >= TUNNEL_THROUGH_M:
                mh = OVER_TUNNEL_CLEAR_M
                h = at.get("height")
                if h is None or not np.isfinite(h) or h < mh + 3.0:
                    at = dict(at, height=mh + 3.0)
            attrs.append(at)
            looks.append(None)
            mins.append(mh)
    if not rings:
        return []
    shared = _shared_walls(rings)
    cover = sum(_area2(r) / 2 for r, mh in zip(rings, mins) if mh <= 0.5) / ((x1 - x0) * (y1 - y0))
    town, dense = cover < TOWN_COVERAGE, cover > BRICK_WALK_COVERAGE
    clat, clon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
    clat, clon = float(clat), float(clon)
    out = []
    for ring, at, sh, look, mh in zip(rings, attrs, shared, looks, mins):
        if mh > 0.5 and at.get("height") is not None and np.isfinite(at["height"]) and look is None:
            at = dict(at, height=max(2.5, float(at["height"]) - mh))     # a building's own height range
        spec = plan(ring, at, int(sh) if look is None else 0, clat, clon, look, town, dense, west_town)
        spec.min_h = mh
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
        if deck_plan(spec, gmin, gmax) is not None:     # an open parking deck: driven into on purpose
            continue
        r = spec.ring
        out.append([round(float(r[:, 0].min() + x0), 2), round(float(r[:, 1].min() + y0), 2),
                    round(gmin - FOUNDATION_M, 2), round(float(r[:, 0].max() + x0), 2),
                    round(float(r[:, 1].max() + y0), 2), round(gmax + spec.height + 6.0, 2)])
    return out


def write(path, mesh, collision_only=False):
    """The tile's .cdae (cdae.encode with one primitive per material; collision_only: a never-drawn collision detail)."""
    import cdae
    P, UV, T, M, mats, C = mesh
    return cdae.write(path, P, T, uvs=UV, material=list(mats), tri_materials=M, object_name="buildings", colors=C,
                      collision_only=collision_only)


STOOP_SHARE = 0.8           # row houses with a front stoop (Baltimore's white marble steps)
STOOP_H = 0.36              # m: two steps up to the door
DOOR_ROAD_M = 30.0          # a front door on the wall facing the nearest road within this
DOOR_CLASSES_SKIP = SHED_CLASSES | INDUSTRIAL_CLASSES
G_DOOR = "door"
FAR_SEND_M = 1600.0         # far building tiles (render-only, the lite mesh) sent within this (Chebyshev)
FAR_DROP_M = 1900.0         # ... dropped beyond
FAR_PER_POSE = 4


MAIN_STREET_CLASSES = {"primary", "secondary", "tertiary"}
BACK_ROAD_CLASSES = {"service", "track", "path", "footway", "cycleway", "pedestrian", "steps", "alley"}
MAIN_STREET_SETBACK_M = 7.0  # m from a main street's edge within which a front wall has a shopfront
MAIN_STREET_SHOPS = 0.75     # the share of such buildings that do


DRIVEWAY_W = 3.2            # m
DRIVEWAY = {"m": "road_asphalt_2lane", "tl": 11.0, "p": 19}     # a road tile chunk's look (roads.LOOK)


def doors(items, lines, x0, y0, drives=None):
    """Prop rows (trees.lua rows, group "door") of a front door per house: on the wall whose middle is
    nearest a road and faces it, a row house's to one side, others' in the middle. lines: [(centreline
    (k, 2) in the anchor frame, half width[, class, urban])]. A building close to an urban main street gets
    a shopfront ground floor instead (spec.shop, most of them). drives (a list) gets a driveway chunk (road
    tile chunk dicts, draped by roadmesh) from each detached house's front to the road's edge."""
    if not lines:
        return []
    segs, smain, shalf, sminor = [], [], [], []
    for ln in lines:
        pts, half = ln[0], ln[1]
        main = len(ln) > 3 and ln[3] and ln[2] in MAIN_STREET_CLASSES     # an urban main street
        p = np.asarray(pts, np.float64)
        if len(p) >= 2:
            segs.append(np.concatenate([p[:-1], p[1:]], 1))
            smain.append(np.full(len(p) - 1, bool(main)))
            shalf.append(np.full(len(p) - 1, float(half)))
            sminor.append(np.full(len(p) - 1, len(ln) > 2 and ln[2] in BACK_ROAD_CLASSES))
    if not segs:
        return []
    S = np.concatenate(segs)                             # (n, 4) x0 y0 x1 y1
    SMAIN, SHALF, SMINOR = np.concatenate(smain), np.concatenate(shalf), np.concatenate(sminor)
    A, B = S[:, :2], S[:, 2:]
    D = B - A
    L2 = np.maximum((D * D).sum(1), 1e-12)
    rows = []
    for spec, gmin, gmax in items:
        no_door = False
        if spec.plain or spec.floors >= 7 or spec.band in (BAND_OFFICE, BAND_OFFICE2) or spec.wall:
            if not spec.wall or spec.plain or spec.floors >= 7:
                no_door = True
        # shops and big boxes: their sign faces the street, not the alley or the parking aisle behind
        storefront = (spec.shop or spec.windows is False) and not spec.plain and spec.roof == "flat"
        if no_door and not storefront:
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
            rank = (bool(SMINOR[k]) if storefront else False, float(dist[k]))
            if best is None or rank < best[0]:
                best = (rank, a, e / Le, Le, out, i, bool(SMAIN[k]) and dist[k] < SHALF[k] + MAIN_STREET_SETBACK_M,
                        k)
        if best is None:
            continue
        _, a, d, Le, out, edge, on_main, kseg = best
        if spec.windows is False:
            spec.front = edge                   # a big box: its storefront faces the road
            if storefront:
                spec.sign = edge
            rows.extend(_wall_sign(spec, a, d, Le, out, gmax))
            continue
        if spec.cls in RESTAURANT_CLASSES and spec.floors == 1:
            rows.extend(_wall_sign(spec, a, d, Le, out, gmax))
        if spec.shop:
            if storefront and not spec.wall:
                spec.sign = edge                # the sign band over the street wall's shopfront
            continue
        if no_door:
            continue
        if on_main and not spec.wall and spec.roof == "flat" and spec.cls not in CHURCH_CLASSES \
                and _hash01(spec.seed, "shop") < MAIN_STREET_SHOPS:
            spec.shop = True                    # a main street's ground floor: a shopfront, no house door
            if not spec.plain:
                spec.sign = edge
            continue
        narrow = Le < 9.0
        f = (0.22 if _hash01(spec.seed, "door") < 0.5 else 0.78) if narrow else 0.5
        spec.door = (edge, Le * f)          # _walls leaves the ground floor's band plain there
        # brick row houses (an eastern city's): white marble steps up to the door
        if narrow and spec.band == BAND_BRICK and spec.roof == "flat" and _hash01(spec.seed, "stoop") < STOOP_SHARE:
            spec.stoop = (edge, Le * f)
        if drives is not None and spec.wall and Le >= 7.0:
            # the driveway beside the door, straight out to the road's edge
            g = 0.82 if f <= 0.5 else 0.18
            p0 = a + d * (Le * g) + out * 0.2
            t = float(np.clip(((p0 - A[kseg]) * D[kseg]).sum() / L2[kseg], 0, 1))
            q = A[kseg] + t * D[kseg]
            v = p0 - q
            dl = float(np.hypot(*v))
            if SHALF[kseg] + 1.0 < dl < DOOR_ROAD_M:
                p1 = q + v / dl * (SHALF[kseg] + 0.1)
                n = max(2, int(dl // 4.0) + 1)
                pts = [p0 + (p1 - p0) * (j / (n - 1)) for j in range(n)]
                drives.append({"m": DRIVEWAY["m"], "w": DRIVEWAY_W, "tl": DRIVEWAY["tl"], "p": DRIVEWAY["p"],
                               "n": [[round(float(pp[0]), 3), round(float(pp[1]), 3), 0.0] for pp in pts]})
        if spec.roof != "flat" and not narrow and _hash01(spec.seed, "fence") < FENCE_SHARE:
            rows.extend(_front_fence(spec, a, d, Le, f, A[kseg], D[kseg], L2[kseg], SHALF[kseg], gmin,
                                     drives is not None and spec.wall and Le >= 7.0))
        p = a + d * (Le * f) + out * 0.03
        base = gmax + (STOOP_H if spec.stoop else 0.0) + (PORCH_DECK_M if spec.porch else 0.0)   # (the deck / stoop)
        rows.append([G_DOOR, round(float(p[0]), 2), round(float(p[1]), 2), round(base, 2),
                     round(math.degrees(math.atan2(d[1], d[0])) % 360.0, 1), 1.0, 2.2,
                     round(_hash01(spec.seed, "dv"), 3)])
    return rows


# Company signs on walls (West Coast USA's, for its fictional firms): (prop group, width m, height m, panel thin
# along its local x (True) or y (False)). Warehouses: Logistics or Rentabox; big retail boxes: Lemon; one-storey
# restaurants: Turboburger.
WALL_SIGNS = {"logistics": ("wallsign_logistics", 10.8, 5.8, True), "rentabox": ("wallsign_rentabox", 8.2, 5.7, True),
              "lemon": ("wallsign_lemon", 11.5, 3.6, True), "burger": ("wallsign_turboburger", 5.8, 2.4, False)}
WALL_SIGN_SHARE = 0.6
RESTAURANT_CLASSES = {"restaurant", "fast_food", "cafe"}


def _wall_sign(spec, a, d, Le, out, gmax):
    """A company sign centred high on a big box's or restaurant's street wall (WALL_SIGNS), if it fits."""
    if _hash01(spec.seed, "wallsign") >= WALL_SIGN_SHARE:
        return []
    if spec.cls in RESTAURANT_CLASSES:
        key = "burger"
    elif spec.wall and str(spec.wall).startswith(METALWALL):
        key = "logistics" if _hash01(spec.seed, "ws") < 0.5 else "rentabox"
    else:
        key = "lemon"
    group, w, h0, thin_x = WALL_SIGNS[key]
    h = min(h0, spec.height * 0.45)                     # scaled down to the wall (trees.lua scales to the height)
    w *= h / h0
    if h < 1.8 or Le < w + 2.0:
        return []
    zc = gmax + spec.height - 0.6 - h / 2               # its top 0.6 m under the parapet
    p = a + d * (Le / 2) + out * 0.35
    # the logo faces the model's local -x (Lemon, Logistics, Rentabox: seen in game 2026-10-14) or its -y
    yaw = math.degrees(math.atan2(out[1], out[0])) + (180.0 if thin_x else 90.0)
    return [[group, round(float(p[0]), 2), round(float(p[1]), 2), round(float(zc), 2), round(yaw % 360.0, 1), 1.0,
             round(h, 2), 0.0]]


G_FENCE = "fence"           # trees.lua: East Coast USA's low wooden fence (eca_bld_wood_fence_c, 5.4 m, along x)
FENCE_SEG_M = 5.4
FENCE_SHARE = 0.3           # detached houses with a fence along the front of their yard (hashed)
FENCE_BACK_M = 3.2          # m from the road's edge (the back of the sidewalk)
FENCE_MIN_YARD_M = 2.5      # the front yard at least this deep between the fence and the house


def _front_fence(spec, a, d, Le, f, A, D, L2, half, z, driveway):
    """Prop rows of a fence along the front of a house's yard, parallel to the road at the back of the sidewalk,
    a little wider than the house, with gaps for the path to the door and the driveway."""
    mid = a + d * (Le / 2)
    t = float(np.clip(((mid - A) * D).sum() / L2, 0, 1))
    q = A + t * D
    v = mid - q
    dist = float(np.hypot(*v))
    if dist - (half + FENCE_BACK_M) < FENCE_MIN_YARD_M:
        return []
    nv = v / dist
    dr = D / math.sqrt(L2)
    centre = q + nv * (half + FENCE_BACK_M)
    yaw = round(math.degrees(math.atan2(dr[1], dr[0])) % 360.0, 1)
    gaps = [float((a + d * (Le * f) - centre) @ dr)]                          # the path to the door
    if driveway:
        gaps.append(float((a + d * (Le * (0.82 if f <= 0.5 else 0.18)) - centre) @ dr))
    n = max(1, int(math.ceil((Le + 3.0) / FENCE_SEG_M)))
    rows = []
    for k in range(n):
        sk = (k - (n - 1) / 2) * FENCE_SEG_M
        if any(abs(sk - g) < FENCE_SEG_M / 2 + 0.6 for g in gaps):
            continue
        p = centre + dr * sk
        rows.append([G_FENCE, round(float(p[0]), 2), round(float(p[1]), 2), round(float(z), 2), yaw, 1.0, 1.3,
                     round(_hash01(spec.seed, "fr", k), 3)])
    return rows


def road_tile_items(frame, tx, ty, md, ground):
    """tile_buildings of road tile (tx, ty), from the map cells under it."""
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    cds = [md.cell(k, layers=("buildings", "building_parts", "roads"), kind="demand")
           for k in roads.cells_of_tile(frame, tx, ty, margin=0.0)]
    return tile_buildings(frame, x0, y0, x1, y1, [c.layers.get("buildings") for c in cds], ground,
                          part_layers=[c.layers.get("building_parts") for c in cds],
                          west_town=district_town(frame, tx, ty, md),
                          roads_xy=road_lines(frame, [c.layers.get("roads") for c in cds], x0, y0),
                          tunnels_xy=tunnel_lines(frame, [c.layers.get("roads") for c in cds], x0, y0))


TOWN_COVERAGE = 0.16        # a tile whose footprints cover less than this share is a town / suburb (heights)
# Small towns of the arid West (Bishop, Moab: one-storey main streets with false fronts, ranch houses), told
# from cities by the footprint share of the district around a tile (1 km: Bishop 0.14, Moab 0.11; Frederick
# 0.22, Fells Point 0.27): a tile's own share cannot (a main-street block is as dense as a city's).
DISTRICT_M = 1000.0
WEST_TOWN_COVERAGE = 0.18
WEST_TOWN_ONE_STOREY = 0.75     # share of predicted one-storey buildings there
FALSE_FRONT_M = 1.2             # m of parapet over a one-storey shop there
POLES_COVERAGE = 0.30       # ... and its streets get wired wooden poles (furniture; Ocean City's photos: poles
                            # everywhere; Fells Point's dense core ~0.45 keeps lamp posts)


def coverage(items, tile=256.0):
    """The share of the tile covered by building footprints."""
    return sum(_area2(s.ring) / 2 for s, _, _ in items if (s.min_h or 0) <= 0.5) / (tile * tile)


def centres(items, tx, ty):
    """The anchor-frame (x, y) centres of a road tile's buildings (road_tile_items' rings are tile-local)."""
    import roads
    x0, y0, _, _ = roads.tile_bbox(tx, ty)
    return [(float(s.ring[:, 0].mean() + x0), float(s.ring[:, 1].mean() + y0)) for s, _, _ in items]


def road_tile(frame, tx, ty, md, ground, lines=None, props=None, items=None):
    """The buildings of road tile (tx, ty) (roads.TILE squares of the anchor frame): (mesh, collider boxes) or
    None. md: mapdata.MapData; ground(lat, lon): the physics ground without the road lift. lines (the road
    centrelines, roads.build_tile) and props (its prop rows): the houses' front doors are added to props.
    items: road_tile_items' result when the caller has it."""
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    if items is None:
        items = road_tile_items(frame, tx, ty, md, ground)
    if props is not None and lines:
        props.extend(doors(items, lines, x0, y0))
    meshes = build_parts(items)
    if not meshes:
        return None
    return meshes, collider_boxes(items, x0, y0), collision_mesh(items)


def far_tile(frame, tx, ty, md, ground, out_dir, vfs_dir, aid):
    """A far (render-only) building tile written to the session folder: (vfs path of its JSON, building
    count) or None. The lite mesh of the same buildings as road_tile's."""
    import json
    import os
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    cds = [md.cell(k, layers=("buildings", "building_parts"), kind="demand")
           for k in roads.cells_of_tile(frame, tx, ty, margin=0.0)]
    items = tile_buildings(frame, x0, y0, x1, y1, [c.layers.get("buildings") for c in cds], ground,
                           part_layers=[c.layers.get("building_parts") for c in cds],
                           west_town=district_town(frame, tx, ty, md))
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
        f.write(json.dumps(data))       # (json.dump to a file: the pure-Python encoder)
    os.replace(path + ".tmp", path)
    return f"{vfs_dir}/{name}", len(items)


# ------------------------------------------------------------------------------------------- the ground photo

PHOTO_GROW_M = 1.5          # m a footprint is grown on the ground photo (edges, eaves) ...
PHOTO_LEAN = 0.12           # ... plus this share of its height (the photo's lean of tall buildings), at most
PHOTO_LEAN_MAX_M = 6.0      # ... this
PHOTO_FILL_M = 12.0         # m: the blur that fills them from the ground around


def photo_mask(frame, x0, y0, size, px, md):
    """A (px, px) bool mask (row 0 = north) of the ground photo's square [x0, x0 + size) x [y0, y0 + size)
    (anchor frame) where buildings stand, grown by their edges and lean: there the photo shows roofs and
    walls that the 3D buildings now stand on or next to. None without buildings."""
    from PIL import Image, ImageDraw
    import mapcells
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0 + size, x0, x0 + size]),
                                      np.array([y0, y0, y0 + size, y0 + size]), 0.0)
    keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
    img = Image.new("L", (px, px), 0)
    draw = ImageDraw.Draw(img)
    mpp = size / px
    seen = set()
    n = 0
    for k in keys:
        f = md.cell(k, layers=("buildings",), kind="demand").layers.get("buildings")
        if f is None:
            continue
        hs = f.attrs.get("height")
        for i in range(len(f.kind)):
            fid = f.ids[i]
            if fid in seen:
                continue
            seen.add(fid)
            parts = f.parts(i)
            if not parts or not parts[0]:
                continue
            ring = np.asarray(parts[0][0], np.float64)
            e, nn = mapgeom.enu_xy(f, frame, ring)
            if e.max() < x0 or e.min() > x0 + size or nn.max() < y0 or nn.min() > y0 + size:
                continue
            h = float(hs[i]) if hs is not None and np.isfinite(hs[i]) else 8.0
            grow = (PHOTO_GROW_M + min(PHOTO_LEAN * h, PHOTO_LEAN_MAX_M)) / mpp
            cx, cy = (e - x0) / mpp, (y0 + size - nn) / mpp
            c = np.array([cx.mean(), cy.mean()])
            pts = np.stack([cx, cy], 1)
            d = pts - c
            r = np.hypot(d[:, 0], d[:, 1])[:, None]
            pts = pts + d / np.maximum(r, 1e-6) * grow          # grown out from its middle
            draw.polygon([tuple(p) for p in pts], fill=255)
            n += 1
    if n == 0:
        return None
    return np.asarray(img) > 0


def _push_pull(c, w):
    """c (h, w, k) float with weights w (h, w) in [0, 1]: every pixel with w < 1 blended toward a smooth
    fill from the weighted pyramid of the pixels around it (push-pull), however far away they are."""
    from PIL import Image
    h, wd = w.shape
    if w.min() >= 1.0 or max(h, wd) <= 1 or w.max() <= 0.0:
        return c
    H, W = (h + 1) // 2, (wd + 1) // 2
    pw = np.zeros((H * 2, W * 2), np.float32)
    pc = np.zeros((H * 2, W * 2, c.shape[2]), np.float32)
    pw[:h, :wd] = w
    pc[:h, :wd] = c * w[:, :, None]
    sw = pw.reshape(H, 2, W, 2).sum((1, 3))
    sc = pc.reshape(H, 2, W, 2, -1).sum((1, 3))
    coarse = _push_pull(sc / np.maximum(sw, 1e-6)[:, :, None], np.minimum(sw, 1.0))
    up = np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(coarse[:, :, k]), "F")
                              .resize((W * 2, H * 2), Image.BILINEAR), np.float32)[:h, :wd]
                   for k in range(c.shape[2])], axis=2)
    return c * w[:, :, None] + up * (1.0 - w[:, :, None])


def fill_photo(rgb, mask, size, px):
    """rgb (px, px, 3) uint8 with the masked pixels replaced by a smooth fill from the unmasked ground
    around them: a push-pull pyramid, so a footprint of any size fills (one PHOTO_FILL_M blur left black
    holes in big blocks), softened by that blur."""
    from PIL import Image, ImageFilter
    if not (~mask).any():
        return rgb
    w = (~mask).astype(np.float32)
    out = _push_pull(rgb.astype(np.float32), w)
    r = max(2.0, PHOTO_FILL_M / (size / px))
    soft = np.asarray(Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r)), np.float32)
    out = np.where(mask[:, :, None], soft, rgb.astype(np.float32))
    return np.clip(out, 0, 255).astype(np.uint8)


BRICK_WALK_COVERAGE = 0.35  # dense old cores (row houses) of brick regions ...
BRICK_WALK_SHARE = 0.55     # ... pave this share of their streets' sidewalks with brick (hashed per street)


def brick_walks(walks, items, lat, lon):
    """Marks the sidewalk runs (roads.build_tile's walks) of a dense brick-region tile that are brick pavers
    (Fells Point's historic streets): every run of a street the same."""
    if not walks or brick_share(lat, lon) < 0.7 or coverage(items) < BRICK_WALK_COVERAGE:
        return
    for wk in walks:
        fid = wk.get("fid")
        if fid is not None and _hash01(str(fid), "brickwalk") < BRICK_WALK_SHARE:
            wk["brick"] = True


_cell_stats = {}                # map cell key -> (lon, lat, area m2) per footprint (district_coverage)
_cell_lock = None


def _cell_lock_get():
    global _cell_lock
    if _cell_lock is None:
        import threading
        _cell_lock = threading.Lock()
    return _cell_lock


def cell_footprints(key, layer):
    """(lon, lat, area m2) arrays of a cell's building footprints (outer rings), cached per cell (layer may
    be None once cached). Pure."""
    _cell_lock_get()
    with _cell_lock:
        got = _cell_stats.get(key)
    if got is not None:
        return got
    if layer is None or len(layer.ids) == 0:
        got = (np.zeros(0), np.zeros(0), np.zeros(0))
    else:
        f = layer
        first = f.ring_off[f.part_off[:-1]]
        a = f.coord_off[first]
        b = f.coord_off[first + 1]
        ll = f.lonlat
        lat0 = float(np.mean(ll[:, 1]))
        x = ll[:, 0] * 111320.0 * math.cos(math.radians(lat0))
        y = ll[:, 1] * 111320.0
        cross = np.r_[x[:-1] * y[1:] - x[1:] * y[:-1], 0.0]
        C = np.r_[0.0, np.cumsum(cross)]
        CX, CY = np.r_[0.0, np.cumsum(ll[:, 0])], np.r_[0.0, np.cumsum(ll[:, 1])]
        n = np.maximum(b - a, 1)
        last = np.maximum(b - 1, a)
        area = np.abs(C[last] - C[a] + x[last] * y[a] - x[a] * y[last]) / 2
        got = ((CX[b] - CX[a]) / n, (CY[b] - CY[a]) / n, area)
    with _cell_lock:
        if len(_cell_stats) > 64:
            _cell_stats.clear()
        _cell_stats[key] = got
    return got


def district_coverage(lat, lon, cells):
    """The share of the DISTRICT_M disc around (lat, lon) covered by building footprints; cells: [(key,
    buildings layer)] covering the disc."""
    seen = 0.0
    mx = 111320.0 * math.cos(math.radians(lat))
    for key, layer in cells:
        lo, la, ar = cell_footprints(key, layer)
        if len(ar):
            d2 = ((lo - lon) * mx) ** 2 + ((la - lat) * 111320.0) ** 2
            seen += float(ar[d2 <= DISTRICT_M * DISTRICT_M].sum())
    return seen / (math.pi * DISTRICT_M * DISTRICT_M)


def district_town(frame, tx, ty, md):
    """Is road tile (tx, ty) in a small town of the arid West (WEST_TOWN_COVERAGE)? The cells of the district
    are loaded like the tile's own (the far building tiles need them anyway); an unreadable one counts as
    empty."""
    import mapcells
    import roads
    import surface
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    lat, lon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
    lat, lon = float(lat), float(lon)
    if not bool(surface.dry_west(lat, lon)):
        return False
    dlat = DISTRICT_M / 111320.0
    dlon = DISTRICT_M / (111320.0 * math.cos(math.radians(lat)))
    cells = []
    for key in mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon):
        with _cell_lock_get():
            known = key in _cell_stats
        if known:
            cells.append((key, None))
            continue
        try:
            cells.append((key, md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings")))
        except Exception:           # not cached / unreadable: counts as empty
            continue
    return district_coverage(lat, lon, cells) < WEST_TOWN_COVERAGE
