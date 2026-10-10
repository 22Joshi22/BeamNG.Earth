-- luajit mod/tests/test_weather.lua
-- beamngEarth/weather.lua: the pure conversions (calibration curve, fog, wind, rain, gravity, gusts,
-- the targets of the two recorded Open-Meteo fixtures as server/weather.py derives them), then the
-- runtime against a stubbed core_environment: grace period, smooth approach, writes only on change,
-- manual overrides from the pause menu, BWM_Weather coexistence, missions, gravity, rain objects and
-- the temperature curve.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local W = require('beamngEarth/weather')
local check, near = T.check, T.near

------------------------------------------------------------------------------------------
-- pure

-- the calibration curve: its own rows, monotone, clamped at both ends
for _, row in ipairs(W.COVERAGE_CURVE) do near(W.coverageFor(row[1]), row[2], 1e-12, 'curve row ' .. row[1]) end
local prev = -1
for i = 0, 100 do
  local c = W.coverageFor(i / 100)
  check(c >= prev, 'coverage monotone at ' .. i)
  prev = c
end
check(W.coverageFor(-1) == 0 and W.coverageFor(2) == W.COVERAGE_CURVE[#W.COVERAGE_CURVE][2], 'curve clamps')
near(W.coverageFor(0.13), (0.79 + 0.90) / 2, 1e-9, 'linear between probe rows')
near(W.coverageFor(0.5, { { 0, 0 }, { 1, 3 } }), 1.5, 1e-12, 'a curve can be passed in')

-- cirrus: none without high cloud; density in its range
local cc, cd = W.cirrusFor(0)
check(cc == 0 and cd == 0, 'no high cloud: no cirrus')
cc, cd = W.cirrusFor(100)
check(cc == 1 and math.abs(cd - W.CIRRUS_DENSITY_MAX) < 1e-12, 'full high cloud')
cc, cd = W.cirrusFor(30)
check(math.abs(cc - 0.3) < 1e-12 and cd > W.CIRRUS_DENSITY_MIN and cd < W.CIRRUS_DENSITY_MAX, 'partial cirrus')

-- fog: Koschmieder, and the height compensation round trip (density at z = 0 thins to sigma at groundZ)
near(W.fogSigma(3912), 1e-3, 1e-15, 'sigma = 3.912 / V')
near(W.fogSigma(nil), W.FOG_K / W.FOG_VIS_DEFAULT, 1e-15, 'no visibility: the default')
near(W.fogSigma(1), W.FOG_K / W.FOG_VIS_MIN, 1e-15, 'visibility clamped low')
local sigma, zg, H = W.fogSigma(36300), 4000, 1000
local d = W.fogDensityFor(sigma, zg, H)
near(d * math.exp(-zg / H), sigma, 1e-15, 'compensated density gives sigma at the ground')
near(W.fogDensityFor(sigma, nil, H), sigma, 1e-15, 'no ground: no compensation')
check(W.fogDensityFor(1e-2, 8000, 500) == W.FOG_DENSITY_MAX, 'density capped at the menu maximum')

-- wind: a west wind (from 270) blows towards +x (east); a north wind towards -y
local x, y = W.windVec(10, 270)
near(x, 10, 1e-9, 'west wind x'); near(y, 0, 1e-9, 'west wind y')
x, y = W.windVec(5, 0)
near(x, 0, 1e-9, 'north wind x'); near(y, -5, 1e-9, 'north wind y')
x, y = W.cloudDir(90)
near(x, -1, 1e-9, 'east wind: clouds drift west'); near(math.sqrt(x * x + y * y), 1, 1e-12, 'unit vector')

-- rain/snow fraction
check(W.precipPct(0, 8) == 0 and W.precipPct(0.04, 8) == 0, 'dry')
check(W.precipPct(0.051, 8) == W.PRECIP_MIN, 'drizzle: at least PRECIP_MIN')
near(W.precipPct(2, 8), 0.5, 1e-12, 'sqrt law')
check(W.precipPct(50, 8) == 1, 'capped at the full pool')

-- normal gravity (Somigliana): equator, pole, 45 deg, and the free-air gradient
near(W.gravityAt(0, 0), 9.7803253359, 1e-9, 'equator')
near(W.gravityAt(90, 0), 9.8321849379, 1e-7, 'pole')
near(W.gravityAt(-90, 0), W.gravityAt(90, 0), 1e-12, 'symmetric')
near(W.gravityAt(45, 0), 9.8061977694, 1e-9, '45 deg (WGS84 Somigliana)')
near(W.gravityAt(36.58, 4400), W.gravityAt(36.58, 0) - 3.086e-6 * 4400, 1e-12, 'free-air gradient')

-- gusts: bounded, and decay without noise
local u = 0.8
for _ = 1, 40 do u = W.gustStep(u, 0.5, W.GUST_TAU_S, 0) end
check(math.abs(u) < 0.01, 'gust decays to 0 without noise, got ' .. u)
u = 0
for i = 1, 2000 do
  u = W.gustStep(u, 1, W.GUST_TAU_S, (i % 2 == 0) and 1.7 or -1.6)
  if u < -1 or u > 1 then break end
end
check(u >= -1 and u <= 1, 'gust bounded')

-- step: 95 % of a jump after LERP_S, exact for any number of steps
local cur, tgt = { a = 0, b = 10 }, { a = 1, b = 10 }
for _ = 1, 600 do W.step(cur, tgt, W.LERP_S / 600, W.LERP_S) end
near(cur.a, 1 - math.exp(-3), 1e-9, 'step: 95 % after LERP_S')
check(cur.b == 10, 'step: unchanged field stays')
local c1 = W.step({ a = 0 }, { a = 1 }, W.LERP_S, W.LERP_S)
near(c1.a, cur.a, 1e-9, 'step: frame-rate independent')

-- describe / compass
check(W.describe(3) == 'overcast' and W.describe(45) == 'fog' and W.describe(63) == 'rain'
  and W.describe(73) == 'snow' and W.describe(95) == 'thunderstorm' and W.describe(81) == 'showers', 'describe')
check(W.compass(232) == 'SW' and W.compass(98) == 'E' and W.compass(359) == 'N', 'compass')

-- override groups and the BWM_Weather rule
local g = W.overrideGroupsFor({ cloudCover = 1, fogDensity = 2, time = 0.3, cloudWeatherSeed = 4 })
check(g.clouds and g.fog and not g.time and not g.cloudWeatherSeed, 'override groups')
check(not W.bwmFighting(nil), 'no BWM')
check(not W.bwmFighting({ currentPreset = 'clear', targetPreset = 'clear', automaticWeather = false }), 'BWM on clear')
check(W.bwmFighting({ currentPreset = 'clear', targetPreset = 'clear', automaticWeather = true }), 'BWM automatic')
check(W.bwmFighting({ currentPreset = 'cloudy', targetPreset = 'cloudy' }), 'BWM preset')
check(W.bwmFighting({ currentPreset = 'clear', targetPreset = 'lightRain' }), 'BWM transitioning to rain')

-- The two recorded fixtures (server/tests/fixtures/openmeteo_*.json, 2026-10-10 ~20:45 UTC) as
-- server/weather.py derive() turns them into messages (test_weather.py checks the same messages
-- through this module under luajit).
local BALTIMORE = { type = 'weather', v = 1, t = 1791665100, lat = 39.29, lon = -76.53, code = 3, isDay = true,
  cover = { total = 100, low = 0, mid = 100, high = 100 }, baseM = 3000, baseAglM = 2967, rainMmH = 0, snowCmH = 0,
  visM = 36300, fog = false, thunder = false, wind = { ms = 3.11, fromDeg = 98, gustMs = 6.7 }, tempC = 20.5,
  dewC = 11.4, elevM = 33, slTempC = 20.71, pressureHpa = 1023.8, credit = 'Weather data by Open-Meteo.com',
  aloft = { ms = 5.62, fromDeg = 211 }, utcOffsetS = -14400, tz = 'America/New_York' }
local WHITNEY = { type = 'weather', v = 1, t = 1791665100, lat = 36.58, lon = -118.29, code = 1, isDay = true,
  cover = { total = 43, low = 0, mid = 43, high = 30 }, baseM = 6317, baseAglM = 2275, rainMmH = 0, snowCmH = 0,
  visM = 66300, fog = false, thunder = false, wind = { ms = 8.81, fromDeg = 232, gustMs = 13.2 }, tempC = 3.5,
  dewC = -14.7, elevM = 4042, slTempC = 29.77, pressureHpa = 619.4, credit = 'Weather data by Open-Meteo.com',
  aloft = { ms = 5.37, fromDeg = 215 }, utcOffsetS = -25200, tz = 'America/Los_Angeles' }

local b = W.targets(BALTIMORE)
near(b.coverage, W.COVERAGE_CURVE[#W.COVERAGE_CURVE][2], 1e-12, 'Baltimore: overcast mid deck -> the top of the curve')
near(b.cirrusCoverage, 1, 1e-12, 'Baltimore: full cirrus')
near(b.cloudAboveKm, 2.967, 1e-9, 'Baltimore: mid-deck base ~3 km above the ground')
near(b.fogSigma, 3.912 / 36300, 1e-15, 'Baltimore: sigma from 36.3 km')
check(b.fogHeight == W.FOG_HEIGHT, 'Baltimore: no fog code')
near(b.windMs, 3.11, 1e-12, 'Baltimore wind'); near(b.gustMs, 6.7, 1e-12, 'Baltimore gust')
check(b.windDirX < -0.9 and math.abs(b.windDirY) < 0.2, 'Baltimore: easterly wind blows west')
check(b.cloudDirX > 0 and b.cloudDirY > 0, 'Baltimore: SW wind aloft drifts clouds NE')
near(b.cloudWindSpeed, 5.62 * W.CLOUD_WIND_SCALE, 1e-12, 'Baltimore cloud drift from the 700 hPa wind')
check(b.rainPct == 0 and b.snowPct == 0, 'Baltimore: dry')
near(b.tempC, 20.71, 1e-12, 'Baltimore: sea-level temperature')

local wt = W.targets(WHITNEY)
near(wt.coverage, W.coverageFor(0.43), 1e-12, 'Whitney: 43 % mid deck')
check(wt.coverage > 0.9 and wt.coverage < 1.1, 'Whitney: partly cloudy on the curve')
near(wt.cirrusCoverage, 0.3, 1e-12, 'Whitney cirrus')
near(wt.cloudAboveKm, 2.275, 1e-9, 'Whitney base above the ground')
x, y = W.windVec(8.81, 232)
near(wt.windDirX * 8.81, x, 1e-9, 'Whitney wind x'); near(wt.windDirY * 8.81, y, 1e-9, 'Whitney wind y')
check(x > 0 and y > 0, 'Whitney: SW wind blows NE')
near(wt.tempC, 29.77, 1e-12, 'Whitney: summit 3.5 C at 4042 m is 29.8 C at sea level')

-- rain, snow and fog messages
local wet = W.targets({ rainMmH = 2, snowCmH = 0, fog = true, visM = 300, cover = { low = 90, mid = 20 } })
near(wet.rainPct, 0.5, 1e-12, 'rain 2 mm/h')
check(wet.fogHeight == W.FOG_HEIGHT_FOG and math.abs(wet.fogSigma - 3.912 / 300) < 1e-12, 'fog')
local snowy = W.targets({ rainMmH = 0, snowCmH = 0.5 })
near(snowy.snowPct, 0.5, 1e-12, 'snow 0.5 cm/h')
check(snowy.cloudDirX ~= nil and snowy.tempC == 15 and snowy.windMs == 0, 'missing fields: defaults')

------------------------------------------------------------------------------------------
-- runtime (stubbed engine)

local frames = 0
local env = { cover = 0.5, cirrusCov = 0.3, cirrusDen = 0, windSpeed = 0.1, cloudDir = vec3(1, 0, 0), fog = 4e-5,
  fogH = 1000, ground = vec3(0, 0, 0), gravity = -9.81, cloudAlt = 1.5, calls = {}, hooks = 0 }
local weatherExt -- the extension (for the hook)
local function hook(changes)
  env.hooks = env.hooks + 1
  weatherExt.onEnvironmentChanged(changes)
end
local function count(n) env.calls[n] = (env.calls[n] or 0) + 1 end
core_environment = {
  getCloudCover = function() return env.cover end,
  setCloudCover = function(v) count('cover'); hook({ cloudCover = v }); env.cover = v end,
  getCloudCirrusCoverage = function() return env.cirrusCov end,
  setCloudCirrusCoverage = function(v) count('cirrusCov'); hook({ cloudCirrusCoverage = v }); env.cirrusCov = v end,
  getCloudCirrusDensity = function() return env.cirrusDen end,
  setCloudCirrusDensity = function(v) count('cirrusDen'); hook({ cloudCirrusDensity = v }); env.cirrusDen = v end,
  getWindSpeed = function() return env.windSpeed end,
  setWindSpeed = function(v) count('windSpeed'); hook({ windSpeed = v }); env.windSpeed = v end,
  getCloudWindDirection = function() return vec3(env.cloudDir.x, env.cloudDir.y, 0) end,
  setCloudWindDirection = function(x, y) count('cloudDir'); hook({ cloudWindDirection = vec3(x, y, 0) }); env.cloudDir = vec3(x, y, 0) end,
  getFogDensity = function() return env.fog, {} end,
  setFogDensity = function(v) count('fog'); hook({ fogDensity = v }); env.fog = v end,
  getFogAtmosphereHeight = function() return env.fogH end,
  setFogAtmosphereHeight = function(v) count('fogH'); env.fogH = v end, -- no hook (environment.lua)
  getGroundWind = function() return vec3(env.ground.x, env.ground.y, env.ground.z) end,
  setGroundWind = function(x, y, z) count('ground'); hook({ groundWind = vec3(x, y, z) }); env.ground = vec3(x, y, z) end,
  getGravity = function() return env.gravity end,
  setGravity = function(v) count('gravity'); hook({ gravity = v }); env.gravity = v end,
  getCloudAltitudeKm = function() return env.cloudAlt end,
}
local sky = { offsets = {}, above = nil, follow = true }
beamngEarth_sky = {
  setRealUtcOffset = function(h, lat, lon) sky.offsets[#sky.offsets + 1] = { h, lat, lon } end,
  setCloudAboveKm = function(km) sky.above = km end,
  setCloudFollow = function(on) sky.follow = on end,
}
local toasts = {}
guihooks = { trigger = function(name, data) toasts[#toasts + 1] = { name, data } end }
local camZ = 10
core_camera = { getPositionXYZ = function() return 0, 0, camZ end }
local veh = { pos = vec3(0, 0, 4000) }
function veh:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function getPlayerVehicle() return veh end
function castRayStatic(from) return from.z - 3990 end -- ground at z = 3990
local level = 'beamng_earth'
function getCurrentLevelIdentifier() return level end

-- level objects: TimeOfDay, LevelInfo (temperature curve with a table setter), the precipitation
local tod = S.newObject('TimeOfDay'); tod:registerObject('tod'); tod.latitude = 36.58
local li = S.newObject('LevelInfo'); li:registerObject('theLevelInfo')
li.curve = { { 0, 15 }, { 1, 15 } }
function li:getTemperatureCurveC() return self.curve end
function li:setTemperatureCurveC(c) self.curve = c end
function li:setEditorDirty(v) self.dirty = v end
scenetree.theLevelInfo = li
local function precip(name)
  local o = S.newObject('Precipitation'); o:registerObject(name)
  function o:setPercentage(p) self.pct = p end
  function o:postApply() self.applied = (self.applied or 0) + 1 end
  return o
end
local rain, snow = precip(W.RAIN_NAME), precip(W.SNOW_NAME)

weatherExt = W
local function run(seconds, dt)
  dt = dt or 0.1
  for _ = 1, math.floor(seconds / dt + 0.5) do frames = frames + 1; W.onUpdate(dt) end
end

W.onClientStartMission()
check(W.getMode() == 'real', 'default mode real (no BWM)')
W.onWeather(WHITNEY)
check(#sky.offsets == 1 and sky.offsets[1][1] == -7 and sky.offsets[1][2] == 36.58, 'real UTC offset to sky')

-- grace: nothing written, and env changes are not overrides
run(W.GRACE_S - 1)
check(next(env.calls) == nil, 'no writes during the grace period')
W.onEnvironmentChanged({ cloudCover = 0, fogDensity = 0 })          -- BWM's start transition
run(2)
check(env.calls.cover and env.calls.fog, 'writes after the grace period')
check(env.hooks > 0, 'our own writes fire the hook ...')
local st = W.status()
check(type(st) == 'string', 'status is a string')

-- smooth approach: coverage moves from 0.5 towards Whitney's target, 95 % after LERP_S
local want = wt.coverage
run(W.LERP_S)
check(math.abs(env.cover - want) < 0.06 * math.abs(want - 0.5) + 0.006, 'coverage approached the target: ' .. env.cover)
near(env.cirrusCov, wt.cirrusCoverage, 0.03, 'cirrus coverage')
check(env.cirrusDen > W.CIRRUS_DENSITY_MIN * 0.9, 'cirrus density set')
-- fog: compensated for the ground at 3990 m
local fogWant = W.fogDensityFor(wt.fogSigma, 3990, W.FOG_HEIGHT)
check(math.abs(env.fog - fogWant) < 0.1 * fogWant, 'fog density compensated for the ground')
near(env.fogH, W.FOG_HEIGHT, 1e-9, 'fog height')
near(sky.above, wt.cloudAboveKm, 0.1, 'cloud base to sky')
-- ground wind towards NE at about the mean speed (gusts on top)
local gw = env.ground
local speed = math.sqrt(gw.x ^ 2 + gw.y ^ 2)
check(gw.x > 0 and gw.y > 0 and speed >= 8.81 * 0.9 and speed <= 13.2 + 0.5, 'ground wind NE, mean..gust: ' .. speed)
-- temperature curve (table setter, editor-dirty for a few frames, then cleared)
check(math.abs(li.curve[1][2] - 29.77) < 1 and li.curve[2][2] == li.curve[1][2], 'flat temperature curve near 29.8')
check(li.dirty == false, 'editor-dirty cleared')
-- gravity: latitude 36.58, height 4000
near(-env.gravity, W.gravityAt(36.58, 4000), W.GRAVITY_EPS, 'gravity by latitude and height')
-- dry: the precipitation stays hidden, no pool
check(rain.hidden == true and snow.hidden == true and rain.applied == nil, 'dry: precipitation hidden')

-- writes only on change: once converged, no calls (the ground wind's gusts keep changing)
run(2 * W.LERP_S)
local before = {}
for k, v in pairs(env.calls) do before[k] = v end
run(30)
check(env.calls.cover == before.cover and env.calls.fog == before.fog and env.calls.gravity == before.gravity,
  'steady state: no cloud, fog or gravity writes')

-- rain arrives: the pool is set once and the fraction follows
W.onWeather({ cover = { low = 100, mid = 50, high = 0 }, rainMmH = 8, snowCmH = 0, visM = 5000, slTempC = 12,
  wind = { ms = 5, fromDeg = 180, gustMs = 9 }, baseAglM = 600, lat = 36.58, lon = -118.29 })
run(W.LERP_S)
check(rain.hidden == false and rain.fields.numDrops == tostring(W.RAIN_POOL) and rain.applied == 1, 'rain pool set once')
check(rain.pct > 0.9, 'rain fraction approached 1: ' .. tostring(rain.pct))
check(snow.hidden == true, 'no snow')
-- the camera above the cloud base: no rain
camZ = 5000
run(1)
check(rain.hidden == true, 'no rain above the clouds')
camZ = 10
run(1)
check(rain.hidden == false, 'rain again below the clouds')

-- a manual change in the pause menu (setState fires the hook with the changed field): fog is now
-- the user's; clouds stay real
local fogCalls = env.calls.fog
W.onEnvironmentChanged({ fogDensity = 25 })
env.fog = 0.025
run(W.USER_QUIET_S - 0.5)
local coverCalls = env.calls.cover
run(10)
check(env.calls.fog == fogCalls and env.fog == 0.025, 'fog overridden: not written again')
check(env.calls.cover > coverCalls, 'clouds still driven')
check(W.statusLine():find('manual overrides', 1, true) ~= nil, 'status line shows the override')
-- back to real: the override is cleared and fog is driven again
W.setMode('real')
run(2)
check(env.calls.fog > fogCalls, 'setMode(real) clears the overrides')

-- manual mode: hands off, precipitation hidden
W.setMode('manual')
local n0 = env.calls.cover
run(5)
check(env.calls.cover == n0 and rain.hidden == true, 'manual: no writes, rain hidden')
W.setMode('real')

-- missions: their own setup changes are not overrides, nothing is written while they run
W.onEnvironmentChanged({ cloudCover = 0.2 })   -- the mission's setup, just before 'started'
env.cover = 0.2
W.onAnyMissionChanged('started')
n0 = env.calls.cover
run(10)
check(env.calls.cover == n0, 'mission: paused')
W.onAnyMissionChanged('stopped')
run(W.MISSION_RESUME_S + 2)
check(env.calls.cover > n0 and env.cover > 0.3, 'mission stopped: resumed (its setup was not an override)')

-- gravity: a manual gravity change (the menu's presets) is the user's; switching it off restores 9.81
W.setMode('real')
W.setGravityEnabled(false)
run(2)
near(env.gravity, -W.GRAVITY_DEFAULT, 1e-12, 'gravity off: the level default')
W.setGravityEnabled(true)
run(2)
near(-env.gravity, W.gravityAt(36.58, 4000), W.GRAVITY_EPS, 'gravity on again')
W.onEnvironmentChanged({ gravity = -1.62 })
env.gravity = -1.62
run(5)
check(env.gravity == -1.62, 'gravity override respected')

-- the server's real gravity (server/gravity.py 'gravity' message): carried to the player's height
-- by the free-air gradient, approached in GRAVITY_STEP steps per check, back to normal gravity on
-- a new session (clearGravity)
near(W.gravityFrom({ g = 9.79, h = 100 }, 600), 9.79 - 500 * W.FREE_AIR, 1e-12, 'gravityFrom: free air')
check(W.gravityToward(nil, 9.8, 1e-3) == 9.8 and W.gravityToward(9.8, 9.7995, 1e-3) == 9.7995,
  'gravityToward: no previous value or within a step: the value itself')
near(W.gravityToward(9.79, 9.8, 1e-3), 9.791, 1e-12, 'gravityToward: one limited step')
W.setMode('real')                                   -- clears the gravity override
run(2)
local gNormal = W.gravityAt(36.58, 4000)
near(-env.gravity, gNormal, W.GRAVITY_EPS, 'override cleared: normal gravity')
check(W.gravityLine():find('no server value', 1, true) ~= nil, 'gravity line: normal by latitude')
local gmsg = { type = 'gravity', v = 1, g = gNormal + 0.0035, h = 3900, lat = 36.58, lon = -118.29, source = 'egm2008' }
local gWant = W.gravityFrom(gmsg, 4000)
W.onGravity(gmsg)
run(0.1)
near(-env.gravity, gNormal + W.GRAVITY_STEP, 1e-9, 'server gravity: the first step only')
run(W.GRAVITY_CHECK_S + 0.05)
near(-env.gravity, gNormal + 2 * W.GRAVITY_STEP, 1e-9, 'server gravity: one step per check')
run(5)
near(-env.gravity, gWant, 1e-12, 'server gravity reached (free air for the 100 m since)')
check(W.gravityLine():find('EGM2008', 1, true) ~= nil, 'gravity line: EGM2008')
local nGrav = env.calls.gravity
W.onGravity({ g = 'x' })
W.onGravity({ g = 1.62 })
run(3)
check(env.calls.gravity == nGrav and math.abs(-env.gravity - gWant) < 1e-12, 'invalid gravity messages ignored')
veh.pos = vec3(0, 0, 4300)                          -- a climb: free air from the server's value
run(2)
near(-env.gravity, W.gravityFrom(gmsg, 4300), W.GRAVITY_EPS, 'climb: free-air gradient')
veh.pos = vec3(0, 0, 4000)
W.clearGravity()
run(5)
near(-env.gravity, gNormal, W.GRAVITY_EPS, 'new session: normal gravity until its message')

-- BWM_Weather: set to a preset -> real weather steps back (auto mode) with one notice
W.setMode('auto')
W.onClientEndMission()
W.onClientStartMission()
BWM_Weather_main = { getWeatherState = function() return { currentPreset = 'cloudy', targetPreset = 'cloudy', automaticWeather = false } end }
run(1)
check(W.getMode() == 'manual', 'BWM fighting: manual by default')
check(#toasts == 1 and toasts[1][1] == 'toastrMsg', 'one notice')
run(10)
check(#toasts == 1, 'notice once')
BWM_Weather_main = { getWeatherState = function() return { currentPreset = 'clear', targetPreset = 'clear', automaticWeather = false } end }
run(W.BWM_CHECK_S + 0.5)
check(W.getMode() == 'real', 'BWM on clear: real again')

-- other levels: nothing happens there
level = 'west_coast_usa'
W.onClientEndMission()
W.onClientStartMission()
for k in pairs(env.calls) do env.calls[k] = 0 end
run(W.GRACE_S + 5)
local any = 0
for _, v in pairs(env.calls) do any = any + v end
check(any == 0, 'no weather or gravity outside the BeamNG.Earth level')

T.done('test_weather')
