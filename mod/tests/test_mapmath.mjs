// node mod/tests/test_mapmath.mjs  (mapmath.js: the world map's stand-in tiles while zooming)
import * as mm from "../beamng_earth/ui/ui-vue/mods/beamngEarth/mapmath.js"
import assert from "node:assert"
const tiles = mm.visibleTiles(39.28, -76.59, 10.5, 1280, 720)
const z = tiles[0].z
// nothing ready: no stand-ins
assert.deepEqual(mm.fallbackTiles(tiles, new Set()), [])
// every parent at z-1 ready: each missing tile covered by its parent, scaled 2x, placed over it
const ready = new Set(tiles.map(t => `${z - 1}/${Math.floor(t.x / 2)}/${Math.floor(t.y / 2)}`))
const fb = mm.fallbackTiles(tiles, ready)
assert.ok(fb.length > 0 && fb.every(f => f.z === z - 1 && Math.abs(f.size - tiles[0].size * 2) < 1e-9))
for (const t of tiles) {
  const p = fb.find(f => f.x === Math.floor(t.x / 2) && f.y === Math.floor(t.y / 2))
  assert.ok(p && p.left <= t.left + 1e-6 && p.top <= t.top + 1e-6 && p.left + p.size >= t.left + t.size - 1e-6, "covers it")
}
// a tile that is ready itself needs no stand-in
const one = new Set([...ready, `${z}/${tiles[0].x}/${tiles[0].y}`])
assert.ok(mm.fallbackTiles([tiles[0]], one).length === 0)
console.log("mapmath fallback ok:", fb.length, "stand-ins for", tiles.length, "tiles")
