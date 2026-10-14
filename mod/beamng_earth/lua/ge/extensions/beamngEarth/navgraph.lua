-- The AI road graph (server/navgraph.py, --traffic on) handed to the game's own navgraph with
-- map.reset(customNodes) (docs/research/2026-10-10-navgraph-traffic-signals.md §2.4-2.8, the plan in §8): BeamNG's
-- traffic and AI then drive our streamed roads. The server sends a graph around the player every ~400 m; this
-- module clips it to the terrain block (the only collidable ground, minus EDGE_M) and applies it whenever a new
-- graph arrives or the block moved, at most every MIN_GAP_S.
--
-- The custom map path's pitfalls (research §2.7), each handled here: vehicles are told to fetch the new graph
-- (mapmgr.requestMap); the node table object stays the same one across resets (gameplay_traffic caches it); every
-- edge carries its speed limit and lanes; the road rules come from a file of ours (the level's info.json has none);
-- a reset by someone else (map.reset() with no graph rebuilds an empty one from our level) is undone next frame.
local M = {}

M.EDGE_M = 60           -- graph nodes this far inside the block's ground box only
M.MIN_GAP_S = 1.0
M.RULES_FILE = '/temp/beamngEarth/roadRules.json'
M.rules = { rightHandDrive = false, turnOnRed = true }    -- US; `rightHandDrive = true` means driving on the left
M.enabled = true

local graph = nil       -- the server's last graph {anchor, nodes, edges}
local applied = nil     -- {anchor, serial, box} of the graph in the game's map now
local dirty = false
local lastApply = -math.huge
local persistent = nil  -- the node table handed out once (research §2.7.2)
local ours = false
local foreign = false
local stats = { applies = 0, lastMs = 0, nodes = 0, edges = 0 }

-- Pure: point inside box {x0, y0, x1, y1} shrunk by m (no box: anywhere).
function M.inside(b, x, y, m)
  return not b or (x >= b[1] + m and x <= b[3] - m and y >= b[2] + m and y <= b[4] - m)
end

-- Pure: the lanes string of an edge (one char per lane, '+' along in -> out, '-' against; research §2.2), the
-- lanes against the direction first for right-hand traffic.
function M.lanes(out, back, leftHand)
  local plus, minus = string.rep('+', out), string.rep('-', back)
  return leftHand and (plus .. minus) or (minus .. plus)
end

-- Pure-ish (needs vec3): the stock map's node table of graph g clipped to box b: {name = {pos, radius,
-- links = {other = edge}}}, every edge shared by both ends (double-sided) with in/out nodes, positions and radii.
-- Returns nodes, node count, edge count.
function M.toNodes(g, b, edgeM, leftHand)
  local nodes, nn, ne = {}, 0, 0
  local keep = {}
  for name, n in pairs(g.nodes) do
    if M.inside(b, n[1], n[2], edgeM) then keep[name] = n end
  end
  local function node(name)
    local t = nodes[name]
    if not t then
      local n = keep[name]
      t = { pos = vec3(n[1], n[2], n[3]), radius = n[4], links = {} }
      nodes[name] = t
      nn = nn + 1
    end
    return t
  end
  for _, e in ipairs(g.edges) do
    local a, bb = e[1], e[2]
    if keep[a] and keep[bb] and a ~= bb then
      local A, B = node(a), node(bb)
      if not A.links[bb] then
        local edge = { drivability = e[7], oneWay = e[3] == 1, lanes = M.lanes(e[4], e[5], leftHand),
          speedLimit = e[6], inNode = a, outNode = bb, inPos = A.pos, outPos = B.pos,
          inRadius = e[8], outRadius = e[8] }
        A.links[bb] = edge
        B.links[a] = edge
        ne = ne + 1
      end
    end
  end
  return nodes, nn, ne
end

function M.onMessage(msg, currentAnchor)
  if not M.enabled or msg.anchorId ~= currentAnchor then return false end
  local ok, g = pcall(jsonDecode, readFile(msg.path) or '')
  if not ok or type(g) ~= 'table' or type(g.nodes) ~= 'table' or type(g.edges) ~= 'table' then return false end
  graph = { anchor = msg.anchorId, serial = msg.serial, nodes = g.nodes, edges = g.edges }
  dirty = true
  return true
end

local function apply(b)
  local nodes, nn, ne = M.toNodes(graph, b, M.EDGE_M, M.rules.rightHandDrive)
  writeFile(M.RULES_FILE, jsonEncode({ roadRules = M.rules }))
  local orig = path.getPathLevelInfo
  path.getPathLevelInfo = function() return M.RULES_FILE end
  local t0 = os.clock()
  ours = true
  local ok, err = pcall(map.reset, nodes)
  ours = false
  path.getPathLevelInfo = orig
  if not ok then
    log('E', 'beamngEarth', 'navgraph: map.reset: ' .. tostring(err))
    return false
  end
  local m = map.getMap()
  if persistent and persistent ~= m.nodes then
    table.clear(persistent)
    for k, v in pairs(m.nodes) do persistent[k] = v end
    m.nodes = persistent
  else
    persistent = m.nodes
  end
  be:queueAllObjectLua('mapmgr.requestMap()')
  stats.applies, stats.lastMs, stats.nodes, stats.edges = stats.applies + 1, (os.clock() - t0) * 1000, nn, ne
  return true
end

-- Every frame: a new graph, a moved block or a foreign reset applies the graph again.
function M.update(aid, b)
  if not M.enabled or not graph then return end
  if graph.anchor ~= aid then
    graph = nil
    return
  end
  if b and applied and (applied.box[1] ~= b[1] or applied.box[2] ~= b[2]) then dirty = true end
  if not (dirty or foreign) then return end
  local t = os.clock()
  if t - lastApply < M.MIN_GAP_S and not foreign then return end
  lastApply = t
  dirty, foreign = false, false
  if apply(b) then
    applied = { anchor = aid, serial = graph.serial, box = b and { b[1], b[2], b[3], b[4] } or {} }
  end
end

-- onNavgraphReloaded (core forwards the hook): someone else rebuilt the map; ours again next frame.
function M.onReloaded()
  if not ours and graph then foreign = true end
end

function M.status()
  return { graph = graph and graph.serial or nil, applies = stats.applies, lastMs = stats.lastMs,
           nodes = stats.nodes, edges = stats.edges }
end

function M.reset()
  graph, applied, dirty, lastApply, persistent, ours, foreign = nil, nil, false, -math.huge, nil, false, false
  stats = { applies = 0, lastMs = 0, nodes = 0, edges = 0 }
end

return M
