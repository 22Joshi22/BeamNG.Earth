"""Render-only dynamic-LOD tiles: the far field, and in mesh mode the near ground too
(docs/V1_2_DESIGN.md §2, §3, §6; docs/V1_3_SPEC.md §A, §B; V1_1_SPEC 2).

A quadtree in the current anchor's ENU plane. Tile (L, i, j) is 256*2^L m wide and covers
[i*S, (i+1)*S) x [j*S, (j+1)*S); its key is "L_i_j" and depends on nothing else, so a terrain
commit never re-keys a tile (v1.1 had per-block apron variants). Levels: L0 (256 m) .. L9
(131 km), and in mesh near-visual mode (v1.3 §B) also L-1 (128 m, 4 m vertex spacing) and L-2
(64 m, 2 m spacing: exactly the terrain block's physics lattice).

Selection (select()):
  - a node of level L >= 1 is refined while the player is closer than K(L) * S; in mesh mode
    a node is also refined down to L-1 within NEAR_RADII[-1] (400 m) and to L-2 within
    NEAR_RADII[-2] (150 m) of the player (min-level radii);
  - hysteresis: a node refined last time stays refined until the player is (1 + h) times its
    switch distance away (h = 0.25, so the band is 25 % of the switch distance);
  - terrain mode only: nodes overlapping a forced box (the committed, pending and predicted
    terrain blocks) are refined down to L0, so the 8 x 8 L0 tiles under every such block are
    always present: the mod hides the ones under the committed block and sinks the edge ring
    (§2.3). Mesh mode has no forced boxes: the TerrainBlock is invisible (physics only);
  - everything intersecting the square of half-size RADIUS (80 km) around the player is kept.
The selected tiles tile the radius square exactly once.

LOD changes are grouped into swap groups (swap_groups()): connected sets of added and removed
tiles with the same footprint (a parent and its descendants), which the mod exchanges with a
vertical crossover (§2.5 C2).

Each tile is 32 x 32 cells (2048 tris) plus a skirt on all 4 edges: a vertical curtain facing
outwards, max(2 m, 0.02 S, 2.5 err + 1 m) deep, so neighbours at different levels and tiles
moving during a crossover never show cracks. Vertex x, y are plane positions; z is the ENU z of
the DEM surface point under it (curvature included). Tiles of level <= 0 use the terrain block's
DEM zoom and height quantum, so their vertices (every 4th / 2nd / every lattice sample for L0 /
L-1 / L-2) equal the terrain's samples bit for bit. Cells are split along the diagonal the
Torque TerrainBlock uses for the same square (torque_split45()), so an L-2 tile is the
TerrainBlock's collision surface exactly. Per tile the builder also measures `err` (max deviation
from a 2x finer reference surface) and, for L0 in terrain mode, `sink` (how far the tile must go
down to be under the terrain block everywhere).

Colour: with imagery (v1.3), every tile has its own texture of its footprint (imagery.py) and
UV0 = (x - x0, y - y0) / S across the footprint, plus UV1 = (x - x0, y - y0) / DETAIL_M, a
world-locked detail-map coordinate (tile origins are multiples of 64 m and DETAIL_M divides 64,
so UV1 is continuous across tiles). Without imagery (--imagery off) the colour comes from one
small palette texture shared by every tile (shipped with the mod, see write_mod_assets()): u
encodes height (with water at the far left), v encodes slope. The palette is mirrored around
v = 0.5, so the result does not depend on the engine's v-flip. Below 1500 m the land colour is
the terrain material's dirt colour, blending into the height palette by 2500 m.
"""
import json
import math
import os
from dataclasses import dataclass

import numpy as np
from PIL import Image

import cdae
import dae
import dem as demmod
import heightmap
import imagery

BASE = 256.0            # L0 tile size
MAX_LEVEL = 9           # L9 = 131 072 m
MESH_MIN_LEVEL = -2     # mesh near-visual mode: L-2 = 64 m tiles on the 2 m physics lattice
CELLS = 32
RADIUS = 80000.0
K_NEAR = {1: 3.0, 2: 3.0}   # K(L) for refining a node of level L >= 1 (§3 recommendation)
K_FAR = 2.5                 # ... for L >= 3
# Mesh mode: everything within NEAR_RADII[m] of the player is level m or finer (v1.3 §B).
NEAR_RADII = {-2: 150.0, -1: 400.0}
HYSTERESIS = 0.25           # a refined node stays refined while d < (1 + h) * its switch distance
HOLD_S = 2.0                # a node changes state at most once per HOLD_S seconds (§2.4)
DETAIL_M = 2.0              # UV1 (detail map) repeat in metres; must divide 64 (see module doc)
FAR_MAX_ZOOM = 14           # DEM zoom cap for L1+ (L0 uses the heightmap's zoom)
SINK_MARGIN = 0.3           # m below the terrain everywhere for a sunk L0 tile
LATTICE = int(BASE / heightmap.SQUARE) + 1      # 129 terrain samples per L0 tile edge
PALETTE_W, PALETTE_H = 256, 32
WATER_U = 0.02
MATERIAL = "bngEarthFar_v3"     # static material shipped with the mod (§6.2)
# Terrain material's macro colour (mean of levels/beamng_earth/art/terrains/bngEarth_base_b.png).
DIRT = (0.39, 0.35, 0.27)
DIRT_UNTIL, DIRT_BLEND_TO = 1500.0, 2500.0      # m of elevation
MOD_SHAPES = ("levels", "beamng_earth", "art", "shapes", "beamngEarth")   # in mod/beamng_earth
MOD_PALETTE = "far_palette.png"
FORMATS = ("cdae", "dae")


def k_default(level):
    return K_NEAR.get(level, K_FAR)


def level_size(level):
    """Tile edge (m) of a level: 256 * 2^L (64 m for L-2)."""
    return BASE * 2.0 ** level


def switch_distance(level, kfun=k_default, radii=None, min_level=0):
    """Distance below which a node of `level` is refined (0: never): K(L) * S for L >= 1, and
    at least NEAR_RADII[m] for every allowed finer minimum level m < L."""
    if level <= min_level:
        return 0.0
    d = kfun(level) * level_size(level) if level >= 1 else 0.0
    for m, r in (NEAR_RADII if radii is None else radii).items():
        if min_level <= m < level:
            d = max(d, r)
    return d


def torque_split45(gx, gy):
    """True where the Torque/BeamNG TerrainBlock splits square (gx, gy) (block-local indices,
    gx east, gy north from the SW corner) along the SW-NE diagonal (x - y = 0); False: along
    the SE-NW diagonal (x + y = 1).

    Torque3D TerrainFile::_buildGridMap (Engine/source/terrain/terrFile.cpp, development
    4c44642, ~line 213) sets for every level-0 (single) square
        bool shouldSplit45 = ((squareX ^ squareY) & 1) == 0;  if (i == 0) split45 = shouldSplit45;
    TerrainBlock::getHeight / getNormal (terrData.cpp ~721/772) and the collision convexes
    (terrCollision.cpp ~440, ~842) evaluate the two triangles of a Split45 square as
    x > y: (BL, BR, TR) / else (BL, TR, TL), and of other squares as x + y < 1: (BL, BR, TL) /
    else (BR, TR, TL). The renderer's index buffer (terrCell.cpp ~649, `xTess == yTess`) uses
    the same parity. Our blocks start at an even lattice index (cx - 1024 with cx a multiple
    of 256 m is a multiple of 4 m), and L-2 tiles at multiples of 64 m, so the block-local
    parity equals the parity of the world lattice index (x / 2 + y / 2) and of a tile's local
    cell index (i + j): build_tile() and surface_at() split cell (i, j) on (i + j) even."""
    return ((np.asarray(gx) ^ np.asarray(gy)) & 1) == 0


@dataclass(frozen=True)
class Block:
    """Footprint of a terrain block: the 2048 m square of its samples (the valid, collidable
    area is [cx-1024, cx+1022]; the 2 m N/E strip is covered by sunk L0 tiles)."""
    cx: float
    cy: float

    @property
    def bbox(self):
        h = heightmap.HALF
        return (self.cx - h, self.cy - h, self.cx + h, self.cy + h)


@dataclass(frozen=True)
class Tile:
    level: int
    i: int
    j: int

    @property
    def size(self):
        return level_size(self.level)

    @property
    def bbox(self):
        s = self.size
        return (self.i * s, self.j * s, (self.i + 1) * s, (self.j + 1) * s)

    @property
    def key(self):
        return f"{self.level}_{self.i}_{self.j}"

    @property
    def node(self):
        return (self.level, self.i, self.j)

    def distance(self, x, y):
        """Horizontal distance from (x, y) to the tile's footprint."""
        x0, y0, x1, y1 = self.bbox
        return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


def tile_of(key):
    lvl, i, j = (int(v) for v in key.split("_"))
    return Tile(lvl, i, j)


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def select(px, py, forced=(), prev_refined=frozenset(), kfun=k_default, h=HYSTERESIS,
           radius=RADIUS, max_level=MAX_LEVEL, hold=frozenset(), min_level=0, radii=None):
    """Desired tiles for a player at (px, py).

    forced: bboxes (terrain blocks) under which everything is refined to L0 (terrain mode).
    prev_refined: the nodes (L, i, j) refined last time (for the hysteresis).
    hold: nodes whose state must not change now (they changed less than HOLD_S ago).
    min_level: finest level (0; MESH_MIN_LEVEL in mesh mode); radii: the min-level radii
    (default NEAR_RADII; only levels >= min_level count).
    Returns ({key: Tile}, set of refined nodes)."""
    lo_x, lo_y, hi_x, hi_y = px - radius, py - radius, px + radius, py + radius
    s = level_size(max_level)
    stack = [(max_level, i, j)
             for j in range(math.floor(lo_y / s), math.ceil(hi_y / s))
             for i in range(math.floor(lo_x / s), math.ceil(hi_x / s))]
    out, refined = {}, set()
    while stack:
        node = stack.pop()
        lvl, i, j = node
        s = level_size(lvl)
        x0, y0, x1, y1 = i * s, j * s, (i + 1) * s, (j + 1) * s
        if not (x0 < hi_x and x1 > lo_x and y0 < hi_y and y1 > lo_y):
            continue                                    # outside the far radius
        if lvl > min_level:
            if lvl > 0 and any(x0 < b[2] and x1 > b[0] and y0 < b[3] and y1 > b[1] for b in forced):
                split = True
            elif node in hold:
                split = node in prev_refined
            else:
                d = math.hypot(max(x0 - px, 0.0, px - x1), max(y0 - py, 0.0, py - y1))
                lim = switch_distance(lvl, kfun, radii, min_level)
                split = d < lim or (node in prev_refined and d < (1.0 + h) * lim)
            if split:
                refined.add(node)
                i2, j2 = 2 * i, 2 * j
                stack += [(lvl - 1, i2, j2), (lvl - 1, i2 + 1, j2), (lvl - 1, i2, j2 + 1), (lvl - 1, i2 + 1, j2 + 1)]
                continue
        t = Tile(lvl, i, j)
        out[t.key] = t
    return out, refined


def nearest_first(tiles, x, y):
    return sorted(tiles, key=lambda t: (t.distance(x, y), t.level, t.key))


def diff(old, new):
    """Keys to add and to remove to go from tile set `old` to `new` (dicts or sets of keys)."""
    return [k for k in new if k not in old], [k for k in old if k not in new]


def swap_groups(old, new):
    """LOD changes from key set `old` to `new` as swap groups: [(adds, removes)], each a
    connected set of added and removed tiles that overlap (a parent and its descendants), so
    both sides have the same footprint. Tiles that enter or leave the far radius have no
    counterpart and form one-sided groups. Sorted lists, deterministic order."""
    adds, removes = diff(old, new)
    side = {k: 0 for k in adds}
    side.update({k: 1 for k in removes})
    by_node = {tile_of(k).node: k for k in side}
    parent = {k: k for k in side}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for k in side:
        lvl, i, j = tile_of(k).node
        while lvl < MAX_LEVEL:
            lvl, i, j = lvl + 1, i >> 1, j >> 1
            a = by_node.get((lvl, i, j))
            if a is not None and side[a] != side[k]:
                parent[find(k)] = find(a)
    comps = {}
    for k in side:
        comps.setdefault(find(k), []).append(k)
    groups = []
    for ks in comps.values():
        groups.append((sorted(k for k in ks if side[k] == 0), sorted(k for k in ks if side[k] == 1)))
    groups.sort(key=lambda g: (g[0] or g[1])[0])
    return groups


def transform_bbox(src, dst, bb):
    """Axis-aligned bbox (in dst's ENU plane) of a bbox given in src's ENU plane."""
    x0, y0, x1, y1 = bb
    xs = np.array([x0, x1, x0, x1, (x0 + x1) / 2])
    ys = np.array([y0, y0, y1, y1, (y0 + y1) / 2])
    e, n, _ = dst.ecef_to_enu(*src.enu_to_ecef(xs, ys, 0.0))
    return (float(e.min()), float(n.min()), float(e.max()), float(n.max()))


def zoom_for_level(lat, level):
    """DEM zoom with pixels about half the vertex spacing (L1+; L0 uses the heightmap zoom)."""
    return demmod.zoom_for(lat, BASE * (1 << level) / CELLS / 2, FAR_MAX_ZOOM)


def skirt_depth(size, err=0.0):
    return max(2.0, 0.02 * size, 2.5 * err + 1.0)


# --- colour ----------------------------------------------------------------------------
def palette_image():
    land = [  # (elevation m, rgb): the height palette
        (0, (0.36, 0.43, 0.24)), (800, (0.42, 0.44, 0.27)), (1800, (0.50, 0.45, 0.33)),
        (2800, (0.53, 0.50, 0.45)), (3800, (0.63, 0.62, 0.61)), (4800, (0.86, 0.87, 0.89)),
    ]
    u = (np.arange(PALETTE_W) + 0.5) / PALETTE_W
    elev = elev_from_u(u)
    base = np.stack([np.interp(elev, [e for e, _ in land], [c[k] for _, c in land]) for k in range(3)], -1)
    # Low land takes the terrain block's dirt colour (§2.3 colour continuity), blending into the
    # height palette between DIRT_UNTIL and DIRT_BLEND_TO; the slope tint fades in with it.
    w = np.clip((elev - DIRT_UNTIL) / (DIRT_BLEND_TO - DIRT_UNTIL), 0.0, 1.0)[:, None]
    base = np.array(DIRT) * (1 - w) + base * w
    base[u < 0.04] = (0.15, 0.30, 0.42)                         # water
    base[(u >= 0.04) & (u < 0.08)] = (0.72, 0.67, 0.50)         # shore
    t = np.abs((np.arange(PALETTE_H) + 0.5) / PALETTE_H - 0.5) * 2  # 0 at v=0.5 (flat) .. 1 at edges (steep)
    rock = np.array([0.44, 0.42, 0.40])
    rw = np.clip(t, 0, 1)[:, None, None] * 0.8 * w[None, :, :]
    img = base[None, :, :] * (1 - rw) + rock * rw
    return Image.fromarray(np.uint8(np.clip(img, 0, 1) * 255 + 0.5), "RGB")


def u_from_elev(elev):
    return 0.1 + 0.88 * np.clip(elev / 5000.0, 0.0, 1.0)


def elev_from_u(u):
    return (u - 0.1) / 0.88 * 5000.0


def material_json(palette_vfs):
    """The static far-tile material (§6.2): a v1.5 Material whose mapTo is the material name
    the tiles carry, so it applies to .cdae and .dae tiles alike (probe P2)."""
    return {MATERIAL: {
        "name": MATERIAL, "mapTo": MATERIAL, "class": "Material", "version": 1.5,
        "persistentId": "6f1c2a3e-4b5d-4e6f-8a9b-0c1d2e3f4a5b",
        "Stages": [{"baseColorMap": palette_vfs, "metallicFactor": 0, "roughnessFactor": 1}, {}, {}, {}],
    }}


def write_mod_assets(mod_dir):
    """Write the palette and the material file into the mod (mod/beamng_earth). The level
    loader picks up every main.materials.json under the level folder."""
    d = os.path.join(mod_dir, *MOD_SHAPES)
    os.makedirs(d, exist_ok=True)
    palette_image().save(os.path.join(d, MOD_PALETTE))
    vfs = "/" + "/".join(MOD_SHAPES) + "/" + MOD_PALETTE
    with open(os.path.join(d, "main.materials.json"), "w") as f:
        json.dump(material_json(vfs), f, indent=2)
        f.write("\n")
    return d


# --- geometry --------------------------------------------------------------------------
def _skirt(nx, ny):
    """Edge vertex index runs, each ordered so that the skirt faces outwards
    (direction along the edge = up x outward normal)."""
    g = np.arange(nx * ny).reshape(ny, nx)          # [j, i], j = north
    return [g[0, :],            # south, west -> east
            g[:, -1],           # east, south -> north
            g[-1, ::-1],        # north, east -> west
            g[::-1, 0]]         # west, north -> south


def surface_at(grid, sp, fx, fy):
    """Height of a tile's triangulated surface at tile-local plane points (fx, fy in metres
    from the SW corner, inside the tile). grid: [j, i] vertex heights, spacing sp. Cells with
    i + j even are split along the SW-NE diagonal, the others along SE-NW, as build_tile
    triangulates them (the TerrainBlock's pattern, see torque_split45)."""
    n = grid.shape[0] - 1
    gx, gy = np.asarray(fx, np.float64) / sp, np.asarray(fy, np.float64) / sp
    i = np.clip(np.floor(gx).astype(np.int64), 0, n - 1)
    j = np.clip(np.floor(gy).astype(np.int64), 0, n - 1)
    a, b = gx - i, gy - j
    z00, z10 = grid[j, i], grid[j, i + 1]
    z01, z11 = grid[j + 1, i], grid[j + 1, i + 1]
    split45 = (i + j) % 2 == 0
    # SW-NE diagonal: triangles (00, 10, 11) where a >= b and (00, 11, 01) above it.
    z45 = np.where(a >= b, z00 + a * (z10 - z00) + b * (z11 - z10),
                   z00 + a * (z11 - z01) + b * (z01 - z00))
    # SE-NW diagonal: triangles (00, 10, 01) where a + b <= 1 and (10, 11, 01) above it.
    z135 = np.where(a + b <= 1.0, z00 + a * (z10 - z00) + b * (z01 - z00),
                    z11 + (1 - a) * (z01 - z11) + (1 - b) * (z10 - z11))
    return np.where(split45, z45, z135)


def grid_triangles(n):
    """Triangles (counter-clockwise from +Z) of an (n+1) x (n+1) vertex grid indexed [j, i]:
    cell (i, j) is split SW-NE when i + j is even, SE-NW otherwise (torque_split45)."""
    nx = n + 1
    jj, ii = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    a = (jj * nx + ii).ravel()
    b, c, d = a + 1, a + nx, a + nx + 1
    even = ((ii + jj) % 2 == 0).ravel()[:, None]
    t1 = np.where(even, np.stack([a, b, d], -1), np.stack([a, b, c], -1))
    t2 = np.where(even, np.stack([a, d, c], -1), np.stack([b, d, c], -1))
    return np.stack([t1, t2], 1).reshape(-1, 3)


@dataclass
class Built:
    pos: list               # [x, y, z] of the tile origin (SW corner, z = rounded mean height)
    positions: np.ndarray   # (n, 3) relative to pos
    normals: np.ndarray     # (n, 3)
    uvs: np.ndarray         # (n, 2) palette coordinates
    planar: np.ndarray      # (n, 2) tile-local xy / S: the footprint UV0 and the tangent frame
    tris: np.ndarray        # (m, 3) counter-clockwise from +Z
    grid: np.ndarray        # (33, 33) absolute vertex heights, float32 (for swap-group deltas)
    err: float              # max |reference - tile| (m)
    sink: float | None      # L0 (terrain mode): drop that puts the tile under the terrain everywhere
    skirt: float            # skirt depth (m)
    size: float             # tile edge (m)

    @property
    def detail_uvs(self):
        """UV1: tile-local metres / DETAIL_M (world-locked: tile origins are multiples of DETAIL_M)."""
        return self.planar * (self.size / DETAIL_M)


def build_tile(frame, dem, tile, zoom, q=None, ref_zoom=None, with_sink=True):
    """Geometry of one tile. zoom: DEM zoom of its vertices; ref_zoom: zoom of the 2x finer
    reference used for `err` (default zoom). For levels <= 0 pass q (the anchor's height
    quantum) and the heightmap zoom: vertices are then Q-rounded 2 m lattice samples, the same
    values the terrain block stores, and (L0, with_sink) `sink` is measured against the
    terrain's 2 m lattice."""
    x0, y0, x1, y1 = tile.bbox
    S = tile.size
    sp = S / CELLS
    elev_fn = lambda la, lo: dem.sample(la, lo, zoom)       # noqa: E731
    terrain = None
    if tile.level <= 0:
        # One evaluation of the terrain lattice over the tile (129² for L0, 65² for L-1, 33²
        # for L-2) gives the vertices (every 4th / 2nd / every sample), the error reference at
        # half the vertex spacing and the sink reference (all samples).
        n = int(S / heightmap.SQUARE) + 1
        step = (n - 1) // CELLS
        k0x, k0y = int(x0 // heightmap.SQUARE), int(y0 // heightmap.SQUARE)
        lxs, lys = heightmap.lattice_coords(k0x, n), heightmap.lattice_coords(k0y, n)
        lelev, lu = heightmap.surface(frame, elev_fn, lxs, lys)
        lq = lu if q is None else heightmap.quantise(lu, q) * q
        xs, ys = lxs[::step], lys[::step]
        elev, U = lelev[::step, ::step], lq[::step, ::step]
        if step >= 2:
            ref = lu[::step // 2, ::step // 2]
        else:                           # L-2: the vertices are the lattice; reference at 1 m
            r = x0 + np.arange(2 * CELLS + 1) * (sp / 2), y0 + np.arange(2 * CELLS + 1) * (sp / 2)
            ref = heightmap.surface_u(frame, elev_fn, *r)
        if tile.level == 0 and with_sink:
            terrain = lq
    else:
        xs = x0 + np.arange(CELLS + 1) * sp
        ys = y0 + np.arange(CELLS + 1) * sp
        elev, U = heightmap.surface(frame, elev_fn, xs, ys)
        if q is not None:
            U = heightmap.quantise(U, q) * q
        rz = zoom if ref_zoom is None else ref_zoom
        rxs = x0 + np.arange(2 * CELLS + 1) * (sp / 2)
        rys = y0 + np.arange(2 * CELLS + 1) * (sp / 2)
        ref = heightmap.surface_u(frame, lambda la, lo: dem.sample(la, lo, rz), rxs, rys)
    U = np.asarray(U, np.float64)

    # Geometric error: the reference against the triangulated tile, on the 2x grid.
    rf = np.arange(2 * CELLS + 1) * (sp / 2)
    RX, RY = np.meshgrid(rf, rf)
    err = float(np.abs(ref - surface_at(U, sp, RX, RY)).max())
    sink = None
    if terrain is not None:
        lf = np.arange(LATTICE) * heightmap.SQUARE
        LX, LY = np.meshgrid(lf, lf)
        sink = float(max(0.0, (surface_at(U, sp, LX, LY) - terrain).max()) + SINK_MARGIN)

    X, Y = np.meshgrid(xs, ys)          # [j, i], j = north
    # Colour: height along u, slope along v (mirrored palette, flat = 0.5).
    gy, gx = np.gradient(elev, ys, xs)
    slope_t = np.clip(np.hypot(gx, gy) / 1.0, 0.0, 1.0)
    water = (elev <= 0.5) & (slope_t < 0.02)
    uu = np.where(water, WATER_U, u_from_elev(elev))
    vv = 0.5 - 0.47 * slope_t

    # Vertex normals from the grid (central differences).
    P = np.stack([X, Y, U], -1)
    di = np.gradient(P, axis=1)
    dj = np.gradient(P, axis=0)
    nrm = np.cross(di, dj)
    nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)

    ny, nx = X.shape
    tris = [grid_triangles(nx - 1)]     # CCW from +Z, the TerrainBlock's diagonals

    # Skirts: copies of the edge vertices, lowered; same normal and uv as the edge.
    P, nrm = P.reshape(-1, 3), nrm.reshape(-1, 3)
    uv = np.stack([uu, vv], -1).reshape(-1, 2)
    depth = skirt_depth(S, err)
    extra_p, extra_n, extra_uv, nv = [], [], [], nx * ny
    for run in _skirt(nx, ny):
        low = P[run] - (0.0, 0.0, depth)
        t0, t1 = run[:-1], run[1:]
        lo0 = nv + np.arange(len(run) - 1)
        lo1 = lo0 + 1
        tris.append(np.stack([t0, lo0, t1, t1, lo0, lo1], -1).reshape(-1, 3))
        extra_p.append(low)
        extra_n.append(nrm[run])
        extra_uv.append(uv[run])
        nv += len(run)
    P = np.concatenate([P] + extra_p)
    nrm = np.concatenate([nrm] + extra_n)
    uv = np.concatenate([uv] + extra_uv)

    pos = [float(x0), float(y0), float(np.round(U.mean()))]
    P -= pos
    planar = (P[:, :2] / S)
    return Built(pos, P, nrm, uv, planar, np.concatenate(tris), U.astype(np.float32), err, sink, depth, S)


def write_built(path, built, fmt="cdae", material=MATERIAL, textured=False, tex_px=None):
    """Write a built tile as .cdae (default) or .dae; returns the file size. textured: the tile
    has its own imagery texture (UV0 = footprint, UV1 = world-locked detail); otherwise UV0 is
    the shared palette coordinate and there is no UV1. tex_px: the texture has a border of
    imagery.gutter(tex_px) texels around the footprint (UV0 is inset to match)."""
    assert len(built.tris) <= 8192
    uvs = built.uvs
    if textured:
        uvs = built.planar if tex_px is None else imagery.gutter_uv(built.planar, tex_px)
    uvs2 = built.detail_uvs if textured else None
    if fmt == "cdae":
        return cdae.write(path, built.positions, built.tris, normals=built.normals, uvs=uvs, uvs2=uvs2,
                          material=material, tangent_uvs=built.planar)
    dae.write_grid_dae(path, built.positions.ravel(), uvs.ravel(), built.tris.ravel(),
                       material=material, normals=built.normals.ravel(),
                       uvs2=None if uvs2 is None else uvs2.ravel())
    return os.path.getsize(path)


def write_tile(path, frame, dem, tile, zoom, q=None, ref_zoom=None, fmt="cdae", cancelled=None):
    """Build and write one tile; returns the Built record, or None if cancelled meanwhile."""
    if cancelled is not None and cancelled.is_set():
        return None
    built = build_tile(frame, dem, tile, zoom, q, ref_zoom)
    write_built(path, built, fmt)
    return built


def group_deltas(group_tiles):
    """(dIn, dOut) of a swap group: how far the new surface rises above the old one at most,
    and how far the old one lies above the new one, evaluated on the finest vertex grid of the
    group (every vertex of both sides). group_tiles: (adds, removes), lists of (Tile, grid)."""
    adds, removes = group_tiles
    if not adds or not removes:
        return 0.0, 0.0
    sp = min(t.size / CELLS for t, _ in adds + removes)
    adds = [(t, np.asarray(g, np.float64)) for t, g in adds]
    removes = [(t, np.asarray(g, np.float64)) for t, g in removes]
    d_in = d_out = 0.0
    for ta, ga in adds:
        ab = ta.bbox
        for tr, gr in removes:
            rb = tr.bbox
            x0, y0, x1, y1 = max(ab[0], rb[0]), max(ab[1], rb[1]), min(ab[2], rb[2]), min(ab[3], rb[3])
            if x0 >= x1 or y0 >= y1:
                continue
            # The intersection of two quadtree tiles is the smaller one: aligned to sp.
            X, Y = np.meshgrid(x0 + np.arange(int(round((x1 - x0) / sp)) + 1) * sp,
                               y0 + np.arange(int(round((y1 - y0) / sp)) + 1) * sp)
            z_new = surface_at(ga, ta.size / CELLS, X - ab[0], Y - ab[1])
            z_old = surface_at(gr, tr.size / CELLS, X - rb[0], Y - rb[1])
            dz = z_new - z_old
            d_in = max(d_in, float(dz.max()))
            d_out = max(d_out, float(-dz.min()))
    return d_in, d_out


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "mod", "beamng_earth")
    print("wrote", write_mod_assets(target))
