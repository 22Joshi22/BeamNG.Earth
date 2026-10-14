# BeamNG.Earth roadmap (2026-10-10)

Goals (user, 2026-10-10): look as good as the official maps (cities, trees, paved roads, varied
environments), a world-map GUI, GPS over continental distances (Cannonball Run, ~4500 km), AI
traffic with working signs and traffic lights, and performance everywhere as content grows. Hard
requirements: players routinely drive 150-300 mph (67-134 m/s), and some fly planes (mods).

Research behind this plan (all 2026-10-10, docs/research/):
origin-distance.md, official-map-look.md, world-data-sources.md, perf-and-architecture.md,
navgraph-traffic-signals.md, long-distance-gps.md, world-map-ui.md.

## Principle: improvements must generalise (user, 2026-10-10)
"An improvement in one part of the world should improve things everywhere." Places like Fells Point,
Monument Valley, the Kern River ravine or a famous trail are test cases, never special cases:
- No location-specific code or hard-coded coordinates in the generator. Every rule is driven by data
  available everywhere (elevation, land cover, climate zone, OSM/Overture tags, imagery statistics),
  with sensible fallbacks where data is missing.
- Per-region choices are allowed only for data sources (e.g. the best imagery per country), never for
  how the world is built from them.
- Every tuning change is checked on a fixed, diverse benchmark set of places (all continents and
  biomes, data-rich and data-poor, flat and steep, cities and wilderness; the list so far is
  docs/PLACES.md; the full set with its cameras is docs/benchmarks.json, run by tools/benchmark.py,
  docs/DEVTOOLS.md "Visual benchmark"), with before/after screenshots and numbers, plus a held-out set that is not used for tuning. A change that improves
  the test place but makes the benchmark worse elsewhere is rejected or reworked.
- Fixes like de-shadowing and colour matching are global methods with automatic gates (they switch
  themselves off where they do not apply), validated on places with and without the problem.

## Principle: deterministic generation (user, 2026-10-10)
The same place must come out the same every time it is generated with the same version: a rock,
tree, building or road marking is always in the same spot, whichever way the player arrived, in
whatever order tiles loaded, after any number of rebases, on any machine.
- Every procedural choice (placement, species, rotation, size, colour variation, roof shape, filler
  buildings in data-poor areas) is a pure function of (world position in a global frame, the input
  data, a fixed per-feature salt, the generator version) - e.g. a hash of the global cell id or of
  the feature's OSM/Overture id. Never of the anchor, tile key, LOD level, load order, thread timing,
  time of day or an unseeded RNG.
- Positions are computed in a global frame (lat/lon/height or ECEF, doubles) and only then
  transformed into the current anchor frame, so a rebase or teleport cannot move anything.
- LOD only changes how detailed something is drawn, never whether or where it exists: the coarse
  version of a forest is the same trees, thinned by a stable rank (each object keeps one fixed
  random rank; a level shows the objects below its threshold).
- Inputs are versioned: generator version + data snapshot ids (imagery capture, DEM source, Overture
  release, OSM date) are part of cache keys and the credits; a data update may change a place, a
  restart or a different route never does. For identical results across players, pin data snapshots.
- Things that are real-time by nature (weather, time of day, traffic) are exempt, but their random
  details (cloud pattern seed, gust phase) are also derived from position + time slot, not from RNG.
- Tests: generate the same area from different anchors, paths and load orders and compare a hash of
  every placed object; a golden-hash test per release; float tolerance only where unavoidable.

## Done today (v1.3.1)
- Server deleted a previous anchor's tile files while the mod still showed them (rebase reload
  outlasting the fixed 30 s prune delay): the engine logged `Could not open '...far_<a>_....cdae'`
  for each touched tile. Mod now reports `pose.held` (previous anchors whose tile objects exist);
  the server prunes an anchor only once no mod holds it. Verified in-game: 0 errors over 3 rebases.
- Motion blur strength held at 0 for 4 frames across rebases/teleports (velocity pass has no
  teleport check: a km-scale camera jump smeared the whole frame, the "flash").
- Rebase distance 10 km -> 15 km (max 16 km: the float32 step is 0.98 mm up to 16384 m, so no
  precision cost; a third fewer rebases).

## Measured today (4K, user's mods active, open desert, car parked)
| | frame |
|---|---|
| everything | ~15.0 ms (66 fps) |
| all ~900 far tiles hidden | 13.8 ms (72 fps) -> the tiles cost ~1.2 ms |
| `lastSplitCastersEnabled` off (user has it on; every preset has it off) | -1.6 ms |
| far-tile materials `castShadows=false` | no change |
| our Lua hooks | 0.17 ms |
| physics (pause A/B) | ~0.4 ms |
CPU ~10 ms/frame is the bound (GPU ~8.3 ms). Most of the frame is the engine + 4K + the user's
other mods (weather, TAA, vehicle FX, particles), not the streamed world.

200 mph simulated drive (20 km at 90 m/s, docs/research/sim_v131_200mph): **streaming cannot keep
up**. The mod imports ~10 tiles/s; the queue grew to ~2000, after the rebase only 321 new tiles had
loaded, and near the car only coarse, untextured tiles were shown, whose heights differ from the
physics ground (car visibly floating). This is the top priority.

## v1.4 "Speed" (foundation, first)
1. **Global tiles for L>=1** on a cube-sphere quadtree with tile-local frames: a rebase only moves
   them (the mod already transforms tiles exactly), no rebuild; on-disk cache across sessions.
   L-2..L0 stay anchor-aligned (physics lattice, ~230 tiles). Rebase reload ~95 s -> ~20 s.
2. **Speed-adaptive selection**: look-ahead along the velocity (the server already gets pose at
   2 Hz with velocity), fine-ring radii shrink with speed, priority = time-to-reach, not distance.
3. **Import throughput**: fewer, bigger L>=1 tiles (64x64 cells, 512 px: ~400 objects instead of
   ~800), governor budget scales with backlog when the frame has headroom.
4. **Near ground always matches physics**: never show a coarse tile under the car; if the fine
   tiles are late, keep a physics-exact fallback (e.g. one coarse mesh built from the same
   heightmap as the terrain block, sent with every terrain commit).
5. **Cheaper terrain-follow**: 512^2 block moved every 128 m (~18 ms est.) instead of 1024^2 every
   256 m (60-70 ms frames today); at 134 m/s that is a commit every ~1 s, so it must be small.
6. **Real view distance (M1h in V1_4_SPEC)**: far field out to the true line-of-sight horizon from
   the eye's height above sea level and the distant terrain's height (Mount Whitney summit: ~310 km
   instead of today's 80 km), very coarse tiles (up to 262 km, ~4 km vertex spacing) beyond ~80 km,
   per-tile horizon cull, distance haze for realism.
7. **Altitude-aware LOD for planes**: LOD driven by camera height as well as position; physics
   block off when far above ground; far field to the horizon (~350 km at 10 km altitude).
Target: 300 mph sustained with the near ground always detailed, and a rebase that is invisible.

## Shared map data (the input of v1.5-v1.9 content)
Status (2026-10-11, built offline on branch feature/mapdata; docs/ARCHITECTURE.md "Map data"):
`server/mapdata.py` gives per global Web-Mercator z14 cell the Overture roads (class, subclass,
surface / flags / width / level / speed rules, connectors), buildings (heights, floors, roof and
facade fields), water and land use (clipped to the cell), infrastructure, ESA WorldCover 10 m and
Meta/WRI CHMv2 1.19 m canopy height, in the cell's local metres with GERS ids, cached per layer and
release, deterministic (content digest), with provenance and licences. Cold 2.9-3.6 s per cell on
the three benchmark cells, warm 0.02-0.06 s. Findings for the generators: building heights are
sparse even in Baltimore (1.9 % height, 5.1 % height or floors in the Fells Point cell: a height
model is needed); no lane counts (width on 2 % of segments); the CHM counts solar panels as 1-8 m
canopy (Kayenta), so trees must be masked by land use / infrastructure. Reviewed adversarially
the same day (branch review-mapdata, 11 bugs fixed, each with a test that failed first): invalid
keys from `cells_for_bbox` at the poles, cell edges off by one for ~1 in 7 points on a north edge,
clipped polygons owned by one cell only ("build the owned features" built a park a quarter whole),
a damaged cache file failing its cell forever, slivers kept or dropped by the BLAS summation order,
a per-file memory cache growing to ~0.75 GiB, a deleted release not raising `ReleaseGone`,
truncated Overture licence texts, the `predownload` kind; pipeline VERSION 2. A dense Midtown
Manhattan cell: 4.8 s cold, peak RSS 381 MB. Next:
- wire it into earthd (one MapData on the server's Fetcher; prefetch the cells ahead along the
  velocity with kind `prefetch`; generators read CellData) and the credits overlay (`CellData.credits()`,
  plus the CC BY datasets the cell's provenance names);
- store long text columns (the `tags` JSON: 16 MB of a city cell's ~40 MB in memory) ragged;
- key v1.4 M6 global tiles' content on `mapcells.cells_for_bbox()`;
- building parts (`buildings/building_part`), places, OSM tags (lanes, width, surface detail) joined
  by `src_record` from a cached regional extract, NLCD in the US; once earthd uses MapData, report
  its disk hits and writes to `cache.Cache` (`<cache>/mapdata/` is under the cap's LRU since the
  merge, 2026-10-11, but only by the files' mtimes: nothing there is protected as in use);
- track Overture releases: the pinned one (`overture.RELEASE`) disappears from the buckets after
  some months (`mapdata.py --latest`).

## v1.5 "Ground truth"
- Per-cell surface physics: the invisible TerrainBlock gets material layers from ESA WorldCover
  (+ OSM roads): ASPHALT / DIRT / GRASS / SAND / GRAVEL / ROCK / MUD / SNOW (no more dust on roads).
  Status (2026-10-11, on by default, `earthd --surface off` to disable): WorldCover classes
  (from the map-data cache, prefetched 3 km around the player, never waited for) and a 35° rock rule
  give 8 layers (asphalt for built-up, grass, dusty / loose dirt, rock, snow, mud, dirt) imported with
  each 512² block as 8 greyscale masks; the materials are runtime clones of the level's dirt with
  other ground models (server/surface.py, terrain.lua). OSM / Overture overrides (roads, parking,
  sand, lawns vs streets) come with the roads work. Tyre contact checked in-game (Fells Point
  ASPHALT, Serengeti GRASS, Everest Base Camp SNOW, Monument Valley DIRT_DUSTY); stock grass
  GroundCover rides on the grass / dusty layers (groundcover.lua).
- Tile material v1.5 (PBR): normal maps baked from the DEM, roughness from land cover (water glint).
- **Real ground materials up close** (user, 2026-10-10: satellite photos are low-res near the car):
  - Material splatting on the near tiles (0-250 m full, blend out to ~1 km, photo only beyond, the
    official maps' detail/macro split): per tile the server writes splat weights for up to 4 material
    layers per v1.5 material (asphalt, concrete, dirt, gravel, sand, grass, rock, forest floor, snow,
    mud), chosen from land cover (ESA WorldCover 10 m, NLCD in the US), OSM/Overture (roads, paths,
    parking, buildings' yards) and a per-pixel classification of the photo itself (colour/texture;
    Sentinel-2 bands give vegetation index where available). Each layer = a stock BeamNG PBR texture
    set (albedo, normal, roughness, AO) at real-world scale, tinted toward the photo's local colour
    (the photo becomes the macro colour), so near ground is sharp and still matches the photo from
    afar. The physics ground model per cell comes from the same classification (one source of truth).
  - Better source imagery where it exists: NAIP 0.6 m (US), state/city orthophotos (e.g. Maryland
    MD iMAP 6-inch / Baltimore city imagery for Fells Point), all public; a per-region source list
    with fallbacks, colour-matched to the neighbours to avoid seams.
  - Upscaling for the mid range (250 m - 2 km) where nothing sharper exists: super-resolution
    (Real-ESRGAN-class model) run on the server and cached per tile; optional, off by default until
    its GPU/CPU cost next to the game is measured; never used where it would invent detail that
    conflicts with the material layers.
  - Today's procedural grain detail map is a placeholder for this.
- **Leaning buildings in the imagery** (user, 2026-10-10: building sides are visible): aerial and
  satellite photos are orthorectified to the bare-earth ground only, so anything tall leans away
  from the camera (relief displacement): roofs are shifted off their footprints and facades are
  painted onto streets and yards. Plan (with v1.9 buildings, partly earlier for the ground):
  - Estimate the lean per photo region (direction and metres of shift per metre of height) by
    matching roof outlines in the photo against footprints with known heights (best shift per
    region), or from the source's capture geometry when published.
  - Mask building pixels on the ground texture: the footprint plus its leaned roof/facade area, and
    fill them with the surrounding ground (street, yard, sidewalk) or the splatted materials, so 3D
    buildings don't stand next to a painted copy of themselves.
  - Reuse the leaned roof pixels, shifted back, as the roof colour of the 3D building.
  - Same for tall trees (canopies shifted), and steep cliffs where the photo is stretched.
  - Prefer "true ortho" city imagery and high-altitude satellite sources (less lean) where available.
- **Baked-in shadows in the imagery** (user, 2026-10-10): satellite/aerial photos carry the sun's
  shading from capture time (dark north slopes, shadowed valleys, building and tree shadows), which
  doubles up with the game's real-time lighting and points the wrong way at other times of day.
  - Terrain-scale shading (hillsides): "de-light" each tile on the server. Estimate the capture sun
    direction per imagery source/region by fitting the image luminance against DEM hillshades for
    candidate sun azimuth/elevation (pick the best correlation; cache per source tile), then divide
    the shading out: albedo ~= image / (ambient + k * hillshade_capture), clamped, with sky-ambient
    term. The game's lighting + DEM normal maps then supply the correct, time-of-day shading.
  - Small shadows (buildings, trees): once those exist as 3D objects (v1.9) their photo shadows
    remain on the ground beside them; detect them from footprints/canopy + the fitted sun direction
    and lift them (shadow-mask brightening), and near the car rely more on land-cover ground
    materials than on the photo.
  - Near ground (0-250 m) blends toward procedural land-cover materials anyway (like the official
    maps' detail band), which hides most baked shadow close up.
  - Prefer imagery captured near noon / in summer where sources offer a choice.
  - Status: de-shadowing built offline behind `--deshadow lambert|full` (default off; docs/ARCHITECTURE.md).
    A tile shown before its region was ready is re-rendered and swapped live (`far_retex`, 2026-10-11).
- **Colour seams in the imagery** (user, 2026-10-10: Monument Valley sits on a colour border that
  stretches for miles): USGS is a mosaic of NAIP flights per state and year, so flight boundaries
  are colour steps. Status: the "seam feather" (docs/research/2026-10-10-imagery-colormatch.md) is
  built offline behind `--colormatch s2` (default off; docs/ARCHITECTURE.md): detected survey seams
  only, each side moved half way against Sentinel-2 as the reference, gain exactly 1 elsewhere.
  Before default-on: a geometric prior for moderate seams in farmland and cities (state boundaries
  from a generic boundary dataset, the NAIP quarter-quad graticule), the far rings (z10-z11),
  faster detection, and an in-game check of known state-line seams (UT/AZ, NM/CO, KS/NE) and snowy
  mountains. In-game 2026-10-11 at the UT/AZ line (36.998218, -110.102293): the seam is gone in the
  low and the high view (steps of 35-95 % per channel found, 4-6 seams per region), mesas and washes
  unchanged; regions took 25-140 s each (first ~9 done in 10 min) after two fixes found there (a
  dropped region was never retried while the player stood still; the region thread now waits out
  dropped background fetches). Seen meanwhile: a restarted server deleted the running game's
  textures (orange "no texture" until a teleport; satmat now re-points; the robustness branch keeps
  the files). The far-ring switch from USGS to EOX needs a regional palette mapping (separate item).
  Live swap (2026-10-11, feature/cm-retex, built offline): a tile built while its region was not ready
  used to keep the raw texture until it was sent again (a teleport); now the server re-renders the
  shown tiles of a region that turns ready (nearest first, `--retex-rate` 5/s, one render at a time)
  under a new file name and sends `far_retex`; the mod switches the material's colour map, at most
  2 a frame through the governor. In-game 2026-10-11 (UT/CO line 38.50,-109.075, fresh regions):
  474 live swaps in 20 min, 0 failed, measured governor `retex` cost 0.63 ms, no orange textures.
  More in-game checks the same night (same camera, same time before/after): KS/NE farmland 39.995,
  -98.25 - the state-line step is gone, fields keep their contrast (no false seams at field edges);
  CO/NM 36.995,-105.75 (high desert by the Rio Grande gorge) - seam gone; Mount Rainier 46.80,
  -121.73 - "no seam within reach" in every region, snow/glaciers/meadows untouched. Images in the
  owner's ~/Pictures (beamng_earth_seam_*). Remaining before default-on: none blocking; the
  state-boundary prior, the far rings and faster detection stay follow-ups.
  Review (2026-10-11): coarse L5/L6 tiles span up to 9/16 regions each and are now re-rendered for
  every one (the first cut stopped at 8 revisions); a failed region over an unusable cache file no
  longer reads as ready (that re-rendered shown tiles in a loop); the mod never switches a material
  back to an older revision.
- Sky: CloudLayer + wind, haze, per-biome cubemap, black night colours.
- GroundCover grass: done with v1.5 surfaces (2026-10-11, groundcover.lua): green Hirochi Raceway grass
  with some dry West Coast tufts on the grass layer, West Coast scrub with sparse green grass between on
  dusty ground (no hard grass / shrub line). No flicker at block moves: CPU placement plus a same-tick
  grass rebuild (docs/ARCHITECTURE.md "Ground surfaces"), at one ~25 ms frame per block move.
  Settings page: "Grass and shrubs" on / off and "Grass density" low / normal / high (2026-10-11).
  Next: per-climate / per-season picks (dry savanna, green temperate, alpine).

## Water (with v1.5/v1.6; user 2026-10-10: lakes and rivers are blue dirt today; keep depths
## roughly real, no underwater detail needed)
- **Sea: done 2026-10-13** (`--sea on`, default off; docs/ARCHITECTURE.md "Sea water"): the ground
  carved under Overture's ocean polygons; WaterBlock tiles that follow the Earth's curve near the player
  (a flat WaterPlane flooded distant shores from altitude). The user tried the first version: "the water
  is working". Lakes and rivers followed the same night (WaterBlocks at their own levels; rivers follow
  the DEM's fall). Next: canals and narrow streams (River objects), bathymetry (NOAA CUDEM, GEBCO),
  fewer tiles (merge neighbours), water colours per kind, default on. Glossy far tiles: little visible
  gain at Sandy Point (the tiles reach the horizon there), parked.
- Where: OSM/Overture water polygons + river lines (with width), ESA WorldCover water class as
  fallback. Surface height: the DEM at the shoreline (DEMs usually show the water surface, flat).
- Depth (bathymetry): GEBCO global grid (~450 m, public) for oceans and big lakes; NOAA coastal
  DEMs (CUDEM, ~3 m, US coasts) and NOAA Great Lakes bathymetry; small lakes and rivers have no
  data, so estimate: depth from width (river hydraulic geometry) and a smooth bowl for lakes,
  clamped to sensible values. Low detail is fine (no underwater content in the game).
- Physics: carve the invisible TerrainBlock below the water surface (so cars sink/float properly)
  and add BeamNG water objects for buoyancy/drowning: local WaterBlocks per lake/river section in
  the anchor frame (a single flat WaterPlane is wrong at distance: Earth curvature puts it 8 m off
  at 10 km, 196 m at 50 km); big lakes/sea split into blocks that follow the curvature.
- Visuals: near water = the engine's water rendering (reflections, waves); far water = low
  roughness / water colour in the tile material (sun glint), shoreline blended in the imagery.
- Interacts with bridges (deck over carved river) and roads (no grading into water).

## Official maps as biome references (user, 2026-10-10: "Monument Valley closely resembles the Utah map")
When building detailed environments (v1.5 ground materials, v1.9 vegetation/props), each biome gets
an official BeamNG map as its look reference and asset source, and a real place to compare against:
| biome | official map (reference + stock assets) | real comparison place |
|---|---|---|
| red-rock desert, canyons, mesas | Utah, USA | Monument Valley, Moab, Page/Lake Powell |
| Mojave desert, dry lake beds, rock piles | Johnson Valley | Johnson Valley / Barstow (the real one is in our world) |
| California coast, chaparral, oak hills | West Coast USA | Central Coast, LA foothills |
| US East Coast forest, towns, rivers | East Coast USA | Baltimore / Fells Point, Chesapeake |
| Mediterranean | Italy | Tuscany, Amalfi coast |
| tropical | Jungle Rock Island | Hawaii, Caribbean |
Method: measure each reference map's numbers (terrain material layers and scales, rock/shrub/tree
species and densities per km², groundcover settings, decal and colour palette, lighting/fog) with the
same tools as docs/research/2026-10-10-official-map-look.md; take matching screenshots (same sun,
similar viewpoint) in the official map and at the real place in our world; tune the generator until
they compare well, then check the result against street-level photos (v1.9 loop).

## Off-road and rock-crawling trails as references (user, 2026-10-10)
Popular trails are where players will test the terrain hardest, and they are well documented:
- Find them from trail databases and OSM (highway=track with tracktype / 4wd_only / smoothness,
  named trails), e.g. Moab (Hell's Revenge, Fins & Things, Golden Spike, Poison Spider Mesa), the
  Rubicon Trail (CA), Johnson Valley's Hammers (King of the Hammers), Black Bear Pass (CO), Mount
  Saint Helens / Gifford Pinchot forest roads, Moab's Shafer Trail, Alpine Loop (CO).
- Use their public photos and videos (trail guides, clubs, YouTube) only as visual references to
  judge and tune the generator: slickrock texture and colour, ledge heights, boulder size and
  density, rut depth, surface types, vegetation along the trail. Nothing from them is shipped.
- Check that the elevation data resolves the features drivers care about (ledges, steps, washes):
  where 1-2 m DEM resolution smooths them away, add procedural rock/ledge detail on the physics
  terrain near trails (bounded by the DEM so the shape stays real).
- A "famous trails" list with start points goes into the saved places / level selector.
- Credits for every reference go into CREDITS.md.

## v1.6 "Roads"
- Status (2026-10-11, first cut, `earthd --roads on` by default): Overture road segments from the
  map-data cache, 256 m tiles within ~640 m of the player (server/roads.py), drawn by the mod as
  DecalRoads (roads.lua): paved -> West Coast USA's asphalt strip, gravel / dirt / tracks -> Johnson
  Valley's dirt tracks, width by class or the width rule. The same roads are painted into the
  surface masks (asphalt / loose gravel / dirt under the tyres). Bridges, tunnels and segments off
  ground level are left out. 809 roads around Fells Point cost +0.08 ms per frame. Settings page:
  "Roads" on / off. Since then: sidewalks, gutters and lines, cobblestone streets, the ground graded
  under roads, raised sidewalks with curbs as one mesh per road tile (West Coast USA's way and
  materials) and storm drain grates (ARCHITECTURE "Raised sidewalks"). Next: curb ramps / driveways,
  corner radii, painted curbs (red / yellow bands) by rule, the navgraph. Since then: roads as one mesh
  per tile (no DecalRoads: they re-clipped on every block move, 140 ms at speed), the road lift, bridge
  decks (split from partial-bridge segments, collidable), smoother grading, batched builds.
  Intersections done (curb returns, crosswalks, curb ramps; ARCHITECTURE "Intersections").
  Street widths measured between the building faces (ARCHITECTURE "Roads", `roadwidth.py`).
  Next: real deck heights from lidar (USGS 3DEP class 17), bridge parapets and piers, tunnels,
  painted curbs by rule.
- Overture transportation (GeoParquet via the STAC per-file index; 2.6-3.7 s per 30 km box cold).
- Near roads: meshes on the 2 m lattice, ground graded under them on the server so physics and
  visuals agree; markings; bridges as collidable decks. Far roads painted into the imagery.
- Bridges and overpasses (user report 2026-10-10: roads dip into rivers where a bridge should be).
  The elevation models are bare-earth: bridge decks are removed, so the ground follows the river
  bed. Needs: OSM/Overture `bridge=yes` + `layer` segments; deck height = a straight/smooth line
  between the two abutments (sampled where the road meets solid ground on each side), never the DEM
  under the span; the deck as its own collidable mesh (the TerrainBlock is a height field and
  cannot carry a surface over a river), with the river valley kept below it; same treatment for
  overpasses and interchanges (layer ordering). Embankments/abutments get graded so the deck meets
  the road without a step.
- **Road tunnels** (user, 2026-10-10), drivable rather than "ignore the road, keep the hill":
  - Data: OSM/Overture `tunnel=yes` (+ `layer`, `tunnel=building_passage`/`culvert` excluded) gives
    the path; the portals are where the road meets the DEM surface at each end; the road inside is a
    smooth grade between the two portals (never the DEM above it).
  - Physics: the TerrainBlock is a height field, so the hill stays solid above the tube; the portal
    mouths need holes in it. Torque terrain supports empty (hole) squares - probe how BeamNG 0.39
    exposes them (layer-map value / material, at the 2 m square size, survives `importMaps`) and
    their cost; the server then writes the hole squares into the block's layer map.
  - Visuals: the mesh tiles get the portal openings cut out (exact level, matching the holes);
    the tube is a collidable mesh per tunnel segment (profile by road class and lane count:
    walls, ceiling, lane markings, lights every ~25 m), split into chunks under the ProceduralMesh
    tri limit and streamed like the roads.
  - Long tunnels (Gotthard 17 km, Eisenhower, Zion-Mount Carmel) cross physics-block and rebase
    boundaries underground: the block-follow logic must keep the tube's collision loaded when the
    block under the car is solid hill, and the far field must not draw the tube.
  - Underwater tubes (e.g. Baltimore's Fort McHenry and Harbor tunnels near the Fells Point test
    city) also need the water surface above them (see Water).
  - Sound: tunnel reverb and echo (Sound design).

## Overhangs, arches and caves (user, 2026-10-10)
The ground is a height field (one height per 2 m square, both for physics and for our mesh tiles),
so nothing can sit above other ground: no natural arches, overhanging cliffs, undercut canyon walls,
rock shelters or caves. The elevation data cannot show them either: DEMs are 2.5D, so an arch is a
wall or a filled-in bump depending on the source.
- **3D rock meshes on top of the height field**, with collision, generated where data says there
  is an overhang:
  - arches and natural bridges: OSM `natural=arch` (point or way); the opening is cut into the
    terrain (hole squares, as for tunnel portals) and a rock arch mesh spans it;
  - overhangs and undercuts: where lidar point clouds exist (USGS 3DEP, IGN LiDAR HD, national
    lidar), points below the DEM surface at cliff faces mark the undercut; elsewhere a generic rule
    (steep cliff + rock type/land cover) adds modest overhang lips;
  - caves: `natural=cave_entrance` as a shallow mouth only (no cave systems).
- Sizes come from lidar where available, otherwise from tags (rarely present) and a generic
  size/shape model by rock type; never hand-placed per landmark (Principle: generalise), and
  deterministic (seeded by the feature id + version).
- Shares its mesh, material and collision pipeline with the v1.5 cliff faces and textures.
- Benchmarks only (not special cases): Arches NP (Delicate, Landscape Arch), Natural Bridges NM,
  Rainbow Bridge, sandstone undercuts in Monument Valley and Zion.

## v1.7 "Navigation"
- Navgraph from our roads via `map.reset(customNodes)` (5 known pitfalls, fixes in the research).
- GPS: OSRM (FOSSGIS default, demo fallback; NY->LA in ~1 s, no key), route held by the server,
  15 km window streamed into `core_groundMarkers`; Photon/Nominatim place search.
- World map: Vue mod page + Leaflet, tiles proxied/cached by earthd on 127.0.0.1 (CSP allows it;
  probe first), click to teleport / set destination.

## v1.8 "Alive"
- Parked cars: done 2026-10-14 through the stock parking system (user's choice: BeamNG's traffic system and
  its player settings). server/parking.py curbside spots on the road tiles, mod parking.lua hands them to
  gameplay_parking as a sites object and starts trafficParkedAmount cars; lot stalls too. 16 cars: ~1 ms.
- AI traffic on our navgraph: done 2026-10-14 behind `--traffic on` (server navgraph.py, mod navgraph.lua;
  ARCHITECTURE "Parked cars and traffic"): the stock gameplay_traffic drives our roads, clipped to the block,
  edge guard, teleports. 7 cars: ~1.6 ms. Working signals and stop signs with switchable heads, bridge decks,
  tunnels left out, road rules by country (left-hand traffic, turn on red, EU red+yellow) (same day).
  Next: default on once the user has driven it; a border map for the country (nearest airport now); parked-car
  density (16 stock cars over thousands of spots look sparse next to the photos' full curbs).
- Street life (2026-10-14): porches, front-yard fences, speed-limit signs (30-70 mph models only), company
  wall signs (the game's fictional firms), shop sign bands, parking-aisle stalls. Next: mailboxes (ECA prefab),
  street-name blades (no stock model), pole-mounted business signs.
- Traffic lights / stop signs from OSM via `core_trafficSignals` (scriptable at runtime), stock
  light and sign props; street lights.

## v1.9 "Cities & nature"
User goals (2026-10-10):
- **Test city: Fells Point, Baltimore MD** (the user lives there and knows it street by street):
  the first city to generate and the reference for judging buildings, streets, waterfront, piers.
- **Sparse-data places must not look empty** (small towns, forests, biomes, small islands): fill
  the blanks by prediction - building footprints where satellite/land cover shows built-up area but
  OSM has none, typical lot/house patterns per region, vegetation by biome and canopy data,
  plausible minor roads/driveways. Data-rich cities (New York) use the real data first.
- **Street-level tuning loop**: compare street-level photos of real places with in-game
  screenshots from the same position/heading and tune generation (heights, roof shapes, facade
  materials and colours, road widths, markings, tree density, street furniture) until they match.
  Openly licensed imagery: Mapillary (CC BY-SA, free API token) / KartaView; not Google Street View
  (its terms forbid this use without permission).
- Buildings: Overture footprints + heights, extruded with roofs, stock tileable materials, merged
  per 256 m chunk with LODs, collision within ~300 m.
- Trees: Meta/WRI 1 m canopy height (tree tops at real positions) + land cover for species, Forest
  items with stock imposter trees out to ~3 km.

## Robustness: no server, lost server, lost internet (user, 2026-10-10; before any public release)
Today: without a server the player spawns on the level's placeholder ground with no explanation; if
the server dies mid-drive the shown tiles stay but the physics block stops following, so the car can
drive off its edge and fall forever; a restarted server deletes the previous session's files at
start-up (live tiles go invisible until re-streamed); without internet, uncached areas get no
terrain at all (same fall risk). Plan:
1. **Clear status, never silent**: an on-screen banner with the state ("Starting world server...",
   "World server not running - start it with ... / it will start automatically", "Connection lost -
   reconnecting", "Offline - using cached data") and what the player can do.
2. **Never fall out of the world**: when the physics block can no longer follow (no server, no data),
   the mod keeps the last block and, as the car nears its edge, warns and then holds the car at the
   edge (an invisible soft wall / gentle stop), or pauses the simulation with the banner. The car is
   never left without ground.
3. **Server comes back = seamless**: a restarted server adopts the running game's session instead of
   deleting its files (the mod reports its session id and the files it holds in hello/pose, the
   server keeps them until released; the same mechanism as pose.held), then resyncs at the current
   origin without a level reload.
4. **Offline mode** (server side): detect failed fetches, serve everything from the disk cache, and
   for uncached areas fall back to a small always-present global coarse DEM (~1 km, downloaded once,
   tens of MB) plus a land-cover-coloured palette texture, so terrain always exists (coarse but
   drivable). Mark those tiles; when the internet returns, rebuild them with full data in the
   background (retex / rebuild swap, invisible). Each source fails over independently (USGS down ->
   NAIP/EOX; Open-Meteo down -> keep the last weather / manual).
5. **Start-up without a server**: the launcher / auto-start (quick win "easy install") starts the
   server with the game; until then the level shows the banner over a safe holding area.
6. **Pre-download** (candidate): cache a region or route for fully offline play.
7. **Tests**: fault injection in the integration drive (kill the server mid-drive, restart it, drop
   network with a fake fetcher, start the mod with no server) and the same as in-game probes.

In-game 2026-10-11 (items 1-3): a server restart mid-session is seamless - the new server adopts
the session whose tiles the game still shows (`adopting session ...`), releases and deletes its
1522 files once the resync committed, 0 missing-file errors (twice, old->new and new->new). Edge
guard (Owens Lake playa, server stopped, a stock test car at 17.7 m/s towards the block edge): it
braked and held the car 15 m inside the edge, banner "connection lost"; under sustained throttle it
then crept 4.6 m outwards in 50 s, fixed (fix/edge-creep: a pull back inside the stop line).
tools/restart_game.sh restarts a crashed or frozen game unattended (Steam, MCP wait, level load).

Status (2026-10-10, built offline, merged to main): the server side of 1 and 4 is in
place: `server/netfetch.py` (docs/research/2026-10-10-rate-limits.md §5) rate-limits every host,
retries with backoff, honours Retry-After, opens a circuit breaker per host and fails over along the
source chains (USGS -> EOX 2023 -> EOX 2016 -> palette; Mapterhorn -> AWS), `--offline` serves the
disk cache only, and the status text says "Offline - using cached data" / "Degraded: ..." (the mod
shows it as is). Reviewed adversarially the same day (late successes no longer close a breaker,
Retry-After makes waiting demand fail over, no doubled timeouts, prefetch past an open host, an
undecodable DEM tile leaves the cache; docs/ARCHITECTURE.md "Network"). Follow-ups:
- Re-texture the degraded (palette-fallback) tiles once the breaker closes: `Server.degraded` lists
  them, but a rebuilt tile needs a new file name (the engine caches by path) and a same-footprint
  replace swap in the protocol, so it waits for the retex / rebuild swap of item 4. (`far_retex`
  exists now for colour seams / de-shadow, but a palette tile's shape names the static material, not
  a per-tile one, so it cannot be retextured in place: it needs the rebuild swap.)
- The coarse global offline DEM (item 4) at `dem.DemUnavailable`; until then an uncached area's
  terrain request fails (`terrain_error`) when neither Mapterhorn nor AWS can be reached.
- Per-host state (breakers, budgets) in `status` for the dev tools window (the mod's banner, item 1, shows the status note: built 2026-10-11, below).
- Mapterhorn revalidation after 180 days (`If-Modified-Since`); Cache-Control for OSM-type hosts
  (`HostPolicy.honor_cache_headers`, unused until such a host exists).
- The near-terrain heightmap prefetch (`Connection.maybe_prefetch`) still asks the DEM as demand;
  give `Dem` a request kind so it comes after the far tiles' demand and is dropped first.
- Select fine levels by 3D distance in flight (rate-limit report §2.2), which removes most of the
  21 req/s to USGS at 10 km altitude (the limiter caps it at 15/s meanwhile). The steady 30 / 134
  m/s demand per host is a test now (`test_demand.NetDemandTest`, below each policy rate).

Status (2026-10-11, built offline on branch feature/robustness; docs/ARCHITECTURE.md "Robustness"):
items 1, 2, 3 and 7 (offline parts).
- 1: the connection banner (`banner.lua`, docs/DEVTOOLS.md "Connection banner"): starting / not running
  (with `python3 server/earthd.py` and the auto-start hint) / connection lost - reconnecting (attempt n)
  / offline / degraded (the server's note) / connected, top centre, hidden when all is well, in the
  pause menu and the big map; the settings page shows the same line.
- 2: the edge guard (`edgeguard.lua`): only while the server cannot move the block (not connected, no
  hello yet, only the level-start flat block, a resync after a reconnect not yet committed, a terrain
  request unanswered for 5 s or a terrain_error within 10 s) the last block is
  kept and, for a vehicle on the ground (ray within 6 m), a soft wall caps the speed towards an edge at
  sqrt(2 * 6 m/s^2 * (d - 15 m)) (warned from 150 m or the braking distance + 30 m), so the car comes
  to rest 15 m inside; a car that left the block anyway and dropped 3 m below its last ground is set
  down inside (spawn.safeTeleport without a reset). Aircraft (off the ground) are never touched; the
  level-start flat block is guarded too (no server at all).
- 3: a restarted server keeps an earlier run's session folder (younger than a day) until the game's
  hello / pose `sessions` (the folders whose files its objects use) stop naming it, then deletes it
  rate-limited; unclaimed ones go after 10 min (a live server's marker is touched every minute). The
  mod keys previous anchors' tiles by session (a new server's anchor 1 never meets the old one's) and
  resyncs at the same origin (no level reload), sending no pose / terrain request naming the old
  session's anchor id until then (a failed resync is asked again).
- Adversarial review (2026-10-11, branch review-robust): a mod whose anchor was 1 sent poses naming
  it to the restarted server before its resync committed, which made that server switch to its own
  anchor 1 early (holes after the old tiles went); the guard turned off at the restarted server's
  hello although nothing could move the block until the resync committed (the car parked at the wall
  could drive off; a resync anchor_error was never retried); the flat start block was unguarded while
  the server was up but the first anchor not built; the sweep's rmtree ran on the event loop. Fixed
  with tests (test_core_robustness, test_edgeguard, test_restart).
- 7: `server/tests/test_restart.py` (session adoption, and a drive whose server is killed and
  restarted: no hole, no vanished file, the guard only without a server, the old folder deleted once
  released), the modsim ports of the guard and of the farfield sessions checked against the Lua
  (`test_modsim`), luajit `test_banner`, `test_edgeguard`, `test_core_robustness`.
Still open: 4's coarse offline DEM and the retexture of degraded tiles; 5 (auto-start); 6; 7's
network-drop with a fake fetcher in the integration drive and the in-game probes. In-game checks:
banner readability / hiding, the soft wall with real vehicles (velocity set with scale 0 every frame
while held: wheel spin and yaw reset each time; trailers are not guarded), the rescue placement, a
real server kill and restart mid-drive (tiles stay visible, the old folder deleted later).

## Caching (user, 2026-10-10: "save and cache stuff so we don't need to redownload it")
Today: raw downloads are already kept on disk forever (~/.cache/beamng-earth: elevation 537 MB,
imagery 481 MB, gravity model 145 MB, 26k files after two days of testing) and revisits reuse them;
built tiles (meshes + textures, ~1 GB per session under the game's temp folder) are rebuilt each
session and deleted at server start; there is no size limit.
Plan (with the netfetch / rate-limit work):
1. **Size cap + LRU eviction** by last use (default e.g. 20 GB, set in the settings page, with a
   usage display and a "clear cache" button); never evict what the current session uses.
2. **Built-tile cache across sessions**: with deterministic generation and global tile keys (v1.4 M6),
   finished tiles are stored under a content key (tile id + generator version + data versions) and
   reused on revisit and after restarts - no rebuild, near-instant reload of known areas.
3. **Licence-aware lifetimes**: keep per-source rules (some services limit caching time or forbid
   offline storage); refresh stale tiles in the background when online; "no coverage" markers expire.
4. **Pre-download** a region or a planned route (Cannonball) for offline play, within the cap.
5. Cache layout versioned so a generator update invalidates only what changed.

Status (2026-10-11, built offline on branch feature/cache-cap; docs/ARCHITECTURE.md "Download cache"):
1 is in place: `server/cache.py` tracks the last use of every file under `dem/`, `imagery/`,
`colormatch/`, `deshadow/` and, since the map-data merge, `mapdata/` (by mtime only: earthd does not
use it yet) (the server's own record of each disk hit and write, persisted in
`<cache>/index.tsv`; a cache from before takes the files' mtimes), a cap (`--cache-cap GB`, default the
page's last choice, else 20; 0 = unlimited) and evicts the least recently used down to 90 % in a
background thread at most every 3 min, never what this server used within the hour, the gravity
model or `netstats/`. The settings page shows "1.2 GB of 20 GB, 34877 files", sets the cap (5 / 10 /
20 / 50 GB / unlimited) and clears the cache after asking (`cache` messages both ways; a `Server()`
without a `Cache` sends none, so the golden streams are unchanged). From 3: per-source lifetimes
(`cache.LIFETIMES`, enforced at start-up; empty, since no current source limits caching time; the
404 markers already expire through netfetch). From 5: a `VERSION` file at the root (a newer layout's
cache is left alone: counted, never evicted or cleared); per-producer versions stay in the paths
(`colormatch/v1/`, `imagery/usgs-ds1-<mode>/`) and an outdated directory ages out through the LRU.
Reviewed (branch review-cache): Clear keeps every file this server session used (not only the last
hour's, as the plan says), the page's usage message never waits for the cache's lock on the event
loop, an empty `VERSION` is written again (atomically) instead of leaving the cache alone, a
`nan` / `-inf` time in the index is a damaged line (it made every later save raise, so a clear never
answered) and an overflowing cap no longer stops the server, and a clear is not sent to a server
without a cache (the page waited 2 min).
Waiting for its in-game check. Follow-ups: refreshing stale tiles in the background (3), the
built-tile cache (2) and pre-download within the cap (4).

## Scheduled quick wins (user picked 2026-10-10; between the big versions)
1. **Saved places** (smallest): bookmarks, recent places, spawn at a place; in the settings page.
   Built offline (2026-10-11, branch feature/places; ARCHITECTURE.md "Saved places"): `places.lua`
   (own file, seeded with docs/PLACES.md and a few well-known drives), recent places recorded by
   `beamngEarth_core.teleport`, a "Places" section on the settings page (save here, lat/lon or a
   Google Maps paste, Go / Rename / Delete, recent places); reviewed offline (stale recent-row
   index, file never overwritten without a .bak copy); waiting for its in-game check.
2. **Easy install and start**: server auto-start with the game / launcher, one settings file,
   Windows support.
3. **Real weather** (Open-Meteo, keyless): clouds, rain, fog, wind; integrates with the game's
   weather/clouds or BWM_Weather. Built offline on branch feature/weather (2026-10-10) with the
   level-file parity objects, the real time zone and gravity by latitude/height
   (ARCHITECTURE.md "Real weather"); waiting for its in-game checks and the cloud calibration.
4. **Airports and runways** for plane mods (pairs with v1.4 M5 flight).
   Groundwork built offline (2026-10-11, branch feature/airports; ARCHITECTURE.md "Airports and runways",
   docs/research/2026-10-11-airports.md): OurAirports runways (public domain; weekly refresh, last good copy
   offline; 16,563 placeable runways, 94 % of large-airport runways, 29 % of small ones; the rest have no
   end coordinates), surfaces -> BeamNG ground models, a graded runway / shoulder / strip / blend height field
   under ICAO / FAA limits as a drop-in `elev_fn` for the heightmap and tiles (steep altiports and bush strips
   keep their shape: Courchevel 19 %, Lukla 15 %), FAA / ICAO marking geometry for DecalRoads, a CLI with
   previews. Wired 2026-10-13 behind `earthd --airports on` (default off): the runways graded into the
   block, the far tiles and the road tiles (`airportgrade.py`; KBWI 10/28 in-game 3x smoother than the
   DEM, within 16 cm of it), the markings drawn as road-tile chunks (low contrast with the stock
   `line_white`: a brighter runway paint material next). Next: a plane mod on 10/28, the paint material,
   then default on; water masks, OSM / Overture runways where OurAirports has no ends, runway ground
   models in the v1.5 layers, spawn places at runway ends.
5. **Data credits overlay** (user, 2026-10-10; docs/DEVTOOLS.md): small text in a screen corner naming
   the imagery / elevation / weather sources around the player. Built offline (2026-10-10, merged
   to main with netfetch and colour seams): server `credits` message (on change, at most 1/s), mod
   `credits.lua`, settings on / corner / size; reviewed adversarially (overlay wording now CREDITS.md's
   word for word, no weather-only message, hysteresis at the 2 km radius); waiting for its in-game check.
All other candidates below are approved too ("all these look good") and get scheduled after these.

## Seasons, snow and weather events (user, 2026-10-10)
Real weather (feature/weather) already brings real temperature, rain vs snow from the forecast,
clouds, fog and wind. On top of it:
- **Snow on the ground from real data**: snow depth/cover from Open-Meteo (snow_depth, hourly,
  global models), NOAA NOHRSC SNODAS (US, ~1 km daily depth and water equivalent, public domain) and
  NOAA IMS / NASA MODIS-VIIRS snow cover (global daily, 1-4 km / 500 m) for where it lies.
  Rendering: a snow layer blended into the tile material by depth, slope (no snow on cliffs), aspect
  and altitude (snow line), with stock BeamNG snow textures; physics: SNOW / ICE ground models on
  the physics terrain cells (v1.5 per-cell surfaces) so grip changes.
- **Events evolve between data updates**: during real snowfall the cover builds up in real time;
  afterwards it melts with a degree-day model driven by the real temperature (faster on sunny
  south-facing slopes, slower in shade/forest); new data snaps it back to reality.
- **Plowing**: roads (v1.6) get cleared by class and time since the snow stopped (highways and main
  roads first, residential later, rural tracks last), leaving banks along the edges and packed snow
  or slush where plowing lags; parking lots and sidewalks follow; ice where it re-froze overnight.
- **Seasons for vegetation**: deciduous trees leaf-off in winter and coloured in autumn, dry/golden
  grass in dry summers (e.g. California), green in wet seasons - driven by latitude, day of year,
  land cover and climate data (phenology), applied to the v1.9 trees/grass and as a seasonal tint of
  vegetation pixels in the imagery (the photos are from one season, usually summer leaf-on).
- **Winter details** (docs/research/2026-10-10-winter-reference-notes.md): snow amount by traffic
  level (busy roads plowed and packed, bus stops/quiet streets snowier, country lanes covered),
  snow banks dirty at the bottom and cleaner on top and low/soft rather than solid walls (BeamNG has
  no deformation; banks should not stop trucks like concrete), ice patches in shade and refrozen
  melt (ICE ground model), lakes and rivers freezing from the temperature history, lit windows at
  night, SNOW/ICE friction tuned for normal tyres (no winter tyres exist in the game).
- **Other events**: heavy rain/storm visuals and sound, fog banks from real visibility, wind storms;
  floods and wildfire smoke later if data allows.
- **References**: "Polish Roads" by mcct (https://www.beamng.com/resources/polish-roads.5846/,
  v3.3, summer and winter versions; shown in MuYe's video "They Added Seasons To BeamNG",
  https://www.youtube.com/watch?v=-Ga0a2fmmJo): the look of winter roads, snow banks, plowed vs
  unplowed surfaces, and how busier roads are better paved/maintained than quiet ones (also a model
  for road surface quality by road class in v1.6). Its licence forbids using or modifying its assets
  ("We do not allow anyone to use/sell our assets/modify map"), so it is a visual reference only:
  screenshots side by side, nothing taken. Real photos of snowy roads/towns for tuning.
- Deterministic and global, like everything else: snow placement is a pure function of position,
  data and time.

## Wind (user, 2026-10-10)
Built: real 10 m wind + gusts from Open-Meteo at 4 Hz, lulls to half the mean, direction swings
growing with gustiness (weather.lua). Still to do:
- **Terrain effects**: speed-up on ridges and summits, shelter in canyons, valleys and behind
  forests/buildings, channelling through gaps and passes (from the DEM and land cover around the
  player; generic rules, no per-place tuning).
- **Gust fronts that travel**: gusts moving across the land with the wind instead of the same wind
  everywhere at once (matters with several vehicles and AI traffic).
- **Visible wind**: grass, trees, dust and snow drift moving with the gusts (with v1.9 vegetation).
- **Wind sound** (Sound design).

## Sound design (user, 2026-10-10: "definitely something we should work on at some point")
First step is in the next build (ambient room tone by climate zone, from the game's own sounds).
Later, as content arrives:
- **Ambience by place**, blended by what is around the player: wind on open ground and ridges
  (louder with real wind and altitude), forest (birds by day, insects/crickets at night), city hum
  and distant traffic near built-up areas, surf and gulls on coasts, water near rivers/lakes,
  silence/wind in deserts and at high altitude; day/night and season variants.
- **Weather sound**: rain on the car and ground, thunder rumble (no visual lightning in the game),
  wind gusts tied to the real gust data.
- **Acoustics**: reverb by surroundings (open plain vs canyon vs city street vs tunnel), using the
  game's reverb zones placed by the server from terrain shape and buildings.
- **Surface sound**: tyre noise per ground type (asphalt, gravel, sand, grass) comes with v1.5
  surface physics; bridges and gratings with v1.6 roads.
- **Life**: traffic (v1.8) and city sounds (v1.9), aircraft near airports.
- All from stock game sounds where possible (no redistribution), positioned so they survive rebases.

## Several vehicles far apart (single player; needed before AI traffic and multiplayer)
Today only the player has ground: the physics TerrainBlock follows the player, so any other vehicle
(a second car the user parked, a trailer left behind, AI traffic, a car the user switched away
from) falls through the world once the block moves away, and beyond ~16 km from the origin its
float32 position gets coarse.
Design direction:
- **Freeze what has no ground**: a vehicle outside the physics block (minus a margin) is frozen
  in place with `setActive(0)` (as the game's vehicle pooling does) and its position kept in
  lat/lon/height by the mod; when the player (and so the block) comes back, it is re-placed exactly
  and woken. Switching to a frozen vehicle = a teleport to it (new anchor, then wake it).
- **Several ground islands** if the engine allows more than one TerrainBlock (probe): a small
  physics block under each active far vehicle (e.g. AI traffic around the player, a second player's
  car in a shared session), each following its vehicle; budget-limited.
- Rebase moves every vehicle (already done today); vehicles stored in lat/lon survive any number of
  rebases and teleports.
- The world map shows every vehicle; "go to vehicle" uses the same teleport path.

## Multiplayer (user wants it; last or near last)
User's idea (2026-10-10): **sessions**. Players close enough to each other share a session: one
shared world origin, one shared rebase, one shared far field; when they drift far enough apart the
session splits (each gets its own origin), and sessions merge again when players come close.
Notes for later:
- Different sessions anywhere on Earth all live near the game's own (0, 0): each client receives
  only its own session's terrain, and cars of other sessions are hidden (and not collidable) on that
  client, since their local coordinates overlap meaningless positions in its world (user, 2026-10-10).
- Fits the vehicles-far-apart design above: a session is a cluster of entities sharing an origin.
- Priority: single player first; multiplayer only once single player is where the user wants it.
- Split/merge thresholds with hysteresis (e.g. merge < 5 km, split > 12 km) so they don't flap.
- Positions over the network in a global frame (lat/lon/height or ECEF doubles); each client maps
  to its session's local frame; a merge is a rebase for the joining player.
- BeamMP compatibility: its position sync assumes one shared map frame; needs a translation layer.

## Candidates (proposed 2026-10-10, approved by the user; scheduled after the quick wins)
Infrastructure
- **Easy install and start**: the server starts automatically with the game (or from a launcher),
  one settings file, Windows support (most BeamNG players are on Windows; today it is Linux-tested).
- **Offline / pre-download**: cache a region or a planned route (Cannonball) ahead of time; a cache
  size limit and cleanup.
- **Saved places**: bookmarks ("home", Fells Point, Whitney summit), recent places, spawn at a place.
- **Multiplayer (BeamMP)**: the floating origin and per-player streaming conflict with multiplayer
  position sync; needs a shared-origin or per-client-transform design. Big; only if wanted.
- **Automated visual checks**: screenshots at fixed places compared after every change.
  *Built offline 2026-10-11 (branch feature/benchmark; first in-game run pending):*
  `tools/benchmark.py run / compare` over `docs/benchmarks.json` (docs/PLACES.md's places, the
  Whitney photo view, 22 global places on every continent, 5 of them held out); fixed sun and clear
  sky, load time / tiles / exactMiss / frame times per place and view, an HTML side by side with
  MAD and SSIM per shot; everything the run changes is restored. Reviewed 2026-10-11 (branch
  review-bench): no shots after a failed teleport, stops when the user drives, re-parks after each
  teleport, MCP timeouts retried, a second Ctrl-C cannot skip a restore step, the free camera's zoom
  restored, compare flags places that failed or never got ready, farmland and a data-poor city added.
  docs/DEVTOOLS.md "Visual benchmark".
World
- **Real weather**: live clouds, rain, fog, wind, temperature from a free forecast API (Open-Meteo),
  via the game's weather/cloud system (or the BWM_Weather mod the user has).
- **Seasons and snow**: real snow cover (satellite snow maps), autumn/winter vegetation colour.
- **Night lights**: street lights and lit windows where cities are (OSM lamps + night-lights
  satellite data), so cities glow at night.
- **Light pollution in the night sky** (user, 2026-10-10): today the stars and the Milky Way are
  the same everywhere (ScatterSky starVisibility 1, milkyWayBrightnessEV 2, nightColor black), so
  Baltimore gets a desert sky. Plan: the server works out the zenith sky brightness at the player
  (mag/arcsec²) from satellite night lights (NASA Black Marble / VIIRS, open data: radiance
  summed over ~200 km with a distance falloff, Walker's law ~d^-2.5) or from the World Atlas of
  Artificial Night Sky Brightness (Falchi et al. 2016; licence to verify) and sends it with the
  weather; the mod maps it to the sky (Bortle 1 -> full Milky Way; city -> starVisibility and the
  Milky Way down, starHaze up, an orange skyglow in nightColor / nightFogColor towards the
  brightest direction). Clouds over a city glow orange and hide the stars; the moon washes out
  faint stars too. Generic: one rule from the brightness value, no per-city tuning.
- **Airports and runways** (for plane mods): flattened, paved runways with markings from OSM,
  taxiways, the airport as a spawn point. (Groundwork from OurAirports: quick win 4 above.)
- **Railways, power lines, fences, guardrails** from OSM as cheap repeated props.
- **Energy infrastructure** (user found the Kayenta PV Solar Plant, 2026-10-10; docs/PLACES.md):
  solar farms from OSM `power=plant` + `plant:source=solar` and `generator:source=solar` areas as
  instanced panel rows (row pitch, tilt from latitude, fixed or single-axis tracking from
  `generator:method`/tags, east-west rows by default), fenced, with service tracks; wind farms from
  `generator:source=wind` points (hub height and rotor size from tags, else the US Wind Turbine
  Database in the US, else a typical size) with turning blades driven by the real wind; substations
  and the power lines between them. All generic rules, deterministic per feature id.
- **Gas stations** at real locations using the game's fuel-station system, so long trips need
  refuelling (fits the Cannonball run).
- **Off-road trails** with surfaces from OSM track grades.
Gameplay
- **Generated challenges**: point-to-point time trials between real places, a timed Cannonball run
  with checkpoints, delivery jobs between real towns.

## Water: paused 2026-10-13 (user: "still some issues but I want to move on, come back later")
Built (`--sea on`, still default off): the sea (curved WaterBlock tiles, NOAA bathymetry), lakes and wide
rivers (tiles fitted to the water, carved beds), narrow rivers and streams (River objects along the
centrelines, carved channels, ripples flowing downstream, a current). Known issues to come back to:
- Confluences and river mouths still look confused in places; holes reported in a lake and in places
  along rivers; a faint darker seam where tiles overlap.
- Water vanishing close to the camera seen again (River near mesh: SubdivideLength / vertex limits).
- Stretched river textures at tight bends (the texture follows the centreline).
- Up to 120 inland tiles near a big river (Harrisburg): measure the frame cost; merge tiles.
- Canals with locks, intermittent streams, glossy far water, lava (volcanoes), default on.

## User requests 2026-10-13 (after water; in this order unless the user reorders)
- **Lava (later, user idea):** River / WaterBlock objects with `liquidType` lava (the "Downhill Mayhem"
  map's lava pools: orange, emissive, density 10000) for volcanoes or other places with lava.
- **Performance, monitored live:** the user drives by hand in different places while the game and
  server metrics are watched; there are still hitches while driving.
- **Texture patterns** visible in some terrain (repeating tiles): find and fix.
- **Sidewalks and intersections:** many need work. Take many in-game photos of sidewalks and
  intersections and compare them with high-resolution aerial or street-level imagery.
- **Decoration:** trees, rocks and other scatter.
- **Airport buildings:** terminals, the control tower, hangars (the runways themselves look good).
- **LOD quality settings** (after the performance work, closer to the final look): at 4K on a large
  monitor, the terrain from half distance outward looks low quality. 2026-10-14 A/B at Lancaster farms:
  `--tex 256,256,512,1024` (2-4x far textures, finer imagery) shows no difference in 2560x1440 shots from
  10 and 200 ft, loads in 205 s instead of 171 s, same frame time. Default kept; the user to try it at 4K.
  If it doesn't help either, look at haze and mip filtering next.

## "Make the world look alive" (user, 2026-10-13 night; references from stock maps + many street photos)
Built 2026-10-14 (ARCHITECTURE "Buildings", "Street furniture", "Trees"):
- Buildings v1 (`--buildings on`): footprints with predicted heights / roofs, stock window sheets, brick
  by region, cornices, front doors, far render-only tiles to 1600 m.
- Street lights along sidewalks, utility poles along country roads.
- Regional tree species (West pines / oaks / palms, junipers and desert scatter), street trees moved
  out of the road corridor onto the verge, denser crowns, cells built ahead after a teleport.
Next, from the photo comparisons (docs/research/streetview/fp*, base_*):
- **Parked cars (needs the user's OK: spawns real vehicles):** the photos are full of them and streets
  look empty without. Plan: the road tiles list curb parking spots (urban roads that already get a
  parking lane, one spot per 6.5 m); the mod hands them to the stock `gameplay_parking` as sites data
  (`setSites({parkingSpots = {...}})`, the West Coast USA format: pos, rot, scl, multi-spots) and
  `setupVehicles(n)` keeps a pool of n parked cars on the spots near the player (stock vehicle pooling).
  Open: the cost of n vehicles, and how pooled cars follow our rebase.
- Traffic signals and stop signs are built from junction shapes (furniture.py); Mapillary map
  features have real positions of signs in cities (42 street lights in a 170 m square of Fells Point;
  the API is slow, so a per-cell cache) for later.
- Building parts (Overture `building_part`): towers' podiums and setbacks; roof clutter (HVAC boxes).
- Facade variety: painted brick, formstone, siding colours per region; window sheet per era.
- Wires between utility poles; signs (stop, speed limits) from data where present.
- The "dark mounds" near Harbor East (President St bridge) are the 4-5 m quays beside the Jones Falls
  outlet, whose water (DEM 0 m) is drawn as bare ground: a water gap (the inlet is not covered by the
  sea tiles), for the water work.
- Road widths: urban trunks / primaries not lined by buildings default to 4 lanes since 2026-10-14
  (Moab's Main Street); real lane counts would need OSM `lanes` (Overture has none).
- Curb ramps: the pad is the first 0.6 m of the slope since 2026-10-14, West Coast USA's yellow tactile
  material (seen yellow from the air at Ocean City).
- Ocean City's yards and park read as pale sand: its imagery is late-season (dormant grass the colour of
  pavement, excess green ~2 on both), so photo_lawns keeps 2 % of the lawn candidates. A season-proof
  lawn test (texture, or imagery from another date) would be needed; left as is.
- Maryland's six-inch imagery (first in the chain since 2026-10-13) is leaf-off spring: brown dormant grass
  and bare branches in every Maryland place; the colour match to USGS only reaches olive. Leaf-on chain
  (`--imagery naipusda,naip,usgs,eox2023,eox2016`): green, a little softer, 87 s vs 81 s at Columbia. Asked
  the user 2026-10-14 (recommended leaf-on); default unchanged until they answer.
- Divided town arterials (Overture one-way trunks at 45 mph or less: Coastal Highway) get a sidewalk on the
  curb side since 2026-10-14; expressway trunks (faster or no limit) none.
- Building heights stay predicted where Overture has none (Baltimore 98 %): the canopy height model
  masks buildings (~0 m on roofs, checked 2026-10-14), so it cannot fill them.
- Near ground looked pale grey in towns: built-up land was the asphalt layer (the dirt layer since
  2026-10-14, and a share of it the mown-lawn layer: surface.py LAWN_*).
- Town ground at street level has a whitish-blue sheen at grazing angles (Bishop, Ocean City, 2026-10-14)
  although the photo is a plain grey: sky reflections. 2026-10-14: base roughness 206 -> 240 and the macro
  roughness off near the camera ([0, 0.8]), as West Coast USA's terrain materials; check it is gone.
- Tried 2026-10-14: a denser, taller humid understory (0.5 per 6 m cell, 1-3.5 m): +24 % items, +2.4 ms at
  Columbia, and the extra bushes land mostly on cleared "tree cover" ground (reads as scrub, not forest).
  Reverted. The forest look needs crowns that close (sizes / spacing) more than ground scatter.
- Forest edges along roads (humid country) get a thicket band of bigger bushes (vegetation.py EDGE_*);
  watch the frame time on wooded roads (about +20k bushes per z14 cell).

## Bugs to look at (reported, not fixed yet)
- **Rain particles "spazzing out"** (user, 2026-10-11): some raindrops jump around or streak across the
  view (seen in Fells Point's real rain). Suspects: the Precipitation object's box following the
  camera or the rebase, or the wind gusts (weather.lua) changing its speed every frame.
- **Road meshes vs coarse ground tiles** (was: road decals partly hidden by coarse ground tiles) (2026-10-11): beyond the exact-ground radius
  around the car (~150 m), the coarser mesh tiles can lie above the physics block and cover parts
  of a road in a jagged pattern (seen in street-view shots taken 150-300 m from the car). Options:
  sink the coarse tiles a little more under roads, draw far roads into the tile imagery, or keep
  DecalRoads to the exact-ground radius.

## In-game probes still to run (when the user is not driving)
Origin P1/P2 (precision at 15 km), GroundCover on a hidden terrain, Forest item cost, decal roads
on mesh tiles, 8-layer terrain import cost, navgraph `map.reset` at ~5k nodes, CEF fetch from
127.0.0.1, stock tree/sign asset borrowing from other levels, the data credits overlay (readability,
pause-menu / big-map hiding).
