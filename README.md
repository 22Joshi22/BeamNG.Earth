# BeamNG.Earth

A 1:1 scale, streamed Earth for [BeamNG.drive](https://www.beamng.com/): drive anywhere on the
planet on real elevation data and satellite imagery, with distance-based level of detail and the
real curvature of the Earth.

Status: early development (v1.3.1). Tested on BeamNG.drive 0.39.4 on Linux (Vulkan). See
[docs/ROADMAP.md](docs/ROADMAP.md) for what is coming (high-speed streaming, real view distance,
roads, water, navigation, traffic, cities).

## How it works
- A small **companion server** (`server/`, Python) downloads elevation and imagery, builds terrain
  tiles in a local flat frame around a moving origin, and streams them to the game over a local
  socket. The game's Lua cannot reach the internet, so the server is required.
- The **mod** (`mod/beamng_earth/`) loads the tiles as meshes, keeps an invisible physics terrain
  under the car, moves the world origin as you drive (floating origin), and blends level-of-detail
  changes so nothing pops.
- Design and measurements: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
  [docs/V1_4_SPEC.md](docs/V1_4_SPEC.md), research notes in [docs/research/](docs/research/).

## Running it (developer setup)
1. Python 3.11+ with `numpy` and `Pillow`.
2. Copy (or sync) `mod/beamng_earth/` to
   `<BeamNG user folder>/current/mods/unpacked/beamng_earth/`.
3. Start the server: `python3 server/earthd.py`
4. Start BeamNG.drive and load the **BeamNG.Earth** level. The mod connects to the server and
   streams the world; the BeamNG.Earth window shows the status and lets you teleport.

Tests: `cd server && python3 -m unittest discover -s tests -p "test_*.py"` and
`cd mod/tests && for f in test_*.lua; do luajit $f; done`.

## Data sources and attribution
The repository contains only code; all map data is downloaded at runtime and cached locally.
- Imagery: USGS The National Map (public domain); Sentinel-2 cloudless by EOX IT Services GmbH
  (https://s2maps.eu, contains modified Copernicus Sentinel data; 2016 release CC BY 4.0, 2023
  release CC BY-NC-SA 4.0, non-commercial).
- Elevation: Mapterhorn (see https://mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen.
Each source's terms apply to the downloaded data; the in-game window shows the attribution line.
The full list of data sources, services, libraries, tools and references, with licences and the
credit each one asks for, is in [CREDITS.md](CREDITS.md).

## License
Code: MIT, see [LICENSE](LICENSE). BeamNG.drive is a trademark of BeamNG GmbH; this is an unofficial
fan project and contains no BeamNG assets.
