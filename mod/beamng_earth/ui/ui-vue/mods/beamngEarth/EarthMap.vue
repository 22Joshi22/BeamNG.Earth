<template>
  <div class="earth-map" :class="{ embedded }" @keydown.esc.stop="onEscape" tabindex="-1" ref="rootEl">
    <!-- the map itself: tiles, the lat/lon grid, markers -->
    <div
      class="map-view"
      ref="viewEl"
      @mousedown="onDown"
      @wheel.prevent="onWheel"
      @dblclick.prevent="onDblClick"
    >
      <img
        v-for="t in tiles"
        :key="t.key"
        class="map-tile"
        :src="t.src"
        :style="{ left: t.left + 'px', top: t.top + 'px', width: t.size + 1 + 'px', height: t.size + 1 + 'px' }"
        draggable="false"
        @load="onTileLoad"
        @error="onTileError($event, t)"
      />
      <svg class="map-grid" :width="w" :height="h">
        <g v-for="g in grid" :key="g.key">
          <line :x1="g.x1" :y1="g.y1" :x2="g.x2" :y2="g.y2" :class="g.cls" />
          <text v-if="g.label" :x="g.lx" :y="g.ly" class="grid-label">{{ g.label }}</text>
        </g>
      </svg>
      <!-- saved places -->
      <div
        v-for="p in placeMarkers"
        :key="'p' + p.id"
        class="marker place"
        :style="{ left: p.sx + 'px', top: p.sy + 'px' }"
        @mousedown.stop
        @click.stop="askTeleport(p.lat, p.lon, p.name)"
      >
        <div class="pin"></div>
        <div class="marker-label">{{ p.name }}</div>
      </div>
      <!-- the pending target -->
      <div v-if="target" class="marker target" :style="{ left: targetScreen.x + 'px', top: targetScreen.y + 'px' }">
        <div class="cross"></div>
      </div>
      <!-- the player -->
      <div v-if="playerScreen" class="marker player" :style="{ left: playerScreen.x + 'px', top: playerScreen.y + 'px' }">
        <div class="arrow" :style="{ transform: 'rotate(' + (state.player.heading || 0) + 'deg)' }" :class="{ noheading: state.player.heading == null }"></div>
      </div>
    </div>

    <!-- top bar: title, search, buttons -->
    <div class="map-top">
      <div class="map-title">BeamNG.Earth map</div>
      <div class="search">
        <input
          ref="searchEl"
          v-model="query"
          class="search-input"
          placeholder="Search saved places, or lat, lon"
          @keydown.enter.stop.prevent="onSearch"
          @keydown.esc.stop.prevent="query = ''"
          @mousedown.stop
        />
        <div v-if="results.length" class="search-results">
          <div v-for="(r, i) in results" :key="i" class="search-result" @mousedown.stop @click="pickResult(r)">
            <span class="result-name">{{ r.name }}</span>
            <span class="result-coords">{{ fmtLatLon(r.lat, r.lon) }}</span>
          </div>
        </div>
        <div v-if="searchMsg" class="search-msg">{{ searchMsg }}</div>
      </div>
      <button class="map-btn" @click="zoomBy(1)" title="Zoom in">+</button>
      <button class="map-btn" @click="zoomBy(-1)" title="Zoom out">-</button>
      <button class="map-btn" :class="{ on: follow }" @click="centreOnMe" title="Centre on the car and follow it">Me</button>
      <button v-if="!embedded" class="map-btn close" @click="close" title="Close (Esc / Alt+M)">Close</button>
    </div>

    <!-- status: tiles unavailable / not connected -->
    <div v-if="notice" class="map-notice">{{ notice }}</div>

    <!-- the confirmation -->
    <div v-if="target" class="confirm" @mousedown.stop>
      <div class="confirm-title">Teleport here?</div>
      <div class="confirm-where">{{ target.name ? target.name + " - " : "" }}{{ fmtLatLon(target.lat, target.lon) }}</div>
      <div v-if="state.cannotGo" class="confirm-warn">{{ state.cannotGo }}</div>
      <div v-if="teleportMsg" class="confirm-warn">{{ teleportMsg }}</div>
      <div class="confirm-btns">
        <button class="map-btn primary" :disabled="!!state.cannotGo || busy" @click="teleport">Teleport</button>
        <button class="map-btn" @click="target = null; teleportMsg = ''">Cancel</button>
      </div>
    </div>

    <!-- bottom: cursor position, scale, attribution -->
    <div class="map-bottom">
      <span class="cursor-pos">{{ cursorText }}</span>
      <span class="attribution">{{ attributionText }}</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from "vue"
import * as mm from "./mapmath.js"

const props = defineProps({ embedded: { type: Boolean, default: false } })
const embedded = props.embedded

const rootEl = ref(null), viewEl = ref(null), searchEl = ref(null)
const w = ref(1280), h = ref(720)
const view = ref({ lat: 20, lon: 0, z: 3 })
const follow = ref(true)              // the view follows the car until the user pans
const state = ref({ places: [], recent: [] })
const target = ref(null)              // { lat, lon, name } waiting for "Teleport here?"
const teleportMsg = ref("")
const busy = ref(false)
const query = ref("")
const results = ref([])
const searchMsg = ref("")
const cursor = ref(null)
const tileErrors = ref(0), tileLoads = ref(0)
const seenPlayer = ref(false)
let pollTimer = null, resizeObs = null
let drag = null

function engineLua(script, cb) {
  if (!window.bngApi || !window.bngApi.engineLua) return
  window.bngApi.engineLua(script, cb)
}

// ---- state from Lua (every second) -------------------------------------------------------
function poll() {
  engineLua("beamngEarth_settings and beamngEarth_settings.uiMapState and beamngEarth_settings.uiMapState() or nil", s => {
    if (!s) { state.value = { places: [], recent: [], missing: true }; return }
    state.value = { places: [], recent: [], ...s }
    if (s.player && follow.value) {
      view.value = { ...view.value, lat: s.player.lat, lon: s.player.lon, z: seenPlayer.value ? view.value.z : 12 }
      seenPlayer.value = true
    }
  })
}

// Tiles are files the server writes into the game's temp folder on request (the game's UI browser cancels every
// http request, even to 127.0.0.1): beamngEarth_settings.mapTiles reports which are there and asks for the rest.
const TILE_DIR = "/temp/beamngEarth/maptiles/"
const readyTiles = ref(new Set())
const tileBase = computed(() => (state.value.mapPort ? TILE_DIR : null))
const wanted = computed(() => {
  if (!tileBase.value) return []
  const v = view.value
  return mm.visibleTiles(v.lat, v.lon, v.z, w.value, h.value)
})
function askTiles() {
  const want = wanted.value.filter(t => !readyTiles.value.has(`${t.z}/${t.x}/${t.y}`))
  if (!want.length) return
  const list = JSON.stringify(want.slice(0, 48).map(t => [t.z, t.x, t.y]))
  engineLua(`beamngEarth_settings and beamngEarth_settings.mapTiles and beamngEarth_settings.mapTiles(jsonDecode('${list}')) or {}`, r => {
    if (!Array.isArray(r) || !r.length) return
    const s = new Set(readyTiles.value)
    r.forEach(k => s.add(k))
    readyTiles.value = s
  })
}
let askTimer = null
watch(wanted, () => { clearTimeout(askTimer); askTimer = setTimeout(askTiles, 120) })
const askLoop = setInterval(askTiles, 1000)
onUnmounted(() => { clearInterval(askLoop); clearTimeout(askTimer) })

const tiles = computed(() => wanted.value
  .filter(t => readyTiles.value.has(`${t.z}/${t.x}/${t.y}`))
  .map(t => ({ ...t, src: `${TILE_DIR}${t.z}/${t.x}/${t.y}.jpg` })))

function onTileLoad() { tileLoads.value++ }
function onTileError(ev, t) {
  tileErrors.value++
  if (ev && ev.target) ev.target.style.visibility = "hidden"   // grey background shows; grid stays
}

const notice = computed(() => {
  const s = state.value
  if (s.missing) return "BeamNG.Earth is not loaded"
  if (!s.connected) return "Companion server not connected: map tiles unavailable (places and the grid still work)"
  if (!s.mapPort) return "Map tiles unavailable (the companion server is older or has --map-tiles off)"
  if (tileErrors.value > 8 && tileLoads.value === 0) return "Map tiles unavailable (no imagery from the server: offline?)"
  return ""
})

const attributionText = computed(() => {
  const a = state.value.attribution || ""
  return state.value.mapPort ? (a.startsWith("Imagery") ? "Map " + a.charAt(0).toLowerCase() + a.slice(1) : a) : ""
})

// ---- markers ------------------------------------------------------------------------------
function screen(lat, lon) {
  const v = view.value
  return mm.toScreen(lat, lon, v.lat, v.lon, v.z, w.value, h.value)
}
const onScreen = p => p.x > -50 && p.y > -50 && p.x < w.value + 50 && p.y < h.value + 50

const placeMarkers = computed(() =>
  (state.value.places || []).map(p => ({ ...p, ...(() => { const s = screen(p.lat, p.lon); return { sx: s.x, sy: s.y } })() }))
    .filter(p => onScreen({ x: p.sx, y: p.sy })))

const playerScreen = computed(() => {
  const p = state.value.player
  return p ? screen(p.lat, p.lon) : null
})
const targetScreen = computed(() => (target.value ? screen(target.value.lat, target.value.lon) : { x: 0, y: 0 }))

// the lat/lon grid: a step that gives lines ~120-300 px apart
const grid = computed(() => {
  const v = view.value
  const pxPerDeg = 256 * Math.pow(2, v.z) / 360
  const steps = [30, 10, 5, 2, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001]
  const step = steps.find(s => s * pxPerDeg < 300) || 0.001
  const tl = mm.fromScreen(0, 0, v.lat, v.lon, v.z, w.value, h.value)
  const br = mm.fromScreen(w.value, h.value, v.lat, v.lon, v.z, w.value, h.value)
  const out = []
  const dec = step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log10(step)))
  let lon0 = tl.lon, lon1 = br.lon
  if (lon1 < lon0) lon1 += 360
  for (let k = Math.ceil(lon0 / step); k * step <= lon1 && out.length < 200; k++) {
    const lon = mm.wrapLon(k * step)
    const x = screen(v.lat, lon).x
    out.push({ key: "x" + k, x1: x, y1: 0, x2: x, y2: h.value, cls: Math.abs(lon) < 1e-9 ? "meridian" : "",
      label: `${Math.abs(lon).toFixed(dec)}°${lon < 0 ? "W" : lon > 0 ? "E" : ""}`, lx: x + 4, ly: h.value - 34 })
  }
  for (let k = Math.ceil(br.lat / step); k * step <= tl.lat && out.length < 400; k++) {
    const lat = k * step
    const y = screen(lat, v.lon).y
    out.push({ key: "y" + k, x1: 0, y1: y, x2: w.value, y2: y, cls: Math.abs(lat) < 1e-9 ? "meridian" : "",
      label: `${Math.abs(lat).toFixed(dec)}°${lat < 0 ? "S" : lat > 0 ? "N" : ""}`, lx: 6, ly: y - 4 })
  }
  return out
})

const cursorText = computed(() => {
  const c = cursor.value
  const z = `zoom ${view.value.z.toFixed(1)}`
  return c ? `${mm.fmtLatLon(c.lat, c.lon)}   ${z}` : z
})

// ---- mouse ---------------------------------------------------------------------------------
function localXY(ev) {
  const r = viewEl.value.getBoundingClientRect()
  return { x: ev.clientX - r.left, y: ev.clientY - r.top }
}

function onDown(ev) {
  if (ev.button !== 0) return
  results.value = []
  const p = localXY(ev)
  drag = { x: p.x, y: p.y, sx: p.x, sy: p.y, moved: false }
  window.addEventListener("mousemove", onMove)
  window.addEventListener("mouseup", onUp)
}

function onHover(ev) {
  if (!viewEl.value) return
  const p = localXY(ev)
  const v = view.value
  cursor.value = mm.fromScreen(p.x, p.y, v.lat, v.lon, v.z, w.value, h.value)
}

function onMove(ev) {
  if (!drag) return
  const p = localXY(ev)
  const dx = p.x - drag.x, dy = p.y - drag.y
  if (!drag.moved && Math.hypot(p.x - drag.sx, p.y - drag.sy) < 4) return
  drag.moved = true
  follow.value = false
  const v = view.value
  const c = mm.pan(v.lat, v.lon, v.z, dx, dy)
  view.value = { ...v, lat: c.lat, lon: c.lon }
  drag.x = p.x; drag.y = p.y
}

function onUp(ev) {
  window.removeEventListener("mousemove", onMove)
  window.removeEventListener("mouseup", onUp)
  const d = drag
  drag = null
  if (d && !d.moved) {
    const p = localXY(ev)
    const v = view.value
    const ll = mm.fromScreen(p.x, p.y, v.lat, v.lon, v.z, w.value, h.value)
    askTeleport(ll.lat, ll.lon, "")
  }
}

function onWheel(ev) {
  const p = localXY(ev)
  const v = view.value
  const z2 = v.z + (ev.deltaY < 0 ? 0.5 : -0.5)
  const r = mm.zoomAt(v.lat, v.lon, v.z, z2, p.x, p.y, w.value, h.value)
  if (Math.abs(p.x - w.value / 2) > 2 || Math.abs(p.y - h.value / 2) > 2) follow.value = false
  view.value = r
}

function onDblClick(ev) {
  const p = localXY(ev)
  const v = view.value
  view.value = mm.zoomAt(v.lat, v.lon, v.z, v.z + 1, p.x, p.y, w.value, h.value)
  follow.value = false
}

function zoomBy(d) {
  const v = view.value
  view.value = { ...v, z: Math.max(mm.MIN_Z, Math.min(mm.MAX_Z, Math.round(v.z + d))) }
}

function centreOnMe() {
  follow.value = true
  const p = state.value.player
  if (p) view.value = { ...view.value, lat: p.lat, lon: p.lon, z: Math.max(view.value.z, 12) }
}

// ---- search --------------------------------------------------------------------------------
function allPlaces() {
  const s = state.value
  return [...(s.places || []), ...(s.recent || []).map(r => ({ ...r, name: r.name || r.text }))]
}

function goTo(lat, lon, name) {
  follow.value = false
  view.value = { lat, lon, z: Math.max(view.value.z, 13) }
  askTeleport(lat, lon, name)
}

function onSearch() {
  searchMsg.value = ""
  const text = query.value.trim()
  if (!text) { results.value = []; return }
  const hits = mm.searchPlaces(allPlaces(), text)
  if (hits.length === 1) { pickResult(hits[0]); return }
  if (hits.length) { results.value = hits; return }
  results.value = []
  // not a place name: a typed latitude, longitude (places.lua parses every format the page accepts)
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiMapParse(${JSON.stringify(text)}) or nil`, r => {
    if (r && typeof r.lat === "number") goTo(r.lat, r.lon, "")
    else searchMsg.value = "No saved place by that name, and not a latitude, longitude (no online place search)"
  })
}

function pickResult(r) {
  results.value = []
  query.value = r.name || ""
  goTo(r.lat, r.lon, r.name || "")
}

// ---- teleport ------------------------------------------------------------------------------
function askTeleport(lat, lon, name) {
  teleportMsg.value = ""
  target.value = { lat, lon: mm.wrapLon(lon), name: name || "" }
}

function teleport() {
  const t = target.value
  if (!t) return
  busy.value = true
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiMapTeleport(${t.lat}, ${t.lon}, ${JSON.stringify(t.name)}) or nil`, r => {
    busy.value = false
    if (r && r.ok) {
      target.value = null
      follow.value = true
      view.value = { ...view.value, lat: t.lat, lon: t.lon }
      if (!embedded) close()
    } else {
      teleportMsg.value = (r && r.error) || "Teleport failed (BeamNG.Earth not loaded?)"
    }
  })
}

function close() {
  engineLua("if beamngEarth_settings and beamngEarth_settings.mapOpened then beamngEarth_settings.mapOpened(false) end; guihooks.trigger('ChangeState', {state = 'play'})")
}

function onEscape() {
  if (target.value) { target.value = null; return }
  if (results.value.length) { results.value = []; return }
  if (!embedded) close()
}

function measure() {
  const el = viewEl.value
  if (!el) return
  w.value = el.clientWidth || 1280
  h.value = el.clientHeight || 720
}

onMounted(() => {
  measure()
  if (window.ResizeObserver) {
    resizeObs = new ResizeObserver(measure)
    resizeObs.observe(viewEl.value)
  }
  viewEl.value.addEventListener("mousemove", onHover)
  engineLua("if beamngEarth_settings and beamngEarth_settings.mapOpened then beamngEarth_settings.mapOpened(true) end")
  poll()
  pollTimer = setInterval(poll, 1000)
  nextTick(() => rootEl.value && rootEl.value.focus())
})

onUnmounted(() => {
  clearInterval(pollTimer)
  if (resizeObs) resizeObs.disconnect()
  window.removeEventListener("mousemove", onMove)
  window.removeEventListener("mouseup", onUp)
  engineLua("if beamngEarth_settings and beamngEarth_settings.mapOpened then beamngEarth_settings.mapOpened(false) end")
})

const fmtLatLon = mm.fmtLatLon
</script>

<style scoped>
.earth-map {
  position: absolute;
  inset: 0;
  background: #1d2228;
  color: #eee;
  font-family: inherit;
  overflow: hidden;
  outline: none;
  user-select: none;
  pointer-events: auto;
}
.earth-map.embedded {
  position: relative;
  width: 100%;
  height: 70vh;
  min-height: 420px;
  border-radius: 6px;
}
.map-view {
  position: absolute;
  inset: 0;
  overflow: hidden;
  cursor: crosshair;
  background: #2a3038;
}
.map-tile {
  position: absolute;
  pointer-events: none;
  image-rendering: auto;
}
.map-grid {
  position: absolute;
  left: 0;
  top: 0;
  pointer-events: none;
}
.map-grid line { stroke: rgba(255, 255, 255, 0.18); stroke-width: 1; }
.map-grid line.meridian { stroke: rgba(255, 210, 120, 0.45); }
.grid-label { fill: rgba(255, 255, 255, 0.65); font-size: 11px; }

.marker { position: absolute; width: 0; height: 0; }
.marker.place { cursor: pointer; z-index: 2; }
.marker.place .pin {
  position: absolute; left: -7px; top: -7px; width: 14px; height: 14px; border-radius: 50%;
  background: #ff9a3c; border: 2px solid #fff; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.6);
}
.marker-label {
  position: absolute; left: 11px; top: -9px; white-space: nowrap; font-size: 12px; padding: 1px 5px;
  background: rgba(0, 0, 0, 0.6); border-radius: 3px;
}
.marker.player { z-index: 4; pointer-events: none; }
.marker.player .arrow {
  position: absolute; left: -11px; top: -13px; width: 22px; height: 26px;
  background: #36a3ff; border: 2px solid #fff;
  clip-path: polygon(50% 0, 100% 100%, 50% 75%, 0 100%);
  transform-origin: 50% 50%;
  filter: drop-shadow(0 0 3px rgba(0, 0, 0, 0.8));
}
.marker.player .arrow.noheading { clip-path: circle(45%); }
.marker.target { z-index: 3; pointer-events: none; }
.marker.target .cross {
  position: absolute; left: -10px; top: -10px; width: 20px; height: 20px; border: 2px solid #ff4b4b; border-radius: 50%;
}

.map-top {
  position: absolute; left: 12px; right: 12px; top: 12px; display: flex; gap: 8px; align-items: flex-start; z-index: 10;
}
.map-title {
  padding: 7px 12px; background: rgba(0, 0, 0, 0.7); border-radius: 4px; font-weight: 600; white-space: nowrap;
}
.search { position: relative; flex: 1; max-width: 460px; }
.search-input {
  width: 100%; box-sizing: border-box; padding: 7px 10px; font-size: 14px; color: #fff;
  background: rgba(0, 0, 0, 0.75); border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px; outline: none;
}
.search-input:focus { border-color: #ff9a3c; }
.search-results {
  position: absolute; left: 0; right: 0; top: 36px; background: rgba(20, 22, 26, 0.95); border-radius: 4px; overflow: hidden;
}
.search-result { padding: 6px 10px; display: flex; justify-content: space-between; gap: 10px; cursor: pointer; }
.search-result:hover { background: rgba(255, 154, 60, 0.35); }
.result-coords { opacity: 0.6; font-size: 12px; }
.search-msg { margin-top: 4px; padding: 5px 10px; font-size: 12px; background: rgba(0, 0, 0, 0.75); border-radius: 4px; }
.map-btn {
  padding: 7px 12px; min-width: 34px; font-size: 14px; color: #fff; cursor: pointer;
  background: rgba(0, 0, 0, 0.7); border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px;
}
.map-btn:hover { background: rgba(60, 60, 60, 0.9); }
.map-btn.on { border-color: #36a3ff; color: #9fd3ff; }
.map-btn.primary { background: #e0761c; border-color: #e0761c; }
.map-btn:disabled { opacity: 0.45; cursor: default; }
.map-btn.close { margin-left: auto; }

.map-notice {
  position: absolute; left: 50%; top: 60px; transform: translateX(-50%); z-index: 9;
  padding: 7px 14px; background: rgba(120, 40, 30, 0.85); border-radius: 4px; font-size: 13px;
}
.confirm {
  position: absolute; left: 50%; bottom: 56px; transform: translateX(-50%); z-index: 11; min-width: 300px;
  padding: 12px 16px; background: rgba(15, 17, 20, 0.92); border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 6px;
}
.confirm-title { font-size: 16px; font-weight: 600; margin-bottom: 4px; }
.confirm-where { font-size: 13px; opacity: 0.85; }
.confirm-warn { margin-top: 6px; font-size: 12px; color: #ffb27a; }
.confirm-btns { display: flex; gap: 8px; margin-top: 10px; }
.map-bottom {
  position: absolute; left: 0; right: 0; bottom: 0; z-index: 10; display: flex; justify-content: space-between; gap: 16px;
  padding: 4px 10px; font-size: 11px; background: rgba(0, 0, 0, 0.6); pointer-events: none;
}
.attribution { text-align: right; opacity: 0.85; }
</style>
