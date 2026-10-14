-- Parked cars (the user, 2026-10-14: "BeamNG handles spawning traffic and parked cars pretty well"): the
-- road tiles carry curbside spots and lot stalls (server/parking.py: rows {x, y, z, heading[, 1 = lot]})
-- and this module hands them to BeamNG's own parking system (gameplay_parking) as a sites object, the way a
-- stock level's city.sites.json does. The stock system spawns the player's parked-car amount (Options >
-- Gameplay > Traffic, trafficParkedAmount) and keeps moving them to free spots near the player as the
-- player drives; the radial menu's traffic entries work as on stock maps.
--
-- One sites object per session. Its spot objects are kept by position (so a car's spot stays the same object
-- while its tile is loaded) and rebuilt at most every REBUILD_S from the spots within KEEP_M of the player
-- (the quadtree is rebuilt with them; finalizeSites). A spot a car stands on stays until the car moves on.
-- A new anchor clears them all (another frame); after a teleport the cars are scattered onto the new spots
-- once there are enough near the player (a rebase moves every vehicle with the world: nothing to do).
local M = {}

M.REBUILD_S = 2.0
M.KEEP_M = 900          -- spots this close to the player are in the sites
M.START_M = 400         -- the parked cars start / scatter once this many spots are within START_M ...
M.START_MIN = 24        -- ... at least this many
M.SPOT_SCL = { 2.5, 6.0, 3.0 }      -- the stock spot size (city.sites.json)
M.enabled = true

local spots = {}        -- tile key -> { anchor, rows }
local objs = {}         -- position key -> parking spot object (current anchor)
local anchorId = nil
local dirty = false
local lastBuild = -math.huge   -- os.clock() of the last rebuild
local sites = nil
local started = false   -- the parked cars were started once this level (the player may remove them)
local scatterPending = false

function M.onRows(aid, key, rows)
  if type(rows) ~= 'table' then return end
  spots[key] = { anchor = aid, rows = rows }
  dirty = true
end

function M.dropKey(key)
  if spots[key] then
    spots[key] = nil
    dirty = true
  end
end

-- The player teleported: the parked cars go to the new place's spots once it has them.
function M.onTeleport() scatterPending = true end

-- the stock extensions the parked cars need (a level without traffic support loads none of them;
-- setupVehicles spawns its group through core_multiSpawn)
M.EXTENSIONS = { 'core_multiSpawn', 'gameplay_traffic', 'gameplay_parking' }
local function parkingSystem()
  if not (gameplay_parking and core_multiSpawn) and extensions and extensions.load then
    pcall(extensions.load, unpack(M.EXTENSIONS))
  end
  return gameplay_parking
end

local function newSites()
  return require('/lua/ge/extensions/gameplay/sites/sites')('BeamNG.Earth')
end

-- Pure: the position key of a spot row (spots from neighbouring tiles never coincide).
function M.spotKey(r) return string.format('%.1f_%.1f', r[1], r[2]) end

local function makeSpot(r)
  local ps = sites.parkingSpots.objectConstructor(sites)
  local h = math.rad(r[4])
  ps:set(vec3(r[1], r[2], r[3]), quatFromDir(vec3(math.cos(h), math.sin(h), 0), vec3(0, 0, 1)),
    vec3(M.SPOT_SCL[1], M.SPOT_SCL[2], M.SPOT_SCL[3]))
  if r[5] ~= 1 then                       -- curbside (lot stalls: either way in, not on a street)
    ps.customFields:addTag('street')      -- mapmgr tracks the car (traffic dodges it)
    ps.customFields:addTag('forwards')    -- parallel parking: nose in the direction of travel
  end
  return ps
end

-- Rebuilds the sites from the spots near the player; returns the number of spots within START_M.
local function rebuild(player)
  sites = sites or newSites()
  local list = sites.parkingSpots
  local keep, near = {}, 0
  local k2, s2 = M.KEEP_M * M.KEEP_M, M.START_M * M.START_M
  for _, t in pairs(spots) do
    if t.anchor == anchorId then
      for _, r in ipairs(t.rows) do
        local dx, dy = r[1] - player.x, r[2] - player.y
        local d2 = dx * dx + dy * dy
        if d2 < k2 then
          local key = M.spotKey(r)
          keep[key] = objs[key] or makeSpot(r)
          if d2 < s2 then near = near + 1 end
        end
      end
    end
  end
  for key, ps in pairs(objs) do
    if not keep[key] and ps.vehicle then keep[key] = ps end   -- a car stands on it
  end
  objs = keep
  table.clear(list.objects)          -- (its metatable answers unknown ids with a "missing" spot)
  for _, ps in pairs(keep) do list.objects[ps.id] = ps end
  list:sort()
  sites:finalizeSites()
  local gp = parkingSystem()
  if gp then gp.setSites(sites) end
  return near
end

function M.update(aid, player)
  if aid ~= anchorId then
    anchorId = aid
    objs = {}
    dirty = true
  end
  if not M.enabled or not player or not dirty or not next(spots) then return end
  local t = os.clock()
  if t - lastBuild < M.REBUILD_S then return end
  lastBuild, dirty = t, false
  local ok, near = pcall(rebuild, player)
  if not ok then
    log('W', 'beamngEarth', 'parking: ' .. tostring(near))
    return
  end
  local gp = gameplay_parking
  if not gp or near < M.START_MIN then return end
  if not started then
    started = true
    local amount = settings and settings.getValue and tonumber(settings.getValue('trafficParkedAmount')) or 0
    if amount > 0 and not gp.getState() then
      local okS, err = pcall(gp.setupVehicles, -1, { pos = vec3(player.x, player.y, player.z) })
      if not okS then log('W', 'beamngEarth', 'parking: setupVehicles: ' .. tostring(err)) end
    end
    scatterPending = false
  elseif scatterPending then
    scatterPending = false
    if gp.getState() then gp.scatterParkedCars() end
  end
end

function M.status()
  local n = 0
  for _ in pairs(objs) do n = n + 1 end
  return { tiles = (function() local c = 0 for _ in pairs(spots) do c = c + 1 end return c end)(), spots = n,
           started = started, active = gameplay_parking and gameplay_parking.getState() or false }
end

-- The level ended: the stock system forgets our sites with it.
function M.reset()
  spots, objs, anchorId, dirty, lastBuild, sites = {}, {}, nil, false, -math.huge, nil
  started, scatterPending = false, false
end

return M
