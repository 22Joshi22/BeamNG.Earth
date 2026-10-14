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
T.done('test_gps')
