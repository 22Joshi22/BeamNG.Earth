# earthd — BeamNG.Earth companion server (protocol 4)

Implements the server side of [`docs/V1_3_SPEC.md`](../docs/V1_3_SPEC.md) §A, §B, §D, §E, §F over
[`docs/V1_2_DESIGN.md`](../docs/V1_2_DESIGN.md) phases 1a, 1b, 1c (server bits), 2 and 3 (on top of
[`docs/V1_1_SPEC.md`](../docs/V1_1_SPEC.md) and [`docs/V1_SPEC.md`](../docs/V1_SPEC.md) where later
versions do not change them). It turns real-world elevation data into 1024² TerrainBlock heightmaps
on a world-locked 2 m lattice that follow the player (physics only in the default `mesh`
near-visual mode), and a dynamic-LOD render-only tile field of binary `.cdae` tiles, each with its
own satellite texture, streamed as swap groups, for the `beamng_earth` mod. In mesh mode the tiles
go down to 64 m (L-2) on the terrain block's own physics lattice, so they draw the ground under
the car too.

## Run

```sh
python3 server/earthd.py                    # listens on 127.0.0.1:47613
python3 server/earthd.py --port 47613 --user-dir ~/.local/share/BeamNG/BeamNG.drive/current \
                         --cache-dir ~/.cache/beamng-earth --workers 3 --far-format cdae -v
```

| Flag | Default | Meaning |
|---|---|---|
| `--port` | `47613` | TCP port on 127.0.0.1 (`0` picks a free port and prints it) |
| `--user-dir` | `~/.local/share/BeamNG/BeamNG.drive/current` | BeamNG user folder. Output goes to `<user-dir>/temp/beamngEarth/<sessionId>/`, which the game sees as `/temp/beamngEarth/<sessionId>/` |
| `--cache-dir` | `~/.cache/beamng-earth` | Download cache root. DEM tiles: `<cache-dir>/dem/<source>/<z>/<x>/<y>.<ext>` |
| `--workers` | `3` | Far-tile worker threads. Keep ≤ 3 so tile building cannot starve the game (§4.5) |
| `--far-format` | `cdae` | `cdae`: server-written binary shapes (default). `dae`: Collada fallback (the engine then compiles each tile itself, ~5× slower to load) |
| `--lod-k` | per level | One K for every level. Default: the layout's K table, K(L1) = K(L2) = 3, K(L3+) = 2.5 with 32 cells (§3); K' = {L1 0.75, L2 1.5, L3 1.5, L4+ 1.25} with 64 |
| `--hysteresis` | `0.25` | A refined node stays refined until d ≥ (1 + h) × its switch distance (v1.3 §D: band ≥ 25 %) |
| `--far-radius` | `80000` | Far field half-size (m) around the player |
| `--nice` | `10` | Niceness increment of the whole process (0 = leave as is) |
| `--imagery` | `usgs,eox2023,eox2016` | Satellite imagery source chain (first source with data wins, per source tile), or `off` for the v1.2 palette look (tiles without `material`) |
| `--near-visual` | `mesh` | `mesh`: the ground near the player is drawn by L-2 (64 m) / L-1 (128 m) tiles and the TerrainBlock is physics only; `terrain`: the v1.2 look (the block is drawn, L0 tiles under it are covered) |
| `--cells` | `32` | Tile cells per edge (docs/V1_4_SPEC.md §3.1, M1). `32`: the v1.3.1 layout, FORMAT 3. `64` (mesh mode only): 8704-tri tiles, FORMAT 4, exact level L-1 (128 m, the 2 m lattice), L0 within 400 m, `hello.cells`, `far_tile.tex`, tile cache 2500. The default stays 32 until the in-game probe P8 passes |
| `--tex` | per layout | Texture edge (px) per level from the finest, the last value repeating. Default with 64 cells `256,256,256,512` (L-1, L0, L1 256 px; L2+ 512 px); with 32 cells from `--tex-near` / `--tex-far` |
| `--tex-near` | `256` | 32 cells: texture edge (px) of the two finest levels (mesh: L-2, L-1; terrain: L0, L1) |
| `--tex-far` | `256` | 32 cells: texture edge (px) of every other level |
| `--tone` | `1.0` | Strength of the imagery tone curve (0 = imagery as downloaded) |
| `--deshadow` | `off` | Undo the capture sun's terrain shading in the USGS tiles (`deshadow.py`): `lambert` = slope shading only, `full` = slopes and the terrain's cast shadows (relit texels then get half the tone curve's shadow lift). `off` = textures byte-identical to before |
| `--near-radii` | `150,400` | Mesh mode: everything within 150 m of the player is the exact level, within 400 m at least the next one (32 cells: L-2, L-1; 64 cells: L-1, L0) |
| `--status-interval` | `0` | Re-send the last status with the current far-tile backlog every S s while it changes (0 = v1.3.1: on events only; `tools/drive.py` runs use 1) |
| `-v` | off | Debug logging |

Requirements: Python 3 (3.10+), numpy, Pillow (≥ 11, for its DXT1 encoder) with WebP support. No
API keys, no other packages (the `.cdae` writer has its own msgpack encoder).

The v1.2 behaviour is `--near-visual terrain --imagery off` (the integration drive and `test_e2e.py`
run that way).

On startup the server prints `earthd listening on 127.0.0.1:<port> session <sessionId> -> <dir> (far tiles: cdae, near visual: mesh, imagery: usgs,eox2023,eox2016)`.
It deletes session folders left by earlier runs (only folders that contain its `.earthd-session` marker).
CPU (§4.5, phase 1c): `main()` lowers the process priority with `os.nice(10)`, and
`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1` are set before numpy is imported. Threads: 2 heightmap
workers, `--workers` (3) tile workers, 8 I/O-bound DEM fetchers, 6 imagery fetchers. The in-memory
DEM LRU holds 512 decoded 512² tiles (~512 MB at most), the imagery LRU 512 decoded 256² tiles
(~100 MB).

## Protocol 4 (as implemented)

Changes from protocol 3 (V1_3_SPEC §E): `hello.protocol = 4`; the server's `hello` gains
`nearVisual` and `attribution`; `far_tile` gains `material`, `texture`, `detail` (all three, or
none when imagery is off or failed for that tile: then it is a v3-style tile with the static
palette material); `far_tile.sink` is sent only in terrain mode; keys can have negative levels
(`-2_i_j`, `-1_i_j`) in mesh mode. Everything else is unchanged.

Frames: 4-byte little-endian length + UTF-8 JSON (unchanged). Numbers for `cx`/`cy` come back
as JSON floats (`512.0`). All positions are in the ENU plane of the anchor named by `anchorId`.

### mod → server

| Message | Notes |
|---|---|
| `{"type":"hello","protocol":4,"format":4,"modVersion":"…"}` | `format` = the newest tile format the mod reads (the v1.4 mod reads 3 and 4; a v1.3.1 mod sends 3). A different `protocol`, or a `format` below the server's, gets a `status` warning. |
| `{"type":"prepare_anchor","requestId":N,"lat":d,"lon":d,"cx":m,"cy":m}` | `cx`,`cy` optional (default 0): centre of the anchor's first terrain in the **new** anchor's frame, snapped to 256 m (clamped to ±30 km). |
| `{"type":"commit_anchor","anchorId":A}` | Switches the far field to A and commits A's first terrain (from `anchor_ready`). Tiles of the previous anchor that were sent but never swapped in are forgotten: the mod drops them. |
| `{"type":"prepare_terrain","requestId":N,"anchorId":A,"cx":m,"cy":m}` | `cx`,`cy` multiples of 256 (other values are snapped). For the active anchor this block becomes the **pending** block: in terrain mode the far field is refined to L0 over it at once and its 64 L0 tiles are built first (mesh mode: no tile depends on a block). |
| `{"type":"commit_terrain","anchorId":A,"cx":m,"cy":m}` | The committed block. **No far-tile key changes**; in terrain mode only the forced-L0 area moves, in mesh mode nothing at all changes. For a newer known anchor it implies `commit_anchor`. |
| `{"type":"pose","anchorId":A,"x","y","z","vx","vy","lat","lon","speed","cam":{"x","y","z","fx","fy","fz","fovDeg"}}` | ~2 Hz. Drives LOD selection, the predicted block and the terrain prefetch. `cam` is optional and only stored for now (the camera-weighted priority is phase 4). A pose with a newer known anchor implies `commit_anchor`; a late pose of the previous anchor is transformed into the active frame. |

### server → mod

| Message | Notes |
|---|---|
| `{"type":"hello","protocol":4,"format":3,"serverVersion":"0.4.0","sessionId":"…","farFormat":"cdae"\|"dae","material":"bngEarthFar_v3","nearVisual":"mesh"\|"terrain","attribution":"…","cells":64}` | `format`: 3 with `--cells 32` (v1.3.1), 4 with `--cells 64`; `cells` (v1.4 M1) only with format 4: the tiles' cells per edge (the mod's crossover fallback error and the exact level of its `exactMiss` metric; 32 without it). `farFormat` tells which shape type the tiles are; `material` is the static palette material (tiles without imagery). `nearVisual`: `mesh` = disable the TerrainBlock's rendering (physics only), the tiles draw all the ground; `terrain` = v1.2. `attribution`: the credit line for the sources in use, e.g. "Imagery: USGS The National Map (public domain); Sentinel-2 cloudless by EOX (CC BY-NC-SA 4.0 / CC BY 4.0); Elevation: Mapterhorn (see mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen". |
| `{"type":"status","text":"…","backlog":n}` | Progress text, and protocol/format mismatch warnings. `backlog` (v1.4 M0): desired far tiles of the current anchor not shown yet; with `--status-interval S` the last status is re-sent every S s while it changes. |
| `{"type":"anchor_ready","requestId":N,"anchorId":A,"lat":d,"lon":d,"groundZ":m,"terrain":{"cx","cy","heightmap","size":1024,"squareSize":2.0,"zBase","maxHeight","q"}}` | `groundZ` = terrain height at (0, 0) of the new frame (bilinear on the quantised grid, i.e. what the game will report), or `null` if (0, 0) is outside the block. `q` = height quantum, `maxHeight = q·65536`, `zBase` a multiple of 64 m (so of `q`). No `anchorKey` yet (lattice anchors are phase 5). |
| `{"type":"anchor_error","requestId":N,"error":"…"}` | |
| `{"type":"terrain_ready","requestId":N,"anchorId":A,"cx","cy","heightmap","size":1024,"squareSize":2.0,"zBase","maxHeight","q"}` | Same terrain fields as in `anchor_ready`. |
| `{"type":"terrain_error","requestId":N,"anchorId":A,"error":"…"}` | Unknown anchor, bad fields or a DEM failure (so a request never hangs). |
| `{"type":"far_tile","anchorId":A,"key":"L_i_j","level":L,"shape":"/temp/beamngEarth/<sid>/far_<A>_<L_i_j>.cdae","pos":[x,y,z],"err":m,"skirt":m,"sink":m,"state":"group","material":"bngEarthSat_<A>_<key>","texture":"/temp/beamngEarth/<sid>/far_<A>_<L_i_j>.dds","detail":true,"tex":256}` | A tile to **load hidden**; it becomes visible only through a `far_swap`. `level` −2…9 (−2, −1 in mesh mode only); footprint `[pos.x, pos.x + 256·2^level]²`. `pos` = SW corner, z = rounded mean height (an integer, so L-2 vertex heights stay exact multiples of `q`). `err` = max deviation of the tile from a 2× finer DEM reference (m). `skirt` = its skirt depth, `max(2, 0.02·S, 2.5·err + 1)`. `sink` (**L0 in terrain mode only**) = how far to lower the tile so it lies ≥ 0.3 m under the terrain block everywhere (measured against the block's own 2 m Q-rounded lattice). `state` is always `"group"` (`"prefetch"` is phase 4). Keys never depend on the terrain block. **Imagery** (all three or none): `material` = the material name written into the shape (create a Material with `mapTo` = it and `colorMap` = `texture` before the tile; the key's `-` signs are written `m`, e.g. `bngEarthSat_3_m2_10_m4`); `texture` = DDS DXT1 with a full mip chain next to the shape, row 0 = the tile's north edge; `detail` = the shape has UV1 (world-locked detail coordinates, one unit per `DETAIL_M` = 2 m). Without them the shape references the static `bngEarthFar_v3` palette material. `tex` (v1.4 M1, format 4 only, with the material) = the texture's edge in px: the mod's governor unit kind (`cdaeTex128/256/512`). |
| `{"type":"far_swap","anchorId":A,"id":G,"add":[keys],"remove":[keys],"dIn":m,"dOut":m}` | A swap group, sent once every `add` has been sent as `far_tile`. Run it once all adds are loaded: show the adds, remove the removes (C2 crossover: spawn adds at z − dIn, raise them, then sink the removes by dOut, then delete). `add` and `remove` cover exactly the same footprint (a parent and its descendants). One-sided groups: `remove: []` for the initial load / tiles entering the 80 km radius, `add: []` for tiles leaving it. `dIn` = max(new − old), `dOut` = max(old − new) over the group (both ≥ 0, evaluated at every vertex of both sides, +0.1 m). Group ids increase per connection. |
| `{"type":"far_remove","anchorId":A,"key":"L_i_j"}` | For the **active** anchor: eviction of a tile sent as `far_tile` that no group needs any more (the desired set changed before its swap); delete it, it was never shown. For a **previous** anchor (after a rebase): remove that shown tile; it is sent only after every `far_swap` whose adds cover its area (footprints compared in the new frame), so apply it once the swaps received before it have run. |
| `{"type":"far_done","anchorId":A,"count":K}` | Every swap group for the current desired set has been sent. K = number of tiles the mod shows once those swaps have run. Sent again after each later change completes. |

Far-field state per connection (`earthd.Connection`): **D** the desired set, **C** the tiles of
emitted swaps (what the mod shows), **S** the tiles sent but not yet swapped in. Groups are
`farfield.swap_groups(C, D)`, recomputed whenever D changes (pose, `prepare_terrain`,
`commit_terrain`, predicted block). Build order: L0 tiles under the committed or pending block
first (the mod's cover and commit gating need them), then tiles of groups that are already partly
sent, then the rest, nearest first (in mesh mode there is no block class: the L-2 tiles around the
player come first by distance). The imagery of the next 12 queued tiles is fetched ahead of their
build.

## Terrain block (§1, §12 P1)

- `hm_<anchorId>_<cx>_<cy>.png`: 1024² 16-bit grayscale, row 0 = north, `squareSize` = 2.0.
  Sample (gx, gy) is at (cx − 1024 + 2·gx, cy − 1024 + 2·gy), computed as integer lattice index × 2.0,
  so every sample is an even-metre point of the anchor frame and a 256 m move is exactly 128 samples.
  Row/column 1023 hold real DEM data. Physics covers (N − 1)·s = 2046 m: [cx − 1024, cx + 1022]; the
  2 m N/E strip up to cx + 1024 is covered by the (sunk) L0 tiles under the block.
- Heights: the engine computes `h = zBase + q16·maxHeight/65536`. The server sends
  `maxHeight = Q·65536` and `zBase = floor((min − 8)/64)·64`, and writes `q16 = round(u/Q) − zBase/Q`,
  so every stored height is `Q·round(u/Q)` whatever the block: **overlapping samples of two blocks
  are bit-identical** (tested on synthetic rough ground, through the DEM tile mosaic, and on real
  Mt Whitney data). Q is chosen once per anchor from a ±15 km relief survey at DEM z10: the smallest of
  {1/256, 1/128, 1/64, 1/32, 1/16} m whose range 65535·Q covers 1.1 × relief + 72 m. A block whose
  relief does not fit uses the next coarser Q (for that block only).
- Heightmaps are generated once per (anchorId, cx, cy) on two worker threads and kept in an LRU
  (6 entries). Eviction deletes the file but never touches the current terrain, the two committed
  before it, the last two handed to the mod, the terrain just built, or the first terrain of an
  anchor no connection has committed yet. (Protecting every recent anchor's first terrain made the
  protected set larger than the cache after a rebase, so each new prefetch was deleted as soon as it
  was built and rebuilt on every pose; found by the integration drive.)
- Prefetch: on each pose ≥ 200 m (Chebyshev) from the block centre, the server predicts the mod's
  next `prepare_terrain` (`predict_request`, REACH = 420 m as in `follow.lua`), builds it in the
  background and forces L0 over it. While a request is pending the prediction starts from the
  pending block (the mod commits that one before it asks again).

## Far field (§2, §3)

- Quadtree in the anchor plane: tile (L, i, j) is 256·2^L m (L-2…L9; L-2 = 64 m, L-1 = 128 m in
  mesh mode, L0 is the floor in terrain mode), key `L_i_j`. Selection (`farfield.select`): a node
  of level L ≥ 1 refines while d < K(L)·S; in mesh mode nodes also refine to L-1 within 400 m and to
  L-2 within 150 m of the player (min-level radii, `--near-radii`). A refined node stays refined until
  d ≥ 1.25 × its switch distance (`--hysteresis 0.25`: the band is 25 % of the switch distance).
  Terrain mode only: nodes overlapping the committed, pending or predicted block refine to L0 (the
  64 L0 tiles under a block are **kept**, never excluded). Mesh mode has no block forcing, cover,
  sink, apron or 2 m strip handling: the block is invisible. A node changes state at most once per
  2 s. Everything intersecting the 80 km square around the player is kept; tiles cover it exactly
  once. About 700 tiles near a block in terrain mode (64 of them under it); 784 in mesh mode
  (48 L-2, 52 L-1, 128 L0, 108 L1, 92 L2, 96 each L3–L5, 64 L6, 4 L7 around the origin).
- Each tile: 32×32 cells (2048 tris) + outward-facing skirts on all four edges (2304 tris). With
  `--cells 64` (v1.4 M1, `farfield.Layout(64)`): 64×64 cells (8704 tris, 4485 verts, 348 KB textured),
  every level keeps its size so its vertex spacing halves, the K' table and the radii L-1 ≤ 150 m /
  L0 ≤ 400 m keep the vertex spacing at a distance (300 tiles at a pose instead of 784; L-1 is the
  exact level: every lattice sample, L0 every 2nd). The server makes one `Layout` and passes it to
  `select`, `build_tile`, `zoom_for_level` and `heightmap`; no module setting is read at call time.
  Tiles of level ≤ 0 use the heightmap's DEM zoom and Q rounding, so their vertices (every 4th / 2nd /
  every lattice sample for L0 / L-1 / L-2) equal the terrain's samples bit for bit; an L-2 tile's
  vertices are exactly the terrain PNG's heights (also in the engine's float32 arithmetic). DEM zoom
  for L1+: pixels ≈ half the vertex spacing, capped at z14.
- **Triangle diagonals = the TerrainBlock's** (`farfield.torque_split45`): cell (i, j) is split
  SW–NE when i + j is even, SE–NW otherwise. Source: Torque3D `TerrainFile::_buildGridMap`
  (Engine/source/terrain/terrFile.cpp, development 4c44642, ~l. 213): for level-0 squares
  `split45 = ((squareX ^ squareY) & 1) == 0`; `TerrainBlock::getHeight`/`getNormal` (terrData.cpp
  ~721/772) and the collision convexes (terrCollision.cpp ~440, ~842) use Split45 = the SW–NE diagonal
  (x − y = 0), the renderer's index buffer (terrCell.cpp ~649) the same parity. Blocks start at an
  even lattice index and L-2 tiles at multiples of 64 m, so local and world parity agree. So the
  visible L-2 surface equals the collision surface between the vertices too (`tests/test_mesh.py`
  checks it against an independent port of `getHeight`). **BeamNG's physics may triangulate its
  terrain itself; this is the Torque rule and needs the in-game check below.**
- Colour, imagery on (default): every tile has its own DDS texture of its footprint (see Imagery)
  and UV0 = (x − x0, y − y0)/S, UV1 = (x − x0, y − y0)/2 m (world-locked: tile origins are multiples of
  64 m, so UV1 is continuous across tiles; skirts reuse their edge's UVs).
- Colour, `--imagery off`: one palette texture shared by every tile (u = height, water at the left
  edge; v = slope, mirrored around 0.5). Below 1500 m elevation the land colour is the terrain material's dirt
  (0.39, 0.35, 0.27) without slope tint, blending into the height palette by 2500 m (§2.3).
- **Material (§6.2): shipped with the mod**, not written by the server. Every tile references
  `bngEarthFar_v3`, defined in `mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/main.materials.json`
  (a v1.5 Material, `mapTo` = its own name, `baseColorMap` = the shipped `far_palette.png` there).
  The level loader loads it with the level; in another level the mod loads it with
  `loadJsonMaterialsFile`. Both files are generated by `python3 server/farfield.py` (writes into
  `mod/beamng_earth`), and `tests/test_farfield.py` checks the shipped copies match the code.
- Tiles are cached per (anchorId, key) (up to 6000 shapes and their textures); a superseded anchor's files are deleted
  once the new anchor's set is complete and none of its tiles are still shown, 30 s later. Every
  delete (pruned anchors and LRU evictions alike) goes through one queue, one file at a time: 5
  files/s in terrain mode (the v1.2 rate that removed the post-rebase freeze, P9). Mesh mode with
  imagery writes ~4x as many files per km (~380 tiles = ~760 files per km: 64/128 m tiles plus a
  texture each; a 10 km anchor leaves ~11k files), so there a long queue drains faster: backlog /
  60 s files/s, at most 40/s, still evenly spaced. An evicted tile that is built again before its
  queued delete keeps its new file.

## `.cdae` far tiles (§6.1, `server/cdae.py`)

Format decoded from engine-written files (golden pairs in `tests/fixtures/`):

- `u32` version word `0x007C001F` (exporter 0x7C, shape version 31), `u32` header size, a msgpack
  header map `{info, compression: false, bodysize, objectNames: ["tile"]}`, then an uncompressed
  body that is a **flat sequence** of msgpack values (TSShape v31 write order): shape bounds/radius,
  19 shape vectors (each `count, elementSize, bin`), names (`tile`, `detail2`), one standard mesh
  (bounds, centre, radius, verts 12 B, tverts 8 B, tverts2 8 B, colors, norms 12 B, encodedNorms,
  primitives 12 B, u32 indices, tangents 16 B, vertsPerFrame, flags 0), 0 sequences, one material
  (name, flags S_Wrap|T_Wrap, reflectance 0, bump/detail −1, 1.0, 1.0).
- Engine conventions reproduced: integral floats are packed as ints; vertices are numbered in
  order of first use by the triangle list; triangles are stored with **reversed winding**; texture
  **v is flipped** (`1 − v`, in float32); one indexed-triangle primitive (`0x20000000`); normals
  as given (or, without normals, the normalised sum of unit face normals, like the engine);
  tangents by Lengyel's method (identical to the engine's for regular UVs; far tiles use the tile's
  planar xy so they are well defined). Shape radius = tubeRadius = half-diagonal of the bounds; mesh
  radius = max vertex distance from the bounds centre; one detail `detail2` (size 2, polyCount = tris).
- Golden test (`tests/test_cdae.py`): for the same geometry the writer reproduces the engine's file
  byte size, header, every shape vector, the vertex/uv/normal arrays exactly and the triangle set
  (the engine reorders triangles for its vertex cache).
- Second UV set (v1.3, textured tiles): `tverts2` holds UV1 in the same convention as `tverts`,
  `(u, 1 − v)` in float32, one per vertex, mesh flags unchanged (0). Verified against a stock shape the
  engine compiled from a two-UV-set Collada file (`art_shapes.zip`,
  `…/string_course/G_C_SC_01_300x20`: max |tverts2 − (u1, 1 − v1)| = 8.5e-8 over all 30 corners;
  `tests/test_mesh.py` repeats the check when the game is installed). A textured tile is ~94 KB
  (85 KB + 8 B UV1 per vertex). The DAE fallback writes UV1 as `TEXCOORD set="1"`.
- Version check: at startup the server reads the version word of the engine's own cached `.cdae`
  files under `<user-dir>/temp/temp/`; if it is not `0x007C001F` it falls back to DAE tiles.

## Imagery (V1_3_SPEC §A, `server/imagery.py`)

| Source | URL (all Web-Mercator XYZ, 256 px, no key) | Zooms used | Licence |
|---|---|---|---|
| `usgs` | `basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}` | 10–16 (~1.9 m/px at 36° N at z16) | public domain |
| `eox2023` | `tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2023_3857/default/g/{z}/{y}/{x}.jpg` | ≤ 14 (Sentinel-2, ~10 m native) | CC BY-NC-SA 4.0 |
| `eox2016` | `tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg` | ≤ 14 | CC BY 4.0 |

- **Rendering**: per tile, a texture of exactly the tile's footprint. Texel centres → ENU (the
  anchor frame) → lat/lon (the same mapping the geometry uses) → Web-Mercator pixel at the zoom whose
  ground resolution is closest to the texel size, capped at the source's max zoom → bilinear. The
  mapping is evaluated exactly on an 8×8 grid of texture cells and bilinearly in between (Pillow
  MESH transform; error < 0.01 texel even on 131 km tiles). Row 0 = north, column 0 = west.
  `--tex-near` (512) for the two finest levels of the mode, `--tex-far` (256) beyond.
- **Coverage**: each texel takes the first source of the chain whose four bilinear taps have data.
  A source tile counts as missing when it is 404/400 or blank (every channel within 2 levels — USGS
  answers with white tiles outside the US at low zooms) unless it is the last source (EOX paints the
  open ocean one uniform deep blue). USGS is used only from z10 up (it serves a global low-res
  basemap at z ≤ 9, which would put a different look on the far rings abroad) and only where its
  z10 and z12 ancestor tiles have data (coverage probes; without them a start in the Alps sent it
  1132 requests that 404'd). US borders: a partly covered USGS tile keeps its white fill (no
  blending at coverage edges, per spec). No source anywhere (beyond ±85°): the dirt colour.
- **Caching / fetching**: raw tiles in `<cache-dir>/imagery/<source>/<z>/<x>/<y>.jpg`, 404s as
  `<y>.404` markers, decoded tiles in a 512-entry LRU, ≤ 6 concurrent requests with the same
  User-Agent as the DEM fetcher, one persistent HTTPS connection per host and fetch thread, 3
  attempts. A source whose fetch failed all attempts is skipped for 30 s (`SOURCE_BACKOFF`), so an
  outage costs each texture nothing instead of ~3.5 s (offline) to ~60 s (timeouts) per source and
  the tile workers keep up. An unreachable source falls through to the next one; if no source can fill a texel
  because of network errors (not 404s), the tile is sent without imagery (palette material) rather
  than with a baked fallback colour.
- **Tone curve** (`--tone`, LUT per channel): +0.06 at black, −0.10 at white, ~0 at mid-grey: lifts
  baked shadows a little and stops bright rock/sand glowing under the engine's own lighting.
- **De-shadowing** (`--deshadow lambert|full`, default off; docs/research/2026-10-10-deshadow-prototype.md,
  method A): per USGS z12 region (~8 km) a background thread fits the capture sun against the DEM
  (z14 imagery, DEM z13 on the same pixels), gates it (r² ≥ 0.25, shadow-class separation ≥ 0.9),
  measures haze, sky/sun ratio and the shadow classifier on a few z16 tiles at the shadow edges, and
  caches the values (`<cache>/deshadow/usgs/12/x/y.json` + `.npz` grids). `Imagery._mosaic` then
  relights each USGS z12–z16 source tile with an 8 px margin from its neighbours; results live in a
  memory LRU and `<cache>/imagery/usgs-ds<ver>-<mode>/` (raw tiles untouched). Tiles of a region that
  is not ready stay raw (renders wait up to 30 s for the regions within 2 km of a new anchor).
- **Texture format: DDS DXT1 (BC1) with a full mip chain** (512²: 10 levels, 174 904 B; 256²: 9 levels,
  43 832 B). Pillow 12 writes DXT1 but always `mipMapCount = 0`, so `imagery.encode_dds` box-reduces
  each level, BC1-encodes it with Pillow's encoder and writes its own header (`DDSD_MIPMAPCOUNT`,
  `DDSCAPS_MIPMAP|COMPLEX`). Pillow's BC1 on real imagery: 31–40 dB PSNR. Encoding a 512² chain
  takes ~4 ms. (PNG was the alternative: the engine would have to decode it on the main thread
  and build mips itself; DDS needs neither.)

## Elevation sources

| Use | Zoom | Source |
|---|---|---|
| Heightmap and L0 tiles | z15 below 58° latitude, z14 at 58–74°, z13 nearer the poles (always ≤ 2.5 m pixels) | Mapterhorn; on 404 → AWS z15 |
| Far tiles L1+ | z13 (L1) down to z5 (L9) at mid latitudes | Mapterhorn; on 404 → AWS (above z12 that is normal outside lidar areas; at z ≤ 12 a 404 is open ocean and negatives are clamped to 0) |
| Anchor relief survey | z10 | same |

Mapterhorn (`tiles.mapterhorn.com`, Terrarium WebP) uses 512 px tiles. It is global on
land up to z12 and deeper where lidar exists. AWS Terrain Tiles (256 px PNG, z ≤ 15) fill a 404
and are resampled onto the 512 px grid. A 404 is cached on disk as a `<y>.404` marker.

## Timings v1.3 (this machine, warm DEM cache, 2026-10-10, real server process)

| Mesh mode + imagery, initial set (784 tiles) | anchor_ready | first tile | 48 L-2 tiles shown | far_done | imagery fetches |
|---|---|---|---|---|---|
| Mt Whitney, cold imagery cache (USGS) | 0.30 s | 0.13 s | 0.71 s | 8.7 s | 876, keep-alive latency median 25 / p95 35 ms |
| Mt Whitney, warm imagery cache | 0.30 s | 0.06 s | 0.64 s | 8.0 s | 0 (876 from disk) |
| Dolomites 46.4, 11.8, cold (EOX; cold DEM too) | 2.26 s | 0.80 s | 1.36 s | 41.2 s | 745 EOX, median 259 / p95 273 ms (their server); 66 USGS probe 404s |
| Pyrenees, warm | 0.30 s | 0.06 s | 0.59 s | 8.3 s | 0 |
| v1.2 baseline (`--near-visual terrain --imagery off`, Whitney) | 0.30 s | 0.02 s | – | 2.7 s (700 tiles) | – |

Per tile (median, 3 workers in parallel, warm): geometry + `.cdae` write 12–17 ms, texture
(reprojection + DXT1 chain + write) 18–23 ms at 512² (L-2, L-1), 8–13 ms at 256². Files: shape 94 KB,
texture 171 KB (512²) / 43 KB (256²); the initial mesh set is 72 MB of shapes + 45 MB of textures.
Cold EOX starts are bound by EOX's ~260 ms per tile at 6 concurrent requests (the near tiles come
first and are ready in ~1.4 s).

## Timings v1.2 (this machine, Mt Whitney 36.5785, −118.2923, warm DEM cache, 2026-10-10)

| | |
|---|---|
| Anchor relief survey (z10) | 0.04 s → Q = 1/16 m (Whitney relief ~3 km) |
| 1024² terrain | 0.24–0.27 s |
| One far tile, build (DEM + geometry + err/sink) | L0 4.1 ms (129² lattice), L1+ 2.0–2.2 ms (median) |
| One far tile, write | `.cdae` 1.5 ms / 85 KB; `.dae` 2.4 ms / 158 KB |
| Initial far set (block at the origin) | 700 tiles; 2.6 s wall with 3 workers at nice 10 (3.1 s on one thread); first tile after 0.02 s |
| Swap-group deltas | 0.3 ms for 1 ↔ 4, 11 ms for an L3 ↔ 64 L0 group |
| Integration drive (11.4 km at 30 m/s with a 50° bend, rebase at 10 km; modelled .cdae import 2 ms) | 24 terrain-follow commits, all at 450–456 m (never gated, none forced at 700 m); overlapping samples identical (0 m in float64 and float32, 0.69–0.80 M samples per commit); 0 far keys changed by a commit; 0 holes in 1533 coverage checks incl. the 2 m N/E strip, rebase gap 0; 2905 tiles (193/km), 727 parsed with `cdae.read`; governor ≤ 2 imports / 4.05 ms per frame, steady streaming p99 2.39 ms, 0 units within 2 frames of the 26 heavy slots; 26 + 2 terrains requested, 30 built (one overtaken by the bend, one by the stop) |

L0 `sink` on Mt Whitney: median 8.7 m, max 40 m (cliffs between the 8 m vertices); on gentle
synthetic ground it is the 0.3 m margin plus a few cm.

## Tests

```sh
python3 -m unittest discover -s server/tests -v        # BNGE_SKIP_DRIVE=1 skips the drive test
```

`test_cdae.py` holds the golden `.cdae` tests. `test_heightmap.py` the lattice/quantum identity
tests. `test_farfield.py` selection, hysteresis, swap groups, err/sink/skirt and the shipped mod
assets. `test_mesh.py` mesh mode: selection down to L-2 with the min-level radii (no gaps/overlaps on
64 m cells), the 25 % hysteresis at 150/400 m, L-2 vertices == terrain PNG heights (float64 and the
engine's float32), the Torque diagonal rule against a port of `TerrainBlock::getHeight`, footprint
UV0 / world-locked UV1, and the engine's UV2 convention (stock shape, if the game is installed).
`test_deshadow.py`: box / guided filter / horizon march against brute force, hue-preserving gain
cap, sun recovery, relit ratio, halo bound and 20 px misregistration on a synthetic ridge scene,
the gates (flat DEM, low r², separation), the region cache (round trip, version bump, lambert reuse),
the imagery hook, and `--deshadow off` byte-identical to the pre-hook renders (`DESHADOW_TIMING=1`:
per-tile cost). `test_imagery.py`: a landmark lat/lon lands on its texel (within 0.2 m, two frames), orientation,
zoom choice, the source chain (404, blank, last-source blank, unreachable, undecodable, coverage
probes, min zoom), disk cache and remembered 404s, prefetch, tone curve, DDS mip chain (each level
the BC1 of the box-reduced previous one, readable by Pillow); plus three real-network checks (USGS
at Whitney, EOX in the Alps, the open ocean), skipped offline. `test_server.py` runs full
protocol-4 sessions against an in-process server with a synthetic DEM (no network): terrain mode
with imagery off (v1.2) in both `cdae` and `dae` formats, and mesh mode with synthetic imagery
(material/texture fields, DDS files, no sink, terrain commits change nothing, a drive with exact
cover at every step, hysteresis near the 150 m threshold, palette fallback when imagery fails). `test_e2e.py` and `test_integration_drive.py` start
a real server and download real tiles, so they need network access; their DEM cache is
`<tmp>/beamng-earth-test-cache`.
`test_demand.py` (v1.4 M0, M1) reproduces table 2.1 of docs/V1_4_SPEC.md (tiles added per second along a
straight drive at 4 Hz poses with hysteresis and HOLD_S), the v1.3.1 and the 64-cell (M1) rows; hooks
for the M3a / M5 rows. The layout-dependent tests (`test_farfield`, `test_mesh`, `test_server`,
`test_protocol_agreement`, `test_integration_drive` mesh) run for both 32 and 64 cells.
`test_drive_tool.py` checks `tools/drive.py` and `tools/drive_driver.lua` offline (profile rule on both
sides, nominal speed, the summary, the driver under luajit, `run_ge` with a fake MCP: polls >= 1 s
apart and an uninstall on every failure path). `test_v131_default.py` checks that a default
`earthd.Server` reproduces the v1.3.1 selection, geometry and heightmap (`fixtures/v131_golden.json`,
written by the v1.3.1 code). `bench_build.py` (not discovered: run it
by hand, it downloads unless `--synthetic`) times `Server.make_tile` per level, cold and warm.

Cross-component (server ↔ mod):
- `test_protocol_agreement.py`: the protocol 4 table above as data (sessions in both terrain/no
  imagery and mesh/imagery modes), checked from both sides: the
  mod's `net.send` constructors and every `msg.X` / `tr.X` it reads (parsed from its Lua), every field
  `earthd.py` reads, every message a real in-process session sends (types, nullability, the L0-only
  `sink`, path formats), the shared constants (protocol/format, lattice, Q ladder, `q·65536`,
  terrain-follow rule, tile grid, material name, file and `mapTo`) and the mod's material loading
  order (the material before the first tile / the terrain block); protocol 4: the `nearVisual`
  values, the imagery material name (`earthd.sat_material`) inside the shape, the texture next to
  the shape (DXT1, full mip chain, the level's size), UV0 = footprint with v flipped (row 0 =
  north), UV1 = tile-local metres / `DETAIL_M` (world-locked), and the mod's Material fields
  (`mapTo`, `colorMap`, detail map on UV1 at `detailScale 1 1`, a stock detail texture).
- `modsim.py`: the mod's runtime rules in Python, every threshold read from the Lua: `follow.lua`,
  `rebase.lua`, `governor.lua`, `crossover.lua` and the `farfield.lua` state machine.
  `test_modsim.py` runs the Lua itself under luajit (with `mod/tests/stubs.lua`) on the same
  schedules (`tests/lua/*.lua`) and requires identical traces, frame by frame.
- `test_integration_drive.py`: the real server and the modelled mod at 60 fps over a 10 km drive with
  a rebase, twice: `--near-visual terrain --imagery off` (v1.2) and `--near-visual mesh` with
  imagery from a deterministic offline source (`tests/fake_imagery_earthd.py`). The §9 / V1_3
  acceptance checks that work offline: (a) bit-identical overlapping samples across every commit,
  (b) far keys stable across commits (terrain mode), (c) no hole at any step including the 2 m N/E
  strip (mesh mode: without the invisible block, plus every frame within 512 m of the player),
  (d) `.cdae` shapes (and textures) that parse and never vanish under a live tile / Material,
  (e) the governor's per-frame budget (no frame over it), heavy-slot isolation and streaming p99
  against the server's real message stream with texture/material costs, no pops, (f) mesh mode:
  an L-2 tile under the player at every pose, every shown L-2 tile over the block equal to the
  block's heightmap (float64 exactly, float32) with the TerrainBlock's diagonals, (g) Materials
  exactly those the tile objects use (no leak), and no burst of deletes of files the mod loaded.
  `BNGE_DRIVE_SPEEDUP` (4), `BNGE_DRIVE_REBASE_M` (10000), `BNGE_DRIVE_IMPORT_MS` (2.0),
  `BNGE_DRIVE_TEX_IMPORT_MS` (3.0), `BNGE_DRIVE_MODES` (`terrain,mesh`),
  `BNGE_DRIVE_KEEP_LOG=file` (`file.<mode>`), `BNGE_SKIP_DRIVE=1`.

## Attribution

Show this in the mod UI (the server sends the line for the sources in use as `hello.attribution`):
"Imagery: USGS The National Map (public domain); Sentinel-2 cloudless by EOX (CC BY-NC-SA 4.0 /
CC BY 4.0); Elevation: Mapterhorn (see mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen".
Full EOX credit: "Sentinel-2 cloudless 2023 by EOX IT Services GmbH (Contains modified Copernicus
Sentinel data 2023)" (CC BY-NC-SA 4.0: personal, non-commercial use) and "Sentinel-2 cloudless 2016
by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2016)" (CC BY 4.0); see
<https://s2maps.eu>. Elevation source details: <https://mapterhorn.com/attribution>,
<https://github.com/tilezen/joerd/blob/master/docs/attribution.md>.
