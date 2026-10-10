-- BeamNG.Earth: real-world sky. Keeps the level's TimeOfDay on the real date and clock for
-- wherever the player is, so core_celestial draws the real sun, moon and stars.
--
-- core_celestial (core/celestial.lua) computes the sky from TimeOfDay year/month/day, `time`
-- (civil local clock as a 0..1 day fraction, noon = 0; celestial.lua:313-318) and the UTC offset
-- (tod.utcOffset if set, else derived from longitude; celestial.lua:285-296). We always write
-- utcOffset explicitly together with a matching local `time`, so the instant (Julian date) is
-- exact even where the timezone guess is wrong; a wrong guess only shifts the displayed clock.
-- Latitude/longitude are kept current by beamngEarth_core (core_celestial.setLocation).
-- The real UTC offset (Open-Meteo timezone=auto, from beamngEarth_weather) replaces the guess
-- within REAL_TZ_KM of the point it was reported for.
--
-- Modes: 'real'   = real UTC now, clock runs at real speed, re-synced every RESYNC_S seconds.
--        'manual' = hands off (use the game's own environment/time controls).

local M = {}

local RESYNC_S = 30
M.REAL_TZ_KM = 200

local mode = 'real'
local timer = RESYNC_S -- sync on the first update
local realTz = nil     -- { hours, lat, lon }: the real UTC offset at a point (setRealUtcOffset)

-- Day of week, 0 = Sunday (Sakamoto), and the Sunday helpers mirror celestial.lua:261-283 so the
-- clock we display agrees with what core_celestial would derive on its own.
local function dow(y, m, d)
  local t = { 0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4 }
  if m < 3 then y = y - 1 end
  return (y + math.floor(y / 4) - math.floor(y / 100) + math.floor(y / 400) + t[m] + d) % 7
end
local function lastSunday(y, m) local last = (m == 4 or m == 9 or m == 11) and 30 or 31; return last - dow(y, m, last) end
local function firstSunday(y, m) return 1 + (7 - dow(y, m, 1)) % 7 end

local function dstActive(rule, y, mo, d)
  if rule == 'eu' then
    if mo < 3 or mo > 10 then return false elseif mo > 3 and mo < 10 then return true end
    return (mo == 3 and d >= lastSunday(y, 3)) or (mo == 10 and d < lastSunday(y, 10))
  elseif rule == 'us' then
    if mo < 3 or mo > 11 then return false elseif mo > 3 and mo < 11 then return true end
    return (mo == 3 and d >= firstSunday(y, 3) + 7) or (mo == 11 and d < firstSunday(y, 11))
  elseif rule == 'au' then
    if mo > 4 and mo < 10 then return false elseif mo < 4 or mo > 10 then return true end
    return (mo == 4 and d < firstSunday(y, 4)) or (mo == 10 and d >= firstSunday(y, 10))
  end
  return false
end

-- Approximate civil UTC offset (hours) for a position: nearest 15-degree meridian plus the same
-- belt-based DST guess as core_celestial's "auto" rule.
function M.utcOffsetFor(lat, lon, y, mo, d)
  local std = math.floor(lon / 15.0 + 0.5)
  local rule = 'none'
  if lat <= -30 then rule = 'au'
  elseif lat >= 30 then rule = (lon > -170 and lon < -30) and 'us' or 'eu' end
  return std + (dstActive(rule, y, mo, d) and 1 or 0)
end

local function distKm(lat1, lon1, lat2, lon2)
  local p1, p2 = math.rad(lat1), math.rad(lat2)
  local a = math.sin((p2 - p1) / 2) ^ 2 + math.cos(p1) * math.cos(p2) * math.sin(math.rad(lon2 - lon1) / 2) ^ 2
  return 2 * 6371.0088 * math.asin(math.min(1, math.sqrt(a)))
end

-- Pure: TimeOfDay fields for UTC epoch seconds `epoch` at (lat, lon); real = { hours, lat, lon }
-- (optional): a real UTC offset, used within REAL_TZ_KM of its point.
-- Returns { year, month, day, time (noon = 0), utcOffset, localHours }.
function M.todFields(epoch, lat, lon, real)
  local u = os.date('!*t', epoch)
  local off
  if real and real.hours and real.lat and real.lon and distKm(lat, lon, real.lat, real.lon) <= M.REAL_TZ_KM then
    off = real.hours
  else
    off = M.utcOffsetFor(lat, lon, u.year, u.month, u.day)
  end
  local l = os.date('!*t', epoch + math.floor(off * 3600 + 0.5))
  local localHours = l.hour + l.min / 60 + l.sec / 3600
  return {
    year = l.year, month = l.month, day = l.day,
    time = (localHours / 24 - 0.5) % 1.0,
    utcOffset = off, localHours = localHours,
  }
end

local function getTod()
  local names = scenetree.findClassObjects('TimeOfDay')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end

function M.sync()
  local tod = getTod()
  if not tod then return false end
  local lat, lon = tonumber(tod.latitude) or 0, tonumber(tod.longitude) or 0
  local f = M.todFields(os.time(), lat, lon, realTz)
  tod.year, tod.month, tod.day = f.year, f.month, f.day
  tod.utcOffset = f.utcOffset
  tod.time = f.time
  tod.dayLength = 86400 -- real-time clock between re-syncs
  tod.play = true
  return true
end

function M.setMode(m)
  if m ~= 'real' and m ~= 'manual' then return 'mode must be "real" or "manual"' end
  mode = m
  if mode == 'real' then timer = RESYNC_S end
  return 'sky mode: ' .. mode
end
function M.getMode() return mode end

-- The real UTC offset (hours) at (lat, lon), from the weather service; re-syncs on the next update.
function M.setRealUtcOffset(hours, lat, lon)
  if not (tonumber(hours) and tonumber(lat) and tonumber(lon)) then return end
  local changed = not realTz or realTz.hours ~= hours
  realTz = { hours = tonumber(hours), lat = tonumber(lat), lon = tonumber(lon) }
  if changed and mode == 'real' then timer = RESYNC_S end
end

function M.status()
  local tod = getTod()
  if not tod then return { mode = mode } end
  local h = ((tonumber(tod.time) or 0) + 0.5) % 1.0 * 24
  return {
    mode = mode, lat = tod.latitude, lon = tod.longitude,
    date = string.format('%04d-%02d-%02d', tod.year or 0, tod.month or 0, tod.day or 0),
    localTime = string.format('%02d:%02d', math.floor(h), math.floor((h % 1) * 60)),
    utcOffset = tod.utcOffset,
  }
end

------------------------------------------------------------------------------------------
-- Clouds (2026-10-10: the level had no CloudLayer, so the sky was always clear).
-- The 0.39 volumetric clouds come from a CloudLayer object (core/environment.lua:858-920 reads
-- coverage, altitudeKm, windDirection, cirrusCoverage; every change needs postApply(), as for
-- LevelInfo). altitudeKm is above the world's z = 0, which in our frame is ~ sea level, so a fixed
-- altitude puts clouds under the player on high ground (the default 0.8 km was below Mount
-- Whitney): the layer follows the ground under the player, CLOUD_ABOVE_KM above it. A rebase moves
-- the world by kilometres; the weather map is shifted by the same amount so the clouds stay put
-- (CloudLayer.setWeatherOffsetKm; CLOUD_SHIFT_SIGN checked in-game). The layer is a level object
-- (tools/make_level_assets.py) so core_environment, the pause menu and weather presets find it; it
-- is created here only for a level file without one. Real weather (beamngEarth_weather) drives
-- coverage, cirrus and drift, and sets the base height above the ground (setCloudAboveKm).

M.CLOUDS_ENABLED = true
M.CLOUD_NAME = 'bngEarthClouds'
M.CLOUD_TEXTURE = 'art/skies/clouds/clouds_normal_displacement.png' -- shared art (west_coast_usa uses it)
M.CLOUD_COVERAGE, M.CLOUD_CIRRUS = 0.5, 0.3
M.CLOUD_ABOVE_KM = 1.8      -- cloud base above the ground under the player
M.CLOUD_MIN_KM = 1.5        -- never lower than this above the world's z = 0
M.CLOUD_RETUNE_KM = 0.4     -- re-set the altitude only when it is off by more than this
M.CLOUD_CHECK_S = 5
M.JUMP_M = 3000             -- a player move this large in one frame is a rebase/teleport
M.CLOUD_SHIFT_SIGN = 1

local cloudAboveKm = nil -- real weather's base above the ground (nil = CLOUD_ABOVE_KM)
local cloudFollow = true -- false while someone else sets the altitude (a manual override)

-- Pure: the cloud altitude (km above z = 0) for ground height groundZ (m), or nil without ground;
-- aboveKm: the base above the ground (default CLOUD_ABOVE_KM).
function M.cloudAltitudeKm(groundZ, aboveKm)
  if not groundZ then return nil end
  return math.max(M.CLOUD_MIN_KM, groundZ / 1000 + (aboveKm or M.CLOUD_ABOVE_KM))
end

-- Pure: the weather offset (km) after the world moved so that the player went from (ox, oy) to
-- (nx, ny) without really moving (a rebase): the map shifts by the same displacement.
function M.shiftedOffsetKm(off, ox, oy, nx, ny)
  local s = M.CLOUD_SHIFT_SIGN / 1000
  return off.x + s * (nx - ox), off.y + s * (ny - oy), off.z or 0
end

local cloudTimer, lastPos = M.CLOUD_CHECK_S, nil

local function cloudObj()
  local names = scenetree.findClassObjects('CloudLayer')
  local o = names and names[1] and scenetree.findObject(names[1])
  if o or not M.CLOUDS_ENABLED then return o end
  o = createObject('CloudLayer')
  o:setField('texture', 0, M.CLOUD_TEXTURE)
  o:setField('exposure', 0, '1.45')
  o:setField('windSpeed', 0, '0.1')
  o:setField('baseColor', 0, '0.99 0.99 0.99 0.9')
  o.canSave = false
  o:registerObject(M.CLOUD_NAME)
  scenetree.MissionGroup:addObject(o)
  o.coverage, o.cirrusCoverage = M.CLOUD_COVERAGE, M.CLOUD_CIRRUS
  o:postApply()
  -- core_environment caches 'no CloudLayer' per class (environment.lua getObject): reset it
  local env = rawget(_G, 'core_environment')
  if env and env.onEditorDeactivated then pcall(env.onEditorDeactivated) end
  return o
end

local function groundUnder(p)
  local d = castRayStatic(vec3(p.x, p.y, p.z + 50), vec3(0, 0, -1), 5000)
  if d and d < 5000 then return p.z + 50 - d end
end

local function updateClouds(dt)
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh or not scenetree.MissionGroup then return end
  local p = veh:getPosition()
  -- rebase: the player jumps by kilometres in one frame without moving; shift the weather map too
  if lastPos and ((p.x - lastPos.x) ^ 2 + (p.y - lastPos.y) ^ 2) > M.JUMP_M * M.JUMP_M
    and rawget(_G, 'CloudLayer') and CloudLayer.getWeatherOffsetKm and CloudLayer.setWeatherOffsetKm then
    pcall(function()
      local off = CloudLayer.getWeatherOffsetKm()
      CloudLayer.setWeatherOffsetKm(Point3F(M.shiftedOffsetKm(off, lastPos.x, lastPos.y, p.x, p.y)))
    end)
  end
  lastPos = { x = p.x, y = p.y }
  cloudTimer = cloudTimer + (dt or 0)
  if cloudTimer < M.CLOUD_CHECK_S then return end
  cloudTimer = 0
  local o = cloudObj()
  if not o then return end
  if not cloudFollow then return end
  local alt = M.cloudAltitudeKm(groundUnder(p), cloudAboveKm)
  if alt and math.abs((tonumber(o.altitudeKm) or 0) - alt) > M.CLOUD_RETUNE_KM then
    o.altitudeKm = alt
    o:postApply()
  end
end

function M.setClouds(on)
  M.CLOUDS_ENABLED = on and true or false
  local o = scenetree.findObject(M.CLOUD_NAME)
  if not M.CLOUDS_ENABLED and o then o:delete() end
  cloudTimer = M.CLOUD_CHECK_S
  return 'clouds ' .. (M.CLOUDS_ENABLED and 'on' or 'off')
end

-- Real weather: the cloud base above the ground (km; nil = CLOUD_ABOVE_KM), and whether the
-- altitude follows the ground at all (off while the user or another mod sets it).
function M.setCloudAboveKm(km) cloudAboveKm = tonumber(km) end
function M.setCloudFollow(on)
  cloudFollow = on and true or false
  if cloudFollow then cloudTimer = M.CLOUD_CHECK_S end
end

function M.onUpdate(dtReal)
  local ok, err = pcall(updateClouds, dtReal)
  if not ok then log('W', 'beamngEarth', 'clouds: ' .. tostring(err)) end
  if mode ~= 'real' then return end
  timer = timer + (dtReal or 0)
  if timer < RESYNC_S then return end
  timer = 0
  M.sync()
end

function M.onClientPostStartMission() timer, cloudTimer, lastPos = RESYNC_S, M.CLOUD_CHECK_S, nil end

return M
