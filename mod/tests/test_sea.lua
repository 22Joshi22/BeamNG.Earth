-- luajit mod/tests/test_sea.lua  (sea.lua: the sea's WaterBlock tiles from the server's 'sea' messages)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  function o:getId() return self.id end
  function o:setScale(s) self.scale = vec3(s.x, s.y, s.z) end
  return o
end
local car = { pos = vec3(100, 100, -1), nudged = 0 }
function car:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function car:getRotation() return { x = 0, y = 0, z = 0, w = 1 } end
function car:setPositionRotation() self.nudged = self.nudged + 1 end
getPlayerVehicle = function() return car end
quat = quat or function(q) return q end
local sea = require('beamngEarth/sea')

local function blocks()     -- the drawn tiles (not the physics block)
  local r = {}
  for _, o in pairs(S.objects) do if o.class == 'WaterBlock' and o.name ~= sea.PHYS_NAME then r[o.name] = o end end
  return r
end
local function physBlock()
  for _, o in pairs(S.objects) do if o.name == sea.PHYS_NAME then return o end end
end
local function count(t) local n = 0; for _ in pairs(t) do n = n + 1 end; return n end
local function tile(key, x, y, z, size, nx, ny) return { key, x, y, z, size, nx or 0, ny or 0 } end
local player = { x = 100, y = 100, z = -1, vx = 0, vy = 0, vz = 0 }

sea.update(1, player)
T.check(count(blocks()) == 0, 'nothing before the server says so')

local six = {}
for i = 0, 5 do six[#six + 1] = tile('512_' .. i .. '_0', 256 + 512 * i, 256, -0.01 * i, 512, (256 + 512 * i) / 6371000, 0) end
sea.onMessage({ type = 'sea', anchorId = 1, tiles = six })
sea.update(1, player)
T.check(count(blocks()) == sea.CREATE_PER_FRAME, 'a few tiles per frame')
sea.update(1, player)
local b = blocks()
T.check(count(b) == 6 and sea.status().tiles == 6, 'all made over the next frames')
local t0 = b[sea.PREFIX .. '512_0_0']
T.check(t0 and t0.pos.x == 256 and t0.pos.z == 0 and t0.scale.x == 512 and t0.scale.z == sea.DEPTH_M,
  'surface at the position, the tile\'s size, DEPTH_M deep')
T.check(t0.canSave == false and t0.fields.baseColor ~= nil and t0.fields.rotation ~= nil, 'not saved, styled, tilted')
T.check(car.nudged >= 1, 'a parked car inside a new tile is re-placed (its physics sees the water)')
local pb = physBlock()
T.check(pb and pb.renderEnabled == false and pb.pos.x == 100 and pb.pos.z == 0 and pb.scale.x == sea.PHYS_SIZE,
  'a physics block around the player at the level of its tile, not drawn')
local made0 = sea.physMade
sea.update(1, player)
T.check(sea.physMade == made0 + 1, 'made again once the batch of new tiles is done')
for _, o in pairs(blocks()) do
  T.check(physBlock().id > o.id, 'newer than every tile (a vehicle takes the newest water object)')
end
sea.update(1, player)
T.check(sea.physMade == made0 + 1, 'not made again while nothing changes')
made0 = sea.physMade
player.x = 100 + sea.PHYS_SIZE / 2 - sea.PHYS_MARGIN + 1
sea.update(1, player)
T.check(sea.physMade == made0 + 1 and physBlock().pos.x == player.x, 'made again around the player near its edge')
player.x = 100

sea.onMessage({ type = 'sea', anchorId = 1, tiles = { six[1], six[2], tile('1024_1_0', 1536, 512, -0.2, 1024) } })
sea.update(1, player)
b = blocks()
T.check(count(b) == 3 and b[sea.PREFIX .. '1024_1_0'] and not b[sea.PREFIX .. '512_5_0'], 'a new set: changed tiles only')
T.check(sea.created == 7, 'the kept ones not made again')

player.vx = 20
local nudged = car.nudged
sea.onMessage({ type = 'sea', anchorId = 1, tiles = { tile('512_0_0b', 100, 100, 0, 512) } })
sea.update(1, player)
T.check(car.nudged == nudged, 'a moving car is not re-placed (it sees new water by moving)')
player.vx = 0
sea.onMessage({ type = 'sea', anchorId = 1, tiles = { tile('512_9_9', 5000, 5000, 0, 512) } })
sea.update(1, player)
T.check(physBlock() == nil, 'no tile under the player: no physics block')

sea.update(2, player)
T.check(count(blocks()) == 0, 'another anchor: removed until the server says')
sea.onMessage({ type = 'sea', anchorId = 2, tiles = six })
sea.update(2, player); sea.update(2, player)
T.check(count(blocks()) == 6, 'the new anchor\'s tiles')
sea.onMessage({ type = 'sea', anchorId = 2, tiles = {} })
sea.update(2, player)
T.check(count(blocks()) == 0, 'no sea near: removed')

sea.onMessage({ type = 'sea', anchorId = 2, tiles = six })
sea.update(2, player)
sea.clear()
T.check(count(blocks()) == 0 and sea.status().want == 0, 'clear: removed and forgotten')

T.done('test_sea')
