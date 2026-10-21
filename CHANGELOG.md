# Changelog

What changed in each release of BeamNG.Earth, newest first.

## 1.5.4 beta (2026-10-21)

Works with BeamNG.drive 0.40.

### Level select
- **A new level select.** BeamNG.Earth's card in the freeroam menu shows the whole Earth with its details, and lists
  every saved place as a starting location with its own aerial picture. Places you add, rename or delete show up
  there by themselves.
- **Random location** starts you on a road somewhere in the world.
- **The default places start you on a road.** Some of them used to start you inside a building or on a steep slope.
  Pécs, Hungary is a new default place, and the Kayenta solar plant is no longer one (it stays in your list if you
  have it).

### World map
- **Controller support, laid out like the stock map.** The right stick moves the map, the triggers zoom, A sets the
  GPS marker (press it again to clear it), X teleports, Y turns autodrive on or off and B goes back.
- **Reduced the chance of the car moving while you use the map.** The sticks and triggers no longer drive the car
  behind the map.
- **The credits box no longer covers the Places panel** or the game's info bar.

### Steam Deck and smaller computers
- **BeamNG.Earth can now run on a Steam Deck**, in Game Mode too. The launcher starts the game through Steam's
  runtime, and on computers with less than 20 GB of memory the world server uses smaller caches and fewer helper
  processes.
- **The world should build much faster on a Steam Deck.** In testing at Baltimore, the ground under the car was ready
  about 8 s after a teleport (before: 17.5 s), and everything around it in about 53 s (before: 85 s).
- **Reduced the chance of "Near the edge of the loaded ground" warnings on a Steam Deck.** Each new piece of ground's
  aerial photo is now made by a separate helper process, so the next piece is not held up behind the rest of the
  server's work.
- **The world server uses much less memory.** Street widths, map data and elevation tiles are now shared between its
  processes instead of each keeping its own copy (in testing with the Steam Deck settings: about 1.7 GB, before:
  4.6 GB).

### Driving and loading
- **The world now loads around the camera, not just the car.** With the free camera or a far chase camera, the
  ground, roads, buildings and trees load where you are looking, while collision stays around your car.
- **Roads ahead should load first at high speed** (GitHub #50). Above about 55 mph, road pieces along your path
  come before those beside or behind you.
- **Much smoother driving through dense forest** (GitHub #66). Each piece of the world now shows at most a few
  thousand trees (in testing near Bodø, Norway on a Steam Deck: 1.66 million trees down to 142,000, and about
  32-36 fps instead of 20 while driving).
- **Reduced the chance of a hitch the first time you reach a tunnel, a bridge or a new kind of road or building.**
  The game now sets up BeamNG.Earth's road and building materials while the map loads.
- **The world server uses less of your CPU while driving.** In testing on a desktop, a 4-minute city drive used about
  0.6 of a CPU core on average (before: 1.6).
- **Teleports should finish a little sooner.** In testing, Baltimore was fully loaded about 25 % sooner.
- **Faster loading in open country with very long straight roads**, such as the Bonneville Salt Flats.

### Bridges
- **Reduced the chance of driving into the ground at the end of a bridge.** A straight bridge whose deck rises toward
  its middle could be drawn flat, below the road it meets at the far end, and a deck meeting a steep embankment could
  end in a step.
- **Reduced the chance of falling through a bridge that loads late.** A bridge deck that appeared as you drove up to
  it was kept from turning solid until you had passed it.
- **Reduced the chance of hitting a low overpass** (GitHub #51). The roads leading onto a short bridge over a highway
  are now raised on gentle approach ramps, so the bridge clears the lanes (from KyzoFox's PR #64; in testing, the
  bridge over I-20 near Temple, GA went from about 4.5 m over the lanes to about 10 m).
- **Reduced the chance of a step where a road meets a bridge** whose two ends are at different heights.

### Roads and ground
- **Road surfaces now follow the ground's own shape**, and underpasses no longer block the road above them.
- **Reduced the chance of medians and traffic islands catching a wheel** (GitHub #55). Narrow curbed strips no longer
  fold back over themselves in their collision.
- **Rural motorways** have two lanes each way between paved shoulders (GitHub #43).
- **A bare verge beside roads without sidewalks**, and roads are painted centred and at least their full width.
- **Salt flats, playas and dry lake beds are bare now.** Rocks lie only where the ground slopes, and trees misread
  from bright ground are left out.
- **Fewer cars and trucks drawn as buildings** (GitHub #80). Machine-learned building outlines on public roads are
  left out.
- **Reduced the chance of buildings floating over driveways and loading docks** (GitHub #60: a church in Oklahoma
  City floated 10 m up).

### From contributors (PR #41 by Madderose, PR #64 by KyzoFox)
- **Reduced the chance of a trailer coming off** when the world re-centres around you.
- **Faster handling of large bursts of messages** from the world server, and fewer road pieces casting shadows.
- **Tiles without roads are no longer rebuilt again and again.**

### Other fixes
- **Linux: the launcher no longer mistakes Steam Linux Runtime for Proton** (GitHub #56). With that compatibility
  setting the mod was put into an old Proton folder the game no longer used.
- **Reduced the chance of terrain failing to load on Windows** when the world server's cache files are in use by
  another of its processes (GitHub #67, "Access is denied").
- **The mod now stays out of the way on other maps**, apart from keeping its connection to the world server.
- **"Degraded" no longer shows** for services the world does not need (weather, search, routing, the update check)
  (GitHub #23).
- **Reduced the chance of the server stopping writing files** if its session folder is deleted while it runs
  (GitHub #27, #54).

If you want to talk about this mod with me or other people, join my Discord server at
[discord.gg/tgf](https://discord.gg/tgf) and head to the #beamng-earth channel.

## 1.5.3 beta (2026-10-19)

Works with BeamNG.drive 0.40.

### High-speed driving
- **Reduced the chance of driving onto bridges and into buildings before they have collision at high speed.** Their
  collision is now made solid as soon as the car is a few seconds away instead of waiting for the next periodic
  rebuild. In testing at about 185 mph, collision was ready at least 480 m ahead of the car (before: as little as
  32 m).
- **Road surfaces should keep up better at high speed.** Above about 55 mph the world server builds the road tiles
  along your path, up to 14 seconds of driving ahead, in the order you will reach them. Things beside and behind you
  come later.
- **The ground under the car now comes first.** The terrain is built ahead of road tiles and far-away scenery when the
  server is busy.
- **Reduced the chance of falling off the edge of the loaded ground.** If the ground ahead is ever late (a slow first
  visit, a busy computer), the game now pauses at the edge with "Waiting for collision to load here" instead of letting
  the car drive off, and carries on by itself once it has loaded.

### Teleporting (drop the car at the camera)
- **Reduced the chance of falling through bridges and buildings after a teleport.** The pause now also waits for the
  bridges, tunnels, sidewalks and buildings around the car to have collision, then sets the car down on them.
- **Teleports to places that are already partly loaded pause too** if their bridges and buildings are not ready yet.
- **Dropping the car under a bridge** now lands it on the road below instead of on the bridge deck.

### Traffic and streets (thanks to hashk014, PR #35)
- **Roundabouts:** traffic now drives one way round in the right direction (the mapped one, else your country's
  driving side), cars on the roundabout have priority, and entries get give-way signs.
- **Traffic slows down for tight bends and hairpins**, braking in time, and follows speed limits that change partway
  along a road. Straight roads keep their posted limit.
- **Narrow old-town streets** between close buildings are drawn as narrow as 3 m wide with narrower sidewalks,
  instead of a wider default that ran into the buildings. Traffic takes narrow two-way streets more slowly.

### BeamNG.drive 0.40
- **Shingle roofs and runway markings should show their textures again.** 0.40 moved some of the game textures
  BeamNG.Earth uses.

### Installing from source (thanks to hashk014, PR #34)
- **Clearer help on Windows when a Python package cannot be installed.** With a Python too new for DuckDB's
  ready-made package, the installer now stops at once and explains how to use 64-bit Python 3.12 or the standalone
  .exe instead of failing after a long build and suggesting a network problem. Only affects installing from the source
  code; the .exe is unchanged.

### Other fixes
- **Grass should come back after starting the BeamNG.Earth map again in the same game session** (for example after
  going back to the main menu).

### Known issues
- Bridges are not perfect yet, and long sessions get more stuttery until you restart the game.
- A very fast drive through a dense city on a first visit can still outrun the road surfaces.

## 1.5.2 beta (2026-10-19)

Works with BeamNG.drive 0.40. Tested on 0.40.0: loading the world, driving in Baltimore, the Golden Gate, and terrain
blocks loading while you drive.

### BeamNG.drive 0.40
- **The map, welcome and feedback screens now open the way 0.40 expects.** 0.40 changed how mods switch UI
  screens; BeamNG.Earth uses the new way and falls back to the old one.
- **Parked cars follow 0.40's renamed parked-traffic setting.** 0.40 renamed the option that sets how many parked
  cars there are; BeamNG.Earth reads the new name first.
- **The Linux grass patch (gcpatch) now works on 0.40.** It finds its place in the new game version on its own, so
  grass should stay when terrain blocks move.

### Tips after updating the game to 0.40
- **0.40 turns all mods off after the update.** Turn mods back on from the main menu, then check that BeamNG.Earth
  is on under Mods > Manage.
- **0.40 may reset your units.** If the speedometer shows km/h, switch it back to mph in the game's options.

### Known issues
- Same as 1.5.0: bridges are not perfect yet, high-speed driving still stutters while the world loads, and long
  sessions get more stuttery until you restart the game.
- 0.40's new weather has not been tested together with BeamNG.Earth's real weather yet.

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
