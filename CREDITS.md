# Credits

BeamNG.Earth is built on open data, open-source software and the work of many people. This file
lists every data source, service, library, tool and reference the project uses or has used, with
its licence and the attribution it asks for. In game, the data credits overlay (a small text in a
screen corner, on by default; docs/DEVTOOLS.md) names the sources of what is around the player right
now, and the settings page shows it together with the full attribution line of the session.

Status: **in use** = used by the current main branch; **branch** = on a feature branch, not merged
yet; **planned** = in the roadmap, not used yet; **reference** = consulted for research or tuning
only, nothing redistributed. Licence notes marked *(verify)* must be re-checked before any release.

## Map data (downloaded at runtime by the companion server and cached on the player's disk)

| Data | Provider | Used for | Licence / terms | Attribution | Status |
|---|---|---|---|---|---|
| USGS National Map ImageryOnly (NAIP-based orthoimagery) | U.S. Geological Survey | ground imagery in the US | public domain (US federal) | "Imagery: USGS The National Map" | in use |
| Sentinel-2 cloudless 2023 | EOX IT Services GmbH (s2maps.eu), contains modified Copernicus Sentinel data 2023 | ground imagery worldwide; the colour reference of `--colormatch s2` (in the session's attribution whenever it is on, and in the on-screen credits for the textures it corrected) | CC BY-NC-SA 4.0 (non-commercial) | "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2023)" (EOX's June 2026 wording; imagery.py uses it) | in use |
| Sentinel-2 cloudless 2016 | EOX IT Services GmbH, contains modified Copernicus Sentinel data 2016 | imagery fallback; `--colormatch` reference fallback | CC BY 4.0 | as above, "...data 2016 & 2017)" | in use |
| Mapterhorn terrain tiles | Mapterhorn (aggregating many national and global elevation sources) | elevation | per source, see https://mapterhorn.com/attribution | "Elevation: Mapterhorn (see mapterhorn.com/attribution)" | in use |
| AWS Terrain Tiles (Terrarium) | Mapzen / Linux Foundation, hosted on AWS Open Data; sources incl. SRTM, GMTED2010, ETOPO1, USGS 3DEP/NED, and others | elevation fallback | per source, see https://github.com/tilezen/joerd/blob/master/docs/attribution.md | "AWS Terrain Tiles / Mapzen" + source list | in use |
| Open-Meteo forecast API | Open-Meteo.com (weather models from national weather services) | real weather, time zone | CC BY 4.0 data, free non-commercial API use | "Weather data by Open-Meteo.com" | in use |
| EGM2008 / EGM96 geopotential model | U.S. National Geospatial-Intelligence Agency (via GeographicLib data files) | real gravity incl. regional anomalies | public domain (NGA) *(verify the mirror's terms)* | "Gravity model: EGM2008, NGA" | in use |
| OpenStreetMap | OpenStreetMap contributors | roads, buildings, water, signs, places | ODbL 1.0 | "© OpenStreetMap contributors" | planned |
| Overture Maps GeoParquet, release 2026-09-23.1 (transportation, buildings, base: water, land use, infrastructure), via its STAC index | Overture Maps Foundation (from OpenStreetMap, TomTom, Microsoft, Google, Esri Community Maps, USGS 3DEP and others) | the map-data pipeline (`server/mapdata.py`): roads, buildings, water, land use, infrastructure | per theme (https://docs.overturemaps.org/attribution/, read 2026-10-11): transportation "ODbL. © OpenStreetMap contributors. Available under the Open Database License. Data from TomTom."; buildings "ODbL. © OpenStreetMap contributors. Available under the Open Database License. Esri Community Maps contributors. Available under CC BY 4.0. Global ML Building Footprints. Licensed by Microsoft under the Open Database License. Google Open Buildings. Available under CC BY 4.0. USGS 3D Elevation Program Digital Elevation Program. Qian Shi, et al. A First High-quality Vector Data of Buildings in East Asian Countries Based on a Comprehensive Large-scale Mapping Framework. Zenodo, 22 July 2023, doi:10.5281/zenodo.8174931. Available under CC BY 4.0. Work derived from BTN 2024 ign.es. Available under CC BY 4.0"; base "ODbL. © OpenStreetMap contributors. Available under the Open Database License. Data from the Daylight Map Distribution. ESA WorldCover. © ESA WorldCover project 2020 / Contains modified Copernicus Sentinel data (2020) processed by ESA WorldCover consortium. Available under CC BY 4.0 DEED. Data products from ETOPO1. Available under Open Data Commons Public Domain Dedication and License. Data from GLOBathy. Available under CC0 1.0 (assumed)." (`overture.THEME_LICENCES`, whole). Each cell's provenance lists the source datasets and licences of its features; the CC BY ones (Esri Community Maps, Google Open Buildings, the East Asia buildings, BTN) need their credit in the overlay where they are shown (`CellData.provenance` names them). The local cache is a derivative database kept on the player's disk, not redistributed | "© OpenStreetMap contributors, Overture Maps Foundation" (Overture's own wording) | branch (feature/mapdata) |
| ESA WorldCover 10 m 2021 v200 (`esa-worldcover` S3 COGs) | European Space Agency / WorldCover consortium (VITO) | land cover per cell (`server/mapdata.py`); planned: surface types, ground materials | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/; the files' own metadata: "CC-BY 4.0") | "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium" (the files' copyright line) | branch (feature/mapdata) |
| Meta / WRI High Resolution Canopy Height Maps v2 (CHMv2, 1.19 m, `dataforgood-fb-data` S3 COGs) | Meta and World Resources Institute | canopy height per cell (`server/mapdata.py`); planned: tree placement | CC BY 4.0 (https://registry.opendata.aws/dataforgood-fb-forests/) | "Canopy height: Meta & WRI" in the overlay; full: "Canopy height: Meta and World Resources Institute (WRI), High Resolution Canopy Height Maps" (the registry's citation: "Meta and World Resources Institude (WRI) - 2024. High Resolution Canopy Height Maps (CHM). Source imagery for CHM © 2016 Maxar.", sic, written for v1) | branch (feature/mapdata) |
| NLCD | U.S. Geological Survey / MRLC | US land cover detail | public domain | "NLCD, USGS/MRLC" | planned |
| GEBCO grid | GEBCO Compilation Group | ocean and lake depths | public domain with attribution | "GEBCO Compilation Group (year) GEBCO Grid" | planned |
| NOAA coastal DEMs / Great Lakes bathymetry | NOAA NCEI | coastal and lake depths | public domain | "NOAA NCEI" | planned |
| National / state orthophotos (e.g. swisstopo, IGN, PNOA, PDOK, MD iMAP) | the respective agencies | sharper imagery outside / inside the US | per source (see docs/research/2026-10-10-imagery-sources-world.md) | per source | planned |
| OSRM routing (FOSSGIS / project-osrm demo) | FOSSGIS e.V. / Project OSRM, on OpenStreetMap data | long-distance GPS | service terms (light, non-commercial use); data ODbL | "Routing: OSRM, © OpenStreetMap contributors" | planned |
| Photon / Nominatim geocoding | Komoot (Photon), OSM Foundation (Nominatim) | place search | service usage policies; data ODbL | "Search: Photon / Nominatim, © OpenStreetMap contributors" | planned |
| 3D BAG (LoD 2.2 buildings, Netherlands) | 3DBAG (TU Delft 3D geoinformation group) | building shapes and roofs | CC BY 4.0 | "3DBAG by tudelft3d and 3DGI" | planned |
| German state LoD2 building models (NRW, Bavaria, Berlin, ...) | the state survey offices | building shapes and roofs | DL-DE Zero / DL-DE BY 2.0 / CC BY 4.0 per state *(verify Bavaria: CC BY vs BY-ND)* | per state | planned |
| swissBUILDINGS3D | swisstopo | building shapes and roofs | open (swisstopo OGD terms) | "© swisstopo" | planned |
| PLATEAU 3D city models | Japan MLIT | building shapes and roofs | PLATEAU terms (CC BY 4.0 compatible) | "PLATEAU (MLIT)" | planned |
| DC Buildings 3D 2024 | DC Office of the CTO | benchmark near Baltimore | CC BY 4.0 | "DC OCTO" | planned (benchmark) |
| USGS 3DEP lidar point clouds | U.S. Geological Survey | building heights/roofs and canopy in the US | public domain | "USGS 3DEP" | planned |
| LiDAR HD | IGN France | buildings, canopy heights | Etalab Open Licence 2.0 | "IGN - LiDAR HD" | planned |
| Mapillary street-level imagery | Mapillary (Meta) | tuning the city generator against real photos | CC BY-SA 4.0, free API token | "Street-level imagery: Mapillary" | planned (reference) |

## Software and libraries

| Software | Used for | Licence | Status |
|---|---|---|---|
| BeamNG.drive (BeamNG GmbH) | the game; its Lua API, engine, built-in MCP server, and stock assets referenced at runtime (never redistributed) | proprietary game; this is an unofficial fan mod | in use |
| Python 3 | companion server and tools | PSF License | in use |
| NumPy | terrain, mesh and imagery processing | BSD-3-Clause | in use |
| Pillow | image decoding and encoding | MIT-CMU (HPND) | in use |
| LuaJIT | offline tests of the mod's Lua | MIT | in use (tests) |
| GeographicLib (data files) | gravity model data | MIT (library) / NGA data public domain | in use |
| Leaflet | world map page | BSD-2-Clause | planned |
| DuckDB | reading Overture GeoParquet (`server/overture.py`; no extensions) | MIT | branch (feature/mapdata) |
| fsspec | the filesystem interface DuckDB reads the remote files through (`server/rangeread.py`) | BSD-3-Clause | branch (feature/mapdata) |
| Git, GitHub, GitHub CLI | version control and hosting | GPL-2.0 (git) / MIT (gh) | in use |

## Tools

- **Claude Code** (Anthropic): the AI coding assistant that designed, wrote and tested most of the code
  and research in this repository together with the project owner.
- BeamNG's built-in MCP server (Options > Advanced) for in-game probes and automated test drives.

## References and inspiration (consulted, nothing redistributed)

- BeamNG's official levels (West Coast USA, Johnson Valley, Utah, Italy, East Coast USA, Jungle Rock
  Island) as look references and stock-asset sources per biome (docs/ROADMAP.md).
- Torque3D (open-source engine BeamNG derives from) source code and docs, for engine behaviour.
- Sebastien Hillaire, "A Scalable and Production Ready Sky and Atmosphere Rendering Technique" (2020),
  the model behind the game's 0.39 sky (for the horizon matching work).
- Open-source projects studied for generation methods: OSM2World, Blosm (blender-osm), Streets GL,
  vts-geospatial, OSRM, Valhalla.
- Popular off-road and rock-crawling trails and their public photos/videos as tuning references for
  terrain and surface generation (e.g. Moab's trails, the Rubicon Trail, Johnson Valley's Hammers);
  see docs/ROADMAP.md. Only used to judge the look; no imagery from them is shipped.
- NAIP flight specifications (sun elevation rules) for the de-shadowing sun fit.
- BeamNG maps built from real lidar, as drive-and-compare references only: "Pikes Peak - LIDAR Edition"
  and "Road Atlanta - LIDAR Edition" (Ewanc), "Mount Coot-tha" (B25Mitch), "Sangerhausen"
  (ElTacoLoco), "Roane County" (bob.blunderton).
- "Polish Roads" by mcct (BeamNG resource 5846, summer and winter versions) and MuYe's video "They
  Added Seasons To BeamNG": visual reference for winter roads, snow, plowing and road surface quality
  by road use. Its assets are not used (the author does not allow reuse).
- The project owner's own knowledge of Fells Point, Baltimore, and their in-game reports.
- Photo benchmark (docs/PLACES.md, docs/benchmarks.json `whitney_photo_south`): "A346, from summit
  of Mount Whitney, California, USA, 2011" by Brian W. Schaller, Wikimedia Commons, CC BY-NC-SA 4.0
  / Free Art License 1.3. Linked, never copied into the repository; only our own renders of the same
  view are kept.
- Z. Wang, A. C. Bovik, H. R. Sheikh, E. P. Simoncelli, "Image quality assessment: from error
  visibility to structural similarity", IEEE Trans. Image Processing 13(4), 2004: the SSIM measure
  `tools/benchmark.py compare` uses (own NumPy implementation).

## User mods mentioned in the docs (not part of BeamNG.Earth)

BWM_Weather, TAA Standalone, PerformanceTweaker, Enhanced Vehicle FX, Realistic Driver Camera,
Dynamic Damage Particles, Dynamic Scratches, SparkMod: other authors' mods the project owner uses;
BeamNG.Earth only coexists with them (e.g. the weather module backs off when BWM_Weather drives the
weather). The pause-menu integration follows the pattern of the TAA Standalone mod.
