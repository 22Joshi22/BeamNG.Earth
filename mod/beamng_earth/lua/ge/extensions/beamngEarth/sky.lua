-- BeamNG.Earth: real-world sky. Keeps the level's TimeOfDay on the real date and clock for
-- wherever the player is, so core_celestial draws the real sun, moon and stars.
--
-- core_celestial (core/celestial.lua) computes the sky from TimeOfDay year/month/day, `time`
-- (civil local clock as a 0..1 day fraction, noon = 0; celestial.lua:313-318) and the UTC offset
-- (tod.utcOffset if set, else derived from longitude; celestial.lua:285-296). We always write
-- utcOffset explicitly together with a matching local `time`, so the instant (Julian date) is
-- exact even where the timezone guess is wrong; a wrong guess only shifts the displayed clock.
-- Latitude/longitude are kept current by beamngEarth_core (core_celestial.setLocation).
--
-- Modes: 'real'   = real UTC now, clock runs at real speed, re-synced every RESYNC_S seconds.
--        'manual' = hands off (use the game's own environment/time controls).

local M = {}

local RESYNC_S = 30

local mode = 'real'
local timer = RESYNC_S -- sync on the first update

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

-- Pure: TimeOfDay fields for UTC epoch seconds `epoch` at (lat, lon).
-- Returns { year, month, day, time (noon = 0), utcOffset, localHours }.
function M.todFields(epoch, lat, lon)
  local u = os.date('!*t', epoch)
  local off = M.utcOffsetFor(lat, lon, u.year, u.month, u.day)
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
  local f = M.todFields(os.time(), lat, lon)
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

function M.onUpdate(dtReal)
  if mode ~= 'real' then return end
  timer = timer + (dtReal or 0)
  if timer < RESYNC_S then return end
  timer = 0
  M.sync()
end

function M.onClientPostStartMission() timer = RESYNC_S end

return M
