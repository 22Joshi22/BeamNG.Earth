-- BeamNG.Earth: real weather at the player. GE extension 'beamngEarth_weather' (loaded by
-- scripts/beamng_earth_weather/modScript.lua). docs/research/2026-10-10-real-weather.md §4.2, measured
-- facts in docs/research/2026-10-10-weather-probe-results.md.
--
-- The server (server/weather.py) sends a 'weather' message: Open-Meteo's current conditions at the
-- player, every 10 min or 25 km; beamngEarth_core hands it to M.onWeather. Every value moves towards
-- its new target smoothly (exponential approach, 95 % in LERP_S) and is written through
-- core_environment, so the pause menu's Time & Weather panel, missions and other mods see the real
-- values: cloud cover (COVERAGE_CURVE, a calibration table), cirrus from high cloud, the clouds'
-- drift from the wind aloft, fog from visibility (density compensated for the height of the ground:
-- the engine's fog thins with absolute z, shaders/common/sceneFog.h.hlsl), ground wind with gusts
-- (core_environment.setGroundWind reaches every vehicle), rain and snow (the level's Precipitation
-- objects; core_environment.setPrecipitation writes 'numOfDrops', a field the engine does not
-- have), and the air temperature (LevelInfo temperatureCurveC, which core_environment turns into
-- the engine's sea-level temperature every frame; the engine lowers it with height itself). The
-- real UTC offset goes to beamngEarth_sky (its clock), and the cloud base above the ground too (sky
-- owns the layer's ground-following altitude and the rebase weather-offset shift).
-- The sun dims under a thick cloud deck (ScatterSky.brightness, the direct light only: the game's
-- clouds cast no shadows, so under overcast the car kept a sharp sun shadow; in-game 2026-10-10 the
-- auto exposure keeps the scene's brightness while the shadows fade). The level's own value is put
-- back when the weather stops driving it (manual mode, a manual cloud change, a mission).
-- Gravity follows the player (GRAVITY_ENABLED, independent of the mode): the server's 'gravity'
-- message (server/gravity.py: WGS84 normal + EGM2008 anomaly at the player, every 2 km / 200 m of
-- height) with the free-air gradient for the height since; normal gravity by latitude and height
-- until the first message (or with a server without it). Changes are written in steps of at most
-- GRAVITY_STEP per check, so a new value is approached smoothly.
-- Everything runs in the BeamNG.Earth level only.
--
-- Modes: 'real' = drive the weather; 'manual' = hands off (the game's own controls). The default is
-- 'real' unless the BWM_Weather mod is set to change the weather itself (automatic weather on, or
-- a preset other than clear): then 'manual', with a notice (research §3). A change made by anyone
-- else (pause menu, another mod) after the start-up grace period is a manual override of that
-- group of values: real weather keeps driving the others; setMode('real') clears the overrides.
-- Missions pause it (they set and restore their own weather).
-- Console: beamngEarth_weather.setMode('real'|'manual'|'auto')   .status()   .setGravityEnabled(bool)

local M = {}

M.LEVEL_ID = 'beamng_earth'
M.LERP_S = 60              -- a new target is 95 % reached after this long
M.APPLY_S = 0.5            -- writes at 2 Hz, only of values that changed
M.WIND_APPLY_S = 1.0       -- setGroundWind queues Lua into every vehicle: at most 1 Hz
M.GROUND_CHECK_S = 5       -- ground height under the player (fog compensation)
M.GRACE_S = 20             -- after a level start: BWM_Weather's start transition, env's setState(init_env)
M.USER_QUIET_S = 3         -- after someone else's change: core_environment lerps it over 1.15 s and
                           -- each of its setters cancels a running lerp, so write nothing meanwhile
M.MISSION_SETUP_S = 2      -- missions set their weather just before 'started': not an override
M.MISSION_RESUME_S = 5
M.BWM_CHECK_S = 5
M.TEMP_WRITE_S = 10        -- temperature curve writes at most this often
M.TEMP_EPS_C = 0.2

-- Calibration: sky fraction of the volumetric deck (0..1, Open-Meteo max(low, mid) / 100) ->
-- CloudLayer.coverage, linear between rows. From the probe (camera straight up, base ~2 km above
-- the ground): nothing below ~0.7, 7 % at 0.79, 19 % at 0.90; looking 20 deg up, a band of small
-- cumulus at 0.7-0.95. Rows above 0.19 are guesses (overcast was never seen up to 0.95; the stock
-- UI goes to 3, BWM_Weather to 2.9): retune here.
M.COVERAGE_CURVE = {
  { 0.00, 0.00 },
  { 0.02, 0.70 },
  { 0.07, 0.79 },
  { 0.19, 0.90 },
  { 0.50, 1.10 },   -- uncalibrated
  { 0.80, 1.50 },   -- uncalibrated
  { 1.00, 2.00 },   -- uncalibrated
}
-- Cirrus: invisible while cirrusDensity is 0 (the CloudLayer default); 0.5 density + 0.5 coverage
-- gave faint streaks in the probe.
M.CIRRUS_DENSITY_MIN, M.CIRRUS_DENSITY_MAX = 0.3, 0.6
-- Fog: extinction sigma = 3.912 / visibility (Koschmieder, 2 % contrast); the engine's density at
-- z = 0 is sigma * exp(groundZ / height), so the player's ground gets sigma.
M.FOG_K = 3.912
M.FOG_VIS_MIN, M.FOG_VIS_MAX = 50, 100000
M.FOG_VIS_DEFAULT = 60000  -- no visibility in the data
M.FOG_HEIGHT = 1000        -- fogAtmosphereHeight, m (the menu's range is 1-1000)
M.FOG_HEIGHT_FOG = 500     -- with fog codes; lower makes valleys below the player opaque
M.FOG_FOLLOW_GROUND = true
M.FOG_DENSITY_MAX = 0.05   -- the menu's maximum (50 / 1000)
-- Clouds drift with the wind aloft; CloudLayer.windSpeed is a texture-scroll scalar (levels 0.1-0.2).
M.CLOUD_WIND_SCALE, M.CLOUD_WIND_MIN, M.CLOUD_WIND_MAX = 0.02, 0.02, 0.5
M.CLOUD_ABOVE_MIN_KM, M.CLOUD_ABOVE_MAX_KM = 0.3, 6
-- Rain/snow fraction of the drop pool: sqrt(rate / full), at least PRECIP_MIN while it falls.
M.RAIN_FULL_MMH, M.SNOW_FULL_CMH, M.PRECIP_MIN = 8, 2, 0.08
M.RAIN_NAME = 'bngEarthRain'      -- level objects (tools/make_level_assets.py)
M.SNOW_NAME = 'bngEarthSnow'
M.RAIN_POOL, M.SNOW_POOL = 4000, 3000                       -- drops (probe: 9000 cost +0.2 ms)
M.GUSTS = true
M.GUST_TAU_S, M.GUST_STD = 4, 0.4
-- Sun under the deck: brightness factor 1 - SUN_DIM_MAX * deck^SUN_DIM_EXP (a few clouds barely
-- matter, overcast leaves 0.3; probe at noon: 0.35 made the car's shadow faint).
M.SUN_DIM_MAX, M.SUN_DIM_EXP = 0.7, 1.5
M.SUN_EPS = 0.02           -- write only when the factor moved this much (each write is a postApply)
M.GRAVITY_ENABLED = true
M.GRAVITY_EPS = 1e-4       -- m/s^2; ~30 m of climb (setGravity queues Lua into every vehicle)
M.GRAVITY_CHECK_S = 1
M.GRAVITY_DEFAULT = 9.81   -- the level's value, restored when switched off
M.GRAVITY_STEP = 1e-3      -- m/s^2 per check at most (100 mGal/s) once ours is set: a smooth approach
M.FREE_AIR = 3.086e-6      -- m/s^2 per m of height (normal free-air gradient)

-- core_environment's onEnvironmentChanged keys -> the group a manual change overrides
M.OVERRIDE_GROUPS = {
  cloudCover = 'clouds',
  cloudCirrusCoverage = 'cirrus', cloudCirrusDensity = 'cirrus', cloudCirrusAltitudeKm = 'cirrus',
  cloudWindDirection = 'cloudWind', windSpeed = 'cloudWind',
  fogDensity = 'fog', fogAtmosphereHeight = 'fog',
  groundWind = 'wind',
  gravity = 'gravity',
  cloudAltitudeKm = 'cloudAltitude',
  numOfDrops = 'precip',
}

------------------------------------------------------------------------------------------
-- pure

local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end

-- Sky fraction (0..1) -> CloudLayer.coverage over COVERAGE_CURVE.
function M.coverageFor(f, curve)
  curve = curve or M.COVERAGE_CURVE
  f = tonumber(f) or 0
  if f <= curve[1][1] then return curve[1][2] end
  for i = 2, #curve do
    local a, b = curve[i - 1], curve[i]
    if f <= b[1] then return a[2] + (b[2] - a[2]) * (f - a[1]) / (b[1] - a[1]) end
  end
  return curve[#curve][2]
end

-- Sky fraction of the deck (0..1) -> the sun's brightness factor.
function M.sunDimFor(deck)
  return 1 - M.SUN_DIM_MAX * clamp(tonumber(deck) or 0, 0, 1) ^ M.SUN_DIM_EXP
end

-- High cloud cover (%) -> cirrusCoverage, cirrusDensity.
function M.cirrusFor(highPct)
  local c = clamp((tonumber(highPct) or 0) / 100, 0, 1)
  if c <= 0 then return 0, 0 end
  return c, M.CIRRUS_DENSITY_MIN + (M.CIRRUS_DENSITY_MAX - M.CIRRUS_DENSITY_MIN) * c
end

-- Visibility (m, nil = unknown) -> extinction per metre.
function M.fogSigma(visM)
  local v = tonumber(visM) or M.FOG_VIS_DEFAULT
  return M.FOG_K / clamp(v, M.FOG_VIS_MIN, M.FOG_VIS_MAX)
end

-- The engine's fogDensity that gives extinction sigma at height groundZ (nil = no compensation).
function M.fogDensityFor(sigma, groundZ, height)
  local d = sigma
  if M.FOG_FOLLOW_GROUND and groundZ then d = sigma * math.exp(groundZ / height) end
  return math.min(d, M.FOG_DENSITY_MAX)
end

-- Ground wind: meteorological "from" direction (deg clockwise from north) and speed -> the world
-- vector the air moves along (ENU: +x east, +y north; the UI's x = s sin(dir), y = s cos(dir) with
-- dir = towards). A west wind (from 270) blows towards +x.
function M.windVec(ms, fromDeg)
  local t = math.rad(tonumber(fromDeg) or 0)
  ms = tonumber(ms) or 0
  return -ms * math.sin(t), -ms * math.cos(t)
end

-- Unit vector the clouds drift towards.
function M.cloudDir(fromDeg)
  return M.windVec(1, fromDeg)
end

-- Rate -> fraction of the drop pool.
function M.precipPct(rate, full)
  rate = tonumber(rate) or 0
  if rate <= 0.05 then return 0 end
  return clamp(math.sqrt(rate / full), M.PRECIP_MIN, 1)
end

-- Normal gravity (WGS84 Somigliana) at geodetic latitude latDeg, minus the free-air gradient for
-- height h (m above the ellipsoid ~ sea level ~ our z). m/s^2, positive.
function M.gravityAt(latDeg, h)
  local s = math.sin(math.rad(tonumber(latDeg) or 0))
  local s2 = s * s
  return 9.7803253359 * (1 + 0.00193185265241 * s2) / math.sqrt(1 - 0.00669437999013 * s2) - M.FREE_AIR * (tonumber(h) or 0)
end

-- The server's gravity msg (g at height msg.h) carried to height h by the free-air gradient.
function M.gravityFrom(msg, h)
  return msg.g - M.FREE_AIR * ((tonumber(h) or 0) - (tonumber(msg.h) or 0))
end

-- The next value to write from have towards want: at most step away from have (no step limit
-- without a previous value of ours).
function M.gravityToward(have, want, step)
  if not have then return want end
  return have + math.max(-step, math.min(step, want - have))
end

-- One step of the gust process (Ornstein-Uhlenbeck around 0, std GUST_STD, time constant tau);
-- n is a unit-variance random number. The speed is ms + (gust - ms) * max(0, u).
function M.gustStep(u, dt, tau, n)
  local s = M.GUST_STD * math.sqrt(2 / tau)
  return clamp(u - u * dt / tau + s * math.sqrt(dt) * n, -1, 1)
end

-- The weather message -> the targets every value moves towards.
function M.targets(msg)
  local cover, wind = msg.cover or {}, msg.wind or {}
  local aloft = msg.aloft or wind
  local deck = math.max(tonumber(cover.low) or 0, tonumber(cover.mid) or 0) / 100
  local cc, cd = M.cirrusFor(cover.high)
  local wx, wy = M.cloudDir(wind.fromDeg)
  local cx, cy = M.cloudDir(aloft.fromDeg)
  local ms = tonumber(wind.ms) or 0
  return {
    coverage = M.coverageFor(deck),
    sunDim = M.sunDimFor(deck),
    cirrusCoverage = cc, cirrusDensity = cd,
    cloudAboveKm = clamp((tonumber(msg.baseAglM) or 1800) / 1000, M.CLOUD_ABOVE_MIN_KM, M.CLOUD_ABOVE_MAX_KM),
    cloudDirX = cx, cloudDirY = cy,
    cloudWindSpeed = clamp((tonumber(aloft.ms) or 0) * M.CLOUD_WIND_SCALE, M.CLOUD_WIND_MIN, M.CLOUD_WIND_MAX),
    fogSigma = M.fogSigma(msg.visM),
    fogHeight = msg.fog and M.FOG_HEIGHT_FOG or M.FOG_HEIGHT,
    windMs = ms, gustMs = math.max(ms, tonumber(wind.gustMs) or ms),
    windDirX = wx, windDirY = wy,
    rainPct = M.precipPct(msg.rainMmH, M.RAIN_FULL_MMH),
    snowPct = M.precipPct(msg.snowCmH, M.SNOW_FULL_CMH),
    tempC = tonumber(msg.slTempC) or 15,
  }
end

-- Exponential approach of every numeric field of cur towards tgt: 95 % after T seconds.
function M.step(cur, tgt, dt, T)
  local a = 1 - math.exp(-3 * dt / T)
  for k, v in pairs(tgt) do
    local c = cur[k]
    cur[k] = (type(c) == 'number') and (c + (v - c) * a) or v
  end
  return cur
end

-- The override groups touched by an onEnvironmentChanged table.
function M.overrideGroupsFor(changes)
  local out = {}
  for k in pairs(changes) do
    local g = M.OVERRIDE_GROUPS[k]
    if g then out[g] = true end
  end
  return out
end

-- BWM_Weather (the user's weather mod) changes the weather itself: automatic weather, or a
-- preset other than clear (its getWeatherState()).
function M.bwmFighting(state)
  if type(state) ~= 'table' then return false end
  if state.automaticWeather == true then return true end
  for _, k in ipairs({ 'targetPreset', 'currentPreset' }) do
    local p = state[k]
    if p ~= nil and p ~= '' and p ~= 'clear' then return true end
  end
  return false
end

local CODES = { [0] = 'clear', [1] = 'mainly clear', [2] = 'partly cloudy', [3] = 'overcast', [45] = 'fog', [48] = 'fog' }
function M.describe(code)
  code = tonumber(code) or -1
  if CODES[code] then return CODES[code] end
  if code >= 95 then return 'thunderstorm' elseif code >= 85 then return 'snow showers'
  elseif code >= 80 then return 'showers' elseif code >= 71 and code <= 77 then return 'snow'
  elseif code >= 61 and code <= 67 then return 'rain' elseif code >= 51 and code <= 57 then return 'drizzle' end
  return '?'
end

function M.compass(deg)
  local names = { 'N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW' }
  return names[math.floor(((tonumber(deg) or 0) % 360) / 45 + 0.5) % 8 + 1]
end

------------------------------------------------------------------------------------------
-- runtime

local mode, modeSource = 'real', 'auto' -- 'auto': follows BWM_Weather; 'user': setMode()
local lastMsg = nil                     -- the latest message, kept across level loads
local lastGravity = nil                 -- the latest 'gravity' message (server), kept the same way
local applying = false                  -- our own core_environment calls (their hook is not an override)
local S

local function resetState()
  S = {
    clock = 0, graceUntil = M.GRACE_S, quietUntil = 0, mission = false,
    override = {}, cur = nil, tgt = lastMsg and M.targets(lastMsg) or nil,
    applyTimer = 0, windTimer = 0, groundTimer = M.GROUND_CHECK_S, gravTimer = 0, bwmTimer = M.BWM_CHECK_S,
    groundZ = nil, gust = 0, gravity = nil,
    temp = { at = -math.huge, value = nil, method = nil }, dirtyFrames = 0,
    precip = { [M.RAIN_NAME] = {}, [M.SNOW_NAME] = {} },
    notified = false, bwm = false, inOurLevel = false,
    sun = { base = nil, applied = nil }, -- the level's ScatterSky.brightness, the value we wrote
  }
end
resetState()

local function env() return rawget(_G, 'core_environment') end
local function sky() return rawget(_G, 'beamngEarth_sky') end

local function envCall(fn, ...)
  if type(fn) ~= 'function' then return false end
  applying = true
  local ok, err = pcall(fn, ...)
  applying = false
  if not ok then log('W', 'beamngEarth', 'weather: ' .. tostring(err)) end
  return ok
end

local function notify(text)
  log('W', 'beamngEarth', 'weather: ' .. text)
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then pcall(gh.trigger, 'toastrMsg', { type = 'warning', title = 'BeamNG.Earth weather', msg = text }) end
end

local function levelInfo()
  local li = scenetree.theLevelInfo
  if li then return li end
  local names = scenetree.findClassObjects('LevelInfo')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end

local function playerPos()
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  return veh and veh:getPosition() or nil
end

local function groundUnder(p)
  local d = castRayStatic(vec3(p.x, p.y, p.z + 50), vec3(0, 0, -1), 5000)
  if d and d < 5000 then return p.z + 50 - d end
end

local function num(v, default) return tonumber(v) or default end

-- The values the engine has now, as the lerp's starting point (targets where unreadable).
local function readEngine(e, tgt)
  local c = {}
  for k, v in pairs(tgt) do c[k] = v end
  c.coverage = num(e.getCloudCover and e.getCloudCover(), tgt.coverage)
  c.cirrusCoverage = num(e.getCloudCirrusCoverage and e.getCloudCirrusCoverage(), tgt.cirrusCoverage)
  c.cirrusDensity = num(e.getCloudCirrusDensity and e.getCloudCirrusDensity(), tgt.cirrusDensity)
  c.cloudWindSpeed = num(e.getWindSpeed and e.getWindSpeed(), tgt.cloudWindSpeed)
  local d = e.getFogDensity and num((e.getFogDensity()), nil)
  local h = e.getFogAtmosphereHeight and num(e.getFogAtmosphereHeight(), nil)
  if d and h and h > 0 then
    c.fogHeight = h
    c.fogSigma = (M.FOG_FOLLOW_GROUND and S.groundZ) and d * math.exp(-S.groundZ / h) or d
  end
  local gw = e.getGroundWind and e.getGroundWind()
  if gw and gw.x then
    local l = math.sqrt(gw.x * gw.x + gw.y * gw.y)
    c.windMs = l
    if l > 0.01 then c.windDirX, c.windDirY = gw.x / l, gw.y / l end
  end
  local cw = e.getCloudWindDirection and e.getCloudWindDirection()
  if cw and cw.x and (cw.x ~= 0 or cw.y ~= 0) then c.cloudDirX, c.cloudDirY = cw.x, cw.y end
  c.rainPct, c.snowPct = 0, 0
  local sun = S.sun
  c.sunDim = (sun.applied and sun.base and sun.base > 0) and sun.applied / sun.base or 1
  return c
end

local function setIf(get, set, want, eps)
  local have = get and tonumber(get())
  if have and math.abs(have - want) <= eps then return end
  envCall(set, want)
end

-- Temperature: a flat temperatureCurveC at the sea-level temperature. core_environment re-reads
-- the curve when LevelInfo is editor-dirty (environment.lua onUpdate), so the flag is set for a few
-- frames. Which setter form the engine takes is found once per level (read back).
local function curveIs(li, tC)
  local ok, c = pcall(function() return li:getTemperatureCurveC() end)
  if not ok then return true end -- no getter: trust a setter that did not fail
  return type(c) == 'table' and type(c[1]) == 'table' and math.abs((tonumber(c[1][2]) or -999) - tC) < 0.01
end
local TEMP_METHODS = {
  table = function(li, tC) li:setTemperatureCurveC({ { 0, tC }, { 1, tC } }) end,
  field = function(li, tC) li:setField('temperatureCurveC', 0, string.format('0 %.2f 1 %.2f', tC, tC)) end,
}
local function writeTemperature(tC)
  local li = levelInfo()
  if not li then return false end
  local t = S.temp
  local order = t.method and { t.method } or { 'table', 'field' }
  for _, name in ipairs(order) do
    if pcall(TEMP_METHODS[name], li, tC) and curveIs(li, tC) then
      t.method = name
      pcall(function() li:setEditorDirty(true) end)
      S.dirtyFrames = 3
      return true
    end
  end
  t.method = 'n/a'
  return false
end

local function drivePrecip(name, pct, pool)
  local o = scenetree.findObject(name)
  if not o then return end
  local st = S.precip[name]
  if pct > 0 and st.pool ~= pool then
    o.numDrops = pool
    o:setField('numDrops', 0, tostring(pool))
    o:postApply()
    st.pool = pool
  end
  if st.pct == nil or math.abs(pct - st.pct) > 0.01 then
    if type(o.setPercentage) == 'function' then o:setPercentage(pct) end
    st.pct = pct
  end
  local hide = pct <= 0.001
  if st.hidden ~= hide then o:setHidden(hide); st.hidden = hide end
end

local function cameraZ()
  local cam = rawget(_G, 'core_camera')
  if not cam or not cam.getPositionXYZ then return nil end
  local ok, _, _, z = pcall(cam.getPositionXYZ)
  return ok and tonumber(z) or nil
end

local function applyPrecip(e, c)
  local rain, snow = c.rainPct, c.snowPct
  if S.override.precip then rain, snow = 0, 0 end
  -- no rain above the cloud base
  local alt = e.getCloudAltitudeKm and tonumber(e.getCloudAltitudeKm())
  local cz = cameraZ()
  if alt and cz and cz > alt * 1000 then rain, snow = 0, 0 end
  drivePrecip(M.RAIN_NAME, rain, M.RAIN_POOL)
  drivePrecip(M.SNOW_NAME, snow, M.SNOW_POOL)
end

-- The sun: ScatterSky.brightness = the level's own value * factor. Restored only while it still
-- holds our value (another mod may have set its own since).
local function sunObject()
  local names = scenetree.findClassObjects and scenetree.findClassObjects('ScatterSky')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end
local function writeSun(factor)
  local o = sunObject()
  if not o then return end
  local sun = S.sun
  local have = tonumber(o:getField('brightness', 0))
  if not have then return end
  if not sun.base or (sun.applied and math.abs(have - sun.applied) > 1e-3) then
    sun.base = have -- first write, or someone else changed it: their value is the new base
  end
  local want = sun.base * factor
  if sun.applied and math.abs(want - sun.applied) < M.SUN_EPS * sun.base then return end
  o:setField('brightness', 0, string.format('%.4f', want))
  o:postApply()
  sun.applied = tonumber(o:getField('brightness', 0)) or want
end
local function restoreSun()
  local sun = S.sun
  if not sun.applied then return end
  local o = sunObject()
  local have = o and tonumber(o:getField('brightness', 0))
  if have and math.abs(have - sun.applied) <= 1e-3 then
    o:setField('brightness', 0, string.format('%.4f', sun.base))
    o:postApply()
  end
  sun.applied = nil
end
M._restoreSun = function() restoreSun() end -- tests

local function apply(e)
  local c, o = S.cur, S.override
  if not o.clouds then
    setIf(e.getCloudCover, e.setCloudCover, c.coverage, 0.005)
    writeSun(c.sunDim)
  else
    restoreSun()
  end
  if not o.cirrus then
    setIf(e.getCloudCirrusCoverage, e.setCloudCirrusCoverage, c.cirrusCoverage, 0.005)
    setIf(e.getCloudCirrusDensity, e.setCloudCirrusDensity, c.cirrusDensity, 0.005)
  end
  if not o.cloudWind then
    local l = math.sqrt(c.cloudDirX ^ 2 + c.cloudDirY ^ 2)
    if l > 1e-3 then
      local x, y = c.cloudDirX / l, c.cloudDirY / l
      local have = e.getCloudWindDirection and e.getCloudWindDirection()
      if not (have and have.x and (have.x * x + have.y * y) > 0.9986) then envCall(e.setCloudWindDirection, x, y, 0) end -- 3 deg
    end
    setIf(e.getWindSpeed, e.setWindSpeed, c.cloudWindSpeed, 0.005)
  end
  if not o.fog and S.groundZ then
    local want = M.fogDensityFor(c.fogSigma, S.groundZ, c.fogHeight)
    local have = e.getFogDensity and tonumber((e.getFogDensity()))
    if not have or math.abs(have - want) > 0.02 * want then envCall(e.setFogDensity, want) end
    setIf(e.getFogAtmosphereHeight, e.setFogAtmosphereHeight, c.fogHeight, 1)
  end
  local sk = sky()
  if sk and sk.setCloudAboveKm then sk.setCloudAboveKm(c.cloudAboveKm) end
  if S.clock - S.temp.at >= M.TEMP_WRITE_S and (not S.temp.value or math.abs(S.temp.value - c.tempC) > M.TEMP_EPS_C) then
    S.temp.at = S.clock
    if writeTemperature(c.tempC) then S.temp.value = c.tempC end
  end
  applyPrecip(e, c)
end

local function applyWind(e, dt)
  if S.override.wind then return end
  local c = S.cur
  local speed = c.windMs
  if M.GUSTS then
    S.gust = M.gustStep(S.gust, dt, M.GUST_TAU_S, (math.random() * 2 - 1) * math.sqrt(3))
    speed = speed + (c.gustMs - c.windMs) * math.max(0, S.gust)
  end
  local l = math.sqrt(c.windDirX ^ 2 + c.windDirY ^ 2)
  local x, y = 0, 0
  if l > 1e-3 then x, y = speed * c.windDirX / l, speed * c.windDirY / l end
  local have = e.getGroundWind and e.getGroundWind()
  if have and have.x and math.abs(have.x - x) < 0.3 and math.abs(have.y - y) < 0.3 then return end
  envCall(e.setGroundWind, x, y, 0)
end

local function applyGravity(e)
  if S.override.gravity or not e.setGravity then return end
  local want = S.gravity
  if M.GRAVITY_ENABLED then
    local p = playerPos()
    if not p then return end
    if lastGravity then
      want = M.gravityFrom(lastGravity, p.z)
    else
      local names = scenetree.findClassObjects('TimeOfDay')
      local tod = names and names[1] and scenetree.findObject(names[1])
      if not tod then return end
      want = M.gravityAt(tonumber(tod.latitude) or 0, p.z)
    end
  elseif S.gravity == nil then
    return -- never set: nothing to restore
  else
    want = M.GRAVITY_DEFAULT
  end
  local have = e.getGravity and tonumber(e.getGravity())
  if have and math.abs(-have - want) <= M.GRAVITY_EPS then
    if not M.GRAVITY_ENABLED then S.gravity = nil end
    return
  end
  if M.GRAVITY_ENABLED then want = M.gravityToward(S.gravity, want, M.GRAVITY_STEP) end
  if envCall(e.setGravity, -want) then S.gravity = M.GRAVITY_ENABLED and want or nil end
end

local function hidePrecip()
  for name in pairs(S.precip) do pcall(drivePrecip, name, 0, 0) end
  pcall(restoreSun)
end

local function checkBwm()
  local b = rawget(_G, 'BWM_Weather_main')
  local st = nil
  if b and b.getWeatherState then
    local ok, v = pcall(b.getWeatherState)
    if ok then st = v end
  end
  S.bwm = M.bwmFighting(st)
  if modeSource == 'auto' then
    local want = S.bwm and 'manual' or 'real'
    if want ~= mode then
      mode = want
      if mode == 'manual' then hidePrecip() else S.cur = nil end
    end
  end
  if S.bwm and not S.notified then
    S.notified = true
    notify(mode == 'manual'
      and 'BWM_Weather is changing the weather, so real weather is off. Set BWM_Weather to Clear with automatic weather off to use real weather.'
      or 'BWM_Weather is also changing the weather: set it to Clear with automatic weather off, or turn real weather off.')
  end
end

local function update(dt)
  if not S.inOurLevel or not scenetree.MissionGroup then return end
  local e = env()
  if not e then return end
  S.bwmTimer = S.bwmTimer + dt
  if S.bwmTimer >= M.BWM_CHECK_S then S.bwmTimer = 0; checkBwm() end
  S.groundTimer = S.groundTimer + dt
  if S.groundTimer >= M.GROUND_CHECK_S then
    S.groundTimer = 0
    local p = playerPos()
    if p then S.groundZ = groundUnder(p) or S.groundZ end
  end
  if S.clock < S.graceUntil or S.mission then return end
  S.gravTimer = S.gravTimer + dt
  if S.gravTimer >= M.GRAVITY_CHECK_S then S.gravTimer = 0; applyGravity(e) end
  if mode ~= 'real' or not S.tgt or S.clock < S.quietUntil then return end
  if not S.cur then S.cur = readEngine(e, S.tgt) end
  M.step(S.cur, S.tgt, dt, M.LERP_S)
  S.applyTimer = S.applyTimer + dt
  if S.applyTimer >= M.APPLY_S then S.applyTimer = 0; apply(e) end
  S.windTimer = S.windTimer + dt
  if S.windTimer >= M.WIND_APPLY_S then applyWind(e, S.windTimer); S.windTimer = 0 end
end

------------------------------------------------------------------------------------------
-- hooks and console API

function M.onWeather(msg)
  if type(msg) ~= 'table' then return end
  lastMsg = msg
  S.tgt = M.targets(msg)
  local sk = sky()
  local off = tonumber(msg.utcOffsetS)
  if sk and sk.setRealUtcOffset and off then sk.setRealUtcOffset(off / 3600, tonumber(msg.lat), tonumber(msg.lon)) end
end

-- The server's real gravity at the player (server/gravity.py; beamngEarth_core hands it over).
function M.onGravity(msg)
  if type(msg) ~= 'table' or type(msg.g) ~= 'number' or msg.g < 9 or msg.g > 10.5 then return end
  lastGravity = { g = msg.g, h = tonumber(msg.h) or 0, source = msg.source, lat = msg.lat, lon = msg.lon }
  S.gravTimer = M.GRAVITY_CHECK_S -- applied on the next update
end

-- A new server session (core, on hello): back to normal gravity until its message arrives.
function M.clearGravity() lastGravity = nil end

-- One line for the settings page: the gravity in use and where it comes from.
function M.gravityLine()
  if not M.GRAVITY_ENABLED then return 'off (level default ' .. M.GRAVITY_DEFAULT .. ' m/s2)' end
  local src = lastGravity and (lastGravity.source == 'egm2008' and 'EGM2008 (server)' or 'normal (server)')
    or 'normal by latitude (no server value)'
  return (S.gravity and string.format('%.5f m/s2', S.gravity) or 'not set yet') .. ', ' .. src
end

function M.onUpdate(dtReal)
  dtReal = dtReal or 0
  S.clock = S.clock + dtReal
  if S.dirtyFrames > 0 then
    S.dirtyFrames = S.dirtyFrames - 1
    if S.dirtyFrames == 0 then
      local li = levelInfo()
      if li then pcall(function() li:setEditorDirty(false) end) end
    end
  end
  local ok, err = pcall(update, dtReal)
  if not ok then log('W', 'beamngEarth', 'weather: ' .. tostring(err)) end
end

function M.onEnvironmentChanged(changes)
  if applying or type(changes) ~= 'table' then return end
  if S.clock < S.graceUntil or S.mission or not S.inOurLevel then return end
  local touched = M.overrideGroupsFor(changes)
  if not next(touched) then return end
  for g in pairs(touched) do S.override[g] = S.clock end
  S.quietUntil = S.clock + M.USER_QUIET_S
  if touched.precip then hidePrecip() end
  if touched.cloudAltitude and sky() and sky().setCloudFollow then sky().setCloudFollow(false) end
end

function M.onAnyMissionChanged(state)
  if state == 'started' then
    S.mission = true
    pcall(restoreSun)
    for g, t in pairs(S.override) do
      if t >= S.clock - M.MISSION_SETUP_S then S.override[g] = nil end -- the mission's own setup
    end
  elseif state == 'stopped' then
    S.mission = false
    S.graceUntil = math.max(S.graceUntil, S.clock + M.MISSION_RESUME_S) -- its restore ran just before
    S.cur = nil
  end
end

local function onLevel()
  resetState()
  S.inOurLevel = getCurrentLevelIdentifier and getCurrentLevelIdentifier() == M.LEVEL_ID
  local sk = sky()
  if sk and sk.setCloudFollow then sk.setCloudFollow(true) end
end
M.onClientStartMission = onLevel
function M.onClientEndMission() resetState() end
function M.onExtensionLoaded()
  if scenetree.MissionGroup and getCurrentLevelIdentifier and getCurrentLevelIdentifier() then onLevel() end
end

-- 'real' or 'manual' (the user's choice), or 'auto' (the default: real unless BWM_Weather is
-- changing the weather).
function M.setMode(m)
  if m ~= 'real' and m ~= 'manual' and m ~= 'auto' then return 'mode must be "real", "manual" or "auto"' end
  modeSource = (m == 'auto') and 'auto' or 'user'
  if m == 'auto' then
    S.bwmTimer = M.BWM_CHECK_S -- decided on the next update
    m = mode
  end
  mode = m
  if mode == 'real' then
    S.override, S.cur, S.quietUntil = {}, nil, 0
    local sk = sky()
    if sk and sk.setCloudFollow then sk.setCloudFollow(true) end
  else
    hidePrecip()
  end
  return 'weather mode: ' .. mode
end
function M.getMode() return mode end

function M.setGravityEnabled(on)
  M.GRAVITY_ENABLED = on and true or false
  S.gravTimer = M.GRAVITY_CHECK_S
  return 'real gravity ' .. (M.GRAVITY_ENABLED and 'on' or 'off')
end

-- One line for the BeamNG.Earth window.
function M.statusLine()
  local m = lastMsg
  local head = 'Weather (' .. mode .. (next(S.override) and ', manual overrides' or '') .. ')'
  if not m then return head .. ': no data yet' end
  local w = m.wind or {}
  return string.format('%s: %.0f C, %s, wind %.0f m/s from %s, visibility %s', head, tonumber(m.tempC) or 0,
    M.describe(m.code), tonumber(w.ms) or 0, M.compass(w.fromDeg),
    m.visM and string.format('%.0f km', m.visM / 1000) or '?')
end

function M.status()
  local overrides = {}
  for g in pairs(S.override) do overrides[#overrides + 1] = g end
  table.sort(overrides)
  return dumps({ mode = mode, modeSource = modeSource, bwmFighting = S.bwm, overrides = overrides,
    inOurLevel = S.inOurLevel, grace = math.max(0, S.graceUntil - S.clock), mission = S.mission,
    groundZ = S.groundZ, gravity = S.gravity, gravityMsg = lastGravity, temperatureCurve = S.temp.method, tempC = S.temp.value,
    msg = lastMsg, target = S.tgt, current = S.cur, line = M.statusLine() })
end

return M
