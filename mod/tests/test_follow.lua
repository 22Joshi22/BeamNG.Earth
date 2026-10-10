-- luajit mod/tests/test_follow.lua  (terrain-follow math and rule, V1_1_SPEC 1a, V1_2_DESIGN 1.2, 2.3)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local f = require('beamngEarth/follow')

-- block geometry: 1024 samples at exactly 2.0 m, collidable width (N-1)*s = 2046 m (P1)
T.check(f.SQUARE_SIZE == 2.0, 'squareSize = 2.0 (world-locked lattice)')
T.check((f.SIZE - 1) * f.SQUARE_SIZE == 2046, 'collidable width (N-1)*s = 2046 m')
T.check(f.REACH == 420, 'REACH = 1020 - 600 = 420 m (same as the server predict_centre)')
local x0, y0 = f.blockOrigin(512, -256)
T.check(x0 == -512 and y0 == -1280, 'block position = (cx-1024, cy-1024)')

-- snapping
T.check(f.snap(127) == 0 and f.snap(128) == 256 and f.snap(-128) == 0 and f.snap(-129) == -256, 'snap256')

-- target = snap256(player + v*4 s)
local tx, ty = f.target(300, 0, 0, 0)
T.check(tx == 256 and ty == 0, 'target at rest')
tx, ty = f.target(300, -50, 30, 0) -- 420 m ahead
T.check(tx == 512 and ty == 0, 'target leads by 4 s')
-- clamped so the player stays >= 600 m from every edge: |c - p| <= 420 per axis
-- (the collidable edges are at c - 1024 and c + 1022)
for _, c in ipairs({ { 0, 0, 200, 0 }, { 1000, 1000, 150, -150 }, { -333, 777, -90, 60 }, { 5, 5, 0, 400 },
  { 164, -164, 300, -300 }, { 676, 0, 500, 0 }, { -420.5, 0, -100, 0 } }) do
  local cx, cy = f.target(c[1], c[2], c[3], c[4])
  T.check(cx % 256 == 0 and cy % 256 == 0, 'target on the 256 m grid')
  T.check(math.abs(cx - c[1]) <= 420 and math.abs(cy - c[2]) <= 420,
    string.format('player (%g,%g) v=(%g,%g) >= 600 m inside block (%d,%d)', c[1], c[2], c[3], c[4], cx, cy))
  T.check(c[1] - (cx - 1024) >= 600 and (cx + 1022) - c[1] >= 600, 'x: >= 600 m from both collidable edges')
end
tx = f.target(0, 0, 200, 0) -- 800 m ahead -> clamped to 256 (<= 420)
T.check(tx == 256, 'fast player: clamped lead (' .. tx .. ')')
tx = f.target(0, 0, -200, 0)
T.check(tx == -256, 'fast player backwards: clamped lead')

-- rule
local d = f.decide
T.check(d(0, 0, 0, 0, nil, nil, nil, 0, 0) == nil, 'no block yet: nothing')
T.check(d(300, 0, 20, 0, 0, 0, nil, 0, 0) == nil, 'at 300 m: no request yet')
local a, cx, cy = d(301, 0, 20, 0, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 256 and cy == 0, 'past 300 m: prepare snap256(p + 4v)')
a, cx, cy = d(-10, 301, 0, 25, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 0 and cy == 512, 'Chebyshev distance, north')
T.check(d(301, 0, 0, 0, 0, 0, nil, 1, 5) == nil, 'prepare waits for retryAt')
T.check(d(310, 0, -60, 0, 0, 0, nil, 0, 0) == nil, 'heading back: target = current centre, no request')
local pend = { cx = 512, cy = 0 }
T.check(d(460, 0, 20, 0, 0, 0, pend, 0, 0) == nil, 'not ready: keep driving on the current block')
pend.ready = true
T.check(d(440, 0, 20, 0, 0, 0, pend, 0, 0) == nil, 'ready at 440 m: hold')
T.check(d(451, 0, 20, 0, 0, 0, pend, 0, 0) == 'commit', 'ready past 450 m: commit')
T.check(d(700, 0, 20, 0, 0, 0, pend, 0, 0) == 'commit', 'ready late: commit at once')
T.check(d(451, 600, 0, 20, 0, 0, pend, 0, 0) == 'discard', 'player left the prepared block: discard')
-- late answer: the player is past the prepared centre by more than 420 m but it is still nearer than
-- the current one (about to run off the block): take it rather than discard it
T.check(d(940, 0, 40, 0, 0, 0, pend, 0, 0) == 'commit', 'late ready block nearer than the current one: commit')
pend.ready = false
T.check(d(940, 0, 40, 0, 0, 0, pend, 0, 0) == 'discard', 'late and not ready: discard')
pend.ready = true
T.check(d(100, 600, 0, 20, 0, 0, pend, 0, 0) == 'discard', 'ready but not past 450 m and off the prepared block: discard')
-- commit gating (design 2.3): past 450 m only once the strip the move uncovers is live;
-- hard override past 700 m; a gated ready block is kept, not discarded
local gateArgs
local closed = function(ncx, ncy) gateArgs = { ncx, ncy }; return false end
local open = function() return true end
pend = { cx = 512, cy = 0, ready = true }
T.check(d(451, 0, 20, 0, 0, 0, pend, 0, 0, open) == 'commit', 'gate open: commit past 450 m')
T.check(d(451, 0, 20, 0, 0, 0, pend, 0, 0, closed) == nil and gateArgs[1] == 512 and gateArgs[2] == 0,
  'gate closed: hold (gate asked about the pending centre)')
T.check(d(699, 0, 20, 0, 0, 0, pend, 0, 0, closed) == nil, 'gate closed at 699 m: hold')
T.check(d(701, 0, 20, 0, 0, 0, pend, 0, 0, closed) == 'commit', 'hard override past 700 m')
T.check(d(940, 0, 40, 0, 0, 0, pend, 0, 0, closed) == 'commit', 'late and gated: hard override')
T.check(d(440, 0, 20, 0, 0, 0, pend, 0, 0, function() error('gate must not be asked below 450 m') end) == nil,
  'gate not evaluated below the commit distance')
local p1, p2 = f.thresholds(350, -20, 0, 0)
T.check(p1 == -50 and p2 == 100, 'UI thresholds')

T.done('test_follow')
