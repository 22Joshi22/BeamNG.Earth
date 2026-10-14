-- GPS to a place picked on the world map (the user, 2026-10-14: "get the game's GPS system to work with this", then
-- "have the server plan out the whole route so it can work with much longer distances"). The destination is kept
-- as latitude / longitude (it outlives anchors and rebases). The server plans the whole road route (routing.py:
-- OSRM on OpenStreetMap); the map draws it, and the game's own route arrows (core_groundMarkers.setPath: arrows on
-- the road, the navigation app, the minimap's line) follow it on our AI road graph (navgraph.lua), which only
-- covers ~1 km around the player: the arrows go to the farthest point of the planned route ahead (up to LOOKAHEAD_M)
-- that the local graph reaches, set again as the player advances or the graph moves on. Leaving the route by more
-- than OFF_ROUTE_M plans it again from where the player is. Without a planned route (offline): steered toward the
-- destination through the graph node nearest it. Within ARRIVE_M: arrived, cleared.
local M = {}

M.CHECK_S = 1.0
M.ARRIVE_M = 40         -- this close (horizontally): arrived
M.FINAL_M = 60          -- a graph node this close to the destination: the arrows end at the destination itself
M.ON_GRAPH_M = 25       -- a route point this close to a graph node is reachable on the local graph
M.LOOKAHEAD_M = 1500    -- how far along the route the arrows may reach
M.SAMPLE_M = 40         -- route points tried for the arrows' target at least this far apart
M.RETARGET_M = 150      -- the arrows' target is moved on in steps of this much route
M.OFF_ROUTE_M = 120     -- farther from the route: planned again ...
M.REROUTE_S = 20        -- ... at most this often

local dest = nil        -- { lat, lon, name }
local route = nil       -- { points = {{lat, lon}...}, dist, dur, version } | false (no route: steering) | nil (none yet)
local routeVersion = 0
local routeError = nil
local pendingId, nextId = nil, 0
local lastRequest = -math.huge
local enu = nil         -- { anchor, x = {}, y = {}, cum = {} } of the route in the current anchor's frame
local progress = 1      -- index of the route segment the player is on
local routedTo = nil    -- the node id (or 'final') the current arrows go to
local lastCheck = -math.huge
local info = nil        -- { dist, bearing, remaining, eta } at the last check

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

-- Pure: the segments {ax, ay, bx, by, z} of the graph's edges (each once).
function M.edgeSegments(nodes)
  local segs, seen = {}, {}
  for id, n in pairs(nodes or {}) do
    local a = n.pos
    for other, _ in pairs(n.links or {}) do
      local key = tostring(id) < tostring(other) and (tostring(id) .. '|' .. tostring(other)) or (tostring(other) .. '|' .. tostring(id))
      local o = nodes[other]
      if a and o and o.pos and not seen[key] then
        seen[key] = true
        segs[#segs + 1] = { a.x, a.y, o.pos.x, o.pos.y, (a.z + o.pos.z) / 2 }
      end
    end
  end
  return segs
end

-- Pure: the distance from (px, py) to the nearest segment of segs, and that segment's z.
function M.nearestEdge(segs, px, py)
  local bd, bz = math.huge, 0
  for _, s in ipairs(segs) do
    local ax, ay, bx, by = s[1], s[2], s[3], s[4]
    local dx, dy = bx - ax, by - ay
    local L2 = dx * dx + dy * dy
    local t = L2 > 0 and math.max(0, math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) or 0
    local qx, qy = ax + t * dx - px, ay + t * dy - py
    local d = qx * qx + qy * qy
    if d < bd then bd, bz = d, s[5] end
  end
  return math.sqrt(bd), bz
end

-- Pure: the compass bearing (deg, 0 = north) from (x0, y0) to (x1, y1) in the east / north frame.
function M.bearing(x0, y0, x1, y1)
  return (math.deg(math.atan2(x1 - x0, y1 - y0)) + 360) % 360
end

-- Pure: the segment of polyline (xs, ys) nearest (px, py), searched over indices [i0, i1): its index i (segment
-- i -> i + 1), the fraction t along it and the distance.
function M.nearestSegment(xs, ys, px, py, i0, i1)
  local bi, bt, bd = 1, 0, math.huge
  for i = math.max(1, i0 or 1), math.min(#xs - 1, (i1 or #xs) - 1) do
    local ax, ay, bx, by = xs[i], ys[i], xs[i + 1], ys[i + 1]
    local dx, dy = bx - ax, by - ay
    local L2 = dx * dx + dy * dy
    local t = L2 > 0 and math.max(0, math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) or 0
    local qx, qy = ax + t * dx - px, ay + t * dy - py
    local d = qx * qx + qy * qy
    if d < bd then bi, bt, bd = i, t, d end
  end
  return bi, bt, math.sqrt(bd)
end

local function clearArrows()
  local gm = rawget(_G, 'core_groundMarkers')
  if gm and gm.setPath then pcall(gm.setPath, nil) end
  routedTo = nil
end

local function core() return rawget(_G, 'beamngEarth_core') end

local function requestRoute(player)
  local c = core()
  if not c or not c.requestRoute or not dest or not player or not player.lat then return end
  nextId = nextId + 1
  if c.requestRoute(nextId, player.lat, player.lon, dest.lat, dest.lon) then
    pendingId, lastRequest = nextId, os.clock()
  end
end

function M.setDestination(lat, lon, name)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon then return false end
  dest = { lat = lat, lon = lon, name = name }
  route, routeError, pendingId, enu, progress, info = nil, nil, nil, nil, 1, nil
  routedTo, lastCheck, lastRequest = nil, -math.huge, -math.huge     -- plan and route at the next update
  return true
end

function M.clear()
  dest, route, routeError, pendingId, enu, info = nil, nil, nil, nil, nil, nil
  clearArrows()
end

function M.status()
  if not dest then return nil end
  return { lat = dest.lat, lon = dest.lon, name = dest.name, dist = info and info.dist, bearing = info and info.bearing,
           remaining = info and info.remaining, eta = info and info.eta, routeVersion = route and route.version or 0,
           progress = progress, planning = pendingId ~= nil, routeError = routeError, pendingId = pendingId }
end

-- The planned route for the map: { version, points = {{lat, lon}...}, dist, dur } or nil.
function M.route()
  if not route then return nil end
  return { version = route.version, points = route.points, dist = route.dist, dur = route.dur }
end

local function toEnu(anchor)
  if enu and enu.anchor == anchor then return enu end
  local xs, ys, cum = {}, {}, { 0 }
  for i, p in ipairs(route.points) do
    xs[i], ys[i] = anchor:geodeticToEnu(p[1], p[2], 0)
    if i > 1 then cum[i] = cum[i - 1] + math.sqrt((xs[i] - xs[i - 1]) ^ 2 + (ys[i] - ys[i - 1]) ^ 2) end
  end
  enu = { anchor = anchor, x = xs, y = ys, cum = cum }
  progress = 1
  return enu
end

-- The arrows' target on the planned route: the farthest sampled route point within LOOKAHEAD_M that lies on the local
-- graph (within ON_GRAPH_M of one of its edges); the stock route planner snaps it to the road.
local segCache = { nodes = nil, segs = {} }
local function routeTarget(e, nodes, i, t)
  if segCache.nodes ~= nodes then segCache = { nodes = nodes, segs = M.edgeSegments(nodes) } end
  local segs = segCache.segs
  local n = #e.x
  local s0 = e.cum[i] + t * (e.cum[i + 1] - e.cum[i])
  local best = nil
  -- sample along the route every SAMPLE_M from the player's spot (the simplified route's vertices can be far apart)
  local s = s0
  local k = i
  while s - s0 <= M.LOOKAHEAD_M do
    s = math.min(s + M.SAMPLE_M, e.cum[n])
    while k < n - 1 and e.cum[k + 1] < s do k = k + 1 end
    local seg = e.cum[k + 1] - e.cum[k]
    local f = seg > 0 and (s - e.cum[k]) / seg or 0
    local x = e.x[k] + f * (e.x[k + 1] - e.x[k])
    local y = e.y[k] + f * (e.y[k + 1] - e.y[k])
    local d, z = M.nearestEdge(segs, x, y)
    local final = s >= e.cum[n] - 1e-6
    if d <= M.ON_GRAPH_M then best = { s = s, x = x, y = y, z = z, final = final } end
    if final then break end
  end
  return best
end

local function setArrows(key, target)
  if key == routedTo then return end
  local gm = rawget(_G, 'core_groundMarkers')
  if not gm or not gm.setPath then return end
  if pcall(gm.setPath, target, { clearPathOnReachingTarget = false }) then routedTo = key end
end

-- Every frame (anchor: the current geodesy anchor; player: {x, y, lat, lon}): the route kept to the destination.
function M.update(anchor, player)
  if not dest or not anchor or not player then return end
  local now = os.clock()
  if now - lastCheck < M.CHECK_S then return end
  lastCheck = now
  -- the planned route: asked once (and on re-routes), taken when it arrives
  if pendingId then
    local c = core()
    local r = c and c.takeRoute and c.takeRoute(pendingId)
    if r then
      pendingId = nil
      if r.points and #r.points >= 2 and (tonumber(r.dist) or 0) >= 1 then
        routeVersion = routeVersion + 1
        route = { points = r.points, dist = r.dist, dur = r.dur, version = routeVersion }
        routeError, enu, routedTo = nil, nil, nil
      else
        routeError = r.error or 'no route'
        if not route then route = false end
      end
    elseif now - lastRequest > 30 then
      pendingId = nil                      -- no answer: try again below
    end
  elseif route == nil then
    requestRoute(player)
  end

  local tx, ty = anchor:geodeticToEnu(dest.lat, dest.lon, 0)
  local dx, dy = tx - player.x, ty - player.y
  local dist = math.sqrt(dx * dx + dy * dy)
  info = { dist = dist, bearing = M.bearing(player.x, player.y, tx, ty), remaining = dist }
  if dist < M.ARRIVE_M then
    if ui_message then pcall(ui_message, 'Arrived: ' .. (dest.name or 'destination'), 5, 'beamngEarthGps') end
    M.clear()
    return
  end
  local m = rawget(_G, 'map') and map.getMap and map.getMap()
  local nodes = m and m.nodes

  if route then
    local e = toEnu(anchor)
    local n = #e.x
    local i, t, off = M.nearestSegment(e.x, e.y, player.x, player.y, progress - 5, progress + 400)
    if off > M.OFF_ROUTE_M then i, t, off = M.nearestSegment(e.x, e.y, player.x, player.y, 1, n) end
    progress = i
    local remaining = e.cum[n] - (e.cum[i] + t * (e.cum[i + 1] - e.cum[i]))
    info.remaining = remaining + off
    if route.dist and route.dist > 0 and route.dur then info.eta = route.dur * remaining / route.dist end
    if off > M.OFF_ROUTE_M and not pendingId and now - lastRequest > M.REROUTE_S then requestRoute(player) end
    local tg = nodes and routeTarget(e, nodes, i, t)
    if tg then
      -- set again only when the target moved on by a good stretch (each setPath re-plans the arrows)
      local key = tg.final and 'final' or ('r' .. math.floor(tg.s / M.RETARGET_M))
      setArrows(key, vec3(tg.x, tg.y, tg.z))
      return
    end
  end
  -- no planned route (yet, or offline), or none of it on the local graph: toward the destination
  local id, pos, nd = M.pickNode(nodes, tx, ty)
  if not id then return end
  local final = nd <= M.FINAL_M
  setArrows(final and 'final' or id, final and vec3(tx, ty, pos.z) or vec3(pos.x, pos.y, pos.z))
end

-- The level ended: the arrows went with it (the destination and its route stay for the next level start).
function M.reset()
  routedTo, info, lastCheck, enu, progress = nil, nil, -math.huge, nil, 1
end

return M
