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
  (placeholder), sky mode, clouds, HUD, dev radii in `/settings/beamngEarth.json`; every default
  is today's behaviour, the view distance is applied only in our level and only when it is not
  the default. The dev overlays stay per session.
- HUD: once the page exists (index.js says so, or the pause menu lists the button) the imgui
  window is a compact status HUD, hidden at level start unless "Status HUD" is on; "Full window"
  brings the old window back for the session. Without the page nothing changes.
