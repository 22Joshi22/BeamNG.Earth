-- Per-tile satellite-imagery materials (docs/V1_3_SPEC.md C, E). Engine-only calls, but the
-- bookkeeping runs under plain luajit with the stubs of mod/tests/stubs.lua.
--
-- A protocol-4 far tile carries `material` (the material name written into its .cdae), `texture`
-- (the imagery file next to it, a VFS path) and `detail` (the shape has UV1 = world-locked detail
-- UVs). Before the tile's TSStatic is created, a Material with mapTo = that name and colorMap =
-- the texture must exist: a Lua-created Material with mapTo = the shape's material name overrides
-- the shape's own (probe P2) and renders the imagery (lead probe probes/v12/p_sat.lua: material
-- 0.13-0.46 ms, tile spawn incl. texture load 2.5-3.5 ms).
-- One Material per name, reference-counted by the tile instances that use it (farfield.lua):
--   create  on first use (farfield runs it as a governor unit 'material' before the tile import);
--   ref     each instance that will spawn with it (a re-sent key shares its predecessor's material);
--   unref   when the instance's object is deleted (or it is dropped before it ever spawned);
--   delete  once no instance refers to it any more - queued, run by farfield as a governor unit
--           'matDelete' AFTER the objects using it are gone (never delete a Material in use).
-- Tiles without `material` (protocol 3, `--imagery off`) keep the static far-tile material and
-- never come here.

local M = {}

-- Ground grain for close-up views: our own procedural texture (tools/make_detail_texture.py), mean
-- exactly 0.5 grey. The v1.0 material adds the detail map with no distance fade, so a stock detail
-- texture showed as a fabric-like repeat on hills hundreds of metres away (in-game, v1.3); this one's
-- coarser mips fade to flat grey, so the GPU's mip selection fades the grain out with distance.
-- Applied on UV1 (detailMapUseUV = 1), which the server writes as world-locked (x, y) / DETAIL_M, so
-- the grain is continuous across tiles; detailScale 0.5 0.5 = one repeat per 2 * DETAIL_M = 4 m.
M.DETAIL_MAP = '/art/beamngEarth/detail_ground.dds'

-- Material fields (setField(name, 0, value)). colorMap is the field the in-game probe verified
-- (p_sat.lua, a version-1.0 Material). useAnisotropic sharpens the imagery at grazing angles
-- (as stock flowgraph/nodes/environment/planet.lua:80); roughness 1 = matte ground.
M.FIELDS = {
  { 'useAnisotropic', '1' },
  { 'roughnessFactor', '1' },
}
M.DETAIL_FIELDS = {
  { 'detailMapUseUV', '1' },
  { 'detailScale', '0.5 0.5' },
}
M.FALLBACK_COLOR = '0.39 0.35 0.27 1' -- texture file missing: the terrain's macro dirt colour

local mats = {}       -- name -> { refs = n, texture = path }
local delQ = {}       -- names whose refs reached 0, to delete (FIFO); checked again when run
local queued = {}     -- name -> true while in delQ
local nMats = 0
local detailChecked, detailOk = false, false
local warned = {}

local function warnOnce(key, text)
  if warned[key] then return end
  warned[key] = true
  log('W', 'beamngEarth', text)
end

local function hasDetail()
  if not detailChecked then
    detailChecked = true
    detailOk = FS:fileExists(M.DETAIL_MAP) and true or false
    if not detailOk then warnOnce('detail', 'imagery detail texture missing: ' .. M.DETAIL_MAP) end
  end
  return detailOk
end

-- Is the Material for this name created (and still ours)?
function M.exists(name) return mats[name] ~= nil end

-- Create the Material (as planet.lua:77-91 / p_sat.lua: createObject, setField, canSave = false,
-- registerObject). Object name = mapTo name, like the static far material. Returns true on success.
-- A Material of that name left over from an earlier session (e.g. an extension reload) is adopted,
-- pointed at our texture: its colorMap may name an older server session's file (anchor ids restart
-- at 1 with every server, so names repeat), and tiles loaded from now on read the current field.
function M.create(name, texture, detail)
  if mats[name] then return true end
  local m = scenetree.findObject(name)
  if m then
    if texture and FS:fileExists(texture) then m:setField('colorMap', 0, texture) end
  else
    m = createObject('Material')
    m:setField('mapTo', 0, name)
    if texture and FS:fileExists(texture) then
      m:setField('colorMap', 0, texture)
    else
      warnOnce('tex:' .. tostring(texture), 'imagery texture missing: ' .. tostring(texture))
      m:setField('diffuseColor', 0, M.FALLBACK_COLOR)
    end
    for _, f in ipairs(M.FIELDS) do m:setField(f[1], 0, f[2]) end
    if detail and hasDetail() then
      m:setField('detailMap', 0, M.DETAIL_MAP)
      for _, f in ipairs(M.DETAIL_FIELDS) do m:setField(f[1], 0, f[2]) end
    end
    m.canSave = false
    m:registerObject(name)
  end
  mats[name] = { refs = 0, texture = texture }
  nMats = nMats + 1
  return true
end

-- One more instance uses the material (it must exist). A different texture under the same name
-- (a restarted server re-uses anchor ids, so names repeat across its sessions): an unused material
-- (waiting for deletion) is pointed at the new file; one still in use keeps its texture, since its
-- tiles' loaded shapes keep theirs anyway.
function M.ref(name, texture)
  local e = mats[name]
  if not e then return false end
  if texture and e.texture and texture ~= e.texture then
    local m = e.refs == 0 and scenetree.findObject(name)
    if m and FS:fileExists(texture) then
      m:setField('colorMap', 0, texture)
      e.texture = texture
    else
      warnOnce('retex:' .. name, 'material ' .. name .. ' re-sent with another texture; keeping ' .. tostring(e.texture))
    end
  end
  e.refs = e.refs + 1
  return true
end

-- An instance using the material is gone (its object deleted, or it never spawned).
function M.unref(name)
  local e = mats[name]
  if not e then return end
  e.refs = e.refs - 1
  if e.refs <= 0 then
    e.refs = 0
    if not queued[name] then
      queued[name] = true
      delQ[#delQ + 1] = name
    end
  end
end

-- Materials waiting for deletion that nobody uses again.
function M.hasDeletes()
  for _, name in ipairs(delQ) do
    local e = mats[name]
    if e and e.refs == 0 then return true end
  end
  return false
end

-- Delete the next unused material (a governor unit). Returns true if one was deleted, false if
-- the queue held only re-used names (they are dropped from it).
function M.deleteNext()
  while #delQ > 0 do
    local name = table.remove(delQ, 1)
    queued[name] = nil
    local e = mats[name]
    if e and e.refs == 0 then
      mats[name] = nil
      nMats = nMats - 1
      local m = scenetree.findObject(name)
      if m then m:delete() end -- editor/api/material.lua:234 deletes Materials the same way
      return true
    end
  end
  return false
end

-- Delete every material we made. Only when no object uses them any more (extension unload after
-- the tiles were deleted, or a new level whose predecessor deleted its objects).
function M.clearAll()
  for name in pairs(mats) do
    local m = scenetree.findObject(name)
    if m then m:delete() end
  end
  mats, delQ, queued, nMats = {}, {}, {}, 0
  detailChecked = false
end

function M.count() return nMats end

-- Call fn(texture) for the texture of every material we hold (in use or waiting for deletion):
-- farfield.heldSessions (the server keeps those files while the material exists).
function M.eachTexture(fn)
  for _, e in pairs(mats) do if e.texture then fn(e.texture) end end
end
function M.refs(name) local e = mats[name]; return e and e.refs or 0 end -- for tests

return M
