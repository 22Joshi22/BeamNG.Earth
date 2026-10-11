# BeamNG.Earth dev tools (spec, 2026-10-10)

User request: "add some dev tools so I can visualise the LODs, the loading and anything else that
might be useful". In-game, opt-in, zero cost when off. Built after v1.4 M0/M1 (they touch the same
files). Engine-only (imgui + debugDrawer), like the existing `ui.lua` window.

## Pause-menu integration (BeamNG 0.39 "Mods" tab)
The pause menu has a mod API (lua/ge/extensions/ui/pause/actions.lua):
`ui_pause_actions.registerModButton{id, tabId = "mods", label, icon, componentName = <.vue path>}`
puts a button in the shared **Mods** tab, and `registerModTab{id, label, icon}` adds a whole tab of our
own. Mods call it from a Vue mod entry `/ui/ui-vue/mods/<mod>/index.js` (`onLoad` / `onUnload`, via
`import { lua } from "@/bridge"`), e.g. the user's taa_standalone mod (index.js + TaaSettings.vue,
using BngSwitch / BngSlider / BngButton from `@/common/components/base`).

BeamNG.Earth uses that:
- a button **"BeamNG.Earth"** in the shared Mods tab (where users look), opening our settings page;
  later its own tab once there are several pages (Settings, World Map, Dev Tools);
- **Settings page** (Vue, `/ui/ui-vue/mods/beamngEarth/EarthSettings.vue`): server connection and
  session, rebase distance, view distance mode (real horizon / fixed km) and haze strength, detail
  level / streaming budget (when the M3 controller exists), sky mode (real time / manual) and the time
  offset, imagery source preference, surface physics on/off (v1.5), attribution text; buttons to open
  the Dev Tools and (v1.7) the World Map. Values persist in the mod's own settings file
  (`/settings/beamngEarth.json`), applied through `beamngEarth_core` functions.
- **Dev tools page**: the toggles and presets below (the in-world overlays and the stats/mini-map
  panels themselves stay imgui: they must draw over the 3D view while driving, cheaply).
- The existing imgui window becomes a compact HUD (status line + quick toggles), hidden by default
  once the settings page exists.

## Data credits overlay (user, 2026-10-10)
Small, unobtrusive text in a screen corner (default bottom-left, semi-transparent, ~11 px, no
window chrome; corner, size and on/off in the settings page; on by default because CC BY and
similar licences ask for visible attribution) naming the sources that built what is around the
player right now, e.g.:
  "Imagery: USGS NAIP 2022, Maryland iMAP 2024 · Elevation: Mapterhorn (USGS 3DEP 1 m) ·
   Weather: Open-Meteo.com · Map data © OpenStreetMap contributors"
- The server knows each tile's sources (imagery source and capture year per tile, the DEM tile's
  underlying dataset from the provider's attribution data, weather, OSM/Overture once used). It
  sends a compact `credits` message whenever the set for the tiles within ~2 km of the player (plus
  a short "far: Sentinel-2 cloudless" part) changes; the mod draws it. No per-frame work beyond one
  imgui text draw.
- The full credit list stays in CREDITS.md and on the settings page.
- Built after the dev tools branch is merged (shares ui.lua / settings.lua).

### Built (2026-10-10, merged to main; offline-tested, in-game check pending)
- **Server** `server/credits.py` + small hooks: `dem.Dem.served` records which provider served each
  DEM tile (Mapterhorn / AWS fallback / none; also when netfetch fails over from Mapterhorn to AWS);
  `Server.make_tile` stores every tile's source ids in `TileInfo.credits` (`img:<source>` for each
  imagery source with texels in its texture, from `Imagery.render`'s counts; with `--colormatch s2`
  also the S2 references for a texture the seam feather changed (the counts' `colormatch` share), and a
  stale texture rendered again takes its new sources; `dem:<provider>` for each DEM tile under the
  footprint at the tile's DEM zoom); `Connection` has a `credits.Tracker`. The near set = sources of
  the shown tiles (previous anchors' included) whose footprint is within 2 km of the player (a near
  source stays near while one of its tiles is within 2.2 km: no flip-flopping when the player rocks
  across the radius), plus Open-Meteo once weather went to the mod; the far set = the other shown
  tiles' sources not already near. Nothing is sent before some shown tile's sources are known (the
  weather alone, e.g. while the first tiles load or after a server restart, would replace the mod's
  full attribution line with one name). The wording is CREDITS.md's, word for word (tested). Message `{"type":"credits","v":1,"near","far","ids","farIds"}`, e.g. near "Imagery: USGS
  The National Map | Elevation: Mapterhorn (see mapterhorn.com/attribution) | Weather data by
  Open-Meteo.com", far "Far imagery: EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH
  (Contains modified Copernicus Sentinel data 2023)". Checked after poses and swaps at most once a
  second, sent only on change, re-sent on hello. `earthd --credits on|off` (default on);
  `Server()` has none, so the golden message streams (test_cli_default, test_v131_default) are
  unchanged. Agreement table, server/README.md protocol table.
- **Mod** `beamngEarth/credits.lua` (driven by core.lua): one background-draw-list rectangle
  (black, 30 %) and one `ImDrawList_AddText1` per frame (white, 80 %, the UI font at its own size),
  no window; the text is wrapped (line breaks between words) and measured with `CalcTextSize` only
  when it, the font size or the screen width changes, at 60 % of the screen width. (In-game
  2026-10-11 the first build used `GetFont()` + `AddText2`: the 0.39 binding's `GetFont()` throws
  "bad argument #3", every frame, which flooded the log and stalled the MCP server. Any imgui error
  now turns the overlay off once, logged.) Shown while our world is active (`st.active`), hidden in the pause menu (UI
  state `menu*`, via core's `onUiChangedState`), on loading screens and while the big map is open
  (`freeroam_bigMapMode.bigMapActive()`). Until the first credits message (an older server, or the
  first seconds of a session) it shows the session's attribution line (`hello.attribution`, or the
  built-in one). A new hello clears the previous session's credits.
- **Settings** (settings.lua + EarthSettings.vue, "Interface"): `creditsVisible` (on),
  `creditsCorner` (bottom-left / bottom-right / top-left / top-right; bottom-left), `creditsSize`
  (small 11 px / normal 14 px; small; **no effect yet**: AddText1 draws at the UI font's size; a
  sized font needs a working ImFont handle or a borderless window with SetWindowFontScale), persisted in `/settings/beamngEarth.json`; the
  "Attribution" section shows the credits on screen now.
- Tests: `server/tests/test_credits.py` (wording against the sources' own credits, DEM keys vs what
  `Dem.sample` reads, `Dem.served`, per-tile sources in `make_tile`, the near / far split, change-only
  and throttled sending, re-send on hello, an in-process session, the default server sending none),
  `test_protocol_agreement.py` (the table, both sides), luajit `mod/tests/test_credits.lua`,
  `test_core_stubbed.lua`.
- Measured offline: a tile's sources cost ~0.12 ms in `make_tile` (a 3 x 3 point DEM-key lookup;
  tile builds take tens of ms); a credits check over 2000 shown tiles ~0.9 ms (at most one a second);
  a message is ~0.3-0.6 KB. Mod: `credits.draw` allocates nothing per frame (0 bytes over 10000
  frames, counted in the interpreter, test_credits.lua; the attribution fallback is a constant string
  from farfield's counters, not a `farfield.stats()` table), ~0.3 us of Lua per frame in the
  interpreter with a stub imgui, plus the two draw-list calls.
- Deviations: the far part uses the same wording as the near one (EOX asks for its exact wording,
  so "far: Sentinel-2 cloudless" became "Far imagery: EOxCloudless ... (Contains modified Copernicus
  Sentinel data 2023)"). No capture year for USGS (ImageryOnly tiles carry no date) and no
  underlying dataset for Mapterhorn (its tiles do not say which survey they come from): the credit
  names Mapterhorn with its attribution page. OSM / Overture are not used yet, so not credited.
  Shown whenever our world is active, also in another level after a teleport (our tiles are on
  screen there too).
- Still to check in-game: the text size and readability on bright and dark ground, that the
  background draw list sits above the 3D view and below the game's own HTML UI as expected, the
  UI state names of the 0.39 pause menu (`menu*` assumed from the stock Lua) and the big map.

## Entry point
- A **"Dev"** collapsible section / tab in the existing BeamNG.Earth window (ui.lua), plus a console
  API `beamngEarth_dev.set('<overlay>', true|false)` and `beamngEarth_dev.preset('lod'|'loading'|'off')`.
- A key binding (default unbound; user picks) to toggle the last-used preset.
- State persists per session only (no settings file writes).
- When every overlay is off: no per-frame work at all (the extension returns at the top of its hooks).

## Overlays (each a checkbox)
1. **LOD tint**: every far tile tinted by level (L-2/L-1 green = matches physics, L0 cyan, L1 blue,
   L2 purple, L3+ orange -> red), so the quadtree is visible on the ground and the horizon.
   Implementation: per-tile Material `diffuseColor` multiply (the tile materials are ours, one per
   tile), applied/restored in time-sliced batches (governor unit kind `devTint`, ~50 materials per
   frame) so toggling never hitches. Tiles created while it is on get the tint at creation.
2. **Tile outlines**: debugDrawer outlines of tile footprints at ground height (colour by level),
   only for tiles within N km (default 3 km; slider), so the cost stays bounded (<= ~150 boxes).
   States drawn differently: shown, hidden-loaded (waiting in a swap group), in crossover, old anchor
   (after a rebase), doomed.
3. **Loading heat**: queued / importing tiles highlighted (outline flashes), the next ~10 imports
   numbered in order, so priority problems (near tiles waiting behind far ones) are visible.
4. **Physics block**: the TerrainBlock's collidable square drawn (and the pending/next block dashed),
   with the terrain-follow commit thresholds; optional **"show physics terrain"** that turns the
   invisible TerrainBlock's rendering on (rendered with a checker material) to compare visuals vs
   collision.
5. **Rings**: near radii (150/400 m today), rebase circle (15 km) and the prefetch circle, the
   far-field radius / horizon distance (M1h), drawn as ground circles.
6. **Under-car probe**: a vertical line at the car: physics ground height vs visible tile height and
   the tile level under the car; turns red when the visible tile is not the exact level or differs by
   > 5 cm (the `exactMiss` metric of V1_4_SPEC).

## Panels (imgui)
- **Stats**: fps, frame ms p50/p95/max (last 5 s), sim ratio, governor budget and per-kind estimates,
  imports/s, deletes/s, queue length, waiting groups, crossovers running, live/old/doomed objects,
  materials, pops, failed, exactMiss count, server backlog, server status line, anchor, distance to
  rebase, last follow / rebase commit ms.
- **Graphs**: rolling 60 s plots (imgui PlotLines) of frame ms, queue length, imports/s, speed.
- **Mini-map**: top-down imgui draw-list view of the tiles around the player (zoom 1-80 km), coloured
  by level and state, with the car arrow, physics block, rings and the server's desired-but-not-shown
  tiles. Click a tile -> its key, level, state, texture size, build/import ms (from the server log
  fields the mod already has).
- **Controls**: freeze LOD (stop applying swaps, for inspecting), hide far tiles beyond X km, force
  rebase now, rebase distance slider (exists), teleport (exists), reload far field, toggle sky mode
  (real/manual) and time of day, motion-blur hold test, screenshot-with-overlays.
- **Log**: last 50 mod events (commits, rebases, swaps blocked at the cap, failures) with timestamps.

## Cost rules
- Off: nothing per frame. On: <= 0.3 ms per frame with every overlay (budget checked with perfmon,
  the panel shows the dev tools' own cost).
- No loops over all tiles every frame: outlines and the mini-map iterate a spatial index updated on
  tile add/remove, limited to the view radius; stats come from counters.
- Material tint changes are time-sliced; never a full reload in one frame.

## Tests
- luajit: the dev module with stubs (toggles, zero work when off, tint batching restores every
  material exactly, spatial index add/remove/query).
- In-game (when the user is not driving): toggle each overlay at the Kern River spot, screenshot,
  perfmon cost per overlay.

## Built (2026-10-10, branch feature/devtools; offline-tested, in-game checks pending)
- `beamngEarth/dev.lua` (extension `beamngEarth_dev`, loaded by the modScript, everything off):
  overlays tint / outlines / loading / physics / physicsTerrain / rings / probe and panels
  stats / graphs / map / controls / log in one imgui window "BeamNG.Earth Dev"; presets off, lod,
  loading, physics, all; `toggleLast()` on an input action ("BeamNG.Earth: toggle dev overlays",
  Options > Controls > Debug, unbound). With everything off `onUpdate` returns at its first line
  and farfield has no watcher (test_dev "off: the hook touches nothing").
- `beamngEarth/tileindex.lua`: per-level hash grids (cell = max(tile, 512 m)); a query visits
  min(cells in range, occupied cells) per level (<= ~900 cells for 3 km, any tile count).
- farfield.lua hooks (small, for the merge): `setWatcher` (onAdd in `spawn`, onRemove in
  `deleteById`, onReset after `transform` and in `forget`), `setFrozen` (swap groups wait),
  `eachTile`, `queuedTiles`, `counters`, `anchorId`, and `inst.importMs`.
- Tint: Material `diffuseColor` slot 0 + `flush()` + `reload()` (as stock trackBuilder), the
  original value kept per material (an empty field restores as '1 1 1 1'); at most 50 materials
  and 0.25 ms per frame, never in a governor heavy or quiet frame. Deviation from the spec: not a
  governor unit kind - the governor's budget ramps to 0 whenever streaming is idle, so a tint
  through it would stall on a parked car; the tint keeps its own slice instead.
- Probe: the visible height is the physics ground + the tile's crossover offset when the tile is
  the exact level (exact tiles match the lattice bit for bit); otherwise only the level is shown.
- Not done: the checker material on the shown physics terrain (it renders with its own dirt
  material), "hide far tiles beyond X km" (it would fight farfield's own hide state), the motion
  blur hold test and screenshot-with-overlays buttons, "reload far field" (Controls has
  reconnect / resync instead).
- Pause menu: `ui/ui-vue/mods/beamngEarth/index.js` registers the button "BeamNG.Earth" (id
  `beamngEarth-settings`, icon globe) in the shared Mods tab and opens `EarthSettings.vue`
  (structure and bridge pattern of taa_standalone: BngSwitch / BngSlider / BngButton, custom
  dropdown, `bngApi.engineLua` with callbacks). `beamngEarth/settings.lua` (extension
  `beamngEarth_settings`) persists rebase distance, view distance mode / fixed km, haze
  (placeholder), sky mode, clouds, weather mode (real / manual, `beamngEarth_weather.setMode`; a
  saved `auto` from before 2026-10-11 loads as real without a warning, `settings.MIGRATE`),
  real gravity (on, `setGravityEnabled`), HUD, dev radii in `/settings/beamngEarth.json`; every default
  is today's behaviour, the view distance is applied only in our level and only when it is not
  the default. Weather and Sky stay on Real and switch themselves to Manual when the weather or the
  time is changed in the game (the pause menu, another mod; ARCHITECTURE.md "Real weather"):
  weather.lua / sky.lua save `manual` through `settings.set` and show one toast; a short note under
  both dropdowns says so. Choosing Real weather is always applied (even when it already is: it clears
  a gravity change made in the game); after a Lua reload both read the saved mode back. The page's 2 s poll (`uiState`) refreshes the values too, so the dropdown
  shows Manual within 2 s (a poll answered within 1 s of the page's own write is not applied to the
  values, so it cannot flip a choice back). The dev overlays stay per session. The Connection section also shows the server
  features as status lines: the weather line, the gravity in use and its source, and the imagery
  de-shadowing mode (`hello.deshadow`, server-side only; "off" without it).
- HUD: once the page exists (index.js says so, or the pause menu lists the button) the imgui
  window is a compact status HUD, hidden at level start unless "Status HUD" is on; "Full window"
  brings the old window back for the session. Without the page nothing changes.
- Places (quick win 1, 2026-10-11, branch feature/places; ARCHITECTURE.md "Saved places"): a
  "Places" section (second, closed by default): "Save current place" (name field, the position
  shown; Save off until there is one), a "Latitude, longitude" field (Google Maps' "lat, lon" paste,
  N/S/E/W, degrees-minutes-seconds) with Go and Save (under the name above), the saved places with
  Go / Rename (inline field, OK / Cancel) / Delete (the button reads "Confirm" for 4 s), and the
  recent places (name or lat/lon, "level start" tag) with Go / Save. Go is disabled with a note
  ("Companion server not connected", "No player vehicle") when it cannot teleport; errors and
  "Going to ..." show in a line at the top. BngInput fields (`v-bng-text-input`, so typing does not
  drive the car), BngButton text buttons, no glyphs, no ::before / ::after. The 2 s poll also
  refreshes the lists (`uiPlaces`), so a teleport from the console or the level start shows up.
