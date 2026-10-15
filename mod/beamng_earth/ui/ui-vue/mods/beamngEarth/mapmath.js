// Web Mercator math for the world map (EarthMap.vue). Pure functions, tested by
// server/tests/test_mapmath.mjs (node). World pixels: 256 * 2^z across, x east, y south.

export const TILE = 256
export const MIN_Z = 2
export const MAX_Z = 18          // server/maptiles.py MAX_Z
export const MAX_LAT = 85.05112878

export function clampLat(lat) { return Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) }

// longitude into -180..180
export function wrapLon(lon) {
  const w = ((lon + 180) % 360 + 360) % 360 - 180
  return w === -180 && lon > 0 ? 180 : w
}

// lat/lon -> world pixels at zoom z (fractional z allowed)
export function project(lat, lon, z) {
  const s = TILE * Math.pow(2, z)
  const phi = clampLat(lat) * Math.PI / 180
  return {
    x: (lon + 180) / 360 * s,
    y: (1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2 * s,
  }
}

// world pixels at zoom z -> lat/lon
export function unproject(x, y, z) {
  const s = TILE * Math.pow(2, z)
  const n = Math.PI - 2 * Math.PI * y / s
  return { lat: 180 / Math.PI * Math.atan(Math.sinh(n)), lon: wrapLon(x / s * 360 - 180) }
}

// The tiles covering a w x h view centred on (lat, lon) at fractional zoom z: the integer tile zoom
// tz = floor(z) and each tile's screen rect (scaled by 2^(z - tz)). x wraps around the world; rows
// outside it are left out.
export function visibleTiles(lat, lon, z, w, h) {
  const tz = Math.max(0, Math.floor(z))
  const scale = Math.pow(2, z - tz)
  const c = project(lat, lon, tz)
  const size = TILE * scale
  const left = c.x - w / 2 / scale
  const top = c.y - h / 2 / scale
  const n = 1 << tz
  const x0 = Math.floor(left / TILE), x1 = Math.floor((left + w / scale) / TILE)
  const y0 = Math.max(0, Math.floor(top / TILE)), y1 = Math.min(n - 1, Math.floor((top + h / scale) / TILE))
  const out = []
  for (let ty = y0; ty <= y1; ty++) {
    for (let tx = x0; tx <= x1; tx++) {
      out.push({
        z: tz, x: ((tx % n) + n) % n, y: ty, ux: tx,
        left: (tx * TILE - left) * scale, top: (ty * TILE - top) * scale, size,
        key: `${tz}/${tx}/${ty}`,
      })
    }
  }
  return out
}

// Screen position (px from the view's top left) of a lat/lon in a w x h view centred on (clat, clon).
// Longitudes take the shortest way round, so a marker near the antimeridian is drawn next to the centre.
export function toScreen(lat, lon, clat, clon, z, w, h) {
  const s = TILE * Math.pow(2, z)
  const p = project(lat, lon, z), c = project(clat, clon, z)
  let dx = p.x - c.x
  if (dx > s / 2) dx -= s
  if (dx < -s / 2) dx += s
  return { x: w / 2 + dx, y: h / 2 + (p.y - c.y) }
}

// lat/lon under a screen position in the same view
export function fromScreen(sx, sy, clat, clon, z, w, h) {
  const c = project(clat, clon, z)
  const r = unproject(c.x + sx - w / 2, c.y + sy - h / 2, z)
  return { lat: clampLat(r.lat), lon: r.lon }
}

// The view after panning by (dx, dy) screen px (drag right: the map moves right, the centre west)
export function pan(clat, clon, z, dx, dy) {
  const c = project(clat, clon, z)
  const r = unproject(c.x - dx, c.y - dy, z)
  return { lat: clampLat(r.lat), lon: r.lon }
}

// Zoom to z2 keeping the point under the screen position (sx, sy) where it is (mouse-wheel zoom).
export function zoomAt(clat, clon, z, z2, sx, sy, w, h) {
  z2 = Math.max(MIN_Z, Math.min(MAX_Z, z2))
  const p = fromScreen(sx, sy, clat, clon, z, w, h)
  const pp = project(p.lat, p.lon, z2)
  const r = unproject(pp.x - (sx - w / 2), pp.y - (sy - h / 2), z2)
  return { lat: clampLat(r.lat), lon: r.lon, z: z2 }
}

// Places whose name contains every word of the query (case-insensitive), best (name starts with
// the query) first; at most `limit`.
export function searchPlaces(list, query, limit = 8) {
  const q = String(query || "").trim().toLowerCase()
  if (!q) return []
  const words = q.split(/\s+/)
  const hits = (list || []).filter(p => {
    const n = String(p.name || "").toLowerCase()
    return words.every(wd => n.includes(wd))
  })
  hits.sort((a, b) => {
    const sa = String(a.name).toLowerCase().startsWith(q) ? 0 : 1
    const sb = String(b.name).toLowerCase().startsWith(q) ? 0 : 1
    return sa - sb
  })
  return hits.slice(0, limit)
}

export function fmtLatLon(lat, lon) {
  return `${Math.abs(lat).toFixed(5)}°${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(5)}°${lon >= 0 ? "E" : "W"}`
}

// Stand-ins for tiles not loaded yet (the user, 2026-10-14: "map tiles disappear as you zoom in and out ... scale the
// resolution we already have loaded and replace it once the up to date resolution gets loaded"): for each tile of
// `tiles` (visibleTiles) not in ready (a Set of "z/x/y"), the nearest loaded ancestor up to `levels` zooms coarser,
// placed and scaled to cover it. Each ancestor once; coarsest first (drawn under the finer ones).
export function fallbackTiles(tiles, ready, levels = 6) {
  const out = new Map()
  for (const t of tiles) {
    if (ready.has(`${t.z}/${t.x}/${t.y}`)) continue
    for (let d = 1; d <= levels && t.z - d >= 0; d++) {
      const pz = t.z - d, f = 1 << d, n = 1 << pz
      const ax = Math.floor(t.ux / f), ay = Math.floor(t.y / f)
      const axw = ((ax % n) + n) % n
      if (!ready.has(`${pz}/${axw}/${ay}`)) continue
      const key = `f${pz}/${ax}/${ay}`
      if (!out.has(key)) {
        out.set(key, { z: pz, x: axw, y: ay, key, size: t.size * f,
          left: t.left - (t.ux - ax * f) * t.size, top: t.top - (t.y - ay * f) * t.size })
      }
      break
    }
  }
  return [...out.values()].sort((a, b) => a.z - b.z)
}
