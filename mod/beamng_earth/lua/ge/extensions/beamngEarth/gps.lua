-- GPS to a place picked on the world map (the user, 2026-10-14: "get the game's GPS system to work with this"). The
-- destination is kept as latitude / longitude (it outlives anchors and rebases); the route is the game's own: the
-- stock ground markers (core_groundMarkers.setPath: arrows on the road, the navigation app, the minimap's line)
-- on the AI road graph (navgraph.lua). That graph only covers the ground around the player (~1 km), so a far
-- destination is reached in steps: the route goes to the graph node nearest the destination, and is set again
-- whenever the graph moves on (every few hundred metres) or that node changes. Within ARRIVE_M: arrived, cleared.
local M = {}

M.CHECK_S = 1.0
M.ARRIVE_M = 40         -- this close (horizontally): arrived
M.FINAL_M = 60          -- a graph node this close to the destination: the route ends at the destination itself

local dest = nil        -- { lat, lon, name }
local routedTo = nil    -- the node id (or 'final') the current route goes to
local lastCheck = -math.huge
local info = nil        -- { dist, bearing } at the last check

-- Pure: the id and position {x, y, z} of the node in `nodes` (map.getMap().nodes: id -> { pos = {x, y, z} })
-- nearest (tx, ty), and its distance; nil when there are none.
function M.pickNode(nodes, tx, ty)
  local best, bd, bp = nil, math.huge, nil
  for id, n in pairs(nodes or {}) do
    local p = n.pos
    if p then
      local dx, dy = p.x - tx, p.y - ty
      local d = dx * dx + dy * dy
      if d < bd then best, bd, bp = id, d, p end
    end
  end
  if not best then return nil end
  return best, bp, math.sqrt(bd)
end

-- Pure: the compass bearing (deg, 0 = north) from (x0, y0) to (x1, y1) in the east / north frame.
function M.bearing(x0, y0, x1, y1)
  return (math.deg(math.atan2(x1 - x0, y1 - y0)) + 360) % 360
end

local function clearRoute()
  local gm = rawget(_G, 'core_groundMarkers')
  if gm and gm.setPath then pcall(gm.setPath, nil) end
  routedTo = nil
end

function M.setDestination(lat, lon, name)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon then return false end
  dest = { lat = lat, lon = lon, name = name }
  routedTo, lastCheck = nil, -math.huge     -- route at the next update
  return true
end

function M.clear()
  dest, info = nil, nil
  clearRoute()
end

function M.status()
  if not dest then return nil end
  return { lat = dest.lat, lon = dest.lon, name = dest.name, dist = info and info.dist, bearing = info and info.bearing }
end

-- Every frame (anchor: the current geodesy anchor; player: {x, y}): the route kept to the destination.
function M.update(anchor, player)
  if not dest or not anchor or not player then return end
  local now = os.clock()
  if now - lastCheck < M.CHECK_S then return end
  lastCheck = now
  local tx, ty = anchor:geodeticToEnu(dest.lat, dest.lon, 0)
  local dx, dy = tx - player.x, ty - player.y
  local dist = math.sqrt(dx * dx + dy * dy)
  info = { dist = dist, bearing = M.bearing(player.x, player.y, tx, ty) }
  if dist < M.ARRIVE_M then
    if ui_message then pcall(ui_message, 'Arrived: ' .. (dest.name or 'destination'), 5, 'beamngEarthGps') end
    M.clear()
    return
  end
  local m = rawget(_G, 'map') and map.getMap and map.getMap()
  local id, pos, nd = M.pickNode(m and m.nodes, tx, ty)
  if not id then return end
  local final = nd <= M.FINAL_M
  local key = final and 'final' or id
  if key == routedTo then return end
  local gm = rawget(_G, 'core_groundMarkers')
  if not gm or not gm.setPath then return end
  local target = final and vec3(tx, ty, pos.z) or vec3(pos.x, pos.y, pos.z)
  if pcall(gm.setPath, target, { clearPathOnReachingTarget = false }) then routedTo = key end
end

-- The level ended: the route went with it (the destination stays for the next level start).
function M.reset()
  routedTo, info, lastCheck = nil, nil, -math.huge
end

return M
