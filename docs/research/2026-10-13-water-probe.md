# Water: first engine probe (2026-10-13)

The roadmap's water item (docs/ROADMAP.md "Water") has nothing built yet: lakes, rivers and the harbour are the aerial photo on flat ground (the DEM's water surface; Fells Point harbour reads -0.02 m in the block, land 1-3 m). The car drives on the harbour.

## What the stock levels use (game files)

- **East Coast USA:** its ocean is one `WaterPlane` ("Ocean", z 8.93).
  - `baseColor` 40 197 254.
  - `depthGradientTex` levels/east_coast_usa/art/water/depthcolor_ramp.dds.
  - `cubemap` cubemap_ocean_reflection.
  - foam, ripple and wave arrays.
- **West Coast USA and Italy:** `WaterBlock`s (boxes) for lakes and quarries, and `River` objects (node splines with width and depth) for rivers.

## In-game probe (Fells Point, runtime objects in our level)

- **`WaterPlane`** (created at runtime, `baseColor` and the stock ripple / foam / depth-ramp textures, z 2 m) **renders**.
  - It is see-through blue-green water with a sun glint over everything below 2 m (`water_2026-10-13/waterplane_2m_fells.jpg`).
  - No noticeable frame cost at that view (`RenderReflections` under 0.3 ms).
- **`WaterBlock`** (created at runtime) **does not render**.
  - `getWorldBox` is right: the position is the box's centre and the scale its size, so the surface is at position.z + scale.z / 2.
  - Nothing is drawn, even with the surface 2 m above the ground (`water_2026-10-13/waterblock_not_drawn.jpg`).
  - Tried: the `scale` field or `setScale`; `postApply`; stock grid settings (gridSize / gridElementSize 1); 200 m and 2.4 km boxes.
  - No log errors.
  - The stock definition (West Coast USA's `WaterBlock_1`, all its fields) spawned the editor's way (`Sim.deserializeObjectFromText(json, true, true)`), surface at 1 m: not drawn either.
  - The editor's own recipe (`editor/createObjectTool.lua` `buildWaterBlock`: its ripple / wave arrays, `/core/art/water/ripple_nm.normal.dds`, `depthcolor_ramp_b.color.png`, `foam_b.color.png`, `DefaultSkyCubemap`, then `reloadTextures()`): not drawn either.
  - So it is not our field values. Untested: a `WaterBlock` in our level's own items file, loaded with the level (a runtime-only problem?); a stock level with ours as the only change.

## Why a plane is not enough

A `WaterPlane` is infinite and flat in the anchor frame, but the earth curves away below it: d^2 / 2R = 0.3 m at 2 km, 7.8 m at 10 km. Coastal land lower than that in the distance would be drawn under water. Lakes need their own levels.

So the plan stays the roadmap's: bounded water per area near the player (blocks or a plane only while the player is near sea-level water), the physics block carved below the surface, and the far tiles' water left to the photo.

## Plan (to agree with the user before building)

1. **Data:** Overture `base/water` polygons and river lines near the player (the map-data pipeline has the base theme). The surface level is the DEM at the shoreline (median inside the polygon near the player).
2. **Physics:**
   - Carve the physics block under water in the server's block build, like the road grading: a shore slope to a capped depth (e.g. 1:5 to 3 m; GEBCO / NOAA CUDEM later).
   - Bridges are already decks over the ground.
3. **Rendering:**
   - Near the player, water objects at the surface level, covering the water polygons in rectangles of a coarse grid, so land lower than the water outside the shore is not flooded.
   - Needs the WaterBlock fix, or a `WaterPlane` per sea-level area while the player is within ~2 km.
4. **Buoyancy:** the engine's water objects give it (to verify with a car).
