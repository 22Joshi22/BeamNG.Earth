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

-- Layer refresh for GroundCover (groundcover.lua): GroundCover does not read the layer map that
-- importMaps writes (getMaterialIdxWs and the tyres do) but a copy the block refreshes only in a
-- frame in which it is drawn and in view; updateGridMaterials, updateAllTextures and toggling the
-- render flag within one call do not refresh it (in-game 2026-10-11). Until then GroundCover sees
-- the previous import's layers, or layer 0 everywhere on a block never drawn. So with the block
-- hidden (mesh mode), each layered import is followed by one frame with the block drawn REFRESH_DROP
-- metres lower (under the mesh ground: in view but hidden by depth), then it goes back; collision
-- is not rebuilt for it (the vehicles never notice a render-only move, in-game). Only while
-- something asks for it (M.layerRefresh).
-- Every setPosition of the block (even to the same place) and every import makes GroundCover drop
-- all its cells and regrow them over ~10 frames (in-game 2026-10-11), so the refresh runs at the
-- end of the import's own frame (core calls tick after the commits and their reloadCollision): the
-- import and the sink share one clear, the restore costs one more, and no flush is needed (the
-- restore's clear already rebuilds the cells at the right height). The engine's own clear at each
-- block move (every 128 m while driving) remains.
M.REFRESH_DROP = 100.0
M.layerRefresh = false
local refresh = nil       -- { x, y, z, drawn } of the block awaiting its refresh frame

-- Surface materials (server --surface on, server/surface.py): runtime clones of M.MATERIAL with other
-- ground models. The block is physics-only in mesh mode, so they only need a name and a ground model;
-- their texture fields are copied so a rendered block (terrain mode, dev "show physics terrain") still
-- draws. They join the TerrainMaterialSet by themselves (in-game probe 2026-10-11).
local TEX_FIELDS = { 'aoBaseTex', 'aoBaseTexSize', 'aoDetailStrength', 'aoDetailTex', 'aoMacroTex', 'aoMacroTexSize',
  'baseColorBaseTex', 'baseColorBaseTexSize', 'baseColorDetailStrength', 'baseColorDetailTex', 'baseColorMacroStrength',
  'baseColorMacroTex', 'baseColorMacroTexSize', 'detailDistances', 'macroDistances', 'heightBaseTex', 'heightBaseTexSize',
  'heightDetailTex', 'heightMacroTex', 'heightMacroTexSize', 'normalBaseTex', 'normalBaseTexSize', 'normalDetailStrength',
  'normalDetailTex', 'normalMacroStrength', 'normalMacroTex', 'normalMacroTexSize', 'roughnessBaseTex', 'roughnessBaseTexSize',
  'roughnessDetailStrength', 'roughnessDetailTex', 'roughnessMacroStrength', 'roughnessMacroTex', 'roughnessMacroTexSize' }

-- Make sure every { name, groundmodel } exists as a TerrainMaterial (M.MATERIAL is the level's own).
-- Returns false if one could not be made.
local function ensureSurfaceMaterials(materials)
  local src = scenetree.findObject(M.MATERIAL)
  for _, nm in ipairs(materials) do
    local name, gm = nm[1], nm[2]
    local existing = scenetree.findObject(name)
    if existing then
      -- another object of that name (a weather Precipitation was once called like a material): the
      -- import would bind the layer to nothing and the engine would make it asphalt
      local okc, cls = pcall(function() return existing:getClassName() end)
      if okc and cls and cls ~= 'TerrainMaterial' then return false end
    end
    if name ~= M.MATERIAL and not existing then
      local ok = pcall(function()
        local m = createObject('TerrainMaterial')
        if src then
          for _, f in ipairs(TEX_FIELDS) do
            local okf, v = pcall(function() return src:getField(f, 0) end)
            if okf and v and v ~= '' then m:setField(f, 0, v) end
          end
        end
        m:setField('internalName', 0, name)
        m:setField('groundmodelName', 0, gm)
        m.canSave = false
        m:registerObject(name)
      end)
      if not ok or not scenetree.findObject(name) then return false end
    end
  end
  return true
end

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

-- layers (optional): { masks = { path, ... }, materials = { { name, groundmodel }, ... } } (same order):
-- one 0/255 mask per material instead of the single layer map
local function importMaps(tb, path, squareSize, maxHeight, withMaterial, layers)
  local materials, textureMaps = {}, {}
  if withMaterial and layers then
    for i, nm in ipairs(layers.materials) do
      materials[i] = nm[1]
      textureMaps[i] = { path = layers.masks[i], selected = false, material = nm[1], materialId = i - 1, channel = 'R', channelId = 0 }
    end
  elseif withMaterial then
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
-- layers (optional, server --surface on): { masks = { path, ... }, materials = { { name, gm }, ... } }
function M.load(path, squareSize, maxHeight, x, y, z, layers)
  if not FS:fileExists(path) then return false, 'heightmap not found: ' .. tostring(path) end
  local tb = getOrCreate()
  if layers then
    local okL = type(layers.masks) == 'table' and type(layers.materials) == 'table' and #layers.masks == #layers.materials
      and #layers.masks > 0 and ensureSurfaceMaterials(layers.materials)
    if okL then
      for _, m in ipairs(layers.masks) do if not FS:fileExists(m) then okL = false; break end end
    end
    if not okL then
      log('W', 'beamngEarth', 'surface layers unusable, importing the single dirt layer')
      layers = nil
    end
  end
  local ok, err = importMaps(tb, path, squareSize, maxHeight, true, layers)
  if not ok then
    -- Ground without a material still collides; it just renders as the placeholder.
    log('W', 'beamngEarth', 'importMaps with material failed (' .. tostring(err) .. '), retrying without')
    ok, err = importMaps(tb, path, squareSize, maxHeight, false)
    if not ok then return false, err end
  end
  tb:setPosition(vec3(x, y, z))
  applyRender(tb) -- mesh mode: hidden again after every import
  refresh = (layers and M.layerRefresh and not renderEnabled) and { x = x, y = y, z = z, drawn = false } or nil
  return true
end

-- Once per frame (core onUpdate, after the frame's commits): the layer refresh after a layered
-- import, over two frames - the block drawn sunk in the import's frame, back in place and hidden in
-- the next. Returns true when a refresh finished this call.
function M.tick()
  local r = refresh
  if not r then return false end
  local tb = M.get()
  if not tb or renderEnabled then -- the block went away, or is drawn anyway now (terrain mode)
    if tb and r.drawn then tb:setPosition(vec3(r.x, r.y, r.z)) end
    refresh = nil
    return false
  end
  if not r.drawn then
    tb:setPosition(vec3(r.x, r.y, r.z - M.REFRESH_DROP))
    tb:setRenderEnabled(true)
    r.drawn = true
    return false
  end
  tb:setRenderEnabled(false)
  tb:setPosition(vec3(r.x, r.y, r.z))
  refresh = nil
  return true
end

function M.refreshPending() return refresh ~= nil end

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
  refresh = nil
end

return M
