-- luajit mod/tests/test_roads.lua  (roads.lua: road tiles from the server become DecalRoads, one chunk per
-- governor unit; drops, anchor changes, clear and the off switch remove them)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

-- DecalRoad methods roads.lua calls (not in the generic stub object)
local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  o.nodes = {}
  function o:insertNodeNoRegen(p, w) self.nodes[#self.nodes + 1] = { p.x, p.y, p.z, w } end
  function o:regenerate() self.regenerated = (self.regenerated or 0) + 1 end
  function o:getId() return self.id end
  return o
end
local files = {}
readFile = function(p) return files[p] end
jsonDecode = function(s) return assert(load('return ' .. s))() end   -- the test writes Lua table text

local roads = require('beamngEarth/roads')

local gov = { tries = 0 }
function gov:try() self.tries = self.tries + 1; return self.allow == nil or self.tries <= self.allow end
function gov:charge() end

local function chunk(x)
  return string.format('{ m = "road_asphalt_2lane", w = 6, tl = 11, p = 20, n = { {%d, 0, 10}, {%d, 10, 10}, {%d, 20, 10} } }', x, x, x)
end
files['/t/a.json'] = '{ chunks = { ' .. chunk(0) .. ', ' .. chunk(5) .. ' } }'
files['/t/b.json'] = '{ chunks = { ' .. chunk(50) .. ' } }'

local function decalRoads()
  local n = 0
  for _, o in pairs(S.objects) do if o.class == 'DecalRoad' then n = n + 1 end end
  return n
end

-- fields of a chunk
local f = {}
for _, kv in ipairs(roads.fieldsFor({ m = 'road_asphalt_2lane', tl = 11, p = 20 })) do f[kv[1]] = kv[2] end
T.check(f.material == 'road_asphalt_2lane' and f.textureLength == '11' and f.renderPriority == '20', 'material, texture length, priority')
T.check(f.drivability == '-1' and f.overObjects == 'false', 'not in the navgraph; drawn on the terrain')

-- a tile: queued, then made one chunk per governor unit
T.check(roads.onTile({ anchorId = 1, key = '0_0', path = '/t/a.json', n = 2 }, 1) == true, 'tile accepted')
T.check(roads.status().queued == 2 and decalRoads() == 0, 'queued, nothing made yet')
gov.allow, gov.tries = 1, 0
roads.update(gov, 1)
T.check(decalRoads() == 1 and roads.status().queued == 1, 'one chunk per allowed unit')
gov.allow, gov.tries = nil, 0
roads.update(gov, 1)
T.check(decalRoads() == 2 and roads.status().roads == 2 and not roads.hasWork(), 'all made')
local some
for _, o in pairs(S.objects) do if o.class == 'DecalRoad' then some = o end end
T.check(#some.nodes == 3 and some.nodes[1][4] == 6 and some.regenerated == 1 and some.canSave == false, 'nodes with width, one regenerate, not saved')

-- a stale tile (another anchor) is ignored
T.check(roads.onTile({ anchorId = 9, key = '0_0', path = '/t/b.json', n = 1 }, 1) == false, 'stale anchor ignored')

-- drop
roads.onTile({ anchorId = 1, key = '1_0', path = '/t/b.json', n = 1 }, 1)
roads.update(gov, 1)
T.check(decalRoads() == 3, 'second tile made')
roads.onDrop({ anchorId = 1, key = '0_0' })
roads.update(gov, 1)
T.check(decalRoads() == 1 and roads.status().tiles == 1, 'dropped tile deleted')

-- a re-sent tile replaces its old roads
roads.onTile({ anchorId = 1, key = '1_0', path = '/t/b.json', n = 1 }, 1)
roads.update(gov, 1)
T.check(decalRoads() == 1 and roads.status().roads == 1, 're-sent tile replaces, no duplicates')

-- a new anchor: the old anchor's roads go
roads.update(gov, 2)
T.check(decalRoads() == 0 and roads.status().tiles == 0, 'new anchor: old roads deleted')

-- unreadable file
T.check(roads.onTile({ anchorId = 2, key = '0_0', path = '/t/missing.json', n = 1 }, 2) == false and roads.status().error ~= nil,
  'unreadable tile reported')

-- off: everything removed at once, new tiles ignored
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/a.json', n = 2 }, 2)
roads.update(gov, 2)
T.check(decalRoads() == 2, 'made again')
roads.setEnabled(false)
T.check(decalRoads() == 0 and roads.status().roads == 0, 'off: removed at once')
T.check(roads.onTile({ anchorId = 2, key = '1_1', path = '/t/a.json', n = 2 }, 2) == false, 'off: tiles ignored')
T.check(roads.setEnabled(true) == true, 'on again: reported (core then asks the server for the tiles)')
T.check(roads.setEnabled(true) == false, 'already on: nothing to ask')

-- raised sidewalks: the tile's sidewalk mesh is one TSStatic that collides; it goes with its tile
files['/t/w.json'] = '{ chunks = { ' .. chunk(0) .. ' }, walk = { shape = "/t/walks_2_5_5.cdae", pos = { 256, 512, 0 } } }'
local function statics()
  local r = {}
  for _, o in pairs(S.objects) do if o.class == 'TSStatic' then r[#r + 1] = o end end
  return r
end
S.calls.reloadCollision = 0
roads.RELOAD_EVERY_S = 0
roads.onTile({ anchorId = 2, key = '5_5', path = '/t/w.json', n = 1 }, 2)
roads.update(gov, 2)
local w = statics()[1]
T.check(#statics() == 1 and roads.status().walks == 1, 'sidewalk mesh made')
T.check(w.fields.shapeName == '/t/walks_2_5_5.cdae' and w.fields.collisionType == 'Visible Mesh' and w.canSave == false,
  'shape, collides with its visible mesh, not saved')
T.check(w.pos.x == 256 and w.pos.y == 512, 'at the tile corner')
T.check(S.calls.reloadCollision == 1, 'collision reloaded for it')
roads.update(gov, 2)
T.check(S.calls.reloadCollision == 1, 'nothing more while nothing changes')
roads.onDrop({ anchorId = 2, key = '5_5' })
roads.update(gov, 2)
T.check(#statics() == 0 and decalRoads() == 0 and S.calls.reloadCollision == 2, 'dropped tile: mesh deleted, collision reloaded')

-- a sidewalk mesh never turns solid around the player's car: held while the car is in its boxes
files['/t/h.json'] = '{ chunks = { }, walk = { shape = "/t/walks_2_7_7.cdae", pos = { 0, 0, 0 } }, ' ..
  'colliders = { walk = { { 100, 100, 0, 120, 110, 1 } } } }'
local car = { pos = vec3(110, 105, 0.5) }
function car:getPosition() return self.pos end
getPlayerVehicle = function() return car end
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/h.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'not made with the car on it')
car.pos = vec3(122, 105, 0.5)          -- within PLAYER_R of the box
roads.update(gov, 2)
T.check(#statics() == 0, 'still held next to it')
car.pos = vec3(200, 105, 0.5)
roads.update(gov, 2)
T.check(#statics() == 1, 'made once the car is clear')
-- made, then the car drives into it before collision is rebuilt: removed, made again once clear
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
roads.RELOAD_EVERY_S = 1e9
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/h.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 1, 'made (car clear)')
car.pos = vec3(110, 105, 0.5)
roads.collisionRebuilt()
T.check(#statics() == 0 and roads.solidAround == 1, 'removed before it turns solid around the car')
car.pos = vec3(200, 105, 0.5)
roads.update(gov, 2)
T.check(#statics() == 1, 'made again once clear')
roads.RELOAD_EVERY_S = 0
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
-- colliders only near the car: a far tile's sidewalk waits, made as the car comes near, dropped far behind
roads.COLLIDE_CHECK_S = 0
roads.RELOAD_EVERY_S = 0
car.pos = vec3(0, 0, 0.5)
files['/t/far.json'] = '{ chunks = { }, walk = { shape = "/t/walks_2_9_0.cdae", pos = { 2304, 0, 0 } } }'
roads.onTile({ anchorId = 2, key = '9_0', path = '/t/far.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'a tile 2.4 km away: no collider yet')
car.pos = vec3(2200, 100, 0.5)
roads.update(gov, 2)
roads.update(gov, 2)
T.check(#statics() == 1, 'made once the car is near')
car.pos = vec3(0, 0, 0.5)
roads.update(gov, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'dropped again far behind')
T.check(roads.collideRadius(0) == roads.COLLIDE_M[1] and roads.collideRadius(45) == 45 * roads.COLLIDE_LEAD_S
  and roads.collideRadius(500) == roads.COLLIDE_M[2], 'the radius grows with speed, within its limits')
roads.onDrop({ anchorId = 2, key = '9_0' })
roads.update(gov, 2)
getPlayerVehicle = nil

-- a road mesh: the tile's DecalRoads are not made; one TSStatic without collision instead
files['/t/r.json'] = '{ chunks = { ' .. chunk(0) .. ', ' .. chunk(5) .. ' }, road = { shape = "/t/road_2_6_6.cdae", pos = { 0, 0, 0 } } }'
S.calls.reloadCollision = 0
roads.onTile({ anchorId = 2, key = '6_6', path = '/t/r.json', n = 2 }, 2)
roads.update(gov, 2)
local rm = statics()[1]
T.check(#statics() == 1 and decalRoads() == 0 and roads.status().roadMeshes == 1, 'road mesh instead of DecalRoads')
T.check(rm.fields.shapeName == '/t/road_2_6_6.cdae' and rm.fields.collisionType == 'None', 'render only')
T.check(S.calls.reloadCollision == 0, 'no collision reload for it')
roads.onDrop({ anchorId = 2, key = '6_6' })
roads.update(gov, 2)
T.check(#statics() == 0 and S.calls.reloadCollision == 0, 'dropped without a collision reload')

-- a Lua reload: the new module state deletes the objects the old one left
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/a.json', n = 2 }, 2)
roads.update(gov, 2)
T.check(decalRoads() == 2, 'made')
package.loaded['beamngEarth/roads'] = nil
local fresh = require('beamngEarth/roads')
T.check(decalRoads() == 0 and fresh.status().roads == 0, 'a reloaded module sweeps the old roads')
roads = fresh

-- level end
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/a.json', n = 2 }, 2)
roads.reset()
T.check(roads.status().tiles == 0 and roads.status().queued == 0, 'level end: forgotten')

T.done('test_roads')
