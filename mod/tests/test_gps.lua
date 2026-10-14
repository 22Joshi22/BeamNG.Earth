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
T.done('test_gps')
