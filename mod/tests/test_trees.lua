-- luajit mod/tests/test_trees.lua  (trees.lua: vegetation tiles become Forest items, thinned by distance)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local items = {}
local fdata = {}
function fdata:createNewItem(d, m, sc) local it = { d = d, m = m, sc = sc }; items[it] = true; return it end
function fdata:removeItem(it) items[it] = nil end
local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  if class == 'Forest' then function o:getData() return fdata end end
  return o
end
worldEditorCppApi = { createObject = function(class) return createObject(class) end }
Sim = { getDataBlockSet = function() return { addObject = function() end } end }
loadJsonMaterialsFile = function() end
MatrixF = function() return { setFromEuler = function() end, setPosition = function(self, p) self.p = p end } end
local files = {}
readFile = function(p) return files[p] end
jsonDecode = function(s) return assert(load('return ' .. s))() end
local trees = require('beamngEarth/trees')
local function count() local n = 0; for _ in pairs(items) do n = n + 1 end; return n end

-- the variant: a height inside its range, scaled to it
local shape, sc = trees.pick('conifer', 20.0, 0.3)
T.check(shape:find('douglasfir_large') ~= nil and sc > 0.9 and sc < 1.1, 'a 20 m conifer: a large fir near its size')
shape, sc = trees.pick('rock', 0.6, 0.1)
T.check(shape:find('rock') ~= nil and math.abs(sc - 0.6 / select(2, unpack({ shape, 0.6 })) * 1) < 2, 'a rock')

-- a tile of 100 items with ranks 0 .. 0.99 at the player: all made; far away: a quarter
local rows = {}
for i = 0, 99 do rows[#rows + 1] = string.format('{0, %d, 10, 5, 0, 1, 12, %.2f}', i, i / 100) end
files['/t/v.json'] = '{ groups = {}, items = { ' .. table.concat(rows, ', ') .. ' } }'
trees.onTile({ anchorId = 1, key = '0_0', path = '/t/v.json', n = 100 }, 1)
trees.update(1, { x = 50, y = 50 })
T.check(count() == 100 and trees.status().items == 100, 'near: every item')
trees.update(1, { x = 50, y = 1900 })
T.check(count() == 20, 'far: the fifth with the lowest ranks, the same trees')
trees.update(1, { x = 50, y = 1100 })
T.check(count() == 35, 'beyond 700 m: 35 %')
trees.update(1, { x = 50, y = 600 })
T.check(count() == 60, 'middle distance: 60 %')

trees.onDrop({ anchorId = 1, key = '0_0' })
trees.update(1, { x = 50, y = 50 })
T.check(count() == 0 and trees.status().tiles == 0, 'dropped: removed')

trees.onTile({ anchorId = 1, key = '0_0', path = '/t/v.json', n = 100 }, 1)
trees.update(1, { x = 50, y = 50 })
trees.update(2, { x = 50, y = 50 })
T.check(count() == 0, 'another anchor: removed')
trees.onTile({ anchorId = 2, key = '0_0', path = '/t/v.json', n = 100 }, 2)
trees.update(2, { x = 50, y = 50 })
trees.clear()
T.check(count() == 0 and trees.status().tiles == 0, 'clear')

T.done('test_trees')
