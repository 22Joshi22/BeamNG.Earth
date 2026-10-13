-- luajit mod/tests/test_terrain_swap.lua  (terrain.swap: the server's .ter swapped in, 2026-10-13)
-- Only after a first importMaps and at the same square size; sets maxHeight, the position and the file
-- (one postApply, no importMaps); the slots follow the materials the .ter names; the base photo is set.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local lattice = require('beamngEarth/lattice')
lattice.configure(512)
local terrain = require('beamngEarth/terrain')
terrain.configure(512)
local calls = S.calls

T.check(terrain.swap('/t/a.ter', 2, 512, 0, 0, 0) == false, 'no block yet: no swap (the caller imports)')
T.check(terrain.load('/t/hm.png', 2, 512, -512, -512, -64), 'first block imported')
local tb = terrain.get()
local imports = calls.importMaps or 0
T.check(terrain.swap('/t/b.ter', 1, 512, 0, 0, 0) == false, 'another square size: no swap')
local missing = FS.fileExists
FS.fileExists = function(_, p) return p ~= '/t/gone.ter' end
T.check(terrain.swap('/t/gone.ter', 2, 512, 0, 0, 0) == false, 'a missing .ter: no swap')
FS.fileExists = missing
local mats = { { 'bngEarthGroundAsphalt', 'ASPHALT_OLD' }, { 'beamngEarthDirt', 'DIRT' } }
local applies = calls.postApply_TerrainBlock or 0
T.check(terrain.swap('/t/c.ter', 2, 1024, -384, -512, -128, { materials = mats }, '/t/c_base.png', 1024) == true, 'swap')
T.check(tb.fields.terrainFile == '/t/c.ter' and tb.applied == '/t/c.ter', 'the .ter set, then applied')
T.check(tb.fields.maxHeight == '1024', 'maxHeight from the message')
T.check(tb.pos.x == -384 and tb.pos.y == -512 and tb.pos.z == -128, 'block moved')
T.check((calls.importMaps or 0) == imports and calls.postApply_TerrainBlock == applies + 1, 'one postApply, no importMaps')
T.check(terrain.slots[1] == 'bngEarthGroundAsphalt' and terrain.slots[2] == 'beamngEarthDirt' and #terrain.slots == 2,
  'material slots = the .ter\'s materials')
T.check(terrain.base() == '/t/c_base.png', 'base photo set')
T.check(terrain.swap('/t/d.ter', 2, 512, 0, 0, 0) == true and terrain.slots[1] == terrain.MATERIAL and #terrain.slots == 1,
  'no layers: the single dirt layer')

T.done('test_terrain_swap')
