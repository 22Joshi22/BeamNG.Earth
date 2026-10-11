-- BeamNG.Earth settings: GE extension 'beamngEarth_settings' and the module the other parts read
-- (docs/DEVTOOLS.md "Pause-menu integration"). The pause menu's Mods tab has a "BeamNG.Earth"
-- button (ui/ui-vue/mods/beamngEarth/index.js) that opens EarthSettings.vue; the page reads and
-- writes everything here through the bridge (bngApi.engineLua, as the user's taa_standalone mod):
--   beamngEarth_settings.uiState()             -> { settings, schema, status, dev }
--   beamngEarth_settings.uiSet(key, value)     -> { ok, value, error } (validated, saved, applied)
--   beamngEarth_settings.uiReset()             every setting back to its default
--   beamngEarth_settings.uiDevSet(name, on)    beamngEarth_settings.uiDevPreset(name)  (dev tools)
--   beamngEarth_settings.setPageExists(true)   (index.js, once the button is registered)
-- Values persist in FILE (jsonReadFile / jsonWriteFile, as taa_standalone). Every default is
-- today's behaviour, so a missing or broken file changes nothing. The dev overlays themselves are
-- per session (DEVTOOLS.md "State persists per session only"); only their parameters persist here.
--
-- The imgui window (ui.lua) becomes a compact status HUD once the page exists, hidden at level
-- start unless `hudVisible` is on (the page itself is the place for settings then). Whether the
-- page exists: index.js reports it, and the pause menu's registry is asked too (load order of the
-- UI and the Lua extensions does not matter). Without the page the window stays as before.

local M = {}

local rebase = require('beamngEarth/rebase')
local ui = require('beamngEarth/ui')
local net = require('beamngEarth/net')
local credits = require('beamngEarth/credits')

M.FILE = '/settings/beamngEarth.json'
M.VERSION = 1
M.BUTTON_ID = 'beamngEarth-settings' -- index.js registerModButton id
M.LEVEL_ID = 'beamng_earth'

-- key -> { kind = 'number'|'bool'|'enum', default, min, max, values, label }. Order = ORDER.
M.SCHEMA = {
  rebaseDistance = { kind = 'number', default = rebase.DEFAULT_DISTANCE, min = rebase.MIN_DISTANCE,
    max = rebase.MAX_DISTANCE, label = 'Rebase distance (m)' },
  viewDistanceMode = { kind = 'enum', default = 'horizon', values = { 'fixed', 'horizon' }, label = 'View distance' },
  viewDistanceKm = { kind = 'number', default = 100, min = 20, max = 1000, label = 'Fixed view distance (km)' },
  hazeStrength = { kind = 'number', default = 0, min = 0, max = 1, label = 'Distance haze (placeholder)' },
  skyMode = { kind = 'enum', default = 'real', values = { 'real', 'manual' }, label = 'Sky' },
  clouds = { kind = 'bool', default = true, label = 'Clouds' },
  weatherMode = { kind = 'enum', default = 'auto', values = { 'auto', 'real', 'manual' }, label = 'Weather' },
  realGravity = { kind = 'bool', default = true, label = 'Real gravity' },
  hudVisible = { kind = 'bool', default = false, label = 'Status HUD at level start' },
  -- the on-screen data credits (credits.lua): on by default, CC BY licences ask for visible attribution
  creditsVisible = { kind = 'bool', default = true, label = 'Data credits on screen' },
  creditsCorner = { kind = 'enum', default = 'bottom-left', values = credits.CORNERS, label = 'Data credits corner' },
  creditsSize = { kind = 'enum', default = 'small', values = { 'small', 'normal' }, label = 'Data credits size' },
  devOutlineKm = { kind = 'number', default = 3, min = 0.5, max = 20, label = 'Dev outlines radius (km)' },
  devMapKm = { kind = 'number', default = 10, min = 1, max = 80, label = 'Dev mini-map radius (km)' },
}
M.ORDER = { 'rebaseDistance', 'viewDistanceMode', 'viewDistanceKm', 'hazeStrength', 'skyMode', 'clouds',
  'weatherMode', 'realGravity', 'hudVisible', 'creditsVisible', 'creditsCorner', 'creditsSize', 'devOutlineKm',
  'devMapKm' }

local values = {}       -- the settings in use (always complete and valid)
local pageFlag = false  -- index.js said the page exists
local appliedView = nil -- last view distance written to LevelInfo (m)

local function defaults()
  local t = {}
  for k, s in pairs(M.SCHEMA) do t[k] = s.default end
  return t
end
M.defaults = defaults
values = defaults() -- until load(): the defaults (= today's behaviour)

-- A valid value for key, or nil + error. Numbers are clamped to the range; strings that read as a
-- number or boolean are accepted (the page sends what its controls hold).
function M.validate(key, v)
  local s = M.SCHEMA[key]
  if not s then return nil, 'unknown setting ' .. tostring(key) end
  if s.kind == 'number' then
    local n = tonumber(v)
    if not n or n ~= n or n == math.huge or n == -math.huge then return nil, key .. ': not a number' end
    return math.max(s.min, math.min(s.max, n))
  elseif s.kind == 'bool' then
    if v == true or v == 'true' or v == 1 then return true end
    if v == false or v == 'false' or v == 0 then return false end
    return nil, key .. ': not a boolean'
  else
    for _, ok in ipairs(s.values) do if v == ok then return v end end
    return nil, key .. ': must be one of ' .. table.concat(s.values, ', ')
  end
end

-- Read the file (path: FILE): every valid known value is taken, the rest stays at its default.
-- Returns the values and a list of warnings (unknown keys, invalid values, an unreadable file).
function M.load(path)
  values = defaults()
  local warnings = {}
  local read = rawget(_G, 'jsonReadFile')
  local ok, data = false, nil
  if read and (not FS or not FS.fileExists or FS:fileExists(path or M.FILE)) then ok, data = pcall(read, path or M.FILE) end
  if ok and type(data) == 'table' then
    for k, v in pairs(data) do
      if k ~= 'version' then
        local val, err = M.validate(k, v)
        if val ~= nil then values[k] = val else warnings[#warnings + 1] = err end
      end
    end
  elseif ok and data ~= nil then
    warnings[#warnings + 1] = 'settings file is not an object'
  elseif not ok and data ~= nil then
    warnings[#warnings + 1] = 'settings file unreadable: ' .. tostring(data)
  end
  for _, w in ipairs(warnings) do log('W', 'beamngEarth', 'settings: ' .. w) end
  return values, warnings
end

-- Write every value (and the version) to the file. Returns true on success.
function M.save(path)
  local write = rawget(_G, 'jsonWriteFile')
  if not write then return false end
  local out = { version = M.VERSION }
  for k, v in pairs(values) do out[k] = v end
  local ok, err = pcall(write, path or M.FILE, out, true)
  if not ok then log('W', 'beamngEarth', 'settings: write failed: ' .. tostring(err)) end
  return ok and err ~= false
end

function M.get(key) return values[key] end

function M.getAll()
  local t = {}
  for k, v in pairs(values) do t[k] = v end
  return t
end

------------------------------------------------------------------------------------------
-- applying values to the running mod (each through the module that owns the behaviour)

local function ext(name) return rawget(_G, name) end

-- LevelInfo.visibleDistance only takes effect after postApply (in-game, 2026-10-10). Only in our
-- level, and the default (the level file's 100 km) never touches the engine: nothing changes until
-- the player picks another distance.
local function applyViewDistance()
  local li = scenetree and scenetree.theLevelInfo
  local level = rawget(_G, 'getCurrentLevelIdentifier') and getCurrentLevelIdentifier()
  if values.viewDistanceMode ~= 'fixed' then
    -- 'horizon' (the default): v1.4 M1h drives it. Coming back from a fixed distance we set, the
    -- level's own distance is put back once (M1h's next status sets its own).
    if li and level == M.LEVEL_ID and appliedView ~= nil then
      li.visibleDistance = M.SCHEMA.viewDistanceKm.default * 1000
      if li.postApply then li:postApply() end
      appliedView = nil
    end
    return
  end
  local m = values.viewDistanceKm * 1000
  if not li or level ~= M.LEVEL_ID or appliedView == m then return end
  if appliedView == nil and m == M.SCHEMA.viewDistanceKm.default * 1000 then return end
  li.visibleDistance = m
  if li.postApply then li:postApply() end
  appliedView = m
end

M.APPLY = {
  rebaseDistance = function(v)
    local core = ext('beamngEarth_core')
    if core and core.setRebaseDistance then core.setRebaseDistance(v) else rebase.setDistance(v) end
  end,
  viewDistanceMode = applyViewDistance,
  viewDistanceKm = applyViewDistance,
  skyMode = function(v)
    local sky = ext('beamngEarth_sky')
    if sky and sky.setMode and sky.getMode and sky.getMode() ~= v then sky.setMode(v) end
  end,
  clouds = function(v)
    local sky = ext('beamngEarth_sky')
    if sky and sky.setClouds and sky.CLOUDS_ENABLED ~= v then sky.setClouds(v) end
  end,
  -- real weather and gravity (weather.lua; the server's --weather / --gravity decide whether data come)
  weatherMode = function(v)
    local w = ext('beamngEarth_weather')
    if w and w.setMode then w.setMode(v) end
  end,
  realGravity = function(v)
    local w = ext('beamngEarth_weather')
    if w and w.setGravityEnabled and w.GRAVITY_ENABLED ~= v then w.setGravityEnabled(v) end
  end,
  hudVisible = function(v)
    if M.pageExists() then ui.visible = v end
  end,
  creditsVisible = function(v) credits.setEnabled(v) end,
  creditsCorner = function(v) credits.setCorner(v) end,
  creditsSize = function(v) credits.setSize(v) end,
  devOutlineKm = function(v)
    local dev = ext('beamngEarth_dev')
    if dev and dev.setParam then dev.setParam('outlineKm', v) end
  end,
  devMapKm = function(v)
    local dev = ext('beamngEarth_dev')
    if dev and dev.setParam then dev.setParam('mapKm', v) end
  end,
  -- hazeStrength: a placeholder until the haze exists (v1.4 M1h)
}

function M.apply(key)
  local f = M.APPLY[key]
  if not f then return end
  local ok, err = pcall(f, values[key])
  if not ok then log('W', 'beamngEarth', 'settings: applying ' .. key .. ' failed: ' .. tostring(err)) end
end

function M.applyAll()
  for _, k in ipairs(M.ORDER) do if k ~= 'hudVisible' then M.apply(k) end end
end

-- Validate, store, save and apply one value. Returns { ok, value, error }.
function M.set(key, v)
  local val, err = M.validate(key, v)
  if val == nil then return { ok = false, error = err, value = values[key] } end
  values[key] = val
  M.save()
  M.apply(key)
  return { ok = true, value = val }
end

------------------------------------------------------------------------------------------
-- the settings page

-- Does the pause-menu page exist? index.js says so, or the pause menu lists our button.
function M.pageExists()
  if pageFlag then return true end
  local pa = ext('ui_pause_actions')
  if not pa or not pa.getVisibleModTabs then return false end
  local ok, tabs = pcall(pa.getVisibleModTabs)
  if not ok or type(tabs) ~= 'table' then return false end
  for _, tab in ipairs(tabs) do
    for _, b in ipairs(tab.buttons or {}) do
      if b.id == M.BUTTON_ID then pageFlag = true; return true end
    end
  end
  return false
end

-- The window becomes the compact HUD once the page exists; hidden unless hudVisible.
local function applyHud()
  ui.compact = true
  ui.hudHidden = not values.hudVisible
  ui.visible = values.hudVisible
end

function M.setPageExists(on)
  pageFlag = on and true or false
  if pageFlag then applyHud() end
  return pageFlag
end

-- Called by core at level start: is the window hidden there? (Nothing changes without the page.)
function M.hudHiddenAtStart()
  if not M.pageExists() then return false end
  applyHud()
  return not values.hudVisible
end

function M.uiState()
  local core = ext('beamngEarth_core')
  local st = core and core._state and core._state() or nil
  local dev = ext('beamngEarth_dev')
  local status = {
    connection = net.state(), lastError = (not net.isConnected()) and net.lastError or nil,
    session = st and st.sessionId, anchorId = st and st.anchorId,
    anchorLat = st and st.anchor and st.anchor.lat, anchorLon = st and st.anchor and st.anchor.lon,
    playerLat = st and st.player and st.player.lat, playerLon = st and st.player and st.player.lon,
    dist = st and st.player and st.player.dist, rebaseDistance = rebase.distance,
    status = st and st.status or '', inOurLevel = st and st.inOurLevel or false,
    attribution = core and core.attribution and core.attribution() or '',
    credits = core and core.credits and core.credits() or nil, -- the on-screen data credits now
  }
  -- server features (status only): weather, gravity, imagery de-shadowing and colour seams
  local w = ext('beamngEarth_weather')
  status.weather = w and w.statusLine and w.statusLine() or nil
  status.gravity = w and w.gravityLine and w.gravityLine() or nil
  status.deshadow = st and st.deshadow or 'off'
  status.colormatch = st and st.colormatch or 'off'
  local schema = {}
  for _, k in ipairs(M.ORDER) do
    local s = M.SCHEMA[k]
    schema[#schema + 1] = { id = k, kind = s.kind, default = s.default, min = s.min, max = s.max, values = s.values,
      label = s.label }
  end
  return { settings = M.getAll(), schema = schema, status = status,
    dev = dev and dev.state and dev.state() or nil, hudVisible = ui.visible }
end

function M.uiSet(key, v) return M.set(key, v) end

function M.uiReset()
  values = defaults()
  M.save()
  M.applyAll()
  return M.getAll()
end

function M.uiDevSet(name, on)
  local dev = ext('beamngEarth_dev')
  if dev and dev.set then dev.set(name, on and true or false) end
  return dev and dev.state and dev.state() or nil
end

function M.uiDevPreset(name)
  local dev = ext('beamngEarth_dev')
  if dev and dev.preset then dev.preset(name) end
  return dev and dev.state and dev.state() or nil
end

-- Show / hide the HUD now (the page's "Show status HUD" switch also sets the level-start default).
function M.uiShowHud(on)
  ui.visible = on and true or false
  return ui.visible
end

------------------------------------------------------------------------------------------
-- extension hooks

function M.onExtensionLoaded()
  M.load()
  M.applyAll()
end

-- other extensions (sky, dev) may load after this one: apply again once the level runs
function M.onClientPostStartMission()
  appliedView = nil
  M.applyAll()
end

return M
