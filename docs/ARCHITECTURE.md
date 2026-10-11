# BeamNG.Earth — Architecture (v1 design)

Target: BeamNG.drive 0.39.4 (native Linux, Vulkan). Research date 2026-10-09.
Raw research + adversarial verification: `docs/research/2026-10-09-research-raw.json`.

Game install: `~/.local/share/Steam/steamapps/common/BeamNG.drive`
User folder (VFS root overlay): `~/.local/share/BeamNG/BeamNG.drive/current`

## Core idea: "globe in the companion, flat bubble in the game"

Same pattern as MSFS "anchor space", Cesium origin-shifting, and KSP floating origin.

- **The game** only ever sees a local East-North-Up (ENU) frame of a few km
  around an *anchor* point. +X east, +Y north, +Z along the ellipsoid normal.
  Gravity stays the stock uniform -Z (error is d/R ≈ 0.07° at 8 km).
- **The companion server (`earthd`)** holds the real WGS84 Earth in float64 ECEF.
  It emits every tile as vertices already transformed into the current anchor's
  ENU frame. Curvature (drop ≈ d²/2R: 7.8 cm @1 km, 7.85 m @10 km, 785 m @100 km)
  and correct polar geometry fall out automatically. Nothing is Web-Mercator-warped.
- **Floating origin.** When the player gets ~8 km from the anchor, the mod
  *rebases*: it picks a new anchor and applies one rigid transform (translation
  plus a rotation of ~d/R) to every tile, vehicle, camera and road. Then it
  rebuilds collision and the navgraph once.
- **Tile index.** A cube-sphere quadtree (S2-style) on the server, so tiles are
  near-uniform everywhere, poles included.

## Hard engine facts (verified in game files / binary)

| Fact | Evidence |
|---|---|
| `ProceduralMesh` builds lit, textured, **collidable** meshes at runtime from Lua tables `{ {verts,uvs,normals,faces{v,n,u}(0-based),material}, ... }` per LOD | `lua/ge/extensions/flowgraph/nodes/environment/planet.lua:301-321`, `util/trackBuilder/proceduralPrimitives.lua:472-575`, `roadArchitect/roadMesh.lua:353` |
| Static collision is ONE global kd-tree, rebuilt only by `be:reloadCollision()` (~0.07 s per 2M tris on this machine; 1.5 s @37M) | user `beamng*.log`, `splineTrack.lua:978` |
| `TSStatic` can load runtime-written `.dae/.dts/.cdae`; `collisionType='None'` = render-only | `util/procTrack.lua:1269-1293` |
| glTF/GLB only with the `-assimp` launch flag (assimp has Draco/basisu/webp built in) | exe string "glTF shapes require the -assimp flag" |
| Materials are creatable at runtime (`createObject('Material')`, `colorMap`, `groundType`) | `planet.lua:77-91` |
| Scene positions are float32; Lua math and vehicle ref-node are double. There is **no runtime origin shift** (`-levelOffset x y z` is load-time only) | `cameraUpdate.lua:145`, `parseArgs.lua:119-131`, `server.lua:61-83` |
| Gravity: uniform scalar on Z (`core_environment.setGravity`) + additive point-mass planets per vehicle (`obj:setPlanets{x,y,z,r,mass}`) | `core/environment.lua:1194`, `vehicle/beamstate.lua:592-663` |
| Vehicle move without reset: `veh:setClusterPosRelRot(-1, x,y,z, qx,qy,qz,qw)` (keeps velocity) | `tech/techCore.lua:909-915`, `spawn.lua:532` |
| **GE Lua cannot reach the internet.** luasocket host allowlist (localhost/LAN/beamng.com only), no working LuaSec TLS, CEF CSP limited to localhost, `ffi.load` and `os.execute` sandboxed | exe strings `luasocket_sandbox`, `unable to parse IP: {} - DENIED` |
| Localhost IPC: `BNGWebWSServer` (native thread, mutex queue) or luasocket TCP `settimeout(0)` | `lua/ge/extensions/mcp/server.lua:109-116`, `wsUtils.lua:27-75` |
| TerrainBlock: flat, axis-aligned, power-of-2 ≤8192², heights ≥0, probably a single physics block. At most a local patch | `util/terrainGenerator.lua`, `tech/terrainImporter.lua` |
| AI navgraph can be injected: `map.reset(customMapNodes)` | `lua/ge/map.lua` |
| Sky follows real position: `core_celestial.setLocation(lat, lon)`, ScatterSky planet-radius atmosphere | `core/celestial.lua` |
| Built-in MCP server (Options › Advanced › enable MCP) → `http://127.0.0.1:29292/mcp` with `run_lua`, `get_logs`, `screenshot` (dev/test harness) | `lua/ge/extensions/mcp/` |
| Mods: `scripts/<mod>/modScript.lua` → `setExtensionUnloadMode(n,'manual')`; `extensions.load(n)` | `core/modmanager.lua:704-711` |

## Phase 0 probe results (measured in-game 2026-10-09, via MCP, scripts in `probes/`)

| Probe | Result |
|---|---|
| Internet from GE Lua | `socket.tcp():connect('example.com'/'93.184.215.14')` gives **"connect restricted"**. Localhost works. `ffi.load` is denied. |
| Localhost TCP throughput | Binary-safe (NUL/0xFF intact), **~700 MB/s** (16 MiB in 23 ms) with blocking receive. **Stream mesh data over TCP; no temp files needed.** |
| FFI under sandbox | **`ffi.cast` is a stub that returns its argument unchanged.** Pointer arithmetic on cdata fails. `ffi.new` + `ffi.copy(dst, luaString, n)` works (16 MiB into `float[]` in 2.8 ms). Parse binary payloads by `ffi.copy`-ing into typed arrays/structs. |
| ProceduralMesh winding | Triangles must be wound so the normal points **up** in BeamNG's frame: for grid quad a=(i,j), b=(i+1,j), c=(i,j+1), d=(i+1,j+1) use **(a,c,b),(b,c,d)**. Static collision is **one-sided**, so wrong winding = fall through the floor. |
| ProceduralMesh collision | Not collidable until `be:reloadCollision()`. After it, `castRayStatic` hits immediately (next call). |
| ProceduralMesh cost | 512 tris: 0.9 ms; 2 048: 2.1 ms; **8 192: 8.9 ms** (createMesh; Lua table build about the same again). Roughly linear. |
| **ProceduralMesh size cliff** | **A single 32 768-tri mesh (129² grid, 98 304 face corners) hangs the game forever** (100% CPU, needs a kill). Twice reproduced (also 131k tris). Suspected limit is 65 535 face corners (a 16-bit index); unconfirmed. **Rule: keep each ProceduralMesh ≤ 8 192 tris (verified safe). Never go above ~20k.** |
| reloadCollision | 2 048 tris: 0.5 ms; 8 192 tris: 2.3 ms (in an otherwise empty smallgrid). |
| Ray tools | `castRayStatic(pos, dir, len)` returns the physics-collision distance (use it to verify drivability). The MCP `raycast` tool only sees render geometry. |
| **Collision rebuild scales linearly with ALL collidable tris, and it blocks the frame** | ProceduralMesh: 131k tris 41 ms, 524k 162 ms (**~0.31 ms per 1k tris**). TSStatic 'Visible Mesh' costs about half (~0.15 ms per 1k). `reloadStaticCollision()` and `reloadCollision(false,true)` cost the same. There is **no cheaper path**. Budget: keep collidable geometry around ~100k tris (≈20–30 ms per rebuild). |
| ProceduralMesh render-only? | **No.** `disableCollision()` does NOT remove it from physics (ray still hits, rebuild cost unchanged). It has no collisionType field. **Far/render-only tiles must be TSStatic with `collisionType='None'`.** |
| TSStatic from runtime-written .dae | Works. A `.dae` written by an external process into `<user>/temp/beamngEarth/` loads immediately via `shapeName='/temp/beamngEarth/x.dae'`, with its PNG texture (relative `init_from`). 8 192 tris: 51 ms spawn; **131k tris: 415 ms** (XML parse, main thread). Large single meshes do NOT hang (unlike ProceduralMesh). **Its collision appears only on a reload in a later frame**, not in the same frame as the spawn. Writer: `server/dae.py`. |
| Drivability | A car drives on ProceduralMesh tiles normally (follows the surface; ground clearance stays constant). |
| **Floating-origin rebase** | Moving 64 tiles (`setPosition`) + `be:reloadCollision()` + `veh:setClusterPosRelRot(-1, p+D, 0,0,0,1)` in one GE call: a 3 km jump at 20 m/s keeps **speed (20.0→20.1) and height continuous, zero damage, zero instability**. The cost is the collision rebuild alone (tile moves 0.4 ms). Script: `probes/p6_rebase.lua`. |
| Time of day | `TimeOfDay.time` **0 = noon, 0.5 = midnight** (the MCP tool description saying 0.5 = noon is wrong). |

### TerrainBlock probes (the faster path, 2026-10-09)
| Probe | Result |
|---|---|
| Runtime create | `TerrainBlock()` + `setTerrFileLvlFolder` + `registerObject` + `MissionGroup:addObject` + `importMaps(png, metersPerPixel, maxHeight, '', {}, {}, false)`: 256² in 67 ms, 2048² in 213 ms. Works on smallgrid. |
| Re-import into existing block | 2048²: **115–138 ms** (PNG compression level makes little difference); **1024²: ~38 ms**. `reloadCollision` after it: 7 ms (2048²), 1.5 ms (1024²). |
| **Collision cost** | `be:reloadCollision()` with only a 2048² terrain: **~7 ms** (with 256²: 0.25 ms), versus 162 ms for 524k ProceduralMesh tris. Terrain does not go through the triangle kd-tree. |
| Live edits | `tb:setHeight(gx,gy,h)` ≈ 0.1 µs per sample (1M samples 108 ms); `updateGrid(min,max)` 1.7 ms per 128², 37 ms per 1024². **Physics picks up edits BY ITSELF within ≤0.5 s, with NO `be:reloadCollision()`** (probe p8: a 41² patch raised to 60 m, the car rests at 60.22). An immediate raycast still sees the old height. This is the hitch-free path for live refinement or patching. (Lead from the BrokenBrainCells terrain-deformation mod, see docs/research/2026-10-09-prior-art-live-updates.md.) |
| Orientation | `importMaps(flipY=false)`: PNG row 0 = **north** (max y). setHeight grid (gx,gy) is relative to the block's SW corner, with gx east and gy north. |
| Render-disabled terrain | `tb:setRenderEnabled(false)` stays **fully collidable** for vehicles, so it can serve as invisible physics under our own visual meshes. |
| GroundPlane | **Once any TerrainBlock exists, smallgrid's GroundPlane no longer collides for vehicles** (castRayStatic still reports it, which is misleading). Outside the block, vehicles fall forever. |
| **Multiple TerrainBlocks** | **Not supported by physics.** A second block (registered, rendered, `getHeight` correct) does not collide at its real heights: vehicles sink through to ~z=0.4. Only one TerrainBlock can be used; deleting the second leaves the first working. |
| Materials | With `materials={}` the terrain renders the pink "NO MATERIAL" placeholder; it needs a TerrainMaterial. |
| Vehicle inputs | Arcade gearbox: holding **brake** at standstill engages **reverse** and accelerates. Always zero inputs after scripted drives. |

### Design decisions forced by the probes
- **SUPERSEDED for ground by the TerrainBlock probes:** drivable ground = ONE runtime TerrainBlock re-imported
  on each rebase (~40 ms for 1024², ~130 ms for 2048²). See `docs/V1_SPEC.md`. ProceduralMesh is now only for
  small collidable extras (bridges, buildings near the player).
- (old) **Near ring (collidable):** ProceduralMesh tiles of **32×32 cells at 2 m = 64 m, 2 048 tris**
  (~5 ms each, so one or two spawns per frame without hitching). The companion streams the
  vertex data as binary over localhost TCP (parsed with `ffi.copy`). The collidable area is
  ~7×7 tiles (~450 m) ≈ 100k tris, so a rebuild costs ≈ 30 ms. Rebuild only when the ring
  shifts (with hysteresis), batched with any pending rebase.
- **Far rings (render-only):** TSStatic + companion-written `.dae` with `collisionType='None'`.
  Keep each one coarse (≤ ~8k tris) because DAE parsing runs on the main thread. A binary
  `.cdae` writer is the later optimisation.
- **Rebase** is cheap apart from the collision rebuild, so do it together with a ring change.

## Components

### Companion `earthd` (Python prototype → Rust later), 127.0.0.1
- Geodesy f64: geodetic ↔ ECEF ↔ ENU(anchor).
- Cube-sphere quadtree tile index; screen-space-error LOD driven by the player
  pose, FOV and screen height sent by the mod at ~10 Hz.
- Fetchers with two **separate** caches: a shareable cache for open data, and a
  private, HTTP-Cache-Control-only cache for bring-your-own-key (BYO-key) providers.
- Builders: terrain mesh (RTIN/simplified grid, skirts, flattened under
  roads/buildings), mipmapped DDS imagery, extruded buildings, road ribbons,
  and navgraph node tables.
- Output: atomic files in `<user>/temp/beamngEarth/<session>/` (versioned names,
  because the engine caches by path), plus small JSON control messages.

### Mod `beamng_earth`
- Level `levels/beamng_earth/`: smallgrid skeleton **without GroundPlane**,
  with SpawnSphere, LevelInfo, ScatterSky and TimeOfDay.
- Extension `beamngEarth_core`, with these parts:
  - `net`: the server connection.
  - `anchor`: the ENU frame, in doubles.
  - `tiles`: the spawn/delete queue, time-sliced to ~2 ms per frame.
  - `collision`: the scheduler for collision rebuilds.
  - `rebase`: the floating-origin shift.
  - `navgraph`, `sky`, and `ui`: teleport to lat/lon, server status, attribution.

### LOD rings (indicative)
| Ring | Range | Ground resolution | Collision |
|---|---|---|---|
| R0 | 0–1 km | 1–2 m DEM + buildings | yes (ProceduralMesh, ≤1–2M tris) |
| R1 | 1–5 km | ~8 m | no |
| R2 | 5–30 km | 30–60 m | no |
| R3 | to horizon | ~250 m | no (fog/atmosphere hides the edge) |

The parent tile stays visible until all its children are loaded (no holes).
R0 collision is rebuilt in batches every ~250–500 m travelled, with the next
tiles prebuilt ahead along the velocity vector.

## Data sources

Physics geometry always comes from open data:
- **Elevation:** Mapterhorn (open, Terrarium WebP, z12 global, up to z18 where
  national lidar exists), with AWS Terrain Tiles as fallback. Above ±85° use
  Copernicus GLO-30, because the Mercator sources stop there.
- **Imagery:** EOX Sentinel-2 cloudless 2016 (CC BY, 10 m) far away, NAIP or
  national orthophotos near the player. Esri World Imagery is optional, using
  the user's own key.
- **Buildings and roads:** Overture and OpenStreetMap, from extracts the server
  holds itself. Never query the public Overpass API at runtime.
- **Land cover and vegetation:** ESA WorldCover and Meta canopy height.
- **Gravity:** WGS84 normal gravity (Somigliana + free air) plus the EGM2008 anomalies (NGA,
  degree 2190, about 9 km; 74 MB once from GeographicLib's mirror of NGA's coefficients;
  45 ms a point): `server/gravity.py`, described in server/README.md "Gravity". Real g runs
  from 9.76 to 9.84 m/s²; the anomalies are −350 to +600 mGal. earthd sends it to the mod as the
  `gravity` message (`--gravity on`, default), applied through the weather extension's `setGravity`.

**Google Photorealistic 3D Tiles** (v2, optional): visual only, the user's own
key, fetched straight from the user's own companion, no shared cache, Google
logo and attribution on screen. It returns 403 for projects billed in the EEA
since 2025-07-08. Bing Maps is retired for new users. Apple has no usable API.

## Roadmap
0. **Probes** (via MCP `run_lua`): sandbox state, IPC round-trip, `createMesh`
   cost on a 257² grid, `reloadCollision` hitch at 0.5/1/2/5M tris, whether
   `disableCollision` excludes a mesh from the rebuild, whether runtime-written
   files in `/temp` load at once, `setClusterPosRelRot` with trailers, jitter
   at `-levelOffset` 20/100/500 km, `visibleDistance` 50–100 km.
1. Skeleton: the mod, the level, the extension, the companion handshake, the
   anchor module, teleport to lat/lon, and sky location.
2. Drivable terrain: the R0 collidable ring from open DEM data, plus floating-origin rebase.
3. LOD to the horizon: R1–R3 render-only rings, imagery, curved ocean, and
   tests at the poles and the antimeridian.
4. Man-made world: roads, navgraph and AI, buildings, and ground types.
5. Performance: vegetation, LRU disk cache, prediction, budgets.
6. Optional: the Google 3D Tiles visual layer, flight mode, radial gravity, multiplayer anchors.

## Top risks
- Collision-rebuild hitches when streaming at highway speed.
- LuaJIT garbage-collection churn from table-based `createMesh`.
- Rebase must move every subsystem that stores absolute positions.
- Float32 precision and depth precision at long view distances.
- Licence/ToS constraints on Google and on non-commercial data sources.
- Open imagery is only 10 m in much of the world.
- Undocumented engine behaviour that may change with game updates.

## v1.1 in-game results (2026-10-10)
| What | Measured |
|---|---|
| Teleport (anchor + 1024² terrain import) | 66–67 ms in-game; server builds the terrain in 0.24–0.38 s |
| Terrain-follow commit (block moves, nothing else does) | ~67–71 ms. Of that, `importMaps` of a real 1024² heightmap is ~49 ms (level-1 PNG) or **~43 ms (uncompressed, now the default)**, and `reloadCollision` 1.6 ms |
| Origin rebase (2 km test; default 10 km) | 75 ms, with exact rotation (0.022° at 2 km), 15 camera state points remapped, longitude continuous |
| Far field | Initial 384–445 tiles (L0…L7) sent in 1.7–2.8 s. After a rebase all tiles are re-streamed in the new frame at ≤6 ms per frame (~10 s); the old tiles stay visible meanwhile, so there are no holes |
| Server restart | The mod reconnects and resyncs with an identity transform; the car doesn't move |

Next optimisations: anchor-independent far tiles (tile-local frames) so a rebase reuses them instead of re-streaming; a binary `.cdae` writer for faster tile loads.

## v1.2 in-game results (2026-10-10, docs/research/perf-session-v12.jsonl)
| What | v1.1 | v1.2 |
|---|---|---|
| Far-tile import | ~22 ms (Collada) | **~1.9 ms** (server-written .cdae; governor estCdae 1.93) |
| Terrain-follow commit / worst frame | 57–66 ms / 71–93 ms | **45–49 ms / 59–63 ms**; neighbouring frames calm (13–16 ms) |
| Ground under the car across terrain moves (400-point P1b auto-check) | resampled every move | **0.00 mm** on every move (one 0.06 mm) |
| Average FPS driving | 70–90 | **90–100** |
| Post-rebase reload | ~44 fps, p95 25–27 ms, ~400 tiles | **73–81 fps, p95 14–16 ms**, sim speed 1.000, ~770 tiles |
| Rebase commit | 61–65 ms | **50 ms** (one 66 ms frame) |
| Freeze ~30 s after rebase (burst delete of loaded files) | 200–625 ms, every rebase (8/8) | **gone**: worst frame 62 ms; deletes run at 5/s |
| Tile crossovers | n/a | 55+ smooth crossovers, 0 pops, 0 failed imports |
| Server RSS | ~410 MB | ~600 MB (DEM LRU raised to 512 tiles) |

## v1.3 in-game results (2026-10-10, docs/V1_3_SPEC.md; shots in docs/research/v13_ingame, drives in docs/research/sim_v13*)
Satellite imagery on every tile; the near ground is drawn by 64/128 m mesh tiles (L-2/L-1) and the
TerrainBlock is physics only (render disabled, stays disabled across `importMaps`).

| What | v1.2 | v1.3 |
|---|---|---|
| Look | brown terrain dirt next to whitening far palette: a hard colour border, mid-distance | USGS (US, ~1.9 m) / EOX (world, ~10 m) imagery on everything, **no border** near or far |
| Ground change across a terrain-follow commit (fixed camera) | the terrain itself was the visible ground | the terrain is invisible: commits change nothing on screen; remaining diff is far crossovers + sub-pixel jitter (3.4 / 10.6 mean, v1.2 1.5 / 10.8) |
| Ground swap at a rebase (fixed camera, 0.5 s vs 10 s after, ~630 tiles replaced) | n/a | **2.4 mean, 0 % of pixels > 30**: invisible |
| Terrain collision vs visible mesh | n/a | BeamNG terrain collision is **exactly bilinear** per 2 m square (probe `probes/v13/p_diag.lua`: 0.320 at (0.8,0.4) and (0.4,0.8) on both parities), not triangulated. The mesh's triangles differ from it by \|twist\|/4 at a square centre: median 0.4 cm, p99 3.9 cm, max 31 cm on a 145 m-relief block. (Fix if ever needed: a centre vertex per L-2 square cuts it to \|twist\|/16.) |
| Textured tile import (governor estCdaeTex) | 1.9 ms (untextured) | 3.6–4.7 ms; material 0.4 ms; material delete 0.04 ms |
| FPS driving (sim, 30 m/s) | 94 | 92 in Kern canyon, 70–77 on open desert; rendering the 850 textured tiles costs only ~0.6 ms (hidden-tiles test) - the rest is streaming |
| Post-rebase reload | 73–81 fps, ~770 tiles | 56–70 fps for ~50 s, ~880 tiles, sim speed 0.996; old tiles drop as soon as new ones cover them (`oldCovered`) |
| Pops / failed imports / material leaks / pink tiles | 0 / 0 / - / - | 0 / 0 / 0 (materials == live tiles after 8 boundary-crossing passes) / none |
| Server deletes (up to 40 files/s in mesh mode) | 5/s | no effect on frame times (server paused vs running: same) |

Fixed in-game during v1.3 bring-up:
- **Tile-edge seam line**: the engine samples material textures with one WRAP sampler
  (`shaders/common/bng.hlsl` `defaultSampler2D`), so each tile edge blended in the opposite edge.
  Textures now carry an 8-texel border of real imagery (`imagery.gutter*`), UV0 inset to match.
- **Fabric-like pattern on distant hills**: the v1.0 material adds the detail map with no distance
  fade (`defaultMatV0.hlsl:246`), so the stock 2 m detail texture repeated visibly at 300 m. Replaced
  by our own procedural `art/beamngEarth/detail_ground.dds` (tools/make_detail_texture.py) whose mip
  chain fades to flat 0.5 grey: the GPU's mip selection fades the grain out with distance.
- Near textures 512 -> 256 px (64 m tiles were 15x finer than the USGS source): half the build time
  and VRAM, no visible change.
- tools/sim_drive.py kept stepping in the old frame after a rebase (26 rebases in a row in the
  test); it now continues from the car's position in the new frame.

Imagery quality limit: USGS ImageryOnly z16 is soft and JPEG-blocky up close (~6 m blocks). The
public-domain **USGS NAIP ImageServer** (`imagery.nationalmap.gov/.../USGSNAIPImagery/ImageServer/exportImage`,
0.6 m, CONUS) is far sharper, but: no tile cache (exportImage only), 4–29 s per request (a 2048²
request covers ~1.2 km), and a different vintage/colour than the USGS tiles (Lake Isabella: other
water level; mosaic seams), so mixing it near the car with USGS beyond would bring back a colour
border. Candidate for v1.4: NAIP for all US levels via large chunked exportImage requests fetched
ahead of the car, plus a `far_retex` message (swap a live tile's texture) for tiles built before
their chunk arrived.

## v1.4 M0: test harness and baselines (docs/V1_4_SPEC.md §3.0; offline parts done 2026-10-10)
- **GE-side driver** `tools/drive.py` + `tools/drive_driver.lua`: the Lua is sent through MCP
  `run_lua` and hooks `onPreRender` with an extension proxy (`newExtensionProxy` +
  `submitEventSinks`, `lua/common/extensions.lua:1081-1164`; no mod file, removed with
  `proxy:destroy()` in a `finally`). Every frame it moves the player along a profile with
  `setClusterPosRelRot(-1, …)` (a yaw-only relative turn, no reset) and
  `applyClusterVelocityScaleAdd(-1, 0, vx, vy, vz)`, rays the static-collision ground
  (`castRayStatic`, a miss = `fall`), keeps the inputs at zero and records each frame (wall ms,
  heavy slot, rebase, harness MCP work, exact miss, tile level under the car). A rebase moves the
  driver's own frame through the wrapped `rebase.moveVehicles`. Error paths: a failed install step
  undoes the earlier ones; an error in a frame, or no poll for 60 s (a dead harness), stops the run,
  parks the car (velocity 0, inputs zero) and restores `rebase.moveVehicles`; every MCP call after
  the first runs inside drive.py's `finally` uninstall; reloading the driver always uninstalls the
  previous one. MCP is polled at most once a second,
  timing runs take no screenshots (`--shots S` = a separate visual run), `--step-mode mcp` keeps the
  old `sim_drive.py` stepping. The summary has the effective speed (a run below 95 % of nominal
  fails), exactMiss (overall and outside 2 s after rebases), commit / rebase frame ms apart from the
  steady frames, max queued, backlog slope, live / objects max, estCdaeTex first vs last quarter and
  the post-rebase reload time.
- **Mod metrics** (behaviour unchanged): `farfield` finds the shown tile under the player by key
  lookup each frame (previous anchors through the inverse rebase transforms) and reports
  `exactMiss` / `exactFrames` / `underLevel` / `waitingGroups` (exact level −2 for 32 cells;
  `setExactLevel(-1)` for M1's 64 cells); `perfmon.tagHarness()` keeps MCP frames out of its
  statistics; `core` keeps the server's `status.backlog` (`status().serverBacklog`).
- **Server**: every `status` carries `backlog` (desired − shown, current anchor); `--status-interval S`
  (default 0 = v1.3.1) re-sends the last status while it changes — harness runs use 1.
- **Default = v1.3.1** (`server/tests/test_v131_default.py`): a default `earthd.Server`'s selection,
  switch distances, DEM zooms, tile geometry, written .cdae / .dae sizes, swap deltas and heightmap
  match `tests/fixtures/v131_golden.json`, written by the v1.3.1 code.
- **Offline**: `server/tests/test_demand.py` reproduces table 2.1's v1.3.1 rows (14.2 / 33.7 / 50.3
  adds/s at 40 / 90 / 134 m/s against 14.4 / 33.4 / 49.9; M1/M3a/M5 hooks marked);
  `server/tests/bench_build.py` (not discovered) times `Server.make_tile` per level, cold and warm.
  First 64-cell numbers (Whitney, one worker): warm 16-18 ms at 256 px (real and synthetic data),
  27-30 ms at 512 px (synthetic); cold L-1 0.48 s and L4 0.60 s with network fetches; 348 KB `.cdae`,
  8704 tris.
- Still to run in-game (user not playing): P0 (the driver's calls), then the D40 / D67 / D90
  baselines with v1.3.1 behaviour.

## v1.4 M1: 64-cell tiles (docs/V1_4_SPEC.md §3.1; built offline 2026-10-10, behind a flag)
- **Server**: `earthd.py --cells 32|64` (mesh mode only for 64; **default 32** = v1.3.1, FORMAT 3,
  until the in-game gate P8 passes). The server makes one frozen `farfield.Layout(cells, block,
  mesh, tex)` and passes it to `select` (K table, near radii), `build_tile`, `zoom_for_level` and
  `heightmap` (block size); there is no module-level `CELLS` any more and no test changes module
  state. 64 cells: FORMAT 4, tiles 64×64 cells (8704 tris, 348 KB textured `.cdae`), every level
  keeps its size, the K' table {0.75, 1.5, 1.5, 1.25} and NEAR_RADII {-1: 150, 0: 400} keep the
  vertex spacing at a distance; the exact level is L-1 (128 m on the 2 m physics lattice, Torque
  diagonals); `--tex` per level (default 256 px to L1, 512 px from L2); `write_built` allows 9000
  tris; tile cache 2500; `hello.cells` and `far_tile.tex` (FORMAT 4 only, so a 32-cell session is
  byte-for-byte v1.3.1).
- **Demand** (`test_demand.py`, real `select` at 4 Hz with hysteresis and HOLD_S): 64 cells 303 /
  309 / 309 tiles and 4.6 / 10.7 / 16.0 adds/s at 40 / 90 / 134 m/s (spec 300; 4.6 / 10.6 / 16.0),
  against 838-867 tiles and 14.2 / 33.7 / 50.3 adds/s for v1.3.1.
- **Mod**: reads FORMAT 3 and 4 (`core.lua FORMATS`; its hello sends 4, the newest); `hello.cells`
  sets `crossover.setCells` (fallback error) and the exact level of the `exactMiss` / under-level
  metric (`farfield.exactLevelFor`: L-2 for 32, L-1 for 64); the import kind follows `far_tile.tex`
  (`governor.texKind`: `cdaeTex128/256/512`, priors 0.75 / 3 / 12 ms = today's `cdaeTex` scaled by
  texture area until P8; FORMAT 3 tiles stay `cdaeTex`). `tools/drive.py` summarises the 256 px kind
  for 64-cell sessions and every textured kind apart.
- **Integration drive** `MeshDrive64Test` (real server, `--cells 64`, 16.6 km with one rebase; the
  32-cell drives are kept): cost per kind and object count c = prior · (1 + 0.0016 (n − 300)) with
  the 256 px import at 6 ms (128 px 4.9, 512 px 10.5). Results: 300 tiles initially (784 with 32
  cells), 125 tiles/km while driving (383), tile objects max 414 (1047); no hole, no pop, no failed
  import; the tile under the player is L-1 at every pose and 664 L-1 tiles match the heightmap bit
  for bit; between vertices the mesh is within |twist|/4 of the bilinear collision at every one of
  ~5500 random points under the player (median 0.19 cm, p99 12.9 cm, max 60 cm on this route: the
  same p99 as the 32-cell drive, 13.0 cm, since both exact levels are the lattice; the spec's 5 cm
  is the p_diag block's figure); streaming-frame p99 12.4 ms of work, steady frame wall p99 27.6 ms
  with this cost model (the in-game rule is ≤ 25 ms: P8 decides).
- Still to do in-game (user not playing): P8 (+P10), then flip the default to 64 (`CELLS_DEFAULT`
  in `server/earthd.py`).

## v1.4 M1 in-game results (2026-10-10, Baltimore, 4K, tools/drive.py with a stock test car)
| run | layout | exact level under the car | queue max | objects max | fps mean | steady frames > 50 ms |
|---|---|---|---|---|---|---|
| D67 | 32 cells (v1.3.1) | 99.9 % | 44 | 970 | 82.5 | 2 |
| D90 | 32 cells (v1.3.1) | **25.5 %** | **2442** | 1187 | 62.6 | 1 |
| D67 | 64 cells (M1) | 100 % | 17 | 341 | 103.0 | 3 |
| D90 | 64 cells (M1) | **100 %** | **35** | 339 | 97.1 | 1 |
| D134 | 64 cells (M1) | 97.0 % (L0 in 46 frames) | 64 | 370 | 81.5 | 0 |
- P8 gate: 64-cell textured imports 4.03 ms (256 px) and 4.21 ms (512 px) at ~300 objects (gate:
  <= 7.5 / 12 ms); the shape (348 KB) dominates. Governor priors set to these values.
- 64 cells is now the command-line default in mesh mode (`--cells 32` keeps the v1.3.1 layout;
  `Server()` itself still defaults to 32, pinned by test_v131_default).
- Terrain-follow commit frames are still 55-80 ms (M4 addresses them); no rebase in these 12 km runs.
- Harness lesson: per-frame placement of a lifted, soft-sprung car (the user's Hopper Off-Road) at
  67 m/s made its physics unstable and the engine removed it. drive.py now drives a stock test car
  (Vivace), freezes the user's vehicle meanwhile and restores it, and stops on a non-finite or runaway
  velocity ('unstable').

## v1.4 M4: the 512² physics block (docs/V1_4_SPEC.md §3.5; built offline 2026-10-10, behind a flag)
- **Server**: `earthd.py --block 512` (mesh mode only; **default 1024** = v1.3.1 until the in-game
  probes below pass). `farfield.Layout(cells, block)` carries the block size (`Layout.grid` 128 m,
  `Layout.block_at`, `Block.size`); `heightmap` takes the size everywhere (`grid(size)`,
  `valid(size)`); `earthd.FOLLOW_RULES[size]` is the mod's follow table (512: prepare 128 m, lead
  2 s, reach 260 m, prefetch from 100 m; fast mode above 150 m/s: lead 3 s, reach 420 m, the prepare
  test 0.75 s ahead), used by `predict_request` and the prefetch start; requests snap to the layout's
  grid; `hello.blockSize` only with 512 (a 1024 session's hello is unchanged), `terrain.size` 512.
- **Mod**: `core.setBlockSize(hello.blockSize or 1024)` first in `hello` calls `lattice.configure`,
  `follow.configure`, `terrain.configure` (the 512 layer / flat maps, also for `loadFlat`) and
  `rebase.configure`; `anchorTerrain` / `terrain_ready` accept the session's size. `follow.lua` has
  flat `*_1024` / `*_512` constants (modsim reads them) and `configure` resets every derived local.
  1024² behaviour is unchanged (no fast mode, no re-check, stale anchors discarded as before).
  - Commit-time re-check (512 only, normal mode): a ready block with
    cheb(p + 2 s·v, pending) > 128 m is discarded and re-prepared **only while the player stays out
    of the 150 m edge band for longer than the heightmap latency (the measured one, at least 1 s)
    + 0.5 s** (never past the hard commit, never to re-request the same centre). The spec's unguarded
    rule discarded nearly every block in the offline model (77 of 78 commits at 134 m/s) and took a
    250 m/s car up to 98 m off the block at 1.2 s latency.
  - A ready block that would still be no better than the current one where the player reaches the
    hard commit distance (straight on) is dropped, so the next frame prepares the target; before
    (review, 2026-10-10) it blocked every request until it left the 260 / 420 m reach.
  - Offline (luajit `test_follow`, the real follow.lua): latency drawn per request from 0.3-1.2 s,
    commits 0-0.15 s late, all headings: ≥ 171 m inside the edges at 134 m/s, ≥ 178 m accelerating
    100 -> 200 m/s (before the two review fixes: 75 m, and up to 116 m off the block); fixed latency:
    134 m/s ≥ 199 m, commits ≥ 0.95 s apart at 0.3 s (spec ≥ 0.9; 0.32 s without the re-check);
    200 m/s at 1.2 s (fast mode) ≥ 216 m; 250 m/s at 0.3 s ≥ 254 m.
  - Stale-anchor rule (512 only): a stale rebase anchor is still committed when the player is
    ≥ 150 m inside its terrain's collidable area; otherwise discarded and re-requested with the
    terrain at p + v·(measured anchor latency + 1 s) (`core.rebaseHint`), so a slow server cannot
    loop. `rebase.STALE_DIST` 450 / 192 m (= the table's commit distance).
  - Assets: `beamngEarth_layer_512.png`, `beamngEarth_flat_512.png` (`tools/make_level_assets.py`).
- **Tests**: `test_heightmap` over (32, 1024²) and (64, 512²) (128 m moves bit-identical, even SW
  lattice index); `test_server` `MeshServer64Block512Test` (+ the prefetch against modsim's 512 rule,
  both modes); `test_protocol_agreement` (both tables, `blockSize`, STALE_DIST per size, the
  512 session's messages); modsim ports + `modsim_trace.lua` 'M' lines (the 512 table, fast mode,
  re-check, stale rule, configure back to 1024); luajit `test_follow`, `test_lattice`, `test_rebase`,
  `test_core_stubbed` (hello.blockSize 512: a 512² anchor accepted, the 512 flat map at level start,
  a 1024² terrain refused, back to 1024 without blockSize).
- **Integration**: `MeshDrive64Block512Test` (30 m/s, 16.5 km, one rebase): 100 commits, every
  overlap bit-identical (196 608-229 376 shared samples), no hole, the tile under the player L-1 at
  every pose, the player ≥ 296 m inside the block, commit 18.4 ms / rebase 44 ms in the cost model.
  `FollowDrive512Test` (in-process server, real time, no far field): 134 m/s at 20°: no late
  terrain, ≥ 243 m inside; 200 m/s with +1.2 s heightmaps (fast mode): ≥ 167 m inside; 250 m/s
  with a 3 s / 5 s anchor latency: one rebase each (5 s: the stale answer discarded once, the
  re-request committed).
- **Still to do in-game** (user not playing; `tools/m4_probes.py`, Lua in `probes/v14/m4_probes.lua`,
  no mod update needed, the 512² heightmap is a crop of the live one): `check`, then **P2**
  (gate: steady 512² import ≤ 30 ms with the layer map; extent [c−512, c+510]; heights equal;
  restore), **P11** (does physics follow a re-import without reloadCollision), **P7 (a)**
  (`p7a-install`, a drive with ≥ 3 rebases, `p7a-read`: the rebase-frame breakdown and remainder),
  **P7 (b)** last (a hidden second block, swap ≤ 5 ms). Then D134 with `--block 512` (commit frames
  ≤ 40 ms, rebase frame per §3.5) before flipping `BLOCK_DEFAULT`.

## v1.4 M1h: the line-of-sight horizon (docs/V1_4_SPEC.md §3.6 "Radius"; built offline 2026-10-10, behind a flag)
- **Server**: `earthd.py --horizon` (mesh mode only; **off by default** until P12 extended passes).
  The eye is the pose's camera z (else player z + 1.5 m) as a height above sea level in the pose's
  anchor frame (`frame.enu_to_geodetic`; the frame's h0 is sea level), quantised to max(10 m, 5 %),
  kept across rebases. Per anchor, one `horizon` worker builds `farfield.HorizonGrid`: the highest
  DEM z6 pixel under every L6 cell (16 km; block maxima of 8 × 8 px, `dem.Dem.block_max`, cached
  across anchors) and its max pyramid up to L10, over ±(min(1500 km, d(h_eye) + d(8900 m)) + 21 km),
  rebuilt bigger for a climbing eye; z5 or coarser where z6 would need > 64 DEM tiles. `select(...,
  max_level=10, disc=True, horizon=Horizon(eye, grid))`: roots at L10 (262 km), a disc of RADIUS =
  clamp(d(h_eye) + d(zmax_region), 80 km, 1500 km), d(h) = sqrt(2R'h), R' = 6371 km / (1 − 0.13),
  zmax_region = the highest L6 cell above the eye's horizon; beyond 80 km a node whose nearest point
  is past d(h_eye) + d(zmax_node) is dropped with its subtree. Within 80 km the selection is today's.
  Textures 256 px at L8/L9, 128 px at L10 (imagery picks the low zoom). Until the first grid the far
  field is the 80 km disc. `hello.horizon: true`; every status carries `radius` (re-sent when it moves
  by > 2 %). `swap_groups` unions up to L10.
- **Mod**: `horizon.lua`; `core.setHorizon(hello.horizon == true)` and `core.setFarRadius(status.radius)`:
  `LevelInfo.visibleDistance` = max(100 km, 1.1 × radius) with 10 % hysteresis, `postApply()` after every
  change (the engine ignores the field without it); `ScatterSky.atmoPlanetRadius` = 6371 (level: 6360)
  while horizon mode is on in an active world; both back to the level's values at level end (sky), on
  unload and with a server without horizon. Distance haze: TODO in `horizon.lua` with the
  `core_environment` knobs (setFogDensity, setFogAtmosphereHeight) and the ScatterSky Mie fields.
- **Offline numbers** (real DEM, 64 cells): Whitney summit 503 km radius, 83 tiles beyond 80 km (379
  in all, 300 today), San Gorgonio (306 km) kept, farthest tile 414 km; 10 km above Whitney 631 km, 97
  beyond; Central Valley floor 289 km, 49 beyond; open ocean 80 km, none beyond (296: the disc has no
  corners). Selection 1-2.5 ms per pose.
- **Tests**: `tests/test_horizon.py` (distances, extent, the pyramid and its lookups, the grid never
  below a DEM pixel, the sampled-DEM fallback and the block cache, the zoom fallback, the disc tiled
  exactly once with and without the cull, San Gorgonio kept from Whitney and dropped without the
  mountain, sea level past the horizon dropped at 2 m-10 km eyes, extra-tile counts, the near field
  and demand unchanged along a 134 m/s drive, L10 swap groups, the server flag / zooms / textures /
  eye); `test_server.MeshServer64HorizonTest` (a session: 80 km until the grid, ~170 km from a 3 m eye
  on the synthetic 1.5 km plateau, the climb to 10 km: a bigger grid, 545 km, L10 tiles at 128 px; a
  rebase's own grid); `test_protocol_agreement` (hello.horizon, status.radius, the horizon session,
  planet radius and margin on both sides); luajit `test_horizon`, `test_core_stubbed`.
- **Still to do in-game** (user not playing): P12 extended with `--horizon` at Whitney summit and at
  10 km: fps / p50 / p95 and perfmon with the distant tiles shown, objects, the L8-L10 import costs;
  screenshots of the horizon (the silhouette meets the sky's horizon band, no sky gap); read back
  atmoPlanetRadius and visibleDistance; no depth / shadow artefacts at 500-650 km; tune the haze.
## De-shadowing of the USGS imagery (`--deshadow`; built offline 2026-10-10, behind a flag, default off)
- Integration of docs/research/2026-10-10-deshadow-prototype.md, method A (method B dropped):
  `server/deshadow.py`, hooked into `imagery.Imagery(..., deshadow=obj)` (`_mosaic` swaps each USGS
  z12–z16 source tile for its relit copy; mosaic alpha 254 marks relit texels) and into
  `earthd.Server.new_anchor` (`prepare`: queue the z12 regions within 16 km, nearest first).
  `--deshadow off` builds no object: textures are byte-identical to the pre-hook code (sha256 of a
  multi-source render set pinned in `test_deshadow.FlagOffTest`).
- Per z12 region (~8 km), one background thread: z14 imagery + DEM z13 on the same pixel grid, sun
  fit (r² + 0.5 MCC, el ≥ 25°), gates r² ≥ 0.25 and class separation ≥ 0.9, lit / lam / prior grids
  (uint8 `.npz`), then classifier, halo-guard statistics, haze and sky/sun ratio from a few z16 tiles
  at the shadow edges and interiors (z14 alone gave the wrong haze and a too-tight guard: blotches and
  a dark rim in the relit shadow). Cached as `<cache>/deshadow/usgs/12/x/y.json`, shared by both modes.
- Per tile: grids sampled at the tile's pixels, shadow mask snapped onto the photo (Bayes + guided
  filter + linear mixing on the terminator + halo guard), gain capped at LAM_CAP 1.6 (slope part) and
  GMAX 10 (total, hue kept), chroma smoothing where boosted; gain-1 pixels keep their exact bytes.
  Relit tiles: memory LRU + `<cache>/imagery/usgs-ds1-<mode>/` (PNG, `.same` for unchanged tiles).
  `full` gives relit texels half the tone curve's shadow lift (`tone_lut(lift=0.5)`).
- Cost (16 cores shared with other builds, load ~6–8): region 2.5 s on the ravine with its tiles on
  disk (fit 1.3 s); per 256 px tile 20 ms at a shadow edge, 4 ms elsewhere with a cast shadow
  nearby, 7 ms lambert-only, ~1 ms unchanged (decided from the grids, before the neighbours are
  fetched), plus the PNG write.
- Real data (offline, tiles from the local cache): ravine 36.506,-118.402 fitted az 95 el 29
  (r² 0.28, just over the gate; the prototype's 3 km window gave 93 / 33.5 at r² 0.63), cast on,
  shadow lifted from sRGB ~(22,26,43) to ~(64,69,74) against ~(91,96,91) sunlit: cooler and flatter
  than the sunlit side (the prototype's known limits: no sky-view / bounce light, LAM_CAP on the
  steep shadowed wall). Central Valley flat 35.40,-119.20: gated off (r² 0.00). Kern Plateau
  36.20,-118.30: gated off (r² 0.08 over the whole region).
- Limits: a texture built before its region was ready keeps raw imagery until the tile is rebuilt
  (no live texture swap message yet); regions are fitted separately, so two regions with different
  suns can show a step in slope shading at their border; z ≤ 11 source tiles stay raw.
- In-game check still to run: the ravine at 08:00 and 16:00 game time, `--deshadow full` against off.
## Real weather, level parity, gravity (quick wins 1, 2, 4; built offline 2026-10-10, feature/weather)
docs/research/2026-10-10-real-weather.md, 2026-10-10-game-feature-quick-wins.md §5.1-5.4, probe facts
in 2026-10-10-weather-probe-results.md.
- **Level objects** (`tools/make_level_assets.py` → `main/MissionGroup/Weather/items.level.json`, a new
  `Weather` group): the CloudLayer `bngEarthClouds` (the fields sky.lua used to create at runtime; sky.lua
  keeps `createObject` as a fallback and then resets core_environment's per-class object cache), two
  camera-following Precipitation objects `bngEarthRain` (`rain_drop`, BWM_Weather's verified 0.39 setup)
  and `bngEarthSnow` (`Snow_menu`), both `numDrops 0` and without a `numOfDrops` field (so BWM_Weather
  keeps its own rain object), a ForestWindEmitter and a 2D reverb SFXEmitter (`snapshot:>Reverb_WC_Open`).
  LevelInfo gets `temperatureCurveC [0,15,1,15]` (ISA until real weather: core_environment now drives
  the engine's temperature and the menu stops showing −273 °C).
- **Server** `server/weather.py` + three hooks in earthd (`Server(weather=...)`, `Connection.weather`
  in `on_pose`, re-send on `hello`; `--weather on|off`, default on; the credit "Weather data by
  Open-Meteo.com" in `hello.attribution`). One keyless GET per fetch (generic user agent), at the 0.1°
  cell of the player, when there is none yet, every 600 s or after 25 km; cached per (cell, 15-min
  slot). `derive()` keeps the physics (rates per hour from the 15-min sums, fog-code visibility,
  cloud base, sea-level temperature, winds aloft, the real UTC offset); the `weather` message is
  additive (no protocol bump) and in the agreement table. Fixtures: Baltimore and Whitney, recorded
  2026-10-10.
- **Mod** `beamngEarth/weather.lua`, its own extension `beamngEarth_weather`
  (`scripts/beamng_earth_weather/modScript.lua`; core.lua only forwards the message; the BeamNG.Earth
  window shows a one-line summary). In the BeamNG.Earth level only. Every value approaches its target
  exponentially (95 % in 60 s), written at 2 Hz (ground wind 1 Hz) and only when it changed, through
  core_environment (the pause menu shows the real values): coverage from max(low, mid) through
  `COVERAGE_CURVE` (probe rows to 19 %, guesses above, retune there), cirrus from high cloud (density
  0.3-0.6, the CloudLayer default 0 hides cirrus), drift from the 700 hPa wind, fog density
  3.912 / V × exp(groundZ / H) (the engine's fog thins with absolute z, `sceneFog.h.hlsl`; capped at the
  menu's 0.05), ground wind from the 10 m wind with an OU gust term up to the gust speed, rain/snow
  as `setPercentage(sqrt(rate / full))` of a 4000/3000-drop pool (none above the cloud base), and a
  flat `temperatureCurveC` at the sea-level temperature (LevelInfo setter found by read-back, editor
  dirty for 3 frames so core_environment re-reads it). The sun dims under the deck: ScatterSky
  `brightness` = the level's value × (1 − 0.7·deck^1.5) (the clouds cast no shadows, so under
  overcast the car kept a sharp sun shadow; in-game at noon 0.35 made it faint while the auto
  exposure kept the scene's brightness); restored in manual mode, on a manual cloud change and for
  missions, unless someone else changed it since. The cloud base above the ground goes to
  sky.lua (it keeps the ground-following altitude and the rebase weather-offset shift); the real UTC
  offset too (`sky.todFields(..., real)` within 200 km of the reported point; Singapore +8, Delhi
  +5.5 instead of the meridian guess).
- **Modes**: `setMode('auto')` (default) = real unless BWM_Weather has automatic weather on or a preset
  other than clear (then manual, one toast); `'real'` / `'manual'` = the user's choice. Writes wait 20 s
  after a level start (BWM's start transition, env's `setState(init_env)`). Any other change
  (onEnvironmentChanged not from our own calls) afterwards overrides its group (clouds, cirrus, cloud
  wind, fog, wind, gravity, cloud altitude, precipitation) and pauses all writes 3 s (env's 1.15 s
  menu lerp would be cancelled by our setters); `setMode('real')` clears overrides. Missions pause it;
  their setup changes are not overrides.
- **Gravity** by latitude and height (WGS84 Somigliana minus 3.086e-6·h; 9.7803 equator, 9.8322 pole)
  through `core_environment.setGravity` when it changes by 1e-4 (~30 m of climb); `GRAVITY_ENABLED`
  (on); a menu gravity change is respected; off restores 9.81.
  - Real gravity (after the merge): earthd's `--gravity on` (default) computes |g| with
    `server/gravity.py` (normal + EGM2008 anomaly at the pose's lat/lon/height above sea level) on
    its own thread, after the first pose and whenever the player moved > 2 km or > 200 m in height,
    and sends the additive `gravity` message (`v`, `g`, `lat`, `lon`, `h`, `source`). Without the
    EGM2008 cache it sends normal gravity (`source: "normal"`) and downloads the 74 MB in a daemon
    thread; the next pose after it is ready recomputes. The mod uses `g − 3.086e-6 · (z − h)` instead
    of the latitude formula while it has a value (cleared on `hello`), and moves the engine's value
    by at most `GRAVITY_STEP` (1e-3 m/s²) per 1 s check once its own is set. Settings page: "Real
    gravity" (on) = `GRAVITY_ENABLED`.
- Tests: `server/tests/test_weather.py` (fixtures, codes, cache, refetch rule, server, the Lua mapping
  under luajit, the level objects = generator output), `mod/tests/test_weather.lua` (pure conversions
  + a stubbed core_environment run: grace, convergence, writes only on change, overrides, BWM, missions,
  gravity, rain objects, temperature curve, other levels untouched), `test_sky.lua` (real offset),
  `test_core_stubbed.lua` (forwarding). Still to check in-game: the temperature-curve setter form,
  `setPercentage` drops visible, coverage calibration above 0.95 (overcast), fog k = 1/H, wind push.
## Dev tools and the pause-menu settings page (docs/DEVTOOLS.md; built offline 2026-10-10)
- `dev.lua` (extension `beamngEarth_dev`): in-world overlays (per-tile LOD tint, tile outlines by
  state, loading heat with the next imports numbered, physics block squares, rings, under-car
  probe) and imgui panels (stats, 60 s graphs, mini-map with click-to-inspect, controls, event
  log). Off by default; off = no per-frame work. Tiles come from `tileindex.lua`, kept current by
  `farfield.setWatcher` only while the tools are on.
- `settings.lua` (extension `beamngEarth_settings`) + `ui/ui-vue/mods/beamngEarth/` (index.js,
  EarthSettings.vue): a "BeamNG.Earth" button in the pause menu's Mods tab, values in
  `/settings/beamngEarth.json`, defaults = today's behaviour. With the page, the imgui window is a
  compact HUD hidden at level start (setting "Status HUD").
- Tests: `mod/tests/test_dev.lua`, `mod/tests/test_settings.lua`. The Vue files are checked
  in-game only.
