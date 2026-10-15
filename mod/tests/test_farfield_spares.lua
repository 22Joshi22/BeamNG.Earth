-- luajit mod/tests/test_farfield_spares.lua  (far field lazy unloading: the spare pool, 2026-10-13)
-- A removed tile of the current anchor is kept hidden and revived when the server sends it again
-- (same key, same shape file) without a new import; spares expire (SPARE_S), are capped (SPARE_MAX),
-- keep their session folder held, and go at an anchor switch / rebase.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local farfield = require('beamngEarth/farfield')

local free = { clock = function() return 0 end }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end
local DT = 1 / 60
local function key(l, i, j) return l .. '_' .. i .. '_' .. j end
local function tile(a, l, i, j, sess)
  return { type = 'far_tile', anchorId = a, key = key(l, i, j), level = l,
    shape = '/temp/beamngEarth/' .. (sess or 's1') .. '/far_' .. a .. '_' .. key(l, i, j) .. '.cdae',
    pos = { i * 256 * 2 ^ l, j * 256 * 2 ^ l, 100 }, err = 0.5, state = 'group' }
end
local function swap(a, id, add, remove)
  return { type = 'far_swap', anchorId = a, id = id, add = add, remove = remove or {}, dIn = 1, dOut = 1 }
end
local function step(n) for _ = 1, n or 1 do farfield.update(free, DT, nil) end end
local function creates() return S.calls.createObject_TSStatic or 0 end
local function settle() step(120) end

farfield.SPARE_MAX, farfield.SPARE_S = 400, 120
farfield.switchAnchor(1, false, { cx = 0, cy = 0 }, 's1')

-- a tile, then its four children replace it: the parent becomes a spare (hidden, not deleted)
farfield.onTile(tile(1, 1, 0, 8)); step()
farfield.onSwap(swap(1, 1, { key(1, 0, 8) })); settle()
local parent = farfield._tile(key(1, 0, 8))
local pid = parent.id
local kids = { key(0, 0, 16), key(0, 1, 16), key(0, 0, 17), key(0, 1, 17) }
for _, k in ipairs(kids) do local l, i, j = k:match('(%-?%d+)_(%-?%d+)_(%-?%d+)'); farfield.onTile(tile(1, 0, tonumber(i), tonumber(j))) end
step()
farfield.onSwap(swap(1, 2, kids, { key(1, 0, 8) })); settle()
T.check(S.objects[pid] ~= nil and S.objects[pid].hidden, 'the replaced tile is kept, hidden (a spare)')
T.check(farfield.stats().spares == 1 and farfield.counters().parked == 1, 'one spare parked')

-- the parent comes back (the player turned around): revived, no new TSStatic
local before = creates()
farfield.onTile(tile(1, 1, 0, 8)); step()
T.check(creates() == before, 'revived without a new import')
T.check(farfield._pending(key(1, 0, 8)).id == pid, 'the same object')
T.check(farfield.counters().revived == 1 and farfield.stats().spares == 0, 'counted, out of the pool')
farfield.onSwap(swap(1, 3, { key(1, 0, 8) }, kids)); settle()
T.check(S.objects[pid] ~= nil and not S.objects[pid].hidden, 'shown again by its swap')
T.check(farfield.stats().spares == 4, 'the four children are spares now')

-- a different shape file for the same key (a new server session) is imported, not revived
before = creates()
farfield.onTile(tile(1, 0, 0, 16, 's2')); step()
T.check(creates() == before + 1, 'another shape file: imported')
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = key(0, 0, 16) }); step()  -- withdrawn

-- spares expire after SPARE_S
farfield.SPARE_S = 0
step(2)
T.check(farfield.stats().spares == 0, 'expired spares leave the pool')
settle()
T.check(#S.liveOf('TSStatic') <= 6, 'and are deleted')
farfield.SPARE_S = 120

-- the cap: the oldest go first
farfield.SPARE_MAX = 2
farfield.onSwap(swap(1, 4, kids, { key(1, 0, 8) })); step()   -- parent -> spare
T.check(farfield.stats().spares <= 2, 'at most SPARE_MAX spares')

-- a spare's folder stays held (the server must not delete its files)
farfield.SPARE_MAX = 400
local found = false
for _, f in ipairs(farfield.heldSessions()) do if f == 's1' then found = true end end
T.check(found, 'spare folders are held')

-- an anchor switch drops every spare into the delete queue
local lone = key(2, 9, 9)
farfield.onTile(tile(1, 2, 9, 9)); step()
farfield.onSwap(swap(1, 6, { lone })); settle()
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = lone }); settle()
local n = farfield.stats().spares
farfield.switchAnchor(2, true, { cx = 0, cy = 0 }, 's1')
T.check(n >= 1 and farfield.stats().spares == 0, 'spares dropped at an anchor switch')

T.done('test_farfield_spares')
