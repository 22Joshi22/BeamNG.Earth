# Changelog

What changed in each release of BeamNG.Earth, newest first.

## 1.5.1 beta (2026-10-19)

Works with BeamNG.drive 0.39.4.

### Buildings
- **Reduced the chance for buildings to spawn floating.** Buildings far from where the map was last centred were
  placed without the curve of the Earth, so they could float by several metres (about 5 m at 8 km, up to 18 m at
  15 km). They now use the same ground height as the terrain.
- **Reduced the chance of gaps under distant buildings.** Their walls now reach a few metres below the ground,
  which hides gaps where the distant terrain is less detailed.

### Update notice
- **The "new version available" notice now stays on screen** in the top-left corner until you choose **Open
  releases page** or **Dismiss**. Dismiss hides it until the next version comes out.
- **Open releases page should now open your browser on Linux too.** The world server opens it, because the game's
  own browser call does nothing in the native Linux version.

### Game updates
- **The launcher now tells you when a game update has turned your mods off**, and how to turn BeamNG.Earth back
  on.
- **The Linux grass patch (gcpatch) is more likely to keep working after a game update.** It can now find its place
  in a new game version on its own.

### Known issues
- Same as 1.5.0: bridges are not perfect yet, high-speed driving still stutters while the world loads, and long
  sessions get more stuttery until you restart the game.

## 1.5.0 (2026-10-18)

The first release out of beta. Big themes: real bridges and tunnels, smoother driving, and credits for every data
source.

### Bridges
- **Real bridge types.** Truss, arch, suspension, cable-stayed and movable bridges are built with their real
  structure, from the US National Bridge Inventory (FHWA) and Wikidata (worldwide). Examples: the Queensboro, the
  Golden Gate, Sydney Harbour, the Rainbow Bridge, Tsing Ma, Tower Bridge.
- **Real heights and shapes.** Deck heights use the inventory's clearance over shipping channels. Decks no longer
  sag between supports. Anchor piers stand at the ends. Twin carriageways of one bridge sit at the same height.
  Double-deck bridges keep the right gap between decks.
- **Painted bridges** in their mapped colour (Golden Gate red, for example). Mapped towers and walkways are drawn as
  part of the bridge, not as buildings with windows.
- **Clearer roads on bridges.** Roads pass through bridge towers and gate buildings. Machine-detected "buildings" on
  bridges and on water (often barges or decks) are removed.
- Ramps start flush with their intersections.

### Tunnels
- **Mapped tunnels follow what's over them.** Depending on that, a tunnel becomes open road, a low stone arch or a
  full tube.
- **Inside.** Lamps on the walls and a darker inner wall.
- **At the mouths.** The portal faces reach the ground. Buildings over tunnels sit above the road. Dark gaps at
  arch mouths are fixed.
- Central Park's sunken roads and park bridges.
- Faster loading near long tunnels (for example the Gotthard).

### Streets and cities
- Street trees along sidewalks, medians on divided roads, and paved plazas and squares.
- Parking garages you can drive into, with ramps.
- Stoops and front steps start from the ground at the door.
- Buildings use their mapped wall material (stone, brick, glass).
- One broken white centre line outside the Americas.
- Props were mirrored (doors backwards on some walls); fixed.
- Rocks and plants sit on the graded ground beside roads.
- Italian orthophotos. Bare scree instead of false snow on mountain passes.

### Performance
- **Smaller hitch each time the terrain moves under you.** Each terrain block now loads only the ground materials it
  uses. In tests the stall dropped from about 300–440 ms to under 100 ms.
- **Buildings have a level of detail.** About a third of a mile away they switch to a simpler version: no window
  bays, roof clutter or steps.
- **What you're looking at loads first.** Road, tree and building tiles in front of the camera are built before
  the ones behind you.
- **Faster world building.** Road tiles build faster, and more of the server's terrain work runs in Rust.
- **Disk space fix.** The game's terrain texture cache no longer grows by about 550 MB a day; a session's entries
  are removed with it. Old entries from earlier versions stay in `temp/art/terrainmaterialcache` until you delete
  them.

### Gameplay
- **Dropping the car far away with the free camera is safe now.** The game pauses, with a banner saying it's waiting
  for collision to load, until the ground under the car has loaded. Then it resumes, instead of the car falling
  through the world.
- **At the edge of the loaded ground the game pauses instead of braking the car.** This happens when the server can't
  move the terrain (for example if it has stopped). It resumes once the ground follows; you can also resume yourself
  and drive back.
- **Credits show every data source** used around you: imagery, elevation, map data (OpenStreetMap, Overture,
  Microsoft, TomTom, and others), land cover, trees, sea depth, bridges, airports, gravity and weather. Each has a
  short name; the full wording stays on the settings page.
- The connection banner says whether the world server or a data provider is the problem.
- The launcher window is easier to read.
- The Windows .exe triggers fewer antivirus false positives.

### Known issues
- Bridges are much better but not perfect yet.
- Houses and buildings float above the ground in many parts of the world.
- High-speed driving still stutters while the world loads around you.
- A long session (about an hour of driving and teleporting) gets steadily more stuttery. Restarting the game
  clears it. The cause is being investigated (GPU memory fragmentation is the main suspect).
