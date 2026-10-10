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
  biomes, data-rich and data-poor, flat and steep, cities and wilderness), with before/after
  screenshots and numbers, plus a held-out set that is not used for tuning. A change that improves
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

## v1.5 "Ground truth"
- Per-cell surface physics: the invisible TerrainBlock gets material layers from ESA WorldCover
  (+ OSM roads): ASPHALT / DIRT / GRASS / SAND / GRAVEL / ROCK / MUD / SNOW (no more dust on roads).
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
- Sky: CloudLayer + wind, haze, per-biome cubemap, black night colours.
- GroundCover grass if probes show it renders on a hidden terrain.

## Water (with v1.5/v1.6; user 2026-10-10: lakes and rivers are blue dirt today; keep depths
## roughly real, no underwater detail needed)
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
- Overture transportation (GeoParquet via the STAC per-file index; 2.6-3.7 s per 30 km box cold).
- Near roads: meshes on the 2 m lattice, ground graded under them on the server so physics and
  visuals agree; markings; bridges as collidable decks. Far roads painted into the imagery.
- Bridges and overpasses (user report 2026-10-10: roads dip into rivers where a bridge should be).
  The elevation models are bare-earth: bridge decks are removed, so the ground follows the river
  bed. Needs: OSM/Overture `bridge=yes` + `layer` segments; deck height = a straight/smooth line
  between the two abutments (sampled where the road meets solid ground on each side), never the DEM
  under the span; the deck as its own collidable mesh (the TerrainBlock is a height field and
  cannot carry a surface over a river), with the river valley kept below it; same treatment for
  overpasses and interchanges (layer ordering), and tunnels as the inverse (ignore the road, keep
  the hill). Embankments/abutments get graded so the deck meets the road without a step.

## v1.7 "Navigation"
- Navgraph from our roads via `map.reset(customNodes)` (5 known pitfalls, fixes in the research).
- GPS: OSRM (FOSSGIS default, demo fallback; NY->LA in ~1 s, no key), route held by the server,
  15 km window streamed into `core_groundMarkers`; Photon/Nominatim place search.
- World map: Vue mod page + Leaflet, tiles proxied/cached by earthd on 127.0.0.1 (CSP allows it;
  probe first), click to teleport / set destination.

## v1.8 "Alive"
- AI traffic on our navgraph (spawn clipped to the physics block, edge guard).
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

## Scheduled quick wins (user picked 2026-10-10; between the big versions)
1. **Saved places** (smallest): bookmarks, recent places, spawn at a place; in the settings page.
2. **Easy install and start**: server auto-start with the game / launcher, one settings file,
   Windows support.
3. **Real weather** (Open-Meteo, keyless): clouds, rain, fog, wind; integrates with the game's
   weather/clouds or BWM_Weather. Built offline on branch feature/weather (2026-10-10) with the
   level-file parity objects, the real time zone and gravity by latitude/height
   (ARCHITECTURE.md "Real weather"); waiting for its in-game checks and the cloud calibration.
4. **Airports and runways** for plane mods (pairs with v1.4 M5 flight).
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
World
- **Real weather**: live clouds, rain, fog, wind, temperature from a free forecast API (Open-Meteo),
  via the game's weather/cloud system (or the BWM_Weather mod the user has).
- **Seasons and snow**: real snow cover (satellite snow maps), autumn/winter vegetation colour.
- **Night lights**: street lights and lit windows where cities are (OSM lamps + night-lights
  satellite data), so cities glow at night.
- **Airports and runways** (for plane mods): flattened, paved runways with markings from OSM,
  taxiways, the airport as a spawn point.
- **Railways, power lines, fences, guardrails** from OSM as cheap repeated props.
- **Gas stations** at real locations using the game's fuel-station system, so long trips need
  refuelling (fits the Cannonball run).
- **Off-road trails** with surfaces from OSM track grades.
Gameplay
- **Generated challenges**: point-to-point time trials between real places, a timed Cannonball run
  with checkpoints, delivery jobs between real towns.

## In-game probes still to run (when the user is not driving)
Origin P1/P2 (precision at 15 km), GroundCover on a hidden terrain, Forest item cost, decal roads
on mesh tiles, 8-layer terrain import cost, navgraph `map.reset` at ~5k nodes, CEF fetch from
127.0.0.1, stock tree/sign asset borrowing from other levels.
