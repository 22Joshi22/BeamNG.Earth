-- The sea: one WaterPlane at sea level while the server says sea water is near (server/seacarve.py,
-- 'sea' messages, --sea on). The server carves the ground under the sea and keeps land above this
-- plane's level; the plane gives the water its look, buoyancy and drowning (a runtime WaterBlock
-- neither draws nor floats anything in our level: docs/research/2026-10-13-water-probe.md).
local M = {}

M.HIDE_ABOVE_M = 300    -- no plane while the player is this far above the sea (the server keeps land
                        -- above the plane near the anchor only; from high up the far tiles show the sea)
M.MOVE_Z = 0.02         -- m: the plane is moved when the level changed this much
M.NAME = 'bngEarthSea'

-- East Coast USA's ocean ("Ocean", levels/east_coast_usa/main), its colours made murkier (a bay, not
-- the open Atlantic); core textures where that level's own may be missing
local FIELDS = {
  { 'baseColor', 0, '46 98 96 255' }, { 'underwaterColor', 0, '60 96 88 240' },
  { 'clarity', 0, '0.15' }, { 'fresnelBias', 0, '-0.5' }, { 'fresnelPower', 0, '0.2' },
  { 'specularPower', 0, '1000' }, { 'reflectDetailAdjust', 0, '0' }, { 'reflectMaxRateMs', 0, '1' },
  { 'fullReflect', 0, '0' }, { 'gridSize', 0, '220' }, { 'gridElementSize', 0, '0.8' },
  { 'depthGradientMax', 0, '5' }, { 'distortEndDist', 0, '15' }, { 'distortFullDepth', 0, '5' },
  { 'waterFogDensity', 0, '0.4' }, { 'waterFogDensityOffset', 0, '0.1' },
  { 'wetDarkening', 0, '0.3' }, { 'wetDepth', 0, '0.5' }, { 'viscosity', 0, '0.001' },
  { 'overallRippleMagnitude', 0, '0.2' }, { 'overallWaveMagnitude', 0, '0.1' },
  { 'overallFoamOpacity', 0, '3.5' }, { 'foamMaxDepth', 0, '0.75' }, { 'foamAmbientLerp', 0, '1' },
  { 'foamRippleInfluence', 0, '0.005' },
  { 'rippleDir', 0, '0 -1' }, { 'rippleDir', 1, '0.707 0.707' }, { 'rippleDir', 2, '-0.5 0.86' },
  { 'rippleMagnitude', 0, '0.5' }, { 'rippleMagnitude', 1, '0.5' }, { 'rippleMagnitude', 2, '0.35' },
  { 'rippleSpeed', 0, '0.008' }, { 'rippleSpeed', 1, '0.05' }, { 'rippleSpeed', 2, '0.003' },
  { 'rippleTexScale', 0, '10 10' }, { 'rippleTexScale', 1, '2 2' }, { 'rippleTexScale', 2, '120 120' },
  { 'waveDir', 0, '0 1' }, { 'waveDir', 1, '-0.707 -0.707' }, { 'waveDir', 2, '0.5 0.86' },
  { 'waveMagnitude', 0, '0.5' }, { 'waveMagnitude', 1, '0.105' }, { 'waveMagnitude', 2, '0.1' },
  { 'waveSpeed', 0, '2' }, { 'waveSpeed', 1, '1' }, { 'waveSpeed', 2, '2' },
  { 'foamDir', 0, '0 1' }, { 'foamDir', 1, '0 -1' }, { 'foamSpeed', 0, '0.01' }, { 'foamSpeed', 1, '0.01' },
  { 'foamOpacity', 1, '5' }, { 'foamTexScale', 1, '4 4' },
}
local TEXTURES = {      -- field, East Coast's file, the core fallback
  { 'rippleTex', '/levels/east_coast_usa/art/water/ripple3.dds', '/core/art/water/ripple_nm.normal.dds' },
  { 'depthGradientTex', '/levels/east_coast_usa/art/water/depthcolor_ramp.dds',
    '/core/art/water/depthcolor_ramp_b.color.png' },
  { 'foamTex', '/core/art/water/foam.dds', '/core/art/water/foam_b.color.png' },
}

local want = nil        -- { anchorId, z } from the server, or nil (no sea near)
local id = nil          -- the plane's object id
local shownZ = nil
M.created = 0

local function exists(p)
  return not FS or FS:fileExists(p)
end

local function find()
  return id and scenetree.findObjectById(id) or nil
end

local function remove()
  local o = find()
  if o then o:delete() end
  id, shownZ = nil, nil
end

local function create(z)
  local o = createObject('WaterPlane')
  for _, f in ipairs(FIELDS) do o:setField(f[1], f[2], f[3]) end
  for _, t in ipairs(TEXTURES) do o:setField(t[1], 0, exists(t[2]) and t[2] or t[3]) end
  o:setField('cubemap', 0, scenetree.findObject('cubemap_ocean_reflection') and 'cubemap_ocean_reflection'
    or 'BNG_Sky_01_cubemap')
  o:setPosition(vec3(0, 0, z))
  o.canSave = false
  o:registerObject(M.NAME)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  id, shownZ = o:getId(), z
  M.created = M.created + 1
end

-- A 'sea' message: { anchorId, on, z } (z: sea level under the player in the anchor frame).
function M.onMessage(msg)
  if msg.on == true and tonumber(msg.z) then
    want = { anchorId = msg.anchorId, z = tonumber(msg.z) }
  else
    want = nil
  end
end

-- Every frame: the plane exists while the server's sea is for the current anchor and the player is
-- not high above it.
function M.update(anchorId, playerZ)
  local show = want ~= nil and want.anchorId == anchorId
    and (playerZ == nil or playerZ - want.z < M.HIDE_ABOVE_M)
  if not show then
    if id then remove() end
    return
  end
  local o = find()
  if not o then
    create(want.z)
  elseif math.abs(want.z - shownZ) > M.MOVE_Z then
    o:setPosition(vec3(0, 0, want.z))
    shownZ = want.z
  end
end

-- Disconnect, a new server session, mod unload: no plane until the server says so again.
function M.clear()
  want = nil
  remove()
end

-- Level end: the level deletes its objects itself.
function M.reset()
  want, id, shownZ = nil, nil, nil
end

function M.status()
  return { want = want and want.z or nil, shown = shownZ, created = M.created }
end

-- A reloaded module (extensions.reload) deletes the plane the old one left.
do
  local old = scenetree and scenetree.findObject and scenetree.findObject(M.NAME)
  if old then old:delete() end
end

return M
