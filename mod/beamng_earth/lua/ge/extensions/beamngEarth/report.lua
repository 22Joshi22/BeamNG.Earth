-- In-game reports for the user (2026-10-13: "a reporting tool that makes it easy for me to take notes on
-- the world or to record the performance of my game ... without needing to invoke you every time").
--
--   Ctrl+Shift+N   a note: the moment is captured at once (screenshot, location, frame times, engine
--                  metrics, the mod's state), then a window takes the note text; Save writes it all.
--   Ctrl+Shift+B   a benchmark: 30 s of frame times (and position, every second) while the player
--                  drives, then the same report with the statistics and every hitch.
--
-- Reports go to <user folder>/beamngEarth/reports/<date>_<time>_<kind>/ (report.json, screenshot.jpg);
-- reports/index.jsonl gets one line per report. Read back by the developer (Claude) later.
local M = {}
M.dependencies = { 'ui_imgui' }

M.BENCH_S = 30
M.RING = 900            -- frames kept for the "recent" statistics in a note (~10 s at 90 fps)
M.DIR = 'beamngEarth/reports/'

local im = nil
local ffi = require('ffi')
local note = nil        -- the open note: { dir, data, buf, open }
local bench = nil       -- the running benchmark: { t0, dts, samples, hitches, dir, data }
local ring, ringN = {}, 0
local toast, toastUntil = nil, 0
local count = 0

-- wall seconds (os.clock is the process CPU time over all threads: stopwatch.lua)
local wallMs = require('beamngEarth/stopwatch').clock()
local function now() return wallMs() / 1000 end

local function stats(dts)
  local n = #dts
  if n == 0 then return { frames = 0 } end
  local s = {}
  local sum, mx = 0, 0
  for i = 1, n do s[i] = dts[i]; sum = sum + dts[i]; if dts[i] > mx then mx = dts[i] end end
  table.sort(s)
  local function pct(p) return s[math.max(1, math.ceil(p * n))] end
  local over = { 33, 50, 100 }
  local res = { frames = n, avgMs = sum / n, fps = n / (sum / 1000), p50 = pct(0.5), p95 = pct(0.95),
    p99 = pct(0.99), maxMs = mx }
  for _, o in ipairs(over) do
    local c = 0
    for i = 1, n do if dts[i] > o then c = c + 1 end end
    res['over' .. o] = c
  end
  return res
end

local function safe(f, ...)
  local ok, v = pcall(f, ...)
  if ok then return v end
  return 'error: ' .. tostring(v)
end

-- Everything the moment can tell: where, what the engine and the mod are doing.
local function capture()
  local d = { time = os.date('%Y-%m-%d %H:%M:%S'), unix = os.time(), level = safe(getCurrentLevelIdentifier) }
  local core = rawget(_G, 'beamngEarth_core')
  if core and core.status then
    local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
    d.mod = ok and st or tostring(st)
    if ok and type(st) == 'table' and st.player then
      d.lat, d.lon = st.player.lat, st.player.lon
      d.anchorId = st.anchorId
    end
  end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if veh then
    local p, v, f = veh:getPosition(), veh:getVelocity(), veh:getDirectionVector()
    d.vehicle = { name = safe(function() return veh:getJBeamFilename() end), pos = { p.x, p.y, p.z },
      speedMs = v:length(), speedMph = v:length() * 2.23694, headingDeg = math.deg(math.atan2(f.x, f.y)) % 360 }
  end
  if core_camera then
    local c = core_camera.getPosition()
    local fw = core_camera.getForward and core_camera.getForward()
    d.camera = { pos = { c.x, c.y, c.z }, forward = fw and { fw.x, fw.y, fw.z } or nil }
  end
  local m = {}
  if Engine.Debug and Engine.Debug.getLastPerformanceMetrics then pcall(Engine.Debug.getLastPerformanceMetrics, m) end
  d.engineMetrics = m
  d.fpsLimit = safe(function() return Engine.getCurrentFpsLimitReason and Engine.getCurrentFpsLimitReason() end)
  d.vram = safe(function() return Engine.Render and Engine.Render.getVRAMUsage and Engine.Render.getVRAMUsage() end)
  local recent = {}
  for i = 1, math.min(ringN, M.RING) do recent[#recent + 1] = ring[i] end
  d.recentFrames = stats(recent)
  return d
end

local function newDir(kind)
  count = count + 1
  local rel = M.DIR .. os.date('%Y%m%d_%H%M%S') .. '_' .. kind .. '_' .. count .. '/'
  if not FS:directoryExists(rel) then FS:directoryCreate(rel, true) end
  return rel
end

local function screenshot(rel)
  if type(createScreenshot2) ~= 'function' then return nil end
  local ok = pcall(createScreenshot2, { filename = rel .. 'screenshot', writeJPG = true, superSampling = 1 })
  return ok and (FS:getUserPath() .. rel .. 'screenshot.jpg'):gsub('\\', '/') or nil
end

local function write(rel, data)
  data.folder = (FS:getUserPath() .. rel):gsub('\\', '/')
  writeFile(rel .. 'report.json', jsonEncodePretty and jsonEncodePretty(data) or jsonEncode(data))
  local line = jsonEncode({ time = data.time, kind = data.kind, folder = data.folder, lat = data.lat, lon = data.lon,
    note = data.note, fps = data.bench and data.bench.fps or (data.recentFrames and data.recentFrames.fps) })
  local idx = M.DIR .. 'index.jsonl'
  local old = readFile(idx) or ''
  writeFile(idx, old .. line .. '\n')
  toast, toastUntil = 'BeamNG.Earth report saved (' .. data.kind .. ')', now() + 3
  log('I', 'beamngEarth', 'report saved: ' .. data.folder)
end

-- Ctrl+Shift+N: capture now, ask for the note.
function M.note()
  if note then return end
  local rel = newDir('note')
  local data = capture()
  data.kind = 'note'
  data.screenshot = screenshot(rel)       -- the view the player saw (the window opens next frame)
  note = { dir = rel, data = data, buf = nil, open = true, frames = 0 }
end

-- Ctrl+Shift+B: a benchmark over the next BENCH_S seconds.
function M.benchmark(seconds)
  if bench then return end
  local rel = newDir('bench')
  bench = { t0 = now(), dur = tonumber(seconds) or M.BENCH_S, dts = {}, track = {}, hitches = {}, dir = rel,
    lastSample = -1, start = capture() }
  bench.start.screenshot = screenshot(rel)
  toast, toastUntil = string.format('BeamNG.Earth benchmark: recording %d s', bench.dur), now() + 3
end

local function benchFrame(dtMs)
  local b = bench
  b.dts[#b.dts + 1] = dtMs
  local t = now() - b.t0
  local core = rawget(_G, 'beamngEarth_core')
  if dtMs > 50 then
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    local p = veh and veh:getPosition()
    b.hitches[#b.hitches + 1] = { t = t, ms = dtMs, pos = p and { p.x, p.y, p.z } or nil }
  end
  if math.floor(t) > b.lastSample then
    b.lastSample = math.floor(t)
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    local p = veh and veh:getPosition()
    local s = { t = math.floor(t), pos = p and { p.x, p.y, p.z } or nil,
      speedMph = veh and veh:getVelocity():length() * 2.23694 or nil }
    if core and core.status then
      local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
      if ok and type(st) == 'table' then
        s.lat, s.lon = st.player and st.player.lat, st.player and st.player.lon
        s.far = st.far and { live = st.far.live, queued = st.far.queued } or nil
        s.trees = st.trees and st.trees.items or nil
        s.sea = st.sea and st.sea.tiles or nil
      end
    end
    local m = {}
    if Engine.Debug and Engine.Debug.getLastPerformanceMetrics then pcall(Engine.Debug.getLastPerformanceMetrics, m) end
    s.metrics = m
    b.track[#b.track + 1] = s
  end
  if t >= b.dur then
    local data = b.start
    data.kind = 'bench'
    data.bench = stats(b.dts)
    data.bench.seconds = t
    data.bench.hitches = b.hitches
    data.track = b.track
    data.endState = capture()
    write(b.dir, data)
    bench = nil
  end
end

-- Save the open note with this text (tests, the console).
function M.saveNote(text)
  if not note then M.note() end
  note.data.note = tostring(text or '')
  write(note.dir, note.data)
  note = nil
end

function M.status()
  return { noteOpen = note ~= nil, benchRunning = bench ~= nil, saved = count }
end

function M.onPreRender(dtReal)
  if not dtReal or dtReal <= 0 then return end
  local ms = dtReal * 1000
  ringN = ringN + 1
  ring[(ringN - 1) % M.RING + 1] = ms
  if bench then benchFrame(ms) end
end

function M.onUpdate()
  im = im or rawget(_G, 'ui_imgui')
  if not im then return end
  if toast and now() < toastUntil then
    im.SetNextWindowPos(im.ImVec2(20, 120), im.Cond_Always)
    if im.Begin('##bngeReportToast', nil, im.WindowFlags_NoDecoration + im.WindowFlags_AlwaysAutoResize
        + im.WindowFlags_NoInputs) then
      im.TextUnformatted(toast)
    end
    im.End()
  end
  if bench then
    im.SetNextWindowPos(im.ImVec2(20, 160), im.Cond_Always)
    if im.Begin('##bngeBench', nil, im.WindowFlags_NoDecoration + im.WindowFlags_AlwaysAutoResize
        + im.WindowFlags_NoInputs) then
      im.TextUnformatted(string.format('Benchmark: %.0f / %d s', now() - bench.t0, bench.dur))
    end
    im.End()
  end
  if not note then return end
  note.frames = note.frames + 1
  if not note.buf then note.buf = im.ArrayChar(4096) end
  im.SetNextWindowSize(im.ImVec2(520, 260), im.Cond_FirstUseEver)
  if im.Begin('BeamNG.Earth note##bngeNote', nil, 0) then
    im.TextUnformatted(string.format('%s  |  %s', note.data.time,
      note.data.lat and string.format('%.5f, %.5f', note.data.lat, note.data.lon) or 'no location'))
    im.TextUnformatted('Your note (what looks wrong, what looks good, performance...):')
    if note.frames == 2 then im.SetKeyboardFocusHere() end
    im.InputTextMultiline('##bngeNoteText', note.buf, im.GetLengthArrayCharPtr(note.buf), im.ImVec2(-1, 140))
    if im.Button('Save note') then
      note.data.note = ffi.string(note.buf)
      write(note.dir, note.data)
      note = nil
    end
    im.SameLine()
    if note and im.Button('Save + 30 s benchmark') then
      note.data.note = ffi.string(note.buf)
      write(note.dir, note.data)
      note = nil
      M.benchmark()
    end
    im.SameLine()
    if note and im.Button('Cancel') then note = nil end
  end
  im.End()
end

return M
