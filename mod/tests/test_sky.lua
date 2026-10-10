-- Offline tests for beamngEarth/sky.lua (pure parts). Run: cd mod/tests && luajit test_sky.lua
package.path = '../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local sky = require('beamngEarth/sky')

local pass, fail = 0, 0
local function check(cond, what)
  if cond then pass = pass + 1 else fail = fail + 1; print('FAIL: ' .. what) end
end
local function near(a, b, tol) return math.abs(a - b) <= tol end

-- 2026-10-10 03:20:09 UTC (the moment this was written)
local epoch = 1791602409

-- Grand Canyon (US rule, DST active in October): UTC-7 standard + 1 = -6
local f = sky.todFields(epoch, 36.1047, -111.8257)
check(f.utcOffset == -6, 'Grand Canyon offset -6, got ' .. f.utcOffset)
check(f.year == 2026 and f.month == 10 and f.day == 9, 'Grand Canyon local date is Oct 9')
check(near(f.localHours, 21 + 20 / 60 + 9 / 3600, 1e-6), 'local hours 21:20:09')
check(near(f.time, (f.localHours / 24 - 0.5) % 1, 1e-12), 'time is noon-zero fraction')

-- Instant is exact regardless of offset: UTC hours = local - offset
local utcHours = (f.localHours - f.utcOffset) % 24
check(near(utcHours, 3 + 20 / 60 + 9 / 3600, 1e-6), 'reconstructed UTC 03:20:09')

-- Tropics: no DST. Singapore lon 103.8 -> std +7 (nearest meridian), no DST
local s = sky.todFields(epoch, 1.35, 103.82)
check(s.utcOffset == 7, 'Singapore approx offset 7, got ' .. s.utcOffset)

-- The real offset (Open-Meteo timezone=auto, via the weather service) wins near its point:
-- Singapore is +8, not the meridian guess +7; 300 km away the guess is back
local real = { hours = 8, lat = 1.35, lon = 103.82 }
local sr = sky.todFields(epoch, 1.30, 103.85, real)
check(sr.utcOffset == 8, 'Singapore real offset 8, got ' .. sr.utcOffset)
check(near((sr.localHours - sr.utcOffset) % 24, 3 + 20 / 60 + 9 / 3600, 1e-6), 'real offset: same instant')
check(sky.todFields(epoch, 1.35, 106.6, real).utcOffset == 7, 'beyond REAL_TZ_KM: the guess')
-- half-hour zones (Delhi +5.5)
check(sky.todFields(epoch, 28.61, 77.21, { hours = 5.5, lat = 28.6, lon = 77.2 }).utcOffset == 5.5, 'Delhi +5.5')

-- Southern temperate (AU rule): Sydney in October -> DST active: std +10 + 1
local a = sky.todFields(epoch, -33.87, 151.21)
check(a.utcOffset == 11, 'Sydney offset 11, got ' .. a.utcOffset)
check(a.day == 10, 'Sydney local date Oct 10')

-- EU rule after last Sunday of October (2026-10-25): Berlin offset +1
local b = sky.todFields(epoch + 20 * 86400, 52.52, 13.40)
check(b.utcOffset == 1, 'Berlin late Oct offset 1, got ' .. b.utcOffset)
local b2 = sky.todFields(epoch, 52.52, 13.40)
check(b2.utcOffset == 2, 'Berlin early Oct offset 2, got ' .. b2.utcOffset)

-- Antimeridian / date line: lon 179.9 -> +12, lon -179.9 -> -12, dates differ by a day
local e = sky.todFields(epoch, 0, 179.9)
local w = sky.todFields(epoch, 0, -179.9)
check(e.utcOffset == 12 and w.utcOffset == -12, 'date line offsets')
check(e.day ~= w.day, 'date line: different local dates')

-- Poles: offsets stay sane
local np = sky.todFields(epoch, 89.9, 0)
check(np.utcOffset == 0 or np.utcOffset == 1, 'north pole offset sane')

-- time stays in [0,1)
for i = 0, 47 do
  local t = sky.todFields(epoch + i * 1800, 36.1, -111.8).time
  check(t >= 0 and t < 1, 'time in [0,1) at step ' .. i)
end

-- clouds: the layer follows the ground; a rebase shifts the weather map by the world's move
check(near(sky.cloudAltitudeKm(4417), 4.417 + sky.CLOUD_ABOVE_KM, 1e-9), 'Whitney: clouds above the summit')
check(near(sky.cloudAltitudeKm(5), 0.005 + sky.CLOUD_ABOVE_KM, 1e-9), 'sea level: CLOUD_ABOVE_KM up')
check(sky.cloudAltitudeKm(-1000) == sky.CLOUD_MIN_KM, 'far below sea level: the minimum altitude')
check(sky.cloudAltitudeKm(nil) == nil, 'no ground: no change')
check(near(sky.cloudAltitudeKm(33, 2.967), 3.0, 1e-9), 'real weather: the base above the ground (Baltimore mid deck)')
check(sky.cloudAltitudeKm(0, 0.3) == sky.CLOUD_MIN_KM, 'a low base still keeps the minimum altitude')
local ox, oy, oz = sky.shiftedOffsetKm({ x = 1, y = 2, z = 0 }, 15000, 0, 0, 0)
check(near(ox, 1 - 15 * sky.CLOUD_SHIFT_SIGN, 1e-9) and oy == 2 and oz == 0, 'rebase shifts the weather offset by the move (km)')

print(string.format('test_sky: %d passed, %d failed', pass, fail))
os.exit(fail == 0 and 0 or 1)
