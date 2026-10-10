-- luajit mod/tests/test_rebase.lua  (the pure origin-rebase prefetch/commit rule, V1_1_SPEC 1b)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local rebase = require('beamngEarth/rebase')
local d = rebase.decide

T.check(rebase.distance == 15000, 'default rebase distance 15 km')
rebase.setDistance(10000) -- the rules below are written for 10 km
T.check(d(9699, nil, nil, 0, 0) == nil, 'no prefetch below distance - 300 m')
T.check(d(9700, nil, nil, 0, 0) == 'prefetch', 'prefetch at distance - 300 m')
T.check(d(9800, nil, nil, 3, 5) == nil, 'no prefetch before retryAt')
T.check(d(9800, nil, nil, 5, 5) == 'prefetch', 'prefetch after retryAt')
T.check(d(9900, { purpose = 'rebase' }, nil, 0, 0) == nil, 'request in flight: wait')
T.check(d(10300, { purpose = 'rebase' }, nil, 0, 0) == nil, 'not ready past the distance: wait')
T.check(d(9900, { purpose = 'rebase', ready = true }, 200, 0, 0) == nil, 'ready below the distance: hold')
T.check(d(10000, { purpose = 'rebase', ready = true }, 300, 0, 0) == 'commit', 'ready at the distance: commit')
T.check(d(11000, { purpose = 'rebase', ready = true }, 400, 0, 0) == 'commit', 'ready late: commit at once')
T.check(d(10050, { purpose = 'rebase', ready = true }, 451, 0, 0) == 'discard', 'prepared terrain no longer under the player: discard')
T.check(d(9710, { purpose = 'rebase', ready = true }, 502, 0, 0) == nil,
  'ready below the distance: hold, though centred for the player 300 m ahead (no re-request loop)')
T.check(d(9800, { purpose = 'rebase', ready = true }, 900, 0, 0) == nil, 'player went elsewhere: judged at the distance')
T.check(d(10000, { purpose = 'rebase', ready = true }, 900, 0, 0) == 'discard', 'player went elsewhere: discard at the distance')
T.check(d(50000, { purpose = 'teleport', ready = true }, 0, 0, 0) == nil, 'teleport is not distance-driven')

-- configurable 2-16 km (console beamngEarth_core.setRebaseDistance / UI slider); above 16384 m
-- the float32 position step doubles
T.check(rebase.setDistance(500) == 2000, 'clamped to 2 km')
T.check(rebase.setDistance(99999) == 16000, 'clamped to 16 km')
T.check(rebase.setDistance('abc') == 16000, 'invalid input keeps the value')
rebase.setDistance(4000)
T.check(d(3699, nil, nil, 0, 0) == nil and d(3700, nil, nil, 0, 0) == 'prefetch', 'prefetch follows the distance')
T.check(d(4000, { purpose = 'rebase', ready = true }, 0, 0, 0) == 'commit', 'commit follows the distance')
rebase.setDistance(10000)

T.done('test_rebase')
