-- luajit mod/tests/test_sea.lua  (sea.lua: the sea-level WaterPlane from the server's 'sea' messages)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  function o:getId() return self.id end
  return o
end
local sea = require('beamngEarth/sea')

local function planes()
  local r = {}
  for _, o in pairs(S.objects) do if o.class == 'WaterPlane' then r[#r + 1] = o end end
  return r
end

sea.update(1, 2.0)
T.check(#planes() == 0, 'nothing before the server says so')

sea.onMessage({ type = 'sea', anchorId = 1, on = true, z = -0.25 })
sea.update(1, 2.0)
local p = planes()[1]
T.check(#planes() == 1 and p.pos.z == -0.25, 'one plane at the sea level sent')
T.check(p.canSave == false and p.fields.baseColor ~= nil and p.fields.rippleTex ~= nil, 'not saved, styled')
sea.update(1, 2.0)
T.check(#planes() == 1 and sea.created == 1, 'made once')

sea.onMessage({ type = 'sea', anchorId = 1, on = true, z = -0.26 })
sea.update(1, 2.0)
T.check(planes()[1].pos.z == -0.25, 'a change under MOVE_Z: not moved')
sea.onMessage({ type = 'sea', anchorId = 1, on = true, z = -0.4 })
sea.update(1, 2.0)
T.check(planes()[1].pos.z == -0.4 and sea.created == 1, 'moved, not made again')

sea.update(1, -0.4 + sea.HIDE_ABOVE_M + 1)
T.check(#planes() == 0, 'high above the sea: removed')
sea.update(1, 5.0)
T.check(#planes() == 1, 'back down: made again')

sea.update(2, 5.0)
T.check(#planes() == 0, 'another anchor: removed until the server says')
sea.onMessage({ type = 'sea', anchorId = 2, on = true, z = 0 })
sea.update(2, 5.0)
T.check(#planes() == 1, 'the new anchor\'s sea')

sea.onMessage({ type = 'sea', anchorId = 2, on = false, z = 0 })
sea.update(2, 5.0)
T.check(#planes() == 0, 'no sea near: removed')

sea.onMessage({ type = 'sea', anchorId = 2, on = true, z = 0 })
sea.update(2, 5.0)
sea.clear()
T.check(#planes() == 0 and sea.status().want == nil, 'clear: removed and forgotten')
sea.update(2, 5.0)
T.check(#planes() == 0, 'stays removed')

T.done('test_sea')
