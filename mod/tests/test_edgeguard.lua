-- luajit mod/tests/test_edgeguard.lua  (never fall out of the world, edgeguard.lua; docs/ROADMAP.md "Robustness" 2)
-- Driven like the game with stubs: a vehicle whose velocity the guard sets (applyClusterVelocityScaleAdd
-- with scale 0) and a ground ray (castRayStatic) over a flat block. Checks: a car driving at an edge is
-- warned, then braked at no more than DECEL and comes to rest STOP_M inside, never past the edge, at
-- 30 and 134 m/s and diagonally into a corner; along the edge and back inwards it drives freely; far
-- inside nothing is called (no ray); a jump or an aircraft over the block is never touched and an
-- aircraft flies off the block untouched; a car that went off the edge and dropped is set down inside
-- once (safeTeleport without a reset, velocity zeroed), rate-limited; the ground ray's miss.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local edgeguard = require('beamngEarth/edgeguard')
local lattice = require('beamngEarth/lattice')

lattice.configure(512)
local BOX = { lattice.groundBox(0, 0) } -- [-512, 510]
T.check(BOX[1] == -512 and BOX[3] == 510, '512 block: collidable [c-512, c+510]')
local GROUND_Z = 100

local rays = 0
function castRayStatic(from, dir_, len)
  rays = rays + 1
  local inside = from.x >= BOX[1] and from.x <= BOX[3] and from.y >= BOX[2] and from.y <= BOX[4]
  if not inside then return len + 1 end -- off the block: nothing below (a miss returns >= the length)
  local d = from.z - GROUND_Z
  if d < 0 or d > len then return len + 1 end
  return d
end
function quatFromDir(d) return { d = d } end
local teleports = {}
spawn = { safeTeleport = function(v, pos, rot, a, b, c, d, reset)
  teleports[#teleports + 1] = { pos = vec3(pos), reset = reset, rot = rot }
  v.pos = vec3(pos.x, pos.y, GROUND_Z + 0.5)
end }

local function newVeh(x, y, z, vx, vy)
  local v = { pos = vec3(x, y, z), vel = vec3(vx, vy, 0), sets = 0 }
  function v:getPosition() return vec3(self.pos) end
  function v:getVelocity() return vec3(self.vel) end
  function v:getDirectionVector() return vec3(1, 0, 0) end
  function v:applyClusterVelocityScaleAdd(node, scale, ax, ay, az)
    self.sets = self.sets + 1
    self.lastNode, self.lastScale = node, scale
    self.vel = vec3(self.vel.x * scale + ax, self.vel.y * scale + ay, self.vel.z * scale + az)
  end
  return v
end

-- a drive: the car keeps its velocity (the driver holds the throttle), physics integrates
local function drive(veh, frames, dt, onFrame)
  local g = edgeguard.new()
  local clock, out = 0, { modes = {}, maxDecel = 0, minEdge = math.huge, firstWarn = nil, firstHold = nil }
  for f = 1, frames do
    clock = clock + dt
    local before = vec3(veh.vel)
    local mode = edgeguard.update(g, veh, BOX, clock)
    out.modes[mode] = (out.modes[mode] or 0) + 1
    if mode == 'warn' and not out.firstWarn then out.firstWarn = { f = f, x = veh.pos.x, y = veh.pos.y } end
    if mode == 'hold' and not out.firstHold then out.firstHold = { f = f, x = veh.pos.x } end
    -- (the last 1 m/s to rest brakes up to ~8 m/s^2: the profile is sampled once a frame; not counted)
    local dv = (before - veh.vel):length() / dt
    if dv > out.maxDecel and f > 1 and before:length() > 1 then out.maxDecel = dv end
    veh.pos = veh.pos + vec3(veh.vel.x * dt, veh.vel.y * dt, veh.vel.z * dt)
    local p = veh.pos
    out.minEdge = math.min(out.minEdge, p.x - BOX[1], BOX[3] - p.x, p.y - BOX[2], BOX[4] - p.y)
    if onFrame then onFrame(f, g, mode) end
  end
  out.g = g
  return out
end

------------------------------------------------------------------------------------------
-- the soft wall

T.near(edgeguard.cap(edgeguard.STOP_M), 0, 1e-12, 'cap: zero at the stop line')
T.near(edgeguard.cap(edgeguard.STOP_M + 75), 30, 1e-9, 'cap: 30 m/s at 75 m before it (6 m/s^2)')
T.check(edgeguard.cap(edgeguard.STOP_M - 4) < 0 and edgeguard.cap(0) == -edgeguard.PULL_MAX, 'cap: inside the stop line a pull back, at most PULL_MAX')
-- creeping under throttle (in-game 2026-10-11: 4.6 m in 50 s): inside the stop line even a slow
-- outward speed is replaced by a pull back
do
  local g = edgeguard.new()
  local box = { -512, -512, 510, 510 }
  local x = 510 - edgeguard.STOP_M + 3            -- 3 m past the stop line
  local mode, setV = edgeguard.step(g, { clock = 1, x = x, y = 0, z = 100, vx = 0.04, vy = 1, box = box, ground = 0.5 })
  T.check(mode == 'hold' and setV and setV[1] < -1 and setV[2] == 1, 'creep: pulled back inwards, along the edge free')
  -- back at the stop line moving inwards: nothing to do
  mode, setV = edgeguard.step(g, { clock = 2, x = 510 - edgeguard.STOP_M - 0.5, y = 0, z = 100, vx = -0.3, vy = 0, box = box, ground = 0.5 })
  T.check(setV == nil, 'at the stop line moving inwards: left alone')
  -- simulate a car pushing outwards at 3 m/s^2 for 60 s at 60 GE frames/s: it stays inside the stop zone
  local px, vx2 = 400, 8
  for i = 1, 3600 do
    vx2 = vx2 + 3 / 60
    local m2, sv = edgeguard.step(g, { clock = 2 + i / 60, x = px, y = 0, z = 100, vx = vx2, vy = 0, box = box, ground = 0.5 })
    if sv then vx2 = sv[1] end
    px = px + vx2 / 60
  end
  T.check(px < 510 - edgeguard.STOP_M + 1, 'sustained throttle for 60 s: held at the stop line (x ' .. string.format('%.2f', px) .. ')')
end
T.check(edgeguard.warnDistance(0) == edgeguard.WARN_M and edgeguard.warnDistance(60) > 300, 'warn distance grows with speed')

local veh = newVeh(0, 0, GROUND_Z + 0.5, 30, 0)
local r = drive(veh, 60 * 40, 1 / 60)
T.check(r.firstWarn and r.firstWarn.x < BOX[3] - edgeguard.WARN_M + 1, '30 m/s east: warned >= 150 m before the edge')
T.check(r.firstHold and r.firstHold.x > r.firstWarn.x, '... then braked')
T.check(r.maxDecel <= edgeguard.DECEL * 1.1, string.format('braking at most DECEL (%.2f m/s^2)', r.maxDecel))
T.check(r.minEdge >= edgeguard.STOP_M - 0.5, string.format('never past the stop line (%.2f m inside)', r.minEdge))
T.check(math.abs(veh.vel.x) < 0.2 and BOX[3] - veh.pos.x < edgeguard.STOP_M + 1, 'at rest at the wall')
T.check(veh.lastNode == -1 and veh.lastScale == 0, 'the velocity is set (scale 0, every cluster), not added to')
-- along the edge and back inwards: free
veh.vel = vec3(0, 20, 0)
local sets = veh.sets
r = drive(veh, 30, 1 / 60)
T.check(veh.sets == sets and math.abs(veh.vel.y - 20) < 1e-9, 'along the edge: untouched')
veh.vel = vec3(-15, 0, 0)
sets = veh.sets
drive(veh, 30, 1 / 60)
T.check(veh.sets == sets and veh.vel.x == -15, 'back inwards: untouched')

-- 134 m/s: braked hard at first (the block is too small for 6 m/s^2 from that speed), never off it
veh = newVeh(-400, 0, GROUND_Z + 0.5, 134, 0)
r = drive(veh, 60 * 20, 1 / 60)
T.check(r.minEdge >= edgeguard.STOP_M - 1, string.format('134 m/s: stays on the block (%.2f m inside)', r.minEdge))
T.check(math.abs(veh.vel.x) < 0.5, '134 m/s: stopped')
-- diagonally into the NE corner
veh = newVeh(100, 100, GROUND_Z + 0.5, 25, 25)
r = drive(veh, 60 * 30, 1 / 60)
T.check(r.minEdge >= edgeguard.STOP_M - 0.5 and BOX[3] - veh.pos.x < 17 and BOX[4] - veh.pos.y < 17, 'corner: held on both axes')
-- the velocity reading lags one frame (set twice with the same target): nothing more happens
do
  local g = edgeguard.new()
  local v2 = newVeh(BOX[3] - edgeguard.STOP_M - 20, 0, GROUND_Z + 0.5, 30, 0)
  edgeguard.update(g, v2, BOX, 1)
  local after = v2.vel.x
  v2.vel = vec3(30, 0, 0) -- the stale reading
  edgeguard.update(g, v2, BOX, 1.016)
  T.near(v2.vel.x, after, 1e-9, 'a stale velocity reading: the same target again, no double braking')
end

------------------------------------------------------------------------------------------
-- far inside: nothing called

rays = 0
veh = newVeh(0, 0, GROUND_Z + 0.5, 5, 0)
sets = veh.sets
local g = edgeguard.new()
T.check(edgeguard.update(g, veh, BOX, 1) == 'clear' and rays == 0 and veh.sets == sets, 'far inside: clear, no ray, no call')

------------------------------------------------------------------------------------------
-- off the ground: never touched

veh = newVeh(BOX[3] - 30, 0, GROUND_Z + 12, 30, 0) -- a jump 12 m above the ground near the edge
g = edgeguard.new()
T.check(edgeguard.update(g, veh, BOX, 1) == 'air' and veh.sets == 0, 'airborne over the block: untouched')
-- an aircraft at 500 m flies off the block: never stopped, never rescued
veh = newVeh(0, 0, GROUND_Z + 500, 80, 0)
teleports = {}
r = drive(veh, 60 * 20, 1 / 60)
T.check(veh.sets == 0 and #teleports == 0 and veh.pos.x > BOX[3] + 500, 'an aircraft flies off the block untouched')
-- a low aircraft that took off near the edge and climbs off the block: above its ground, never rescued
veh = newVeh(BOX[3] - 200, 0, GROUND_Z + 0.5, 70, 0)
teleports = {}
r = drive(veh, 60 * 10, 1 / 60, function(f, gg, mode)
  if f == 5 then veh.vel = vec3(70, 0, 8) end -- lift off at once
end)
T.check(#teleports == 0, 'a climbing aircraft leaving the block: no rescue')

------------------------------------------------------------------------------------------
-- rescue: the car went off the edge (the server died with the car already there) and falls

teleports = {}
g = edgeguard.new()
veh = newVeh(BOX[3] - 3, 50, GROUND_Z + 0.5, 0, 0)
edgeguard.update(g, veh, BOX, 1) -- on the ground near the edge: its ground recorded
T.check(g.lastGroundZ and math.abs(g.lastGroundZ - GROUND_Z) < 1e-9, 'the ground under the car is recorded near the edge')
veh.pos = vec3(BOX[3] + 4, 50, GROUND_Z - 1)
veh.vel = vec3(10, 0, -5)
T.check(edgeguard.update(g, veh, BOX, 1.1) == 'air' and #teleports == 0, 'off the edge, not yet below its ground: nothing yet')
veh.pos = vec3(BOX[3] + 6, 50, GROUND_Z - 4)
T.check(edgeguard.update(g, veh, BOX, 1.2) == 'hold' and #teleports == 1, 'dropped below its ground: rescued')
local tp = teleports[1]
T.near(tp.pos.x, BOX[3] - edgeguard.STOP_M, 1e-9, 'set down STOP_M inside the edge')
T.near(tp.pos.y, 50, 1e-9, '... at the same place along it')
T.check(tp.pos.z == GROUND_Z + 1 and tp.reset == false, '... above its last ground, without a reset (damage kept)')
T.check(veh.vel:length() == 0, '... and stopped')
veh.pos = vec3(BOX[3] + 8, 50, GROUND_Z - 8)
edgeguard.update(g, veh, BOX, 1.5)
T.check(#teleports == 1, 'rescues are rate-limited (one per RESCUE_GAP_S)')
edgeguard.update(g, veh, BOX, 2.3)
T.check(#teleports == 2, '... and repeated after it')
veh.pos = vec3(BOX[3] + 8, 50, GROUND_Z - 8)
edgeguard.update(g, veh, BOX, 1.2 + edgeguard.RESCUE_WINDOW_S + 5)
T.check(#teleports == 2, 'long after it left the ground: not rescued (not ours to catch)')
-- a new world frame (core: an anchor committed): the remembered ground height is the old frame's
g = edgeguard.new()
veh = newVeh(BOX[3] - 3, 50, GROUND_Z + 0.5, 0, 0)
edgeguard.update(g, veh, BOX, 30)
edgeguard.newFrame(g)
veh.pos = vec3(BOX[3] + 6, 50, GROUND_Z - 40) -- e.g. the new anchor's origin 40 m higher, off the block
T.check(edgeguard.update(g, veh, BOX, 30.5) == 'air' and #teleports == 2,
  'after a frame change: no rescue from the old frame\'s ground height')

------------------------------------------------------------------------------------------
-- the ground ray

castRayStatic = nil
T.check(edgeguard.groundBelow(vec3(0, 0, GROUND_Z + 1)) == nil, 'no ray function: no ground (nothing acts)')
function castRayStatic(from, d, len) return len end
T.check(edgeguard.groundBelow(vec3(0, 0, 0)) == nil, 'a miss (the full length): no ground')
function castRayStatic(from, d, len) return edgeguard.RAY_UP + 0.4 end
T.near(edgeguard.groundBelow(vec3(0, 0, 0)), 0.4, 1e-12, 'a hit: the height above the ground')

T.done('test_edgeguard')
