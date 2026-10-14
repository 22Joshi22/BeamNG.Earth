// The world map's tile math (mod ui/ui-vue/mods/beamngEarth/mapmath.js). Run: node server/tests/test_mapmath.mjs
// (server/tests/test_maptiles.py runs it too when node is installed).
import assert from "node:assert/strict"
import * as m from "../../mod/beamng_earth/ui/ui-vue/mods/beamngEarth/mapmath.js"

const near = (a, b, eps, what) => assert.ok(Math.abs(a - b) <= eps, `${what}: ${a} vs ${b}`)

// known tile: Fells Point, Baltimore (39.2826, -76.5916) at z15 is tile 9412/12490 (the OSM slippy-map formula, asinh(tan))
{
  const p = m.project(39.2826, -76.5916, 15)
  assert.equal(Math.floor(p.x / 256), 9412)
  assert.equal(Math.floor(p.y / 256), 12490)
}
// the world corners and the equator
{
  const a = m.project(0, 0, 0)
  near(a.x, 128, 1e-9, "x of 0,0"); near(a.y, 128, 1e-9, "y of 0,0")
  const b = m.project(m.MAX_LAT, -180, 1)
  near(b.x, 0, 1e-9, "west edge"); near(b.y, 0, 1e-3, "north edge")
}
// round trips
for (const [lat, lon] of [[39.2826, -76.5916], [-33.86, 151.21], [64.1, -21.9], [0, 179.99], [-85, -179.99]]) {
  for (const z of [0, 3.5, 12, 18]) {
    const p = m.project(lat, lon, z)
    const r = m.unproject(p.x, p.y, z)
    near(r.lat, lat, 1e-7, `lat round trip ${lat},${lon} z${z}`)
    near(r.lon, lon, 1e-7, `lon round trip ${lat},${lon} z${z}`)
  }
}
// screen <-> lat/lon in a view; the centre maps to the screen centre
{
  const v = [39.28, -76.59, 13.3, 1600, 900]
  const c = m.toScreen(39.28, -76.59, ...v)
  near(c.x, 800, 1e-6, "centre x"); near(c.y, 450, 1e-6, "centre y")
  const p = m.fromScreen(123, 456, ...v)
  const s = m.toScreen(p.lat, p.lon, ...v)
  near(s.x, 123, 1e-6, "screen round trip x"); near(s.y, 456, 1e-6, "screen round trip y")
  // north is up, east is right
  assert.ok(m.toScreen(39.3, -76.59, ...v).y < 450)
  assert.ok(m.toScreen(39.28, -76.5, ...v).x > 800)
}
// the antimeridian: a marker just east of it shows right of a centre just west of it
{
  const s = m.toScreen(0, -179.9, 0, 179.9, 10, 1000, 1000)
  assert.ok(s.x > 500 && s.x < 1000, `antimeridian marker at ${s.x}`)
  assert.equal(m.wrapLon(190), -170); assert.equal(m.wrapLon(-190), 170); assert.equal(m.wrapLon(180), 180)
}
// pan: dragging right moves the centre west; zoomAt keeps the point under the cursor
{
  const p = m.pan(10, 10, 8, 100, 0)
  assert.ok(p.lon < 10)
  near(p.lat, 10, 1e-9, "pan keeps lat")
  const before = m.fromScreen(300, 200, 40, -75, 9, 1200, 800)
  const z = m.zoomAt(40, -75, 9, 11.5, 300, 200, 1200, 800)
  const after = m.fromScreen(300, 200, z.lat, z.lon, z.z, 1200, 800)
  near(after.lat, before.lat, 1e-7, "zoomAt lat"); near(after.lon, before.lon, 1e-7, "zoomAt lon")
  assert.equal(m.zoomAt(0, 0, 5, 99, 0, 0, 10, 10).z, m.MAX_Z)
  assert.equal(m.zoomAt(0, 0, 5, -3, 0, 0, 10, 10).z, m.MIN_Z)
}
// visible tiles cover the view exactly once, wrap x, and drop rows outside the world
{
  const w = 1000, h = 700
  const tiles = m.visibleTiles(39.28, -76.59, 12.4, w, h)
  assert.ok(tiles.length > 0)
  for (const t of tiles) {
    assert.equal(t.z, 12)
    assert.ok(t.x >= 0 && t.x < 4096 && t.y >= 0 && t.y < 4096)
    assert.ok(t.left < w && t.left + t.size > 0 && t.top < h && t.top + t.size > 0, `tile ${t.key} off screen`)
  }
  // every screen pixel sampled is in exactly one tile
  for (const [sx, sy] of [[0, 0], [w - 1, h - 1], [500, 350], [3, 690]]) {
    const n = tiles.filter(t => sx >= t.left && sx < t.left + t.size && sy >= t.top && sy < t.top + t.size).length
    assert.equal(n, 1, `pixel ${sx},${sy}`)
  }
  const world = m.visibleTiles(0, 179, 2, 2000, 3000)
  assert.ok(world.every(t => t.y >= 0 && t.y < 4 && t.x >= 0 && t.x < 4))
  assert.ok(new Set(world.map(t => t.key)).size === world.length)
}
// search: every word, case-insensitive, prefix matches first
{
  const list = [{ name: "Fells Point" }, { name: "Point Reyes" }, { name: "Mount Whitney" }, { name: "Baltimore Point" }]
  assert.deepEqual(m.searchPlaces(list, "point").map(p => p.name), ["Point Reyes", "Fells Point", "Baltimore Point"])
  assert.deepEqual(m.searchPlaces(list, "fells POINT").map(p => p.name), ["Fells Point"])
  assert.deepEqual(m.searchPlaces(list, "  "), [])
  assert.deepEqual(m.searchPlaces(null, "x"), [])
}
assert.equal(m.fmtLatLon(39.28, -76.59), "39.28000°N, 76.59000°W")
console.log("mapmath ok")
