-- The sea: WaterBlock tiles that follow the Earth's curve, from the server's 'sea' messages (server
-- seatiles.py / seacarve.py, --sea on; docs/research/2026-10-13-water-probe.md). Each tile is a square
-- at the sea level of its centre, tilted to the local vertical there; the server carves the ground
-- under the sea. A WaterBlock's surface is at its position (the engine caps its volume at the box's
-- centre), its volume DEPTH_M / 2 below.
local M = {}

M.DEPTH_M = 200         -- the blocks' z scale: water down to 100 m below the surface (sea floors <= 80 m)
M.CREATE_PER_FRAME = 4  -- tiles made per frame (a full set of ~50 in under a second, no hitch)
M.NUDGE_SPEED = 1.0     -- m/s: a car slower than this inside a new tile is re-placed where it is, so its
                        -- physics sees the water (a parked car only notices water when it moves)
M.PREFIX = 'bngEarthSea_'
-- A vehicle takes one water object at a time, the one made last that it is in (2026-10-13: on the edge
-- between two tiles only the nodes over one of them were in water). So one more block, not drawn,
-- PHYS_SIZE m square around the player at the level of the tile under it, is made again after any
-- new tile (it must be the newest) and when the player nears its edge or the level under it changes.
M.PHYS_SIZE = 256
M.PHYS_MARGIN = 64      -- m: made again around the player when it is closer than this to its edge
M.PHYS_NAME = 'bngEarthSeaPhys'

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

local want = nil         -- { anchorId, tiles = { key -> tile } } from the server, or nil
local made = {}         -- key -> { id, t }
local madeAnchor = nil
local phys = nil        -- { id, x, y, z } of the physics block
local physStale = false -- tiles were made since it was: made again once a frame makes none
M.created, M.nudges, M.physMade = 0, 0, 0

local function exists(p)
  return not FS or FS:fileExists(p)
end

local function removePhys()
  if phys then
    local o = scenetree.findObjectById(phys.id)
    if o then o:delete() end
    phys = nil
  end
end

local function removeAll()
  for _, m in pairs(made) do
    local o = scenetree.findObjectById(m.id)
    if o then o:delete() end
  end
  made, madeAnchor = {}, nil
  removePhys()
end

-- The tile of the server's set the point is in (the nearest centre if on an edge), or nil.
local function tileAt(p)
  local best, bd
  for _, t in pairs(want and want.tiles or {}) do
    if math.abs(p.x - t.x) <= t.size / 2 and math.abs(p.y - t.y) <= t.size / 2 then
      local d = (p.x - t.x) ^ 2 + (p.y - t.y) ^ 2
      if not bd or d < bd then best, bd = t, d end
    end
  end
  return best
end

local function makePhys(p, z)
  removePhys()
  local o = createObject('WaterBlock')
  for _, f in ipairs(FIELDS) do o:setField(f[1], f[2], f[3]) end
  o:setField('gridElementSize', 0, tostring(M.PHYS_SIZE / 16))
  o:setPosition(vec3(p.x, p.y, z))
  o:setScale(vec3(M.PHYS_SIZE, M.PHYS_SIZE, M.DEPTH_M))
  o.canSave = false
  o:registerObject(M.PHYS_NAME)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  if o.setRenderEnabled then o:setRenderEnabled(false) end
  phys = { id = o:getId(), x = p.x, y = p.y, z = z }
  M.physMade = M.physMade + 1
end

local function create(key, t)
  local o = createObject('WaterBlock')
  for _, f in ipairs(FIELDS) do o:setField(f[1], f[2], f[3]) end
  for _, x in ipairs(TEXTURES) do o:setField(x[1], 0, exists(x[2]) and x[2] or x[3]) end
  o:setField('cubemap', 0, 'BNG_Sky_01_cubemap')
  o:setField('gridElementSize', 0, tostring(math.max(4, t.size / 64)))
  -- the local vertical: a tilt of |n| radians about (-ny, nx, 0)
  local n = math.sqrt(t.nx * t.nx + t.ny * t.ny)
  if n > 1e-9 then
    o:setField('rotation', 0, string.format('%.9f %.9f 0 %.6f', -t.ny / n, t.nx / n, math.deg(math.asin(math.min(n, 1)))))
  end
  o:setPosition(vec3(t.x, t.y, t.z))
  o:setScale(vec3(t.size, t.size, M.DEPTH_M))
  o.canSave = false
  o:registerObject(M.PREFIX .. key)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  made[key] = { id = o:getId(), t = t }
  M.created = M.created + 1
end

local function inside(t, p)
  return math.abs(p.x - t.x) <= t.size / 2 and math.abs(p.y - t.y) <= t.size / 2
end

-- A 'sea' message: { anchorId, tiles = { {key, x, y, z, size, nx, ny}, ... } }.
function M.onMessage(msg)
  local tiles = {}
  for _, r in ipairs(type(msg.tiles) == 'table' and msg.tiles or {}) do
    if type(r) == 'table' and #r >= 7 then
      tiles[tostring(r[1])] = { x = tonumber(r[2]), y = tonumber(r[3]), z = tonumber(r[4]), size = tonumber(r[5]),
        nx = tonumber(r[6]), ny = tonumber(r[7]) }
    end
  end
  want = { anchorId = msg.anchorId, tiles = tiles }
end

-- Every frame: the tiles of the server's set for the current anchor exist, the others are gone.
-- player: { x, y, z, vx, vy, vz } in the anchor frame (or nil).
function M.update(anchorId, player)
  if want == nil or want.anchorId ~= anchorId then
    if madeAnchor ~= nil then removeAll() end
    return
  end
  if madeAnchor ~= anchorId then removeAll(); madeAnchor = anchorId end
  for key, m in pairs(made) do
    if not want.tiles[key] then
      local o = scenetree.findObjectById(m.id)
      if o then o:delete() end
      made[key] = nil
    end
  end
  local n, nudge = 0, false
  for key, t in pairs(want.tiles) do
    if not made[key] then
      if n >= M.CREATE_PER_FRAME then break end
      create(key, t)
      n = n + 1
      if player and inside(t, player) and player.z < t.z + 1 then nudge = true end
    end
  end
  if player then
    local t = tileAt(player)
    if not t then
      if phys then removePhys() end
    elseif (physStale and n == 0) or not phys or math.abs(player.x - phys.x) > M.PHYS_SIZE / 2 - M.PHYS_MARGIN
        or math.abs(player.y - phys.y) > M.PHYS_SIZE / 2 - M.PHYS_MARGIN or math.abs(t.z - phys.z) > 0.05 then
      makePhys(player, t.z)
      physStale = false
      if player.z < t.z + 1 then nudge = true end      -- in the water: its physics must take this one
    end
    if n > 0 then physStale = true end
  end
  if nudge and player then
    local speed = math.sqrt((player.vx or 0) ^ 2 + (player.vy or 0) ^ 2 + (player.vz or 0) ^ 2)
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    if veh and speed < M.NUDGE_SPEED then
      local p, r = veh:getPosition(), quat(veh:getRotation())
      veh:setPositionRotation(p.x, p.y, p.z, r.x, r.y, r.z, r.w)
      M.nudges = M.nudges + 1
    end
  end
end

-- Disconnect, a new server session, mod unload: no tiles until the server says so again.
function M.clear()
  want = nil
  removeAll()
end

-- Level end: the level deletes its objects itself.
function M.reset()
  want, made, madeAnchor, phys, physStale = nil, {}, nil, nil, false
end

function M.status()
  local n = 0
  for _ in pairs(made) do n = n + 1 end
  local w = 0
  for _ in pairs(want and want.tiles or {}) do w = w + 1 end
  return { tiles = n, want = w, created = M.created, nudges = M.nudges, physMade = M.physMade,
    phys = phys and phys.z or nil }
end

-- A reloaded module (extensions.reload) deletes the tiles (and the old WaterPlane) the old one left.
do
  if scenetree and scenetree.findClassObjects then
    for _, cls in ipairs({ 'WaterBlock', 'WaterPlane' }) do
      for _, name in ipairs(scenetree.findClassObjects(cls) or {}) do
        if tostring(name):sub(1, #M.PREFIX) == M.PREFIX or name == 'bngEarthSea' or name == M.PHYS_NAME then
          local o = scenetree.findObject(name)
          if o then o:delete() end
        end
      end
    end
  end
end

return M
