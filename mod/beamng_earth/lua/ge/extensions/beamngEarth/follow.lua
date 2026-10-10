-- Terrain-follow: the drivable TerrainBlock is re-centred under the player in the SAME anchor
-- frame (docs/V1_1_SPEC.md 1a, docs/V1_2_DESIGN.md 1.2, 2.3, 12). Pure math + the prepare/commit
-- rule, so it runs under plain luajit.
--
-- The block has N = 1024 samples at squareSize 2.0 m (lattice.lua). Its collidable area is
-- [cx-1024, cx+1022] x [cy-1024, cy+1022] (2046 m, probe P1); it sits on the 8 x 8 L0 far tiles of
-- [cx-1024, cx+1024]^2. Its centre is always a multiple of 256 m.

local lattice = require('beamngEarth/lattice')

local M = {}

local floor, ceil, abs, max = math.floor, math.ceil, math.abs, math.max

M.SIZE = lattice.N             -- samples per side
M.SQUARE_SIZE = lattice.SQUARE -- m
M.HALF = lattice.HALF          -- block SW corner = centre - HALF
M.GRID = lattice.GRID          -- block centres snap to this grid
M.PREPARE_DIST = 300           -- Chebyshev distance player -> centre that requests a new terrain
M.COMMIT_DIST = 450            -- ... that commits a ready one (once its uncovered strip is live)
M.HARD_COMMIT_DIST = 700       -- ... that commits a ready one regardless (still >= 320 m from the edge)
M.EDGE_MARGIN = 600            -- the new centre keeps the player at least this far from every edge
M.LOOKAHEAD_S = 4              -- target = snap256(player + velocity * LOOKAHEAD_S)
M.RETRY_S = 5                  -- wait after a terrain_error before asking again

-- Max |centre - player| per axis: 1020 - 600 = 420 m (design 1.2; the server's predict_centre uses
-- the same value). The collidable east/north edge is at +1022, so this keeps >= 602 m there.
M.REACH = 1020 - M.EDGE_MARGIN
local REACH = M.REACH

function M.snap(v) return floor(v / M.GRID + 0.5) * M.GRID end

function M.cheb(ax, ay, bx, by) return max(abs(ax - bx), abs(ay - by)) end

-- Snap one axis, then pull it back onto the grid inside [p - REACH, p + REACH].
-- The interval is 840 m wide, so it always holds a multiple of 256.
local function clampAxis(c, p)
  if c > p + REACH then
    c = floor((p + REACH) / M.GRID) * M.GRID
  elseif c < p - REACH then
    c = ceil((p - REACH) / M.GRID) * M.GRID
  end
  return c
end

-- Centre of the next block for a player at (px, py) moving at (vx, vy) m/s.
function M.target(px, py, vx, vy)
  local t = M.LOOKAHEAD_S
  return clampAxis(M.snap(px + (vx or 0) * t), px), clampAxis(M.snap(py + (vy or 0) * t), py)
end

-- Block SW corner for a centre (the TerrainBlock position).
function M.blockOrigin(cx, cy) return lattice.blockOrigin(cx, cy) end

-- The prepare/commit rule.
--   px, py, vx, vy: player position / velocity in the current anchor frame
--   cx, cy: centre of the committed block (nil before the first terrain)
--   pending: nil or { cx, cy, ready } (the terrain request in flight)
--   gate: optional function(ncx, ncy) -> true when every far tile that the move uncovers
--     (old block - new block) is live and the new block's area is split down to L0 (design 2.3
--     "commit gating", farfield.uncoverReady); nil = always true
-- Returns 'prepare' (+ new cx, cy), 'commit', 'discard' or nil. 'commit' is a request: the caller
-- runs it in a governor heavy slot (governor.lua), so it may be returned on several frames.
function M.decide(px, py, vx, vy, cx, cy, pending, now, retryAt, gate)
  if not cx then return nil end
  local d = M.cheb(px, py, cx, cy)
  if pending then
    local pd = M.cheb(px, py, pending.cx, pending.cy)
    -- past the commit distance a ready block is taken whenever it is better than the current one,
    -- even if it no longer keeps the full 600 m margin (late answer): the next request follows at once
    if pending.ready and pd < d and d > M.COMMIT_DIST then
      if d > M.HARD_COMMIT_DIST or not gate or gate(pending.cx, pending.cy) then return 'commit' end
      return nil -- better than the current block: wait for the uncovered strip's tiles, keep it
    end
    -- a prepared centre that would no longer keep the player 600 m inside its edges is useless
    if pd > REACH then return 'discard' end
    return nil
  end
  if d <= M.PREPARE_DIST or now < (retryAt or 0) then return nil end
  local tx, ty = M.target(px, py, vx, vy)
  if tx == cx and ty == cy then return nil end -- heading back towards the centre: nothing to gain
  return 'prepare', tx, ty
end

-- Distances for the UI: to the prepare threshold and to the commit threshold (<= 0 = passed).
function M.thresholds(px, py, cx, cy)
  if not cx then return nil end
  local d = M.cheb(px, py, cx, cy)
  return M.PREPARE_DIST - d, M.COMMIT_DIST - d, d
end

return M
