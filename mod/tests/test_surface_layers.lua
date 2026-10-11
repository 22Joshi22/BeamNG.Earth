-- luajit mod/tests/test_surface_layers.lua  (terrain.lua with the server's surface layers, --surface on:
-- one mask per material, the materials created as clones of the level's dirt with their ground
-- models, the single dirt layer when the layers are unusable)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local terrain = require('beamngEarth/terrain')

-- the level's own dirt material (loaded from main.materials.json in the game)
local dirt = S.newObject('TerrainMaterial'); dirt:registerObject('beamngEarthDirt')
dirt:setField('baseColorBaseTex', 0, '/levels/beamng_earth/art/terrains/bngEarth_base_b.png')
dirt:setField('groundmodelName', 0, 'DIRT')
local set = S.newObject('TerrainMaterialTextureSet'); set:registerObject(terrain.TEXTURE_SET)

local MATS = { { 'bngEarthGroundAsphalt', 'ASPHALT_OLD' }, { 'bngEarthGroundGrass', 'GRASS' }, { 'bngEarthGroundDusty', 'DIRT_DUSTY' },
  { 'bngEarthGroundLoose', 'DIRT_DUSTY_LOOSE' }, { 'bngEarthGroundRock', 'ROCK' }, { 'bngEarthGroundSnow', 'SNOW' },
  { 'bngEarthGroundMud', 'MUD' }, { 'beamngEarthDirt', 'DIRT' } }
local masks = {}
for i = 1, 8 do masks[i] = '/temp/beamngEarth/s1/hm_1_0_0_m' .. (i - 1) .. '.png' end

T.check(terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS }),
  'load with surface layers')
local tb = terrain.get()
local imp = tb.lastImport
T.check(#imp.mats == 8 and #imp.maps == 8, '8 materials and 8 maps')
for i = 1, 8 do
  T.check(imp.mats[i] == MATS[i][1] and imp.maps[i].path == masks[i] and imp.maps[i].material == MATS[i][1]
    and imp.maps[i].materialId == i - 1 and imp.maps[i].channel == 'R', 'map ' .. i .. ': mask, material, id')
end
for i = 1, 7 do
  local m = scenetree.findObject(MATS[i][1])
  T.check(m and m.class == 'TerrainMaterial' and m.fields.groundmodelName == MATS[i][2]
    and m.fields.internalName == MATS[i][1] and m.fields.baseColorBaseTex == dirt.fields.baseColorBaseTex and m.canSave == false,
    'clone ' .. MATS[i][1] .. ': ground model, textures copied, not saved')
end
T.check(dirt.fields.groundmodelName == 'DIRT', 'the level dirt material untouched')
-- a second load reuses the clones
local before = S.calls.createObject_TerrainMaterial or 0
terrain.load('/temp/beamngEarth/s1/hm_1_128_0.png', 2, 4096, -384, -512, 1000, { masks = masks, materials = MATS })
T.check((S.calls.createObject_TerrainMaterial or 0) == before, 'clones made once')

-- a missing mask: the single dirt layer (the block must still import)
local realExists = FS.fileExists
FS.fileExists = function(_, p) return p ~= masks[3] end
T.check(terrain.load('/temp/beamngEarth/s1/hm_1_256_0.png', 2, 4096, -256, -512, 1000, { masks = masks, materials = MATS }),
  'a missing mask still loads')
T.check(#tb.lastImport.mats == 1 and tb.lastImport.mats[1] == 'beamngEarthDirt', '... with the single dirt layer')
FS.fileExists = realExists
-- a material name taken by another kind of object (the weather's Precipitation was 'bngEarthSnow'):
-- the single dirt layer, never a layer bound to nothing (the engine makes those asphalt)
do
  local precip = S.newObject('Precipitation'); precip:registerObject('bngEarthClash')
  function precip:getClassName() return 'Precipitation' end
  local clash = { { 'bngEarthClash', 'SNOW' }, { 'beamngEarthDirt', 'DIRT' } }
  terrain.load('/temp/beamngEarth/s1/hm_1_640_0.png', 2, 4096, 128, -512, 1000, { masks = { masks[1], masks[2] }, materials = clash })
  T.check(#tb.lastImport.mats == 1 and tb.lastImport.mats[1] == 'beamngEarthDirt', 'a name taken by a non-material: the single layer')
end
-- mismatched lists: the single layer
terrain.load('/temp/beamngEarth/s1/hm_1_384_0.png', 2, 4096, -128, -512, 1000, { masks = { masks[1] }, materials = MATS })
T.check(#tb.lastImport.mats == 1, 'masks and materials of different lengths: the single layer')
-- no layers (server --surface off): as before
terrain.load('/temp/beamngEarth/s1/hm_1_512_0.png', 2, 4096, 0, -512, 1000)
T.check(#tb.lastImport.mats == 1 and tb.lastImport.mats[1] == 'beamngEarthDirt', 'no layers: the single dirt layer')

-- GroundCover layer refresh (terrain.layerRefresh): in mesh mode a layered import is followed by one
-- frame with the block drawn REFRESH_DROP lower, then back in place, hidden, and the cells flushed
do
  local flushes = 0
  _G.flushGroundCoverGrids = function() flushes = flushes + 1 end
  terrain.setRenderEnabled(false)
  terrain.layerRefresh = false
  terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS })
  T.check(not terrain.refreshPending() and not terrain.tick(), 'no refresh unless asked for')
  terrain.layerRefresh = true
  terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000)
  T.check(not terrain.refreshPending(), 'no refresh after a single-layer import')
  terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS })
  T.check(terrain.refreshPending() and tb.renderEnabled == false and tb.pos.z == 1000, 'armed; the import itself leaves the block hidden in place')
  T.check(terrain.tick() == false, 'frame 1: not done yet')
  T.check(tb.renderEnabled == true and tb.pos.z == 1000 - terrain.REFRESH_DROP and tb.pos.x == -512, 'frame 1: drawn, sunk')
  T.check(terrain.tick() == true and flushes == 1, 'frame 2: done, cells flushed')
  T.check(tb.renderEnabled == false and tb.pos.z == 1000 and not terrain.refreshPending(), 'frame 2: hidden, back in place')
  T.check(terrain.tick() == false and flushes == 1, 'then nothing')
  -- a new import between the two frames: the new position wins, the refresh starts over
  terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS })
  terrain.tick()
  terrain.load('/temp/beamngEarth/s1/hm_1_128_0.png', 2, 4096, -384, -512, 1010, { masks = masks, materials = MATS })
  T.check(tb.renderEnabled == false and tb.pos.z == 1010 and tb.pos.x == -384, 'a re-import mid-refresh: hidden, at its own place')
  terrain.tick(); terrain.tick()
  T.check(tb.pos.z == 1010 and tb.renderEnabled == false and flushes == 2, '... and refreshed there')
  -- terrain mode meanwhile (the block drawn anyway): no sinking, the block put back
  terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS })
  terrain.tick()
  terrain.setRenderEnabled(true)
  T.check(terrain.tick() == false and tb.pos.z == 1000 and not terrain.refreshPending(), 'terrain mode mid-refresh: back in place, dropped')
  terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS })
  T.check(not terrain.refreshPending(), 'terrain mode: never armed')
  terrain.setRenderEnabled(false)
  terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS })
  terrain.onLevelChanged()
  T.check(not terrain.refreshPending(), 'a level change drops it')
  terrain.layerRefresh = false
end

-- groundcover.fieldsFor: a stock definition re-pointed at our layer, its element count scaled
do
  _G.jsonNull = _G.jsonNull or setmetatable({}, { __tostring = function() return 'null' end })
  local gc = require('beamngEarth/groundcover')
  local def = { name = 'g', class = 'GroundCover', persistentId = 'x', __parent = 'veg', position = { 1, 2, 3 },
    material = 'm_grass', radius = 50, maxElements = 306000, shapesCastShadows = false, windGustLength = jsonNull,
    Types = { { layer = 'Grass', billboardUVs = { 0, 0, 1, 0.5 }, probability = jsonNull, sizeMin = 0.15 },
              { layer = 'RockyDirt', sizeMax = 0.5 }, { billboardUVs = jsonNull } } }
  local calls = gc.fieldsFor(def, 'bngEarthGroundGrass', 0.5)
  local f = {}
  for _, c in ipairs(calls) do f[c[1] .. '#' .. c[2]] = c[3] end
  T.check(f['material#0'] == 'm_grass' and f['radius#0'] == '50' and f['maxElements#0'] == '153000', 'fields copied, elements scaled')
  T.check(f['shapesCastShadows#0'] == '0' and f['windGustLength#0'] == nil, 'booleans as 0/1, nulls left out')
  T.check(f['name#0'] == nil and f['position#0'] == nil and f['persistentId#0'] == nil and f['Types#0'] == nil, 'identity fields left out')
  T.check(f['layer#0'] == 'bngEarthGroundGrass' and f['layer#1'] == 'bngEarthGroundGrass' and f['layer#2'] == nil,
    'every plant type on our layer; an empty type stays empty')
  T.check(f['billboardUVs#0'] == '0 0 1 0.5' and f['sizeMin#0'] == '0.15' and f['probability#0'] == nil and f['sizeMax#1'] == '0.5',
    'type fields by index')
  local again = gc.fieldsFor(def, 'bngEarthGroundGrass', 0.5)
  local same = #again == #calls
  for i = 1, #calls do same = same and again[i][1] == calls[i][1] and again[i][2] == calls[i][2] and again[i][3] == calls[i][3] end
  T.check(same, 'deterministic order')
  T.check(gc.fieldsFor({ maxElements = 10 }, 'x', 0.01)[1][3] == '1', 'at least one element')
  local names = {}
  local unique = true
  for _, d in ipairs(gc.DEFS) do
    local n = d.name or d.src
    unique = unique and not names[n]
    names[n] = true
  end
  T.check(unique, 'one object name per definition (a stock one on two layers gets its own name)')
end

T.done('test_surface_layers')
