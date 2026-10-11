-- luajit mod/tests/test_settings.lua  (the pause-menu settings, settings.lua; docs/DEVTOOLS.md)
-- Defaults (= today's behaviour), load / save round trip through jsonReadFile / jsonWriteFile,
-- validation (clamping, types, enums, unknown keys, broken files), applying values through the
-- module that owns them, the "page exists" rule that turns the imgui window into a hidden HUD, and
-- the page's bridge calls.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local rebase = require('beamngEarth/rebase')
local ui = require('beamngEarth/ui')
local settings = require('beamngEarth/settings')

-- in-memory settings files
local files, writes = {}, 0
function jsonReadFile(path)
  local f = files[path]
  if f == 'THROW' then error('broken json') end
  return f
end
function jsonWriteFile(path, data, pretty)
  writes = writes + 1
  local copy = {}
  for k, v in pairs(data) do copy[k] = v end
  files[path] = copy
  return true
end
FS = { fileExists = function(_, p) return files[p] ~= nil end }

local dist0, vis0, compact0 = rebase.distance, ui.visible, ui.compact

------------------------------------------------------------------------------------------
-- defaults

local vals, warn = settings.load()
local d = settings.defaults()
T.check(#warn == 0, 'no file: no warnings')
local same = true
for k, v in pairs(d) do if vals[k] ~= v then same = false end end
T.check(same, 'no file: every value is its default')
T.check(d.rebaseDistance == rebase.DEFAULT_DISTANCE and d.skyMode == 'real' and d.clouds == true
  and d.viewDistanceMode == 'fixed' and d.viewDistanceKm == 100 and d.hudVisible == false,
  'defaults are today\'s behaviour (15 km rebase, real sky, clouds on, 100 km, HUD hidden with the page)')
for _, k in ipairs(settings.ORDER) do T.check(settings.SCHEMA[k] ~= nil, 'ORDER key in SCHEMA: ' .. k) end
local n = 0
for _ in pairs(settings.SCHEMA) do n = n + 1 end
T.check(n == #settings.ORDER, 'every SCHEMA key is in ORDER')

------------------------------------------------------------------------------------------
-- validation

T.check(settings.validate('rebaseDistance', '12000') == 12000, 'number from a string')
T.check(settings.validate('rebaseDistance', 99999) == rebase.MAX_DISTANCE, 'number clamped to max')
T.check(settings.validate('rebaseDistance', -5) == rebase.MIN_DISTANCE, 'number clamped to min')
T.check(settings.validate('rebaseDistance', 'abc') == nil, 'not a number refused')
T.check(settings.validate('rebaseDistance', 0 / 0) == nil, 'NaN refused')
T.check(settings.validate('rebaseDistance', math.huge) == nil, 'inf refused')
T.check(settings.validate('clouds', 'false') == false and settings.validate('clouds', 1) == true, 'booleans from strings / 0-1')
T.check(settings.validate('clouds', 'maybe') == nil, 'non-boolean refused')
T.check(settings.validate('skyMode', 'manual') == 'manual' and settings.validate('skyMode', 'night') == nil, 'enum values')
local v, e = settings.validate('nope', 1)
T.check(v == nil and e:match('unknown'), 'unknown key refused')

------------------------------------------------------------------------------------------
-- load: valid values taken, the rest default with a warning

files[settings.FILE] = { version = 1, rebaseDistance = 30000, skyMode = 'night', clouds = false, extra = 3,
  viewDistanceKm = '250' }
vals, warn = settings.load()
T.check(vals.rebaseDistance == rebase.MAX_DISTANCE and vals.clouds == false and vals.viewDistanceKm == 250,
  'load: valid values taken (clamped, from strings)')
T.check(vals.skyMode == 'real', 'load: an invalid enum keeps the default')
T.check(#warn == 2, 'load: warnings for the invalid enum and the unknown key (' .. #warn .. ')')
files[settings.FILE] = 'THROW'
vals, warn = settings.load()
T.check(vals.clouds == true and #warn == 1 and warn[1]:match('unreadable'), 'load: a broken file -> defaults + warning')
files[settings.FILE] = 42
vals, warn = settings.load()
T.check(vals.clouds == true and #warn == 1, 'load: a non-object file -> defaults + warning')
files[settings.FILE] = nil

------------------------------------------------------------------------------------------
-- set: validated, saved, applied through the owner

local coreCalls, skyCalls = {}, {}
beamngEarth_core = { setRebaseDistance = function(m) coreCalls[#coreCalls + 1] = m; return rebase.setDistance(m) end }
local skyMode, clouds = 'real', true
beamngEarth_sky = { getMode = function() return skyMode end,
  setMode = function(m) skyCalls[#skyCalls + 1] = 'mode ' .. m; skyMode = m end,
  setClouds = function(on) skyCalls[#skyCalls + 1] = 'clouds ' .. tostring(on); clouds = on; beamngEarth_sky.CLOUDS_ENABLED = on end,
  CLOUDS_ENABLED = true }
settings.load()
writes = 0
local r = settings.set('rebaseDistance', 9000)
T.check(r.ok and r.value == 9000 and rebase.distance == 9000 and coreCalls[#coreCalls] == 9000,
  'set rebaseDistance: applied through beamngEarth_core.setRebaseDistance')
T.check(writes == 1 and files[settings.FILE].rebaseDistance == 9000 and files[settings.FILE].version == settings.VERSION,
  'set: saved with the version')
r = settings.set('rebaseDistance', 'x')
T.check(not r.ok and r.value == 9000 and writes == 1, 'set invalid: refused, nothing saved')
settings.set('skyMode', 'manual')
settings.set('clouds', false)
T.check(skyMode == 'manual' and clouds == false and #skyCalls == 2, 'sky mode and clouds applied through beamngEarth_sky')
settings.set('skyMode', 'manual')
T.check(#skyCalls == 2, 'an unchanged sky mode is not re-applied')
-- round trip
local saved = files[settings.FILE]
vals = settings.load()
T.check(vals.rebaseDistance == 9000 and vals.skyMode == 'manual' and vals.clouds == false, 'save -> load round trip')
T.check(saved.hudVisible == false and saved.devOutlineKm == 3, 'every value is written')
-- real weather and gravity: through beamngEarth_weather (defaults = its own: real, gravity on)
T.check(settings.get('weatherMode') == 'real' and settings.get('realGravity') == true, 'weather defaults: real, real gravity on')
local wCalls, wMode = {}, 'real'
beamngEarth_weather = { GRAVITY_ENABLED = true,
  setMode = function(m) wCalls[#wCalls + 1] = 'mode ' .. m; wMode = m end, getMode = function() return wMode end,
  setGravityEnabled = function(on) wCalls[#wCalls + 1] = 'gravity ' .. tostring(on); beamngEarth_weather.GRAVITY_ENABLED = on end,
  statusLine = function() return 'Weather (real): 20 C' end, gravityLine = function() return '9.78676 m/s2, EGM2008 (server)' end }
settings.set('weatherMode', 'manual')
settings.set('realGravity', false)
T.check(wCalls[1] == 'mode manual' and wCalls[2] == 'gravity false', 'weather mode and gravity applied through beamngEarth_weather')
settings.set('realGravity', false)
T.check(#wCalls == 2, 'an unchanged gravity switch is not re-applied')
T.check(not settings.set('weatherMode', 'stormy').ok, 'weather mode: only real / manual')
-- 'auto' (the old default) is real now: set and loaded as 'real', without a warning
r = settings.set('weatherMode', 'auto')
T.check(r.ok and r.value == 'real' and wMode == 'real' and files[settings.FILE].weatherMode == 'real', "set('auto') = real")
files[settings.FILE].weatherMode = 'auto'
vals, warn = settings.load()
T.check(vals.weatherMode == 'real' and #warn == 0, "a saved 'auto' loads as 'real' without a warning (" .. #warn .. ')')
T.check(#settings.SCHEMA.weatherMode.values == 2 and settings.validate('weatherMode', 'auto') == 'real', 'weather values: real, manual')
-- the switch to manual when the weather (time) is changed in the game: weather.lua / sky.lua set
-- their own mode, then save it here; applying it again is not needed (and not done)
local nCalls, nSky = #wCalls, #skyCalls
wMode, skyMode = 'manual', 'manual'
local rw, rs = settings.set('weatherMode', 'manual'), settings.set('skyMode', 'manual')
T.check(rw.ok and rs.ok and files[settings.FILE].weatherMode == 'manual' and files[settings.FILE].skyMode == 'manual',
  'switch to manual: saved')
T.check(#wCalls == nCalls and #skyCalls == nSky, 'switch to manual: the mode is not applied again')
T.check(settings.uiState().settings.weatherMode == 'manual' and settings.uiState().settings.skyMode == 'manual',
  'switch to manual: the page reads it (uiState, polled every 2 s)')
T.check(settings.load().weatherMode == 'manual', 'switch to manual: kept for the next start')
settings.set('weatherMode', 'real')
settings.set('skyMode', 'real')
T.check(wCalls[#wCalls] == 'mode real' and skyCalls[#skyCalls] == 'mode real', 'Real again from the page: applied')
-- Real chosen while already real (the dropdown, a reset to defaults): applied as before, which clears
-- a gravity change made in the game (weather.lua keeps it until setMode('real'))
nCalls = #wCalls
settings.set('weatherMode', 'real')
T.check(#wCalls == nCalls + 1 and wCalls[#wCalls] == 'mode real', 'Real while real: applied again (the gravity override)')
settings.set('realGravity', true)
-- without the core extension: straight to rebase.lua
beamngEarth_core = nil
settings.set('rebaseDistance', 7000)
T.check(rebase.distance == 7000, 'rebaseDistance without the core extension: rebase.setDistance')

------------------------------------------------------------------------------------------
-- view distance: only in our level, the default never touches the engine

local post = 0
scenetree.theLevelInfo = { visibleDistance = 100000, postApply = function() post = post + 1 end }
local level = 'west_coast_usa'
function getCurrentLevelIdentifier() return level end
settings.set('viewDistanceKm', 300)
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and post == 0, 'view distance: other levels untouched')
level = 'beamng_earth'
settings.onClientPostStartMission()
T.check(scenetree.theLevelInfo.visibleDistance == 300000 and post == 1, 'view distance: applied in our level, with postApply')
settings.set('viewDistanceMode', 'horizon')
settings.set('viewDistanceKm', 200)
T.check(scenetree.theLevelInfo.visibleDistance == 300000 and post == 1, 'view distance: horizon mode leaves it to M1h')
settings.uiReset()
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and post == 2, 'reset: back to 100 km (applied, it had changed)')
settings.onClientPostStartMission()
T.check(post == 2, 'the default at level start: the engine is not touched')

------------------------------------------------------------------------------------------
-- the page: compact HUD, hidden unless hudVisible

ui.visible, ui.compact = true, false
T.check(not settings.pageExists() and not settings.hudHiddenAtStart() and ui.compact == false,
  'no page: the window stays as before')
ui_pause_actions = { getVisibleModTabs = function()
  return { { tabId = 'mod:mods', buttons = { { id = 'taa-standalone-settings' }, { id = settings.BUTTON_ID } } } }
end }
T.check(settings.pageExists(), 'page: found in the pause menu registry')
T.check(settings.hudHiddenAtStart() == true and ui.compact and ui.visible == false, 'page: compact HUD, hidden at start')
settings.set('hudVisible', true)
T.check(ui.visible == true, 'hudVisible on: shown at once')
T.check(settings.hudHiddenAtStart() == false and ui.visible == true, 'hudVisible on: shown at level start')
settings.set('hudVisible', false)
T.check(ui.visible == false, 'hudVisible off: hidden at once')
ui_pause_actions = nil
T.check(settings.setPageExists(true) and settings.pageExists(), 'index.js can say the page exists')
T.check(settings.uiShowHud(true) == true and ui.visible, 'uiShowHud')

------------------------------------------------------------------------------------------
-- bridge calls

local devCalls = {}
beamngEarth_dev = { set = function(nm, on) devCalls[#devCalls + 1] = nm .. '=' .. tostring(on) end,
  preset = function(nm) devCalls[#devCalls + 1] = 'preset ' .. nm end,
  setParam = function(nm, v) devCalls[#devCalls + 1] = nm .. ':' .. v end,
  state = function() return { active = true } end }
local stTbl = { sessionId = 'abc', anchorId = 3, anchor = { lat = 1, lon = 2 }, status = 'ok', inOurLevel = true,
  player = { lat = 1.1, lon = 2.2, dist = 500 } }
beamngEarth_core = { _state = function() return stTbl end, attribution = function() return 'credits' end }
local s = settings.uiState()
T.check(s.settings.rebaseDistance == settings.get('rebaseDistance') and #s.schema == #settings.ORDER, 'uiState: settings + schema')
T.check(s.status.connection == 'disconnected' and s.status.session == 'abc' and s.status.anchorId == 3
  and s.status.attribution == 'credits' and s.status.dist == 500, 'uiState: status from the core')
T.check(s.dev and s.dev.active, 'uiState: dev state')
T.check(s.status.weather == 'Weather (real): 20 C' and s.status.gravity:match('EGM2008') and s.status.deshadow == 'off',
  'uiState: server features (weather, gravity, de-shadowing off)')
stTbl.deshadow = 'full'
T.check(settings.uiState().status.deshadow == 'full', 'uiState: de-shadowing mode from the hello')
T.check(s.status.colormatch == 'off', 'uiState: colour seams off without hello.colormatch')
stTbl.colormatch = 's2'
T.check(settings.uiState().status.colormatch == 's2', 'uiState: colour-seam mode from the hello')
T.check(settings.uiDevSet('tint', true).active and devCalls[1] == 'tint=true', 'uiDevSet forwards')
settings.uiDevPreset('lod')
T.check(devCalls[2] == 'preset lod', 'uiDevPreset forwards')
settings.uiSet('devOutlineKm', 5)
T.check(devCalls[3] == 'outlineKm:5', 'dev radius applied to the dev module')
local res = settings.uiReset()
T.check(res.rebaseDistance == rebase.DEFAULT_DISTANCE and files[settings.FILE].rebaseDistance == rebase.DEFAULT_DISTANCE,
  'uiReset: defaults saved')

------------------------------------------------------------------------------------------
-- the compact HUD draws (permissive imgui stub)

local drawnText = {}
ui_imgui = setmetatable({
  BoolPtr = function(v) return { [0] = v } end, DoublePtr = function(v) return { [0] = v } end,
  FloatPtr = function(v) return { [0] = v } end, Begin = function() return true end,
  Button = function() return false end, TextUnformatted = function(t) drawnText[#drawnText + 1] = t end,
}, { __index = function() return function() end end })
beamngEarth_dev = { isActive = function() return false end, toggleLast = function() end }
ui.visible, ui.compact = true, true
local view = { connection = 'connected', anchorId = 2, dist = 1200, toRebase = 13800, farText = 'Far tiles: 300 live',
  status = 'ok', defaultLat = 1, defaultLon = 2, rebaseDistance = 15000, rebaseMin = 2000, rebaseMax = 16000, attribution = 'c' }
local okDraw, errDraw = pcall(ui.draw, view, { teleport = function() end, setRebaseDistance = function(m) return m end })
local joined = table.concat(drawnText, '\n')
T.check(okDraw and joined:match('rebase in 13.8 km') and joined:match('pause menu > Mods > BeamNG.Earth'),
  'compact HUD: status line and the settings hint (' .. tostring(errDraw) .. ')')
ui.compact, drawnText = false, {}
okDraw = pcall(ui.draw, view, { teleport = function() end, setRebaseDistance = function(m) return m end })
T.check(okDraw and table.concat(drawnText, '\n'):match('Anchor: #2'), 'full window still draws')

-- leave the shared modules as found
rebase.distance, ui.visible, ui.compact, ui.hudHidden = dist0, vis0, compact0, false
T.done('test_settings')
