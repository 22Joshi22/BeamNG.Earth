# Places to test and benchmark

Spots found while driving (mostly by the project owner) that show something worth checking. They are
**benchmarks, never special cases** (docs/ROADMAP.md "Principle: improvements must generalise"):
every fix found here must come from a rule that works everywhere, and is checked here and on
the rest of the list before and after. Teleport with `beamngEarth_core.teleport(lat, lon)`.

| Place | lat, lon | What it shows | Relevant work |
|---|---|---|---|
| Fells Point, Baltimore MD | 39.2834, -76.5934 | the owner's home area: dense old city, harbour, the Fort McHenry / Harbor tunnels nearby | v1.9 cities (first test city), water, tunnels, street-level tuning |
| Mount Whitney summit | 36.5785, -118.2923 | the default start; 4416 m; 400 km line-of-sight horizon | M1h horizon, gravity (9.78671 m/s² measured in-game), sky |
| Kern River ravine (Sierra Nevada) | 36.506422, -118.402437 | strong cast shadows baked into the aerial imagery | de-shadowing |
| Monument Valley mesas | 36.7818, -110.1393 | mesas and buttes; near the stock Utah map's look; cliff faces stretched by the height field | v1.5 cliff materials, overhangs, biome reference (Utah) |
| Arizona / Utah state line, Monument Valley | 36.998218, -110.102293 | a colour seam between two imagery surveys running for miles | colour matching (seam feather) |
| Kayenta PV Solar Plant, Arizona | 36.7874, -110.2382 | a 55.3 MW solar farm (OSM way 750628044, since 2017-05): rows of panels only blurry in the imagery | energy infrastructure (ROADMAP), imagery resolution / real materials |
