-- The near, drivable ground: one runtime TerrainBlock, created once and re-imported whenever
-- it follows the player or the anchor changes (docs/V1_1_SPEC.md 1a). Engine-only module.
-- N = 1024 samples at squareSize exactly 2.0 m (world-locked lattice, lattice.lua and
-- docs/V1_2_DESIGN.md 1.2, 12): collidable area 2046 m x 2046 m; re-import ~42 ms warm (P7).
--
-- Near visual 'mesh' (docs/V1_3_SPEC.md B, C): the block stays the collision surface but is not
-- drawn (setRenderEnabled(false): a SceneObject method, exe strings "setRenderEnabled" /
-- "isRenderEnabled", used by stock editor/particleEditor2.lua:227; a render-disabled TerrainBlock
-- stays fully collidable, ARCHITECTURE.md TerrainBlock probes). Whether importMaps keeps the flag
-- is unverified, so it is re-applied after every import.
--
-- Creation mirrors util/terrainGenerator.lua:177-181 (TerrainBlock(), setName,
-- registerObject, setTerrFileLvlFolder, MissionGroup:addObject) and the material setup
-- of terrainGenerator.lua:451-463 (addMaterial/updateMaterial, then importMaps with a
-- material-name list and one layer map per material).
--
-- Block size (docs/V1_4_SPEC.md 3.5, M4): configure(size) from the server's hello.blockSize picks
-- the 1024² or 512² layer map and flat heightmap (the layer map must match the heightmap size).

local lattice = require('beamngEarth/lattice')

local M = {}

M.NAME = 'bngEarthTerrain'
M.SIZE = lattice.N              -- samples per side (configure); the layer map has the same size
M.SQUARE_SIZE = lattice.SQUARE  -- 2.0 m
M.MATERIAL = 'beamngEarthDirt'
M.TEXTURE_SET = 'beamngEarthTerrainMaterialTextureSet'

local TERRAIN_DIR = '/levels/beamng_earth/art/terrains/'
local MATERIALS_FILE = TERRAIN_DIR .. 'main.materials.json'
-- per block size (tools/make_level_assets.py): all white = material 0 everywhere; all zero, 16-bit
local LAYER_MAPS = { [1024] = TERRAIN_DIR .. 'beamngEarth_layer_1024.png', [512] = TERRAIN_DIR .. 'beamngEarth_layer_512.png' }
local FLAT_HEIGHTMAPS = { [1024] = TERRAIN_DIR .. 'beamngEarth_flat_1024.png', [512] = TERRAIN_DIR .. 'beamngEarth_flat_512.png' }
local LAYER_MAP, FLAT_HEIGHTMAP = LAYER_MAPS[1024], FLAT_HEIGHTMAPS[1024]

local materialsChecked = false
local renderEnabled = true -- false in mesh mode; survives levels (a server setting)

-- Our level loads main.materials.json by itself (the level loader scans the level folder,
-- server/server.lua:200-212 loadJsonMaterialsFile). In any other level (testing) load it on demand.
local function ensureMaterials()
  if materialsChecked then return end
  materialsChecked = true
  if not scenetree.findObject(M.MATERIAL) or not scenetree.findObject(M.TEXTURE_SET) then
    log('I', 'beamngEarth', 'loading terrain materials from ' .. MATERIALS_FILE)
    loadJsonMaterialsFile(MATERIALS_FILE)
  end
end

function M.get()
  return scenetree.findObject(M.NAME)
end

-- The session's block size (1024 or 512, after lattice.configure): the imports' layer map and the
-- flat start map follow it. Returns the size in use.
function M.configure(size)
  local n = LAYER_MAPS[size] and size or 1024
  M.SIZE, LAYER_MAP, FLAT_HEIGHTMAP = n, LAYER_MAPS[n], FLAT_HEIGHTMAPS[n]
  return n
end

local function getOrCreate()
  local tb = M.get()
  if tb then return tb end
  ensureMaterials()
  tb = TerrainBlock()
  tb:setName(M.NAME)
  tb:registerObject(M.NAME)
  tb.canSave = false
  -- importMaps keeps its .ter data under this folder; /temp is writable and outside any level.
  tb:setTerrFileLvlFolder('/temp/beamngEarth/')
  -- v1.5 terrain materials need the block to name its TerrainMaterialTextureSet
  -- (terrainGenerator.lua:418, editor/terrainEditor.lua:1280).
  tb:setField('materialTextureSet', 0, M.TEXTURE_SET)
  scenetree.MissionGroup:addObject(tb)
  local ok, err = pcall(function()
    tb:addMaterial(M.MATERIAL, -1)
    tb:updateMaterial(0, M.MATERIAL)
  end)
  if not ok then log('W', 'beamngEarth', 'terrain addMaterial failed: ' .. tostring(err)) end
  return tb
end

-- Apply the render flag to the block. Terrain mode never calls the engine unless the flag was off
-- before (keeps the v1.2 path untouched).
local function applyRender(tb, force)
  if (force or not renderEnabled) and tb.setRenderEnabled then tb:setRenderEnabled(renderEnabled) end
end

-- Draw the block (terrain mode) or not (mesh mode: physics only).
function M.setRenderEnabled(on)
  on = on and true or false
  if on == renderEnabled then return end
  renderEnabled = on
  local tb = M.get()
  if tb then applyRender(tb, true) end
end

function M.isRenderEnabled() return renderEnabled end

local function importMaps(tb, path, squareSize, maxHeight, withMaterial)
  local materials, textureMaps = {}, {}
  if withMaterial then
    -- same table layout as terrainGenerator.lua:183-185 getTextureMap()
    materials = { M.MATERIAL }
    textureMaps = { { path = LAYER_MAP, selected = false, material = M.MATERIAL, materialId = 0, channel = 'R', channelId = 0 } }
  end
  local ok, res = pcall(function()
    return tb:importMaps(path, squareSize, maxHeight, '', materials, textureMaps, false)
  end)
  if not ok then return false, tostring(res) end
  if res == false then return false, 'importMaps returned false' end
  return true
end

-- Load a heightmap PNG and place the block's SW corner at (x, y, z).
-- PNG row 0 is the north edge with flipY=false (V1_SPEC "Why terrain-based ground").
-- The caller rebuilds collision (be:reloadCollision) once all world edits of the frame are done.
function M.load(path, squareSize, maxHeight, x, y, z)
  if not FS:fileExists(path) then return false, 'heightmap not found: ' .. tostring(path) end
  local tb = getOrCreate()
  local ok, err = importMaps(tb, path, squareSize, maxHeight, true)
  if not ok then
    -- Ground without a material still collides; it just renders as the placeholder.
    log('W', 'beamngEarth', 'importMaps with material failed (' .. tostring(err) .. '), retrying without')
    ok, err = importMaps(tb, path, squareSize, maxHeight, false)
    if not ok then return false, err end
  end
  tb:setPosition(vec3(x, y, z))
  applyRender(tb) -- mesh mode: hidden again after every import
  return true
end

-- Flat ground at z=0, centred on the origin, so the player has something to stand on before
-- the first anchor (the session's block size: a 512² session imports the 512² flat map).
function M.loadFlat()
  local x, y = lattice.blockOrigin(0, 0)
  return M.load(FLAT_HEIGHTMAP, M.SQUARE_SIZE, 50, x, y, 0)
end

-- smallgrid's GroundPlanes stop colliding once a TerrainBlock exists (V1_SPEC), so hide them.
-- setHidden as in levels/smallgrid/mainLevel.lua:27.
function M.hideGroundPlanes()
  for _, name in ipairs(scenetree.findClassObjects('GroundPlane') or {}) do
    local o = scenetree.findObject(name)
    if o and o.setHidden then o:setHidden(true) end
  end
end

function M.delete()
  local tb = M.get()
  if tb then tb:delete() end
end

function M.onLevelChanged()
  materialsChecked = false
end

return M
