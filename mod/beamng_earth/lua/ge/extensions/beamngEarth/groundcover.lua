-- Grass and low shrubs near the car (docs/ROADMAP.md v1.5; server --surface on). GroundCover renders on
-- the render-disabled physics block and honours its layer map (docs/research/2026-10-11-engine-probes.md
-- §4), so binding stock GroundCover definitions to the surface layers (server/surface.py: grassland /
-- forest floor -> bngEarthGroundGrass, shrubland -> bngEarthGroundDusty) puts grass where the land
-- cover says grass, nothing on asphalt, bare ground, rock, snow or mud. GroundCover reads the layers
-- from a copy the block refreshes only when drawn: terrain.lua does that after every layered import
-- while M.ensure has made the objects (terrain.layerRefresh).
--
-- The definitions are stock ones, read at runtime from official levels' own files (nothing is copied
-- into the mod; Polish Roads is not used, CREDITS.md): their materials are loaded first (a shape whose
-- material is missing when it is first drawn stays "no material"), then each GroundCover is recreated
-- with its plant types' layer pointed at ours and its element count scaled by `share`. Green Hirochi
-- Raceway grass with a little of West Coast USA's dry grass on grassland, West Coast scrub with sparse
-- green grass between on dusty ground. Generic: one rule for every place; per-biome / per-climate /
-- per-season variants are a later step. Console: beamngEarth_groundcover.setEnabled(bool), .status()

local terrain = require('beamngEarth/terrain')

local M = {}

local HIROCHI, WCUSA = '/levels/hirochi_raceway/', '/levels/west_coast_usa/'
M.SOURCES = {
  hirochi = { materials = HIROCHI .. 'art/shapes/groundcover/main.materials.json',
              items = HIROCHI .. 'main/MissionGroup/vegetation/terrain_grass/items.level.json' },
  wcusa = { materials = WCUSA .. 'art/groundcover/main.materials.json',
            items = WCUSA .. 'main/MissionGroup/vegetation/items.level.json' },
}
-- stock GroundCover (source, src name) -> our layer, element share; `name` (default src) names our
-- object, for a stock definition used on two layers
M.DEFS = {
  { source = 'hirochi', src = 'small_grass_close', layer = 'bngEarthGroundGrass', share = 0.5 },
  { source = 'hirochi', src = 'long_grass_distant', layer = 'bngEarthGroundGrass', share = 0.5 },
  { source = 'wcusa', src = 'grass_dry_1', layer = 'bngEarthGroundGrass', share = 0.15 },
  { source = 'wcusa', src = 'shrubs_near', layer = 'bngEarthGroundDusty', share = 0.25 },
  -- sparse grass between the shrubs (user 2026-10-11: a hard line between grassland and shrubland)
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_scrub', layer = 'bngEarthGroundDusty', share = 0.2 },
  { source = 'hirochi', src = 'long_grass_distant', name = 'long_grass_distant_scrub', layer = 'bngEarthGroundDusty', share = 0.15 },
}
M.PREFIX = 'bngEarthGC_'
M.enabled = true

local created = {}        -- object names we made
local loadedMaterials = {} -- source -> true
local lastError = nil

local SKIP = { name = true, class = true, persistentId = true, __parent = true, position = true, Types = true,
  internalName = true, canSave = true }

local function fieldValue(v)
  if type(v) == 'table' then
    local parts = {}
    for i, x in ipairs(v) do parts[i] = tostring(x) end
    return table.concat(parts, ' ')
  end
  if type(v) == 'boolean' then return v and '1' or '0' end
  return tostring(v)
end

-- The stock definitions of one source by name, from its items file (one JSON object per line).
local function readDefs(source)
  local file = M.SOURCES[source].items
  local text = readFile and readFile(file)
  if type(text) ~= 'string' then return nil, 'cannot read ' .. file end
  local want = {}
  for _, d in ipairs(M.DEFS) do if d.source == source then want[d.src] = true end end
  local out = {}
  for line in text:gmatch('[^\r\n]+') do
    if line:find('"GroundCover"', 1, true) then
      local ok, d = pcall(jsonDecode, line)
      if ok and type(d) == 'table' and want[d.name] then out[d.name] = d end
    end
  end
  return out
end

-- Pure: the setField calls (name, index, value) that recreate a stock definition on `layer`, its
-- element count scaled by `share`.
function M.fieldsFor(def, layer, share)
  local calls = {}
  for k, v in pairs(def) do
    if not SKIP[k] and v ~= jsonNull then
      if k == 'maxElements' then v = math.max(1, math.floor((tonumber(v) or 0) * share + 0.5)) end
      calls[#calls + 1] = { k, 0, fieldValue(v) }
    end
  end
  if type(def.Types) == 'table' then
    for i, t in ipairs(def.Types) do
      for k, v in pairs(t) do
        if v ~= nil and v ~= jsonNull then
          if k == 'layer' then v = (v ~= '' and layer or '') end
          calls[#calls + 1] = { k, i - 1, fieldValue(v) }
        end
      end
    end
  end
  table.sort(calls, function(a, b) if a[1] ~= b[1] then return a[1] < b[1] end return a[2] < b[2] end)
  return calls
end

local function removeAll()
  for _, name in ipairs(created) do
    local o = scenetree.findObject(name)
    if o then pcall(function() o:delete() end) end
  end
  created = {}
end

-- Create the GroundCovers (once per level; the surface layers exist). Returns true if made.
function M.ensure()
  if not M.enabled or #created > 0 then
    terrain.layerRefresh = #created > 0
    return #created > 0
  end
  if not scenetree.MissionGroup then return false end
  local ok, err = pcall(function()
    local defs = {}
    for _, d in ipairs(M.DEFS) do
      if not defs[d.source] then
        if not loadedMaterials[d.source] then
          loadJsonMaterialsFile(M.SOURCES[d.source].materials)
          loadedMaterials[d.source] = true
        end
        local e
        defs[d.source], e = readDefs(d.source)
        if not defs[d.source] then error(e) end
      end
      local def = defs[d.source][d.src]
      if def then
        local name = M.PREFIX .. (d.name or d.src)
        if not scenetree.findObject(name) then
          local gc = createObject('GroundCover')
          for _, c in ipairs(M.fieldsFor(def, d.layer, d.share)) do gc:setField(c[1], c[2], c[3]) end
          gc.canSave = false
          gc:registerObject(name)
          scenetree.MissionGroup:addObject(gc)
          pcall(function() gc:postApply() end) -- the first layered GroundCover once showed nothing until this
        end
        created[#created + 1] = name
      else
        log('W', 'beamngEarth', 'ground cover: no ' .. d.src .. ' in ' .. d.source)
      end
    end
  end)
  if not ok then
    lastError = tostring(err)
    log('W', 'beamngEarth', 'ground cover: ' .. lastError)
    removeAll()
  end
  terrain.layerRefresh = #created > 0
  return #created > 0
end

function M.setEnabled(on)
  M.enabled = on and true or false
  if not M.enabled then
    removeAll()
    terrain.layerRefresh = false
  end
  return M.enabled
end

-- The level ended (its objects are gone with it).
function M.reset()
  created = {}
  terrain.layerRefresh = false
end

function M.status()
  return { enabled = M.enabled, objects = #created, materialsLoaded = next(loadedMaterials) ~= nil, error = lastError }
end

return M
