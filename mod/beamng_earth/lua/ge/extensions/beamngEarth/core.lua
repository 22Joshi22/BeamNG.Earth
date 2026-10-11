-- BeamNG.Earth core: GE extension 'beamngEarth_core' (lua/ge/extensions/beamngEarth/core.lua).
-- Ties together the server connection, the two world-moving mechanisms - terrain-follow (frequent,
-- same anchor) and origin rebase (rare, new anchor) - the far field, the sky location and the UI.
-- Contract: docs/V1_3_SPEC.md (protocol 4) over docs/V1_2_DESIGN.md (its section 12 wins), over
-- docs/V1_1_SPEC.md and docs/V1_SPEC.md.
-- Every frame first opens the governor's frame (governor.lua): far-tile streaming gets a small,
-- ramped per-frame budget, and the heavy operations (terrain commit, rebase, teleport) run in
-- isolated heavy slots with no streaming in or around them.
-- Near visual (server hello.nearVisual): 'mesh' = the far-tile system draws all the ground and the
-- TerrainBlock is physics-only (render disabled, re-applied after every import); 'terrain' = v1.2.
-- Block size (server hello.blockSize, docs/V1_4_SPEC.md 3.5 M4; none = 1024, v1.3.1): 512 sets the
-- lattice, terrain-follow table, layer / flat maps and rebase staleness rule of the session
-- (setBlockSize), before anything else reads them.
-- Horizon (server hello.horizon, docs/V1_4_SPEC.md 3.6 M1h; none = off, v1.3.1): LevelInfo.visibleDistance
-- follows the server's far-field radius (status.radius, +10 %) and the sky's planet radius is the
-- tiles' (horizon.lua); the level's values come back when it ends.
-- Robustness (docs/ROADMAP.md "Robustness: no server, lost server, lost internet" 1-3): the
-- connection banner (banner.lua) says what is wrong with the world server, the edge guard
-- (edgeguard.lua) holds the car on the last physics block while the server cannot move it (never
-- while the server is healthy), and hello / pose name the session folders whose files the far
-- tiles still use (farfield.heldSessions), so a restarted server keeps them (server/README.md).
--
-- World logic is active in our level (levels/beamng_earth) from the start, and in any other
-- level once the player teleports (for testing). Console helpers:
--   beamngEarth_core.teleport(lat, lon)   beamngEarth_core.toggleUI()   beamngEarth_core.status()
--   beamngEarth_core.setRebaseDistance(m)   beamngEarth_core.setFarBudget(ms)  (streaming budget, 1-4 ms)

local M = {}
M.dependencies = { 'ui_imgui' }

-- Helper modules live next to this file. 'lua/ge/extensions/?.lua' is on package.path
-- (main.lua:58 extensions.addModulePath), so they resolve as 'beamngEarth/<name>'.
local geodesy = require('beamngEarth/geodesy')
local net = require('beamngEarth/net')
local terrain = require('beamngEarth/terrain')
local farfield = require('beamngEarth/farfield')
local crossover = require('beamngEarth/crossover')
local rebase = require('beamngEarth/rebase')
local follow = require('beamngEarth/follow')
local lattice = require('beamngEarth/lattice')
local governor = require('beamngEarth/governor')
local camshift = require('beamngEarth/camshift')
local stopwatch = require('beamngEarth/stopwatch')
local horizon = require('beamngEarth/horizon')
local ui = require('beamngEarth/ui')
local settings = require('beamngEarth/settings') -- the pause-menu page's values (docs/DEVTOOLS.md)
local credits = require('beamngEarth/credits') -- the on-screen data credits (docs/DEVTOOLS.md)
local places = require('beamngEarth/places') -- saved and recent places (the settings page)
local banner = require('beamngEarth/banner') -- the connection banner (docs/DEVTOOLS.md)
local edgeguard = require('beamngEarth/edgeguard') -- never fall off the last block (docs/ROADMAP.md)
local groundcover = require('beamngEarth/groundcover') -- grass on the surface layers (docs/ROADMAP.md v1.5)
local roads = require('beamngEarth/roads') -- DecalRoads from the server's road tiles (docs/ROADMAP.md v1.6)

local LEVEL_ID = 'beamng_earth'
local MOD_VERSION = '0.4.0'
local PROTOCOL = 4
-- Far-tile artefact formats the mod reads: 3 (v1.3.1, 32-cell tiles) and 4 (docs/V1_4_SPEC.md 3.1
-- M1: 64-cell tiles down to L-1, hello.cells, far_tile.tex). hello.format = the newest (FORMAT).
local FORMAT = 4
local FORMATS = { 3, 4 }
local POSE_INTERVAL = 0.5      -- ~2 Hz
local SKY_INTERVAL = 5.0       -- TimeOfDay lat/lon follow
local VISIBLE_DISTANCE = 100000
local LAG_PROBE_FRAMES = 10   -- simulated frames to watch for a rebase's vehicle move to show up
local PLACE_DELAY_FRAMES = 2   -- let the collision rebuild settle before placing the vehicle
local PRE_LIFT = 5             -- m above groundZ + 1 to hold the vehicle until it is placed
local LOADING_AFTER_TELEPORT_S = 3 -- governor loading mode (8 ms budget) this long after a teleport
local SLOW_SPEED, SLOW_S = 1, 3    -- ... and while the player is below 1 m/s for over 3 s
local DEFAULT_LAT, DEFAULT_LON = 36.5785, -118.2923 -- near Mt Whitney / Lone Pine, CA
-- The server cannot move the block when it is not connected, has not said hello, or does not answer
-- terrain requests: one unanswered for STUCK_S, or a terrain_error within ERROR_HOLD_S. Only then
-- does the edge guard run.
local STUCK_S = 5
local ERROR_HOLD_S = 10
local ATTRIBUTION = 'Elevation: Mapterhorn (CC-BY etc., see mapterhorn.com), AWS Terrain Tiles / Mapzen'
-- shown once satellite-textured tiles arrive (docs/V1_3_SPEC.md C; EOX's required wording)
local IMAGERY_ATTRIBUTION = 'Imagery: USGS The National Map (public domain); EOxCloudless https://cloudless.eox.at by '
  .. 'EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2023 / 2016 & 2017; CC BY-NC-SA 4.0 / CC BY 4.0)'
local FULL_ATTRIBUTION = IMAGERY_ATTRIBUTION .. '\n' .. ATTRIBUTION

local st -- all runtime state, see resetState()
local rebaseLag = 0 -- frames between a vehicle move request and its visibility (measured, see commitRebase)
local roadsSession = nil -- the server session whose road tiles are shown (roads.lua)
local gov = governor.new() -- outlives levels (keeps its target and cost estimates)
local warned = {}          -- one-time warnings
local nearVisual = 'terrain' -- from the server's hello; outlives levels like the connection
local serverAttribution = nil -- hello.attribution (protocol 4): the credits for the sources it uses
local blockSize = 1024        -- the session's terrain block size (hello.blockSize); outlives levels
local guardFailed = false     -- the edge guard threw once: off for the rest of the game (logged)
local bannerInfo = { port = net.PORT } -- banner.update's inputs, filled every frame (no allocation)
local horizonOn = false       -- hello.horizon (M1h); outlives levels like the connection
local farRadius = nil         -- status.radius: the server's far-field radius (m), horizon mode only
local cacheInfo = nil         -- the server's last 'cache' message (download cache usage); outlives levels
local cacheClears = 0         -- answers to a clear so far (the settings page waits for the next one)

local function resetState()
  st = {
    inOurLevel = false,
    active = false,          -- world logic running (rebase, far field, sky, pose)
    anchor = nil,            -- geodesy anchor of the committed frame
    anchorId = nil,
    anchorKey = nil,         -- server's opaque anchor key (protocol 3), for display
    anchorSession = nil,     -- the server session (hello.sessionId) the committed anchor belongs to
    resync = false,          -- a server said hello while an anchor is committed: the block cannot
                             -- follow until the resync (same origin, that server's anchor) commits
    pending = nil,           -- anchor request { requestId, purpose = 'teleport'|'rebase'|'resync', ready, msg }
    nextRequestId = 1,
    retryAt = 0,
    block = nil,             -- { cx, cy } centre of the committed terrain block, current anchor frame
    terrainPending = nil,    -- terrain-follow request { requestId, anchorId, cx, cy, ready, msg }
    terrainRetryAt = 0,
    rebasing = nil,          -- a rebase between its two phases (see commitRebase)
    lagProbe = nil,          -- watches, at onPreRender, when the last rebase's vehicle move became visible
    paused = false,
    lastFollowMs = nil, lastRebaseMs = nil, lastRebaseWait = nil, followCount = 0, rebaseCount = 0,
    clock = 0,
    sessionId = nil,
    status = '',
    placeFrames = nil,       -- countdown to placing the player after a teleport
    placeZ = nil,
    poseTimer = 0,
    skyTimer = SKY_INTERVAL,
    autoTeleportDone = false,
    player = nil,            -- { x, y, z, vx, vy, lat, lon, dist, speed } of this frame
    cam = nil,               -- { x, y, z, fx, fy, fz, fovDeg } of this frame (core_camera), or nil
    loadingUntil = 0,        -- governor loading mode until this clock (after a teleport)
    slowFor = 0,             -- s the player has been below SLOW_SPEED
    serverBacklog = nil,     -- status.backlog of the server's last status (v1.4 M0), nil before / from v1.3.1
    anchorLatency = nil,     -- s from prepare_anchor to anchor_ready of the last rebase request (M4 stale rule)
    terrainLatency = nil,    -- s from prepare_terrain to terrain_ready of the last one (M4 follow re-check)
    visibleDistance = nil,   -- LevelInfo.visibleDistance set for the horizon (M1h); nil = VISIBLE_DISTANCE
    flat = false,            -- the level-start flat block is loaded (centred on the origin), no anchor block yet
    serverNote = nil,        -- the network note in the server's last status ('offline' / 'degraded', banner.noteOf)
    serverNoteDetail = nil,
    terrainErrorAt = nil,    -- clock of the last terrain_error since the last terrain arrived
    guard = edgeguard.new(), -- the edge guard's state (mode 'off' while the block can follow)
    guardBox = {},           -- the collidable area it guards (reused)
  }
end
resetState()

local function warnOnce(key, text)
  if warned[key] then return end
  warned[key] = true
  log('W', 'beamngEarth', text)
end

local function setStatus(text)
  st.status = text
  log('I', 'beamngEarth', text)
end

local function hypot(x, y) return math.sqrt(x * x + y * y) end

------------------------------------------------------------------------------------------
-- anchors

local function nextId()
  local id = st.nextRequestId
  st.nextRequestId = id + 1
  return id
end

local teleportTo -- defined with M.teleport (the level start's own teleport is one too)

-- the level start's teleport: to the window's lat/lon (the default start until it is changed)
local function startTeleport()
  local lat, lon = ui.getLatLon(DEFAULT_LAT, DEFAULT_LON)
  return teleportTo(lat, lon, nil, 'start')
end

-- cx, cy: where the anchor's first terrain should be centred in the NEW frame
-- ((0,0) for a teleport, snap256(predicted) for a rebase; V1_1_SPEC 3).
local function requestAnchor(lat, lon, purpose, cx, cy)
  if not net.isConnected() then
    setStatus('Companion server not connected (127.0.0.1:' .. net.PORT .. ')')
    return false
  end
  local id = nextId()
  st.pending = { requestId = id, purpose = purpose, lat = lat, lon = lon, sentAt = st.clock }
  net.send({ type = 'prepare_anchor', requestId = id, lat = lat, lon = lon, cx = cx or 0, cy = cy or 0 })
  return true
end

-- Where the player will be, in the new frame, when the rebase commits: the new anchor is the
-- player's position now, and the commit comes once the distance reaches rebase.distance.
-- M4 stale-anchor rule (rebase.KEEP_EDGE > 0, the 512² block): at or past the distance (a re-request
-- after a stale anchor was discarded) the terrain goes where the player will be when the answer
-- arrives, p + v * (measured anchor latency + 1 s), so a slow server cannot make it stale forever.
local function rebaseHint(pl)
  local rem = math.max(0, rebase.distance - pl.dist)
  if rem <= 0 and rebase.KEEP_EDGE > 0 then
    local lead = rebase.aheadLead(st.anchorLatency)
    local lat = lead - rebase.LEAD_EXTRA_S
    return follow.centreNear(pl.vx * lead, pl.vy * lead, pl.vx * lat, pl.vy * lat, follow.reachFor(pl.vx, pl.vy))
  end
  local px, py = 0, 0
  if pl.speed > 1 then px, py = pl.vx / pl.speed * rem, pl.vy / pl.speed * rem end
  return follow.target(px, py, pl.vx, pl.vy)
end

-- Terrain centre for a resync (same frame): where terrain-follow would put it now.
local function resyncHint()
  local pl = st.player
  if pl then return follow.target(pl.x, pl.y, pl.vx, pl.vy) end
  if st.block then return st.block.cx, st.block.cy end
  return 0, 0
end

-- R = I and t = 0 (a resync re-registers the same origin).
local function isIdentity(xf)
  local r, t = xf.r, xf.t
  return math.abs(t[1]) + math.abs(t[2]) + math.abs(t[3]) < 1e-6
    and math.abs(r[2]) + math.abs(r[3]) + math.abs(r[4]) + math.abs(r[6]) + math.abs(r[7]) + math.abs(r[8]) < 1e-12
end

-- The terrain an anchor_ready carries (V1_1_SPEC 3), or nil + error.
local function anchorTerrain(msg)
  local tr = msg.terrain
  if type(tr) ~= 'table' then return nil, 'anchor_ready without terrain' end
  if tr.size ~= terrain.SIZE then return nil, 'unsupported terrain size ' .. tostring(tr.size) end -- the session's (M4)
  if type(tr.heightmap) ~= 'string' or not FS:fileExists(tr.heightmap) then
    return nil, 'heightmap not found: ' .. tostring(tr.heightmap)
  end
  return tr
end

-- Import a terrain message ({cx, cy, heightmap, squareSize, zBase, maxHeight, q}) as the block.
-- The caller rebuilds collision. squareSize must be the 2.0 m lattice (lattice.lua); maxHeight is
-- q * 65536 with zBase a multiple of q (design 12, P1), checked here and logged once if not.
local function loadBlock(tr)
  local x, y = follow.blockOrigin(tr.cx, tr.cy)
  local sq = tonumber(tr.squareSize) or follow.SQUARE_SIZE
  if math.abs(sq - follow.SQUARE_SIZE) > 1e-9 then
    warnOnce('square', string.format('server squareSize %.12g, mod lattice %.12g: terrain-follow geometry will be off',
      sq, follow.SQUARE_SIZE))
  end
  local maxH, warn = lattice.heightParams(tr)
  if warn then warnOnce('height', 'terrain height scale: ' .. warn) end
  local layers = (type(tr.layers) == 'table' and type(tr.materials) == 'table')
    and { masks = tr.layers, materials = tr.materials } or nil -- server --surface on (surface.py)
  if layers then groundcover.ensure() end
  local ok, err = terrain.load(tr.heightmap, sq, maxH, x, y, tr.zBase, layers)
  -- every block import / move drops all the grass on it; rebuild it before this frame is drawn
  groundcover.afterBlockChange()
  return ok, err
end

-- M1h: in horizon mode LevelInfo.visibleDistance follows the server's radius (+10 %, with
-- hysteresis, never below VISIBLE_DISTANCE, each change with postApply) and the sky uses the tiles'
-- planet radius; otherwise both go back to what they were (only if horizon mode changed them).
local function applyHorizon()
  if not st.active then return end
  if horizonOn then
    horizon.applySky()
    local v = horizon.visibleTarget(farRadius, st.visibleDistance or VISIBLE_DISTANCE, VISIBLE_DISTANCE)
    if v and horizon.setVisibleDistance(v) then st.visibleDistance = v end
  else
    horizon.restoreSky()
    if st.visibleDistance and horizon.setVisibleDistance(VISIBLE_DISTANCE) then st.visibleDistance = nil end
  end
end

local function activateWorld()
  if st.active then return end
  st.active = true
  -- bigMapMode.lua:866-867 sets the same LevelInfo field at runtime
  if scenetree.theLevelInfo then scenetree.theLevelInfo.visibleDistance = VISIBLE_DISTANCE end
  terrain.hideGroundPlanes()
  applyHorizon()
end

local function sendCommits(anchorId, cx, cy)
  net.send({ type = 'commit_anchor', anchorId = anchorId })
  net.send({ type = 'commit_terrain', anchorId = anchorId, cx = cx, cy = cy })
end

-- Teleport: switch to a ready anchor and place the player at its origin.
-- Motion blur across a frame change. The blur's velocity pass derives every pixel's motion from
-- the camera's world-space move since the last frame, with no teleport check
-- (shaders/common/postFx/motionBlur/clear.cs.hlsl:25-37): a rebase or teleport moves the camera
-- by kilometres and the frame smears at full strength (the "flash" seen in-game at rebases).
-- Its strength goes to 0 for a few frames and back (strength, not isEnabled: as the graphics
-- settings do it, core/settings/graphic.lua:1392). docs/research/2026-10-10-origin-distance.md.
local BLUR_HOLD_FRAMES = 4
local blurHold = { frames = 0, saved = nil }

local function holdMotionBlur()
  local fx = scenetree.PostFxMotionBlur
  if not fx then return end
  if blurHold.frames <= 0 then
    blurHold.saved = tonumber(fx.strength)
    if not blurHold.saved or blurHold.saved <= 0 then blurHold.saved = nil; return end
    fx.strength = 0
  end
  blurHold.frames = BLUR_HOLD_FRAMES
end

local function tickMotionBlur()
  if blurHold.frames <= 0 then return end
  blurHold.frames = blurHold.frames - 1
  if blurHold.frames > 0 then return end
  local fx = scenetree.PostFxMotionBlur
  -- the player may have changed the setting meanwhile (then it is no longer 0): leave theirs
  if fx and blurHold.saved and (tonumber(fx.strength) or 0) == 0 then fx.strength = blurHold.saved end
  blurHold.saved = nil
end

local function commitTeleport(msg)
  local tr, err = anchorTerrain(msg)
  gov:heavyNow() -- the user asked for a jump: no waiting for a calm frame
  local sw = stopwatch.new()
  local ok
  if tr then ok, err = loadBlock(tr) end
  -- a new place: grass made fresh (with tools/gcpatch the engine keeps it across block moves)
  if ok then groundcover.rebuild() end
  if not ok then
    setStatus('Terrain import failed: ' .. tostring(err))
    st.retryAt = st.clock + rebase.RETRY_S -- don't re-request every frame
    return false
  end
  farfield.switchAnchor(msg.anchorId, false, { cx = tr.cx, cy = tr.cy }, st.sessionId)
  st.loadingUntil = st.clock + LOADING_AFTER_TELEPORT_S
  st.placeFrames = PLACE_DELAY_FRAMES
  st.placeZ = (msg.groundZ or 0) + 1.0
  -- The player's old coordinates may now lie anywhere on the new terrain.
  -- Lift it above the new origin in this same frame (velocity zeroed as in spawn.lua:532-534)
  -- so physics never sees that; placePlayer() then sets it down properly.
  local veh = getPlayerVehicle(0)
  holdMotionBlur()
  if veh then
    veh:setClusterPosRelRot(-1, 0, 0, st.placeZ + PRE_LIFT, 0, 0, 0, 1)
    veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
  end
  be:reloadCollision()

  st.anchor, st.anchorId, st.anchorKey = geodesy.newAnchor(msg.lat, msg.lon), msg.anchorId, msg.anchorKey
  st.anchorSession, st.resync = st.sessionId, false
  st.block, st.terrainPending, st.terrainErrorAt = { cx = tr.cx, cy = tr.cy }, nil, nil
  edgeguard.newFrame(st.guard)
  sendCommits(msg.anchorId, tr.cx, tr.cy)
  activateWorld()
  st.skyTimer, st.poseTimer = SKY_INTERVAL, POSE_INTERVAL -- refresh both right away
  setStatus(string.format('Anchor #%s at %.5f, %.5f (teleport, %.0f ms)', tostring(msg.anchorId),
    msg.lat, msg.lon, sw()))
  return true
end

-- Second phase of a rebase: everything except the vehicles, in one frame, from onUpdate.
local function finishRebase()
  local rb = st.rebasing
  st.rebasing = nil
  gov:heavyNow() -- phase 2 must follow phase 1's vehicle move; its frame is a heavy slot anyway
  local sw = stopwatch.new()
  local msg, tr, xf = rb.msg, rb.terrain, rb.xf
  local ok, err = loadBlock(tr)
  -- the world shifts under the grass: made fresh (with tools/gcpatch the engine would keep it)
  groundcover.rebuild()
  if not ok then log('E', 'beamngEarth', 'rebase terrain import failed: ' .. tostring(err)) end
  farfield.transform(xf)
  -- a resync with a restarted server keeps the previous session's tiles the same way (st.sessionId
  -- is the new session's): they go once the new session's tiles cover them
  farfield.switchAnchor(msg.anchorId, true, { cx = tr.cx, cy = tr.cy }, st.sessionId)
  local cs = camshift.shift(xf, rb.playerOld[1], rb.playerOld[2], rb.playerOld[3])
  be:reloadCollision()

  st.anchor, st.anchorId, st.anchorKey = rb.newAnchor, msg.anchorId, msg.anchorKey
  st.anchorSession, st.resync = st.sessionId, false
  st.block, st.terrainPending, st.terrainErrorAt = { cx = tr.cx, cy = tr.cy }, nil, nil
  edgeguard.newFrame(st.guard)
  sendCommits(msg.anchorId, tr.cx, tr.cy)
  activateWorld()
  st.skyTimer, st.poseTimer = SKY_INTERVAL, POSE_INTERVAL
  st.lastRebaseMs, st.lastRebaseWait = rb.phase1Ms + sw(), rb.waited
  st.rebaseCount = st.rebaseCount + 1
  setStatus(string.format('Anchor #%s at %.5f, %.5f (%s, %.0f ms, %.3f deg, phase 2 after %d frame(s), camera: %d points%s)%s',
    tostring(msg.anchorId), msg.lat, msg.lon, rb.purpose, st.lastRebaseMs, math.deg(xf:angle()), rb.waited,
    cs.points + cs.triples, cs.viaDebug and '' or ', no camData', ok and '' or ' TERRAIN FAILED'))
end

-- Is the player's move visible to GE yet (veh:getPosition, the value the camera reads at
-- cameraUpdate.lua:145)? Nearer the new position than the old one = moved.
local function moveVisible(probe)
  local veh = getPlayerVehicle(0)
  local m = probe.playerId and probe.moved[probe.playerId]
  if not veh or not m or veh:getID() ~= probe.playerId then return true end
  local p = veh:getPosition()
  local function d2(q) return (p.x - q[1]) ^ 2 + (p.y - q[2]) ^ 2 + (p.z - q[3]) ^ 2 end
  return d2(m.new) < d2(m.old)
end

-- Origin rebase (V1_1_SPEC 1b), purpose 'rebase' or 'resync'.
--
-- Timing. GE calls update() -> onUpdate "after input and before physics" and luaPreRender() ->
-- onPreRender "right before the rendering, and after running the physics" (main.lua:819-824, 920),
-- and the camera is computed in core_camera's onPreRender (camera.lua: M.onPreRender =
-- cameraUpdate.onPreRender). setClusterPosRelRot is a request to the physics side, so the vehicle
-- is expected to show up at its new position in the onPreRender of the SAME frame. Everything else
-- (terrain + collision, far tiles, the camera's state) is changed directly by Lua and must change
-- in the onUpdate of the frame whose physics step applies the move: then physics never sees the
-- car and the ground in different frames, and the camera never renders the car in one frame with
-- its own history (or the world) in the other.
-- Phase 1 (vehicles) runs now; phase 2 (finishRebase) runs `st.rebaseLag` simulated frames later,
-- in onUpdate. rebaseLag starts at 0 (same frame) and is measured on every rebase: onPreRender
-- records after how many frames the move became visible, and the next rebase uses that.
local function commitRebase(msg, purpose)
  local tr, err = anchorTerrain(msg)
  if not tr then
    setStatus('Rebase failed: ' .. tostring(err))
    st.retryAt = st.clock + rebase.RETRY_S
    return false
  end
  local sw = stopwatch.new()
  local newAnchor = geodesy.newAnchor(msg.lat, msg.lon)
  local xf = st.anchor:transformTo(newAnchor)
  -- nothing to move for an identity transform (resync); setClusterPosRelRot from the GE-side
  -- position would only pull the car back to where GE last saw it
  local moved, playerId = {}, nil
  if not isIdentity(xf) then
    holdMotionBlur()
    moved, playerId = rebase.moveVehicles(xf, geodesy)
  end
  local m = playerId and moved[playerId]
  st.rebasing = { msg = msg, terrain = tr, purpose = purpose, newAnchor = newAnchor, xf = xf,
    wait = rebaseLag, waited = 0,
    playerOld = m and m.old or { st.player and st.player.x or 0, st.player and st.player.y or 0, st.player and st.player.z or 0 } }
  st.lagProbe = { moved = moved, playerId = playerId, frames = 0 }
  st.rebasing.phase1Ms = sw()
  if moveVisible(st.lagProbe) then
    st.lagProbe = nil -- applied synchronously: no lag at all
    st.rebasing.wait = 0
  end
  if st.rebasing.wait <= 0 then finishRebase() end
  return true
end

local function commitAnchor(msg, purpose)
  if st.rebasing then finishRebase() end -- never mix two frame changes
  if purpose == 'teleport' or not st.anchor then return commitTeleport(msg) end
  return commitRebase(msg, purpose)
end

-- Terrain-follow commit (V1_1_SPEC 1a, V1_2_DESIGN 2.3), in a governor heavy slot, atomic in one
-- frame: importMaps + setPosition + reloadCollision + the far tiles' cover change. Vehicles and
-- camera stay where they are; far tiles only sink / hide / come back (no imports in this frame).
local function commitTerrain(msg)
  local sw = stopwatch.new()
  local ok, err = loadBlock(msg)
  if not ok then
    setStatus('Terrain-follow import failed: ' .. tostring(err))
    st.terrainRetryAt = st.clock + follow.RETRY_S
    return false
  end
  be:reloadCollision()
  farfield.onBlockCommit(msg.cx, msg.cy)
  st.block = { cx = msg.cx, cy = msg.cy }
  net.send({ type = 'commit_terrain', anchorId = st.anchorId, cx = msg.cx, cy = msg.cy })
  st.lastFollowMs = sw()
  st.followCount = st.followCount + 1
  log('I', 'beamngEarth', string.format('terrain block now at (%d, %d), %.1f ms', msg.cx, msg.cy, st.lastFollowMs))
  return true
end

-- Put the player vehicle on the new ground at the origin, upright, keeping its heading.
-- spawn.safeTeleport with resetVehicle=true resets the vehicle, drops its bounding box onto
-- the ground (spawn.lua:494 getBBOnGround) and zeroes velocity (spawn.lua:533-534).
-- The heading-preserving quat follows main.lua:1306.
local function placePlayer(z)
  local veh = getPlayerVehicle(0)
  if not veh then return end
  local dir = veh:getDirectionVector()
  dir = vec3(dir.x, dir.y, 0)
  if dir:length() < 1e-3 then dir = vec3(0, 1, 0) end
  spawn.safeTeleport(veh, vec3(0, 0, z), quatFromDir(dir:normalized(), vec3(0, 0, 1)), nil, nil, false, nil, true)
end

------------------------------------------------------------------------------------------
-- messages

local function formatKnown(f)
  for _, v in ipairs(FORMATS) do if v == f then return true end end
  return false
end

local function onMessage(msg)
  local t = msg.type
  if t == '_connected' then
    -- sessions: the session folders our far tiles still use (a restarted server keeps them)
    net.send({ type = 'hello', protocol = PROTOCOL, format = FORMAT, modVersion = MOD_VERSION,
      sessions = farfield.heldSessions() })
    setStatus('Connected to companion server')
  elseif t == '_disconnected' then
    st.pending, st.sessionId, st.terrainPending = nil, nil, nil
    cacheInfo = nil
    st.serverNote, st.serverNoteDetail, st.terrainErrorAt = nil, nil, nil
    -- an auto-teleport lost with the connection is retried on the next hello
    if not st.anchor then st.autoTeleportDone = false end
    setStatus('Disconnected from companion server: ' .. tostring(msg.error))
  elseif t == 'hello' then
    st.sessionId = msg.sessionId
    -- road tiles belong to the session that sent them: a restarted server (anchor ids start again
    -- at 1) never drops the old session's tiles, so they stayed forever (2026-10-11: meshes of four
    -- sessions in the scene, some built by older code)
    if roadsSession ~= msg.sessionId then
      roads.clear()
      roadsSession = msg.sessionId
    end
    st.serverNote, st.serverNoteDetail = nil, nil
    -- a new server session: its own gravity message follows the first pose (none with --gravity off)
    local w = rawget(_G, 'beamngEarth_weather')
    if w and w.clearGravity then w.clearGravity() end
    st.deshadow = msg.deshadow -- server-side imagery de-shadowing ('lambert' / 'full'; nil = off), shown on the settings page
    st.colormatch = msg.colormatch -- server-side imagery colour-seam removal ('s2'; nil = off), likewise
    -- the download cache: the new server's usage follows its hello; it gets the page's size cap (the
    -- server keeps it for its next start too)
    cacheInfo = nil
    local settings = rawget(_G, 'beamngEarth_settings')
    if settings and settings.cacheCapGb then net.send({ type = 'cache', capGb = settings.cacheCapGb() }) end
    -- the on-screen credits: this session's own follow (the attribution line until then)
    credits.clear()
    -- M4: the terrain block size first (everything below may use the lattice / follow table); a
    -- server without hello.blockSize has the v1.3.1 1024² block
    local size, okSize = M.setBlockSize(msg.blockSize or 1024)
    if not okSize then
      setStatus('Unsupported terrain block size ' .. tostring(msg.blockSize) .. ': using ' .. size)
    end
    if msg.protocol ~= PROTOCOL then
      setStatus('Protocol mismatch: server ' .. tostring(msg.protocol) .. ', mod ' .. PROTOCOL)
    elseif msg.format ~= nil and not formatKnown(msg.format) then
      setStatus('Format mismatch: server ' .. tostring(msg.format) .. ', mod ' .. table.concat(FORMATS, '/'))
    end
    -- M1 (FORMAT 4): the tiles' cells per edge; a FORMAT 3 server sends none (32). It sets the
    -- crossover's fallback error and the exact (physics lattice) level of the exactMiss metric.
    local cells = crossover.setCells(msg.cells)
    farfield.setExactLevel(farfield.exactLevelFor(cells))
    -- every far tile references the server's material name; this mod ships (and loads) one
    -- static material (farfield.MATERIALS_FILE). A different name renders every tile without it.
    if msg.material ~= nil and msg.material ~= farfield.MATERIAL then
      warnOnce('material', 'far-tile material mismatch: server ' .. tostring(msg.material) .. ', mod ' .. farfield.MATERIAL)
      setStatus('Far-tile material mismatch: server ' .. tostring(msg.material) .. ', mod ' .. farfield.MATERIAL)
    end
    -- protocol 4: who draws the near ground (a protocol-3 server sends nothing: the terrain does),
    -- and the credit line for the imagery / elevation sources the server actually uses
    M.setNearVisual(msg.nearVisual)
    -- M1h: horizon mode (a server without hello.horizon: the level's sky and 100 km view distance)
    M.setHorizon(msg.horizon == true)
    serverAttribution = (type(msg.attribution) == 'string' and msg.attribution ~= '') and msg.attribution or nil
    if st.anchor then
      -- A (re)started server knows nothing of our anchor: re-register the SAME geodetic origin under
      -- a new anchorId. The transform is then the identity, so nothing in the world moves (a shift
      -- here could happen close to the origin, where camshift cannot tell positions from offsets).
      -- Until it commits the block cannot follow (st.resync: the edge guard runs, a failed resync is
      -- asked again in updateWorld).
      st.resync = true
      requestAnchor(st.anchor.lat, st.anchor.lon, 'resync', resyncHint())
    elseif st.inOurLevel and not st.anchor and not st.autoTeleportDone then
      st.autoTeleportDone = true
      startTeleport()
    end
  elseif t == 'status' then
    st.status = tostring(msg.text)
    -- the server's network note ("Offline - using cached data" / "Degraded: ..."): the banner shows it
    st.serverNote, st.serverNoteDetail = banner.noteOf(msg.text)
    -- v1.4 M0: the server's far-tile backlog (desired - shown) rides on every status; nil from a
    -- v1.3.1 server. For the harness (status()), nothing here acts on it.
    st.serverBacklog = tonumber(msg.backlog)
    -- M1h: the far-field radius rides on every status in horizon mode (the view distance follows it)
    if msg.radius ~= nil then M.setFarRadius(msg.radius) end
  elseif t == 'anchor_ready' then
    local p = st.pending
    if not p or msg.requestId ~= p.requestId then return end -- stale answer
    if p.purpose == 'rebase' or (p.purpose == 'resync' and st.anchor) then
      p.ready, p.msg = true, msg -- committed in updateWorld() (distance rule / when not paused)
      if p.purpose == 'rebase' and p.sentAt then st.anchorLatency = st.clock - p.sentAt end
    else
      st.pending = nil
      commitAnchor(msg, p.purpose)
    end
  elseif t == 'anchor_error' then
    local p = st.pending
    if not p or msg.requestId ~= p.requestId then return end
    st.pending = nil
    st.retryAt = st.clock + rebase.RETRY_S
    setStatus('Anchor request failed: ' .. tostring(msg.error))
  elseif t == 'terrain_ready' then
    local p = st.terrainPending
    if not p or msg.requestId ~= p.requestId or msg.anchorId ~= st.anchorId then return end -- stale
    if msg.size ~= terrain.SIZE or msg.cx ~= p.cx or msg.cy ~= p.cy then
      st.terrainPending, st.terrainRetryAt = nil, st.clock + follow.RETRY_S
      setStatus('Unexpected terrain_ready (size ' .. tostring(msg.size) .. ')')
      return
    end
    p.ready, p.msg = true, msg -- committed by the distance rule in updateWorld()
    if p.sentAt then st.terrainLatency = st.clock - p.sentAt end
    st.terrainErrorAt = nil
  elseif t == 'terrain_error' then
    local p = st.terrainPending
    if not p or msg.requestId ~= p.requestId then return end
    st.terrainPending, st.terrainRetryAt = nil, st.clock + follow.RETRY_S
    st.terrainErrorAt = st.clock -- the edge guard holds the car meanwhile
    setStatus('Terrain request failed: ' .. tostring(msg.error))
  elseif t == 'roads_tile' then
    roads.onTile(msg, st.anchorId)
  elseif t == 'roads_drop' then
    roads.onDrop(msg)
  elseif t == 'far_tile' then
    farfield.onTile(msg)
  elseif t == 'far_swap' then
    farfield.onSwap(msg)
  elseif t == 'far_remove' then
    farfield.onRemove(msg)
  elseif t == 'far_done' then
    farfield.onDone(msg)
  elseif t == 'far_retex' then
    farfield.onRetex(msg) -- a shown tile's re-rendered texture (--colormatch / --deshadow)
  elseif t == 'weather' then
    -- real weather (server weather.py): applied by the separate beamngEarth_weather extension
    local w = rawget(_G, 'beamngEarth_weather')
    if w and w.onWeather then w.onWeather(msg) end
  elseif t == 'gravity' then
    -- real gravity at the player (server gravity.py): applied by beamngEarth_weather's setGravity path
    local w = rawget(_G, 'beamngEarth_weather')
    if w and w.onGravity then w.onGravity(msg) end
  elseif t == 'credits' then
    -- the sources around the player (server credits.py): msg.near, msg.far, drawn by credits.lua
    credits.onMessage(msg)
  elseif t == 'cache' then
    -- the download cache (server cache.py): its usage for the settings page; clearedBytes answers a clear
    local prev = cacheInfo
    cacheInfo = { usedBytes = tonumber(msg.usedBytes) or 0, capBytes = tonumber(msg.capBytes) or 0,
      files = tonumber(msg.files) or 0, ready = msg.ready == true, managed = msg.managed ~= false,
      clearedBytes = prev and prev.clearedBytes, clears = cacheClears }
    if msg.clearedBytes ~= nil then
      cacheClears = cacheClears + 1
      cacheInfo.clearedBytes, cacheInfo.clears = tonumber(msg.clearedBytes) or 0, cacheClears
    end
  end
end

------------------------------------------------------------------------------------------
-- per frame

-- The rendering camera (core_camera, core/camera.lua:1927-1937: getPositionXYZ, getForwardXYZ,
-- getFovDeg), for the server's view-angle priority and the off-screen swap test. nil if unavailable.
local function cameraState()
  local cam = rawget(_G, 'core_camera')
  if not cam or not cam.getPositionXYZ then return nil end
  local ok, x, y, z = pcall(cam.getPositionXYZ)
  if not ok or type(x) ~= 'number' then return nil end
  local okF, fx, fy, fz = pcall(cam.getForwardXYZ)
  if not okF or type(fx) ~= 'number' then return nil end
  local okV, fov = pcall(cam.getFovDeg)
  return { x = x, y = y, z = z, fx = fx, fy = fy, fz = fz, fovDeg = (okV and type(fov) == 'number') and fov or 60 }
end

local function updatePlayer(dt)
  st.player = nil
  st.cam = cameraState()
  local veh = getPlayerVehicle(0)
  if not veh or not st.anchor then return end
  local p = veh:getPosition()
  local v = veh:getVelocity()
  local lat, lon = st.anchor:enuToGeodetic(p.x, p.y, p.z)
  st.player = { x = p.x, y = p.y, z = p.z, vx = v.x, vy = v.y, lat = lat, lon = lon, dist = hypot(p.x, p.y),
    speed = v:length() }
  st.slowFor = st.player.speed < SLOW_SPEED and (st.slowFor + dt) or 0
end

local function updateSky(dt)
  st.skyTimer = st.skyTimer + dt
  if st.skyTimer < SKY_INTERVAL or not st.player then return end
  st.skyTimer = 0
  local lat, lon = st.player.lat, st.player.lon
  if core_celestial and core_celestial.setLocation then
    core_celestial.setLocation(lat, lon) -- core/celestial.lua:762, writes TimeOfDay.latitude/longitude
  else
    local names = scenetree.findClassObjects('TimeOfDay')
    local tod = names and names[1] and scenetree.findObject(names[1])
    if tod then tod.latitude, tod.longitude = lat, lon end
  end
end

-- Does the connected server know our anchor? After a server restart the committed anchor's id is
-- the old session's, and the new server numbers its own anchors from 1 again: until the resync
-- commits, a pose / prepare_terrain naming it would name one of the new server's anchors (its resync
-- anchor 1 is the usual one), which it would switch to and stream tiles for that the far field files
-- under the old session.
local function anchorKnown()
  return st.anchorId ~= nil and st.anchorSession == st.sessionId
end

local function updateWorld(dt)
  if st.placeFrames then
    st.placeFrames = st.placeFrames - 1
    if st.placeFrames <= 0 then
      st.placeFrames = nil
      placePlayer(st.placeZ)
    end
    return -- no rebase decisions while the player is being placed
  end
  local pl = st.player
  if not pl then return end

  st.poseTimer = st.poseTimer + dt
  -- no pose before the resync with a restarted server commits (anchorKnown; hello named the
  -- session folders meanwhile)
  if st.poseTimer >= POSE_INTERVAL and net.isConnected() and anchorKnown() then
    st.poseTimer = 0
    local c = st.cam
    -- held: previous anchors of this server's session only (the far field's older sessions' anchor
    -- ids are another server's); sessions: every session folder in use
    net.send({ type = 'pose', anchorId = st.anchorId, x = pl.x, y = pl.y, z = pl.z, vx = pl.vx, vy = pl.vy,
      lat = pl.lat, lon = pl.lon, speed = pl.speed,
      held = (farfield.session() == st.sessionId) and farfield.heldAnchors() or {},
      sessions = farfield.heldSessions(),
      cam = c and { x = c.x, y = c.y, z = c.z, fx = c.fx, fy = c.fy, fz = c.fz, fovDeg = c.fovDeg } or nil })
  end

  -- a resync the server could not build (anchor_error: offline, never visited) is asked again
  if st.resync and not st.pending and st.sessionId and st.clock >= st.retryAt then
    requestAnchor(st.anchor.lat, st.anchor.lon, 'resync', resyncHint())
  end

  -- origin rebase first: when it commits, the terrain-follow state belongs to the old frame
  local p = st.pending
  local staleDist, edgeDist
  if p and p.ready and (p.purpose == 'rebase' or p.purpose == 'resync') then
    p.anchorObj = p.anchorObj or geodesy.newAnchor(p.msg.lat, p.msg.lon)
    p.xf = p.xf or st.anchor:transformTo(p.anchorObj)
    local nx, ny = p.xf:apply(pl.x, pl.y, pl.z)
    local tr = p.msg.terrain or {}
    staleDist = follow.cheb(nx, ny, tr.cx or 0, tr.cy or 0)
    -- M4 stale-anchor rule: how far inside the prepared terrain's collidable area the player is
    local x0, y0, x1, y1 = lattice.groundBox(tr.cx or 0, tr.cy or 0)
    edgeDist = math.min(nx - x0, x1 - nx, ny - y0, y1 - ny)
  end
  local action = rebase.decide(pl.dist, p, staleDist, st.clock, st.retryAt, edgeDist)
  if p and p.ready and p.purpose == 'resync' then
    -- the player may have driven off the terrain the server prepared meanwhile: ask again
    if staleDist > rebase.STALE_DIST then
      action = nil
      -- at most one re-request per RETRY_S: if the server can't centre the block where we ask,
      -- re-asking every frame turns into a request storm
      if st.clock >= st.retryAt then
        st.retryAt = st.clock + rebase.RETRY_S
        requestAnchor(st.anchor.lat, st.anchor.lon, 'resync', resyncHint())
      end
    else
      action = 'resync'
    end
  end
  if (action == 'commit' or action == 'resync') and st.paused then action = nil end -- vehicles move only with physics running
  if action == 'prefetch' then
    if net.isConnected() then requestAnchor(pl.lat, pl.lon, 'rebase', rebaseHint(pl)) end
  elseif action == 'commit' or action == 'resync' then
    -- a heavy op: it runs once the governor has ramped streaming down (asked again every frame);
    -- terrain-follow waits meanwhile, its state belongs to the frame that is about to change
    if gov:requestHeavy() then
      st.pending = nil
      commitAnchor(p.msg, p.purpose)
    end
    return
  elseif action == 'discard' then
    st.pending = nil -- the next frame prefetches at the player's position
  end

  -- terrain-follow
  local b = st.block
  -- commit gating: the far tiles of the strip the move uncovers must be live, and no coarse tile
  -- may straddle the new block (design 2.3, farfield.uncoverReady)
  local gate = b and function(ncx, ncy) return farfield.uncoverReady(b.cx, b.cy, ncx, ncy) end
  local fa, tx, ty = follow.decide(pl.x, pl.y, pl.vx, pl.vy, b and b.cx, b and b.cy, st.terrainPending,
    st.clock, st.terrainRetryAt, gate, st.terrainLatency)
  if fa == 'prepare' then
    if net.isConnected() and anchorKnown() then
      local id = nextId()
      st.terrainPending = { requestId = id, anchorId = st.anchorId, cx = tx, cy = ty, sentAt = st.clock }
      net.send({ type = 'prepare_terrain', requestId = id, anchorId = st.anchorId, cx = tx, cy = ty })
    end
  elseif fa == 'commit' then
    if gov:requestHeavy() then -- isolated heavy slot (governor.lua); until then the request repeats
      local tp = st.terrainPending
      st.terrainPending = nil
      commitTerrain(tp.msg)
    end
  elseif fa == 'discard' then
    st.terrainPending = nil
  end
end

------------------------------------------------------------------------------------------
-- robustness: the edge guard and the connection banner (docs/ROADMAP.md "Robustness" 1, 2)

local function terrainStuck()
  local tp = st.terrainPending
  if tp and not tp.ready and tp.sentAt and st.clock - tp.sentAt > STUCK_S then return true end
  return st.terrainErrorAt ~= nil and st.clock - st.terrainErrorAt < ERROR_HOLD_S
end

-- Can the server move the physics block? Connected, its hello arrived, an anchor block committed
-- (terrain-follow never moves the level-start flat block), no resync pending (a server that came
-- back: until it commits, nothing follows) and terrain requests answered.
local function serverHealthy()
  return net.isConnected() and st.sessionId ~= nil and st.block ~= nil and not st.resync and not terrainStuck()
end

-- The collidable area of the block in use (the anchor's, else the level-start flat block), or nil.
local function guardBox()
  local cx, cy
  if st.block then cx, cy = st.block.cx, st.block.cy
  elseif st.flat then cx, cy = 0, 0
  else return nil end
  local box = st.guardBox
  box[1], box[2], box[3], box[4] = lattice.groundBox(cx, cy)
  return box
end

-- After the frame's world update (a block commit has already moved the block): the guard runs only
-- while the server cannot move the block, with physics running and the player placed (also on the
-- level-start flat block, before any anchor: no server at all).
local function updateGuard()
  local g = st.guard
  if guardFailed or serverHealthy() or st.placeFrames or st.paused then
    g.mode = 'off'
    return
  end
  local veh = getPlayerVehicle(0)
  local box = guardBox()
  if not veh or not box then
    g.mode = 'off'
    return
  end
  local ok, err = pcall(edgeguard.update, g, veh, box, st.clock)
  if not ok then
    guardFailed, g.mode = true, 'off'
    log('E', 'beamngEarth', 'edge guard turned off: ' .. tostring(err))
  end
end

local function updateBanner(dt)
  local i = bannerInfo
  i.net, i.session = net.state(), st.sessionId ~= nil
  i.everConnected, i.attempts = net.everConnected(), net.attempts()
  i.note, i.noteDetail = st.serverNote, st.serverNoteDetail
  local m = st.guard.mode
  i.edge = (m == 'warn' or m == 'hold') and m or nil
  -- connected and said hello, yet the guard runs: the server does not (yet) build the terrain
  i.stuck = (i.session and net.isConnected()) and true or false
  banner.update(dt, i)
end

-- The credits line: the server's own (protocol 4 hello) when it sent one; else ours, with the
-- imagery credits once satellite-textured tiles arrived.
local function attributionText(fs)
  if serverAttribution then return serverAttribution end
  return (fs.textured > 0) and FULL_ATTRIBUTION or ATTRIBUTION
end

local function buildView()
  local pl = st.player
  local fs = farfield.stats()
  local b = st.block
  local toPrepare, toCommit
  if pl and b then toPrepare, toCommit = follow.thresholds(pl.x, pl.y, b.cx, b.cy) end
  local tp = st.terrainPending
  return {
    defaultLat = DEFAULT_LAT, defaultLon = DEFAULT_LON,
    connection = net.state() .. (st.sessionId and ('  (session ' .. tostring(st.sessionId) .. ')') or '')
      .. ((not net.isConnected() and net.lastError) and ('  - ' .. tostring(net.lastError)) or ''),
    anchorId = st.anchorId,
    anchorLat = st.anchor and st.anchor.lat, anchorLon = st.anchor and st.anchor.lon,
    playerLat = pl and pl.lat, playerLon = pl and pl.lon,
    dist = pl and pl.dist, toRebase = pl and (rebase.distance - pl.dist),
    rebasePending = st.pending and st.pending.purpose == 'rebase' and (st.pending.ready and 'ready' or 'preparing') or nil,
    blockCx = b and b.cx, blockCy = b and b.cy,
    toFollowPrepare = toPrepare, toFollowCommit = toCommit,
    followPending = tp and string.format('(%d, %d) %s', tp.cx, tp.cy, tp.ready and 'ready' or 'preparing') or nil,
    farText = st.anchor and string.format('Far tiles (%s near ground): %d live (%d covered), %d loading, %d swaps waiting, '
      .. '%d moving, %d old/deleting%s, %d imagery materials',
      fs.nearVisual, fs.live, fs.covered, fs.queued, fs.groups, fs.transitions, fs.old + fs.deleting,
      fs.done and ' (done)' or '', fs.materials),
    lastFollowMs = st.lastFollowMs, followCount = st.followCount,
    lastRebaseMs = st.lastRebaseMs, rebaseCount = st.rebaseCount,
    rebaseDistance = rebase.distance, rebaseMin = rebase.MIN_DISTANCE, rebaseMax = rebase.MAX_DISTANCE,
    status = st.status,
    attribution = attributionText(fs),
  }
end

-- The on-screen credits before the server's first credits message (or from an older server): the
-- attribution line. Called every frame then, so no farfield.stats() table: its counters only.
local function creditsFallback() return attributionText(farfield.counters()) end

-- The imgui window, the on-screen credits (one text draw; the attribution line until the server
-- sends credits) and the connection banner (in our level or world only; hidden like the credits).
local function drawUi(dt)
  ui.draw(buildView(), { teleport = M.teleport, setRebaseDistance = M.setRebaseDistance })
  credits.draw(st.active, creditsFallback)
  updateBanner(dt)
  banner.draw(st.inOurLevel or st.active, credits.hiddenByUi())
end

local function onUpdate(dtReal, dtSim)
  dtReal = dtReal or 0
  st.clock = st.clock + dtReal
  -- the governor's frame opens first: its clock is the wall time since the last onUpdate (hptimer)
  gov:setLoading(st.placeFrames ~= nil or st.clock < st.loadingUntil or st.slowFor > SLOW_S)
  gov:beginFrame(farfield.hasWork() or roads.hasWork())
  for _, msg in ipairs(net.update(dtReal)) do
    local ok, err = pcall(onMessage, msg)
    if not ok then log('E', 'beamngEarth', 'message ' .. tostring(msg.type) .. ' failed: ' .. tostring(err)) end
  end
  if not scenetree.MissionGroup then return end -- no level loaded
  tickMotionBlur()

  st.paused = (dtSim or dtReal) <= 0
  local rb = st.rebasing
  if rb then
    -- phase 2 of a rebase is due once its vehicle move reaches this frame's physics step
    if not st.paused then
      rb.wait, rb.waited = rb.wait - 1, rb.waited + 1
    end
    if rb.wait <= 0 then
      finishRebase()
    else
      drawUi(dtReal)
      return -- positions are in neither frame for decisions this frame
    end
  end

  updatePlayer(dtReal)
  if st.active then
    updateWorld(dtReal)
    farfield.update(gov, (gov.w1 or dtReal * 1000) / 1000, st.cam, st.player)
    roads.update(gov, st.anchorId)
    updateSky(dtReal)
    updateGuard()
  end
  drawUi(dtReal)
end

-- After physics, before rendering (main.lua:819-824): measure when a rebase's vehicle move became
-- visible. Hook order relative to core_camera does not matter here; this only measures.
local function onPreRender(dtReal, dtSim)
  local probe = st.lagProbe
  if not probe then return end
  if moveVisible(probe) then
    st.lagProbe = nil
    if probe.frames ~= rebaseLag then
      log('W', 'beamngEarth', string.format('rebase: vehicle move became visible after %d frame(s), expected %d; '
        .. 'using %d from now on (this rebase may have shown one mismatched frame)', probe.frames, rebaseLag, probe.frames))
    else
      log('I', 'beamngEarth', 'rebase: vehicle move visible after ' .. probe.frames .. ' frame(s), as scheduled')
    end
    rebaseLag = math.min(probe.frames, 3)
    st.lastMoveLag = probe.frames
  elseif (dtSim or dtReal or 0) > 0 then
    probe.frames = probe.frames + 1
    if probe.frames > LAG_PROBE_FRAMES then
      st.lagProbe = nil
      log('W', 'beamngEarth', 'rebase: vehicle move not visible after ' .. LAG_PROBE_FRAMES .. ' frames')
    end
  end
end

-- The game's UI state (pause menu, loading screens): the credits hide in the pause menu.
local function onUiChangedState(toState, fromState)
  credits.setUiState(toState)
end

------------------------------------------------------------------------------------------
-- level lifecycle

local function onLevelStart()
  -- the connection outlives levels: keep its session and the request id sequence
  local sessionId, nextRequestId = st.sessionId, st.nextRequestId
  resetState()
  st.sessionId, st.nextRequestId = sessionId, nextRequestId
  farfield.forget()
  terrain.onLevelChanged()
  st.inOurLevel = getCurrentLevelIdentifier() == LEVEL_ID
  -- the window opens by itself only in our level; elsewhere use beamngEarth_core.toggleUI(). With
  -- the pause-menu settings page it is a compact HUD, hidden unless the settings show it.
  ui.visible = st.inOurLevel and not settings.hudHiddenAtStart()
  if not st.inOurLevel then return end

  activateWorld()
  -- Something to stand on until the first anchor arrives (our level has no GroundPlane).
  local ok, err = terrain.loadFlat()
  if ok then
    st.flat = true
    be:reloadCollision()
    st.placeFrames, st.placeZ = PLACE_DELAY_FRAMES, 1.0
  else
    setStatus('Flat terrain failed: ' .. tostring(err))
  end
  if net.isConnected() then
    st.autoTeleportDone = true
    startTeleport()
  end
end

local function onClientStartMission()
  onLevelStart()
end

local function onClientEndMission()
  -- runs before the level deletes its objects (server/server.lua:19 vs :34): delete the far tiles
  -- ourselves first, so their per-tile materials (not level objects) can go after them
  farfield.clear()
  horizon.restoreSky() -- M1h: the level's sky planet radius (its LevelInfo goes with the level)
  groundcover.reset() -- its objects go with the level
  roads.reset()
  local sessionId, nextRequestId = st.sessionId, st.nextRequestId
  resetState()
  st.sessionId, st.nextRequestId = sessionId, nextRequestId
end

local function onExtensionLoaded()
  log('I', 'beamngEarth', 'beamngEarth_core loaded')
  -- loaded while a level is already running (e.g. extensions.reload from the console)
  if scenetree.MissionGroup and getCurrentLevelIdentifier() then onLevelStart() end
end

local function onExtensionUnloaded()
  net.close()
  farfield.clear()
  roads.clear()
  terrain.delete()
  -- M1h: the level stays loaded; give it back its sky and view distance
  horizon.restoreSky()
  if st.visibleDistance and horizon.setVisibleDistance(VISIBLE_DISTANCE) then st.visibleDistance = nil end
end

------------------------------------------------------------------------------------------
-- public / console API

-- kind: 'teleport' (M.teleport) or 'start' (the level start's), for the recent places list; 'tool'
-- (M.teleport with opts.record == false: test tools like tools/benchmark.lua) is not recorded
teleportTo = function(lat, lon, name, kind)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon or lat < -90 or lat > 90 or lon < -180 or lon > 180 then
    setStatus('Teleport: invalid lat/lon')
    return false
  end
  ui.setLatLon(lat, lon)
  if requestAnchor(lat, lon, 'teleport') then
    setStatus(string.format('Preparing terrain at %.5f, %.5f ...', lat, lon))
    if kind ~= 'tool' then
      local ok, err = pcall(places.noteTeleport, lat, lon, name, kind)
      if not ok then log('W', 'beamngEarth', 'recent places: ' .. tostring(err)) end
    end
    return true
  end
  return false
end

-- name (optional): the place's name for the recent places (places.lua; the settings page's Go);
-- opts (optional): { record = false } keeps it out of the recent places (test tools)
function M.teleport(lat, lon, name, opts)
  local kind = (type(opts) == 'table' and opts.record == false) and 'tool' or 'teleport'
  return teleportTo(lat, lon, name, kind)
end

function M.toggleUI() ui.visible = not ui.visible end

-- The session's terrain block size (docs/V1_4_SPEC.md 3.5, M4): 1024 (v1.3.1) or 512, from the
-- server's hello.blockSize. Sets the lattice, the terrain-follow table, the importMaps layer / flat
-- maps and the rebase staleness rule together. Returns the size in use and whether `size` was valid.
function M.setBlockSize(size)
  local n, ok = lattice.configure(tonumber(size))
  follow.configure(n)
  terrain.configure(n)
  rebase.configure(n)
  blockSize = n
  return n, ok
end

-- Who draws the near ground: 'mesh' (far tiles; the TerrainBlock is physics-only, render disabled)
-- or 'terrain' (v1.2; anything else). Set by the server's hello; callable from the console to test.
function M.setNearVisual(mode)
  nearVisual = (mode == 'mesh') and 'mesh' or 'terrain'
  terrain.setRenderEnabled(nearVisual ~= 'mesh')
  farfield.setNearVisual(nearVisual)
  return nearVisual
end

-- Horizon mode (docs/V1_4_SPEC.md 3.6 M1h), from the server's hello.horizon: the view distance follows
-- the far-field radius (setFarRadius, from status.radius) and the sky uses the tiles' planet radius.
-- Off restores both. Returns the mode in use.
function M.setHorizon(on)
  horizonOn = on and true or false
  if not horizonOn then farRadius = nil end
  applyHorizon()
  return horizonOn
end

-- The server's far-field radius in metres (status.radius, horizon mode). Returns the view distance in use.
function M.setFarRadius(m)
  farRadius = tonumber(m)
  applyHorizon()
  return st.visibleDistance or VISIBLE_DISTANCE
end

-- Origin rebase distance in metres (2000-25000, default 10000). Returns the value in use.
function M.setRebaseDistance(m)
  local d = rebase.setDistance(m)
  log('I', 'beamngEarth', 'rebase distance ' .. d .. ' m')
  return d
end

-- Far-tile streaming budget per frame in ms (the governor's target, 1-4 ms; default 2). Returns the value in use.
function M.setFarBudget(ms)
  return gov:setTarget(ms)
end

function M.status()
  return dumps({ net = net.state(), lastError = net.lastError, session = st.sessionId, active = st.active,
    inOurLevel = st.inOurLevel, nearVisual = nearVisual, terrainRendered = terrain.isRenderEnabled(),
    anchorId = st.anchorId, anchorKey = st.anchorKey, pending = st.pending and st.pending.purpose,
    block = st.block, terrainPending = st.terrainPending and { st.terrainPending.cx, st.terrainPending.cy },
    rebaseDistance = rebase.distance, lastFollowMs = st.lastFollowMs, lastRebaseMs = st.lastRebaseMs,
    lastRebaseWaitFrames = st.lastRebaseWait, rebaseLag = rebaseLag, lastMoveLag = st.lastMoveLag, player = st.player, far = farfield.stats(), governor = gov:snapshot(), status = st.status,
    serverBacklog = st.serverBacklog, followCount = st.followCount, rebaseCount = st.rebaseCount,
    cells = crossover.cells(), blockSize = blockSize, anchorLatency = st.anchorLatency,
    terrainLatency = st.terrainLatency, horizon = horizonOn, farRadius = farRadius,
    visibleDistance = st.visibleDistance or VISIBLE_DISTANCE, banner = banner.current().id, guard = st.guard.mode,
    sessions = farfield.heldSessions(), tileSession = farfield.session() })
end

-- The connection state line (the settings page shows it): the banner's { id, level, text } (text
-- 'World server connected' while all is well) and the edge guard's mode.
function M.banner()
  local b = banner.current()
  return { id = b.id, level = b.level, text = banner.line(), guard = st.guard.mode }
end

-- net.close() drops its '_disconnected' event, so reset the connection state here.
function M.reconnect()
  net.close()
  onMessage({ type = '_disconnected', error = 'reconnect requested' })
end

-- The credits line in use (the settings page shows it).
function M.attribution() return attributionText(farfield.stats()) end

-- The on-screen data credits in use: the server's (near + far), else the attribution line.
function M.credits() return credits.text(creditsFallback) end

-- The download cache (server cache.py), for the settings page: the last usage the server sent (nil
-- before any: no server, or one without a cache), and the page's requests to the server: the size cap
-- in GB (0 = unlimited), a clear (answered with clearedBytes), fresh usage numbers. Each returns
-- whether it went out (false without a server session; a clear also needs the server's numbers, and
-- says why it did not go out).
function M.cacheState() return cacheInfo end
local function cacheReady() return net.isConnected() and st.sessionId ~= nil end
function M.cacheSetCap(gb)
  if not cacheReady() then return false end
  net.send({ type = 'cache', capGb = gb })
  return true
end
function M.cacheClear()
  if not cacheReady() then return false, 'server not connected' end
  -- a server without a cache sends no 'cache' after hello and would never answer the clear
  if not cacheInfo then return false, 'the server has no download cache' end
  net.send({ type = 'cache', clear = true })
  return true
end
function M.cacheQuery()
  if not cacheReady() then return false end
  net.send({ type = 'cache' })
  return true
end

function M._state() return st end -- read-only: the dev tools and the settings page
function M._rebaseLag() return rebaseLag end -- for tests
function M._governor() return gov end

-- The Roads setting: off removes them; on again asks the server to send the tiles once more.
function M.setRoadsEnabled(on)
  if roads.setEnabled(on) then net.send({ type = 'roads_reset' }) end
end

M.onUpdate = onUpdate
M.onPreRender = onPreRender
M.onUiChangedState = onUiChangedState
M.onClientStartMission = onClientStartMission
M.onClientEndMission = onClientEndMission
M.onExtensionLoaded = onExtensionLoaded
M.onExtensionUnloaded = onExtensionUnloaded

return M
