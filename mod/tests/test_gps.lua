-- luajit mod/tests/test_gps.lua  (the world map's GPS destination on the game's route)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local G = require('beamngEarth/gps')
local nodes = { a = { pos = { x = 0, y = 0, z = 1 } }, b = { pos = { x = 300, y = 0, z = 2 } }, c = { pos = { x = 0, y = 400, z = 3 } } }
local id, p, d = G.pickNode(nodes, 1000, 50)
T.check(id == 'b' and p.z == 2 and math.abs(d - math.sqrt(700 * 700 + 50 * 50)) < 1e-6, 'the node nearest a far destination')
T.check(G.pickNode({}, 0, 0) == nil, 'no graph: none')
T.check(math.abs(G.bearing(0, 0, 0, 10)) < 1e-9 and math.abs(G.bearing(0, 0, 10, 0) - 90) < 1e-9
  and math.abs(G.bearing(0, 0, -10, 0) - 270) < 1e-9, 'bearings: north 0, east 90, west 270')
T.check(G.setDestination(39.28, -76.59, 'x') and G.status().name == 'x', 'destination set')
T.check(not G.setDestination(nil, 1), 'a bad destination refused')
G.clear()
T.check(G.status() == nil, 'cleared')
local xs, ys = { 0, 100, 100 }, { 0, 0, 100 }
local si, st, sd = G.nearestSegment(xs, ys, 50, 10)
T.check(si == 1 and math.abs(st - 0.5) < 1e-9 and math.abs(sd - 10) < 1e-9, 'on the first segment, halfway, 10 m off')
si, st, sd = G.nearestSegment(xs, ys, 120, 60)
T.check(si == 2 and math.abs(st - 0.6) < 1e-9 and math.abs(sd - 20) < 1e-9, 'on the second segment')
local gnodes = { a = { pos = { x = 0, y = 0, z = 1 }, links = { b = {} } }, b = { pos = { x = 200, y = 0, z = 3 }, links = { a = {} } } }
local segs = G.edgeSegments(gnodes)
T.check(#segs == 1, 'one edge, once')
local ed, ez = G.nearestEdge(segs, 100, 10)
T.check(math.abs(ed - 10) < 1e-9 and ez == 2, 'a point beside the middle of an edge: on the graph')
local e = { x = { 0, 100, 100 }, y = { 0, 0, 100 }, cum = { 0, 100, 200 } }
local sl = G.routeSlice(e, 50, 160, 25)
T.check(#sl == 6 and sl[1][1] == 50 and sl[3][1] == 100 and sl[3][2] == 0 and sl[6][1] == 100 and sl[6][2] == 60,
  'the route sliced every 25 m round its corner, both ends included')
sl = G.routeSlice(e, 150, 5000, 25)
T.check(sl[#sl][2] == 100 and #sl == 3, 'a slice past the end stops at the destination')
local ids = G.pathNodes({ { pos = 1 }, { wp = 'a' }, { wp = 'a' }, { wp = 'b' }, { pos = 2, fixed = true }, { pos = 3 } })
T.check(#ids == 2 and ids[1] == 'a' and ids[2] == 'b', 'the AI path: the graph nodes of the planner path, in order, once')
local cmd = G.aiCommand({ 'a', 'b"c' })
T.check(cmd:find('driveUsingPath', 1, true) and cmd:find('"a","b\\"c"', 1, true) and cmd:find('driveInLane = "on"', 1, true),
  'the AI command lists the nodes quoted, in lane')
T.check(loadstring(cmd) ~= nil and loadstring(G.aiCommand(nil)) ~= nil, 'both AI commands are valid Lua')
T.check(G.aiCommand(nil) == 'ai.setMode("disabled")', 'autodrive off: the AI disabled')
-- the AI's path starts on the edge under the car (not at the first node past a turn ahead)
local g2 = { w = { pos = { x = 0, y = 0, z = 0 }, links = { j = {} } }, j = { pos = { x = 100, y = 0, z = 0 }, links = { w = {}, s = {} } },
             s = { pos = { x = 160, y = -60, z = 0 }, links = { j = {} } } }
local ea, eb = G.edgeUnder(g2, 40, 2, 1, 0)
T.check(ea == 'w' and eb == 'j', 'the car heading east on w-j')
T.check(G.edgeUnder(g2, 40, 20, 1, 0) == nil, 'nothing within 8 m: no edge')
local jn = G.joinStart({ 's' }, 'w', 'j', g2)
T.check(#jn == 3 and jn[1] == 'w' and jn[2] == 'j' and jn[3] == 's', 'the path goes through the junction, not across the corner')
jn = G.joinStart({ 'j', 's' }, 'w', 'j', g2)
T.check(#jn == 3 and jn[1] == 'w', 'the edge\'s start put in front')
jn = G.joinStart({ 'w', 'j', 's' }, 'w', 'j', g2)
T.check(#jn == 3, 'already starting there: unchanged')

-- the AI's path across a road-graph change: from the edge under the car, as far as every step is an edge in both graphs
do
  local function n(x, links) return { pos = { x = x, y = 0 }, links = links } end
  local old = { a = n(0, { b = 1 }), b = n(100, { a = 1, c = 1 }), c = n(200, { b = 1, d = 1 }), d = n(300, { c = 1 }) }
  local new = { a = n(0, { b = 1 }), b = n(100, { a = 1, c = 1 }), c = n(200, { b = 1 }), d = n(300, {}) }
  local ids = { 'a', 'b', 'c', 'd' }
  T.check(G.pathIndexAt(ids, old, 150, 3) == 2, 'the car on the second edge')
  local v = G.validFrom(ids, 2, { old, new })
  T.check(#v == 2 and v[1] == 'b' and v[2] == 'c', 'cut where the new graph has no edge')
  v = G.validFrom(ids, 1, { old })
  T.check(#v == 4, 'all valid in the old graph')
  T.check(#G.validFrom({ 'a', 'x' }, 1, { old }) == 1, 'an unknown node: cut')
end
T.done('test_gps')
