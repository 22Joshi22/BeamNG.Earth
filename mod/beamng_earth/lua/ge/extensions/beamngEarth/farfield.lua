-- Far field (protocol 4, docs/V1_3_SPEC.md B-E over docs/V1_2_DESIGN.md 2.3, 2.5, 2.7, 5.4, 6, 12):
-- render-only TSStatic tiles from server-written .cdae (or .dae) files, kept as a dynamic-LOD
-- quadtree by the server.
--
-- Messages (core.lua routes them here):
--   far_tile {anchorId, key, level, shape, pos = SW corner, err, sink?, state = 'group'|'prefetch',
--             material?, texture?, detail?, tex?}
--     A tile to load. It is created HIDDEN (probe P6: a hidden TSStatic pays its whole import at
--     registerObject, and unhiding later costs nothing), within the governor's budget. 'prefetch'
--     tiles load only when nothing else waits.
--     With `material` (protocol 4, satellite imagery) the tile's own Material (mapTo = material,
--     colorMap = texture, detail map on UV1 if `detail`) is created first, as its own governor unit,
--     and deleted with the last tile using it (satmat.lua). Without it (protocol 3 tiles,
--     `--imagery off`) the tile uses the static far-tile material. `tex` (FORMAT 4, docs/V1_4_SPEC.md
--     3.1 M1) is the texture's edge in px: the governor unit kind cdaeTex128/256/512 (governor.texKind).
--   far_swap {anchorId, id, add = [keys], remove = [keys], dIn?, dOut?}
--     One LOD change. It runs once every add is loaded, never earlier ("no removal before the
--     replacement is loaded", so never a hole), and groups that share a key run in arrival order.
--     It runs as a C2 vertical crossover (crossover.lua) whenever any of it is in view, at any
--     distance (v1.3 D: no "beyond 30 km instant" rule), else as an instant swap (C1). At the
--     transition cap a group in view within NEAR_POP_M waits for a free slot instead of popping.
--     dIn >= max(new - old), dOut >= max(old - new) are the server's measured amplitudes; without
--     them (or for a one-sided group) the bound 1.2 * (err_new + err_old) + 0.2 m is used.
--   far_remove {anchorId, key}  a prefetch eviction, or a previous anchor's tile after a rebase:
--     applied once every tile received before it is shown (v1.1 rule).
--   far_done {anchorId, count}  the desired set is sent; after a rebase, the previous anchor's tiles
--     go once everything received is shown.
-- Keys are opaque. A tile's footprint is [pos.x, pos.x + 256*2^level] x [pos.y, ...] (levels below
-- 0 are the finer near tiles of mesh mode: L-1 = 128 m, L-2 = 64 m).
--
-- Near visual (server hello.nearVisual, setNearVisual):
--   'terrain' (v1.2): the TerrainBlock is the visible near ground. The server keeps the 8 x 8 L0
--     tiles under the committed block loaded, and this module decides their state locally from the
--     block's [cx-1024, cx+1024]^2 ("covered", design 2.3):
--       edge ring (touching the block boundary): lowered by the server's `sink` (>= max(tile -
--         terrain) + 0.3 m), so it is under the terrain everywhere; it acts as an under-floor and
--         covers the 2 m N/E strip past the block's collidable width (lattice.lua);
--       interior: hidden.
--     onBlockCommit runs in the commit frame (the governor's heavy slot): newly uncovered tiles come
--     back at their true height at once; newly covered ones sink over 0.3 s (then hide if interior).
--   'mesh' (v1.3 B): the TerrainBlock is physics-only (render disabled, core.lua/terrain.lua) and the
--     tiles are the whole visible ground: nothing is ever covered, sunk or hidden for the block, and
--     terrain-follow commits are not gated on far tiles (uncoverReady is always true). Queued tiles
--     import nearest the camera first, and after a rebase a previous anchor's tile goes as soon as
--     settled current tiles cover it (peekImport, dropCoveredOld).
--
-- After an origin rebase every tile is moved into the new frame (R*T + t, exact doubles kept here,
-- never read back from float32 scene positions) and kept while the server swaps it out. If a
-- previous anchor's add was dropped (still loading at the switch, or arriving late), the far_removes
-- of that anchor are ignored instead (the dropped tile may be what covered their area) and its
-- tiles wait for far_done.
--
-- Ground under the player (docs/V1_4_SPEC.md 3.0 M0, the `exactMiss` metric of 3.4): every update
-- given the player's position finds the shown tile under it by key lookup (no loop over tiles) and
-- counts the frame as an exact miss when, in mesh mode, that tile is not the exact level (the
-- physics lattice: L-2 with 32-cell tiles; M1's 64 cells make it L-1: core.lua sets it from
-- hello.cells through setExactLevel(exactLevelFor(cells))), is in a
-- crossover more than 1 cm off its height, or there is none. A previous anchor's exact tile counts
-- as exact. stats() reports exactMiss / exactFrames / underLevel; exactState() is for the per-frame
-- driver (tools/drive_driver.lua).
--
-- Dev tools (dev.lua, docs/DEVTOOLS.md): setWatcher(w) makes this module call w.onAdd(inst) when a
-- tile's object is created, w.onRemove(id) when an object is deleted and w.onReset() when every
-- footprint moved (rebase) or the field was forgotten; eachTile / queuedTiles / counters are
-- read-only views; setFrozen(true) holds swap groups (inspection). Off by default: no watcher, not
-- frozen, so none of this does anything.

local M = {}

local geodesy = require('beamngEarth/geodesy')
local crossover = require('beamngEarth/crossover')
local lattice = require('beamngEarth/lattice')
local satmat = require('beamngEarth/satmat')
local governor = require('beamngEarth/governor')

local floor, min, max, abs, huge, ln = math.floor, math.min, math.max, math.abs, math.huge, math.log

M.MATERIAL = 'bngEarthFar_v3' -- the material name inside the server's shapes (design 6.2)
M.MATERIALS_FILE = '/levels/beamng_earth/art/shapes/beamngEarth/main.materials.json'
M.EDGE_BAND = 128       -- a covered tile nearer than this to the block boundary is in the edge ring
M.HIDE_DEPTH = 2.0      -- covered tile without a server `sink`: how far it sinks before it hides
M.DELETE_PRESSURE = 64  -- deletes wait for the import queue unless this many objects are doomed
M.MAX_DT = 0.1          -- s; longest step of the transition clock per frame
M.EXACT_LEVEL = -2      -- the exact (physics lattice) level of the 32-cell mesh layout (server MESH_MIN_LEVEL)
M.MAX_LEVEL = 9         -- coarsest level (server farfield.MAX_LEVEL); the horizon mode's L10 tiles (M1h)
                        -- are never under the player (they split within 328 km), so shownAt stops here
M.EXACT_OFF = 0.01      -- m; an exact tile further than this off its height (crossover) is a miss

local pending = {}      -- key -> instance received but not shown yet (current anchor)
local live = {}         -- key -> shown instance (current anchor), covered or not
local old = {}          -- 'anchorId/key' -> shown instance of a previous anchor
local oldCount = 0
local mainQ, mH, mT = {}, 1, 0 -- instances to import, FIFO (server order)
local preQ, pH, pT = {}, 1, 0  -- prefetch instances to import when mainQ is empty
local nQueued = 0       -- instances still to import
local groups = {}       -- far_swap groups in arrival order: { id, add, remove, state = 'wait'|'run'|'done', ... }
local activeGroups = 0  -- running crossovers
-- far_remove records { inst, seq, oldKey }, FIFO [rH, rT]. The tail is kept explicitly: once the
-- head entries are set to nil, #removals may return 0 (a border before them), not the tail.
local removals, rH, rT = {}, 1, 0
local doomed = {}       -- object ids to delete
local matOf = {}        -- doomed object id -> name of the per-tile material it holds (released at delete)
local doomedAnchor = {} -- doomed object id -> anchorId of its shape file (heldAnchors)
local moving = {}       -- instance -> true while its z offset changes over time
local lossy = {}        -- anchorId -> true: one of its adds was dropped, ignore its removes
local block = nil       -- committed terrain block { cx, cy, x0, y0, x1, y1 } (cover box)
local anchorId = nil    -- current (committed) anchor
local doneReceived = false
local seqN = 0          -- arrival counter of far_tile messages
local serial = 0        -- object name counter
local clock = 0         -- s, transition clock (advanced by update)
local materialChecked = false
local nearVisual = 'terrain' -- 'terrain' | 'mesh' (server hello; survives forget(): a server setting)
local counters = { imports = 0, failed = 0, crossovers = 0, instant = 0, pops = 0, textured = 0, materials = 0,
  capWaits = 0, oldCovered = 0, exactFrames = 0, exactMiss = 0 }
local exactLevel = M.EXACT_LEVEL
local under = { level = nil, old = false, miss = false } -- the tile under the player, last update
-- Previous anchors whose tiles may still be shown, newest first: { anchor, invs = inverse transforms
-- from the current frame back to that anchor's plane, applied in order } (the player lookup).
local oldFrames = {}
local lastFrame = { units = 0, moving = 0 }
local watcher = nil     -- dev tools observer (setWatcher), nil = none
local frozen = false    -- dev tools: swap groups wait (setFrozen)

local function oldKey(a, key) return tostring(a) .. '/' .. key end

local function deleteById(id)
  local o = scenetree.findObjectById(id)
  if o then o:delete() end
  if watcher then watcher.onRemove(id) end
end

local function getObj(inst) return inst.id and scenetree.findObjectById(inst.id) end

-- TSStatic visibility: setHidden as in levels/smallgrid/mainLevel.lua:27; the field otherwise.
local function setHidden(o, h)
  if o.setHidden then o:setHidden(h) else o.hidden = h end
end

-- Static far-tile material (design 6.2). Our level loads every *materials.json under its folder at
-- level load (lua/ge/server/server.lua:199-213), so this only matters in other levels (testing).
-- It must exist before the first tile: a Material with mapTo = the shape's material name overrides
-- the shape's own (probe P2).
local function ensureMaterial()
  if materialChecked then return end
  materialChecked = true
  if scenetree.findObject(M.MATERIAL) then return end
  if FS:fileExists(M.MATERIALS_FILE) then
    log('I', 'beamngEarth', 'loading far-tile material from ' .. M.MATERIALS_FILE)
    loadJsonMaterialsFile(M.MATERIALS_FILE)
  else
    log('W', 'beamngEarth', 'far-tile material file missing: ' .. M.MATERIALS_FILE)
  end
end

------------------------------------------------------------------------------------------
-- instances

local function newInst(msg, seq)
  local level = tonumber(msg.level) or 0
  local size = lattice.L0 * 2 ^ level
  local x, y, z = tonumber(msg.pos[1]) or 0, tonumber(msg.pos[2]) or 0, tonumber(msg.pos[3]) or 0
  return {
    key = tostring(msg.key), level = level, shape = msg.shape, seq = seq, anchor = msg.anchorId,
    x = x, y = y, z = z, qx = 0, qy = 0, qz = 0, qw = 1, rotated = false,
    x0 = x, y0 = y, x1 = x + size, y1 = y + size, -- footprint (AABB in the current frame)
    err = tonumber(msg.err) or crossover.defaultErr(level),
    sink = tonumber(msg.sink),
    prefetch = msg.state == 'prefetch',
    -- per-tile imagery material (protocol 4); nil = the static far material (protocol 3 tile)
    material = type(msg.material) == 'string' and msg.material ~= '' and msg.material or nil,
    texture = type(msg.texture) == 'string' and msg.texture or nil,
    detail = msg.detail == true,
    tex = tonumber(msg.tex), -- texture px (FORMAT 4); nil from a FORMAT 3 server
    mat = nil, matFailed = false, -- mat: the material name this instance holds a reference to
    id = nil, queued = true, loaded = false, failed = false, shown = false, doomed = false,
    cover = 'none', coverOff = 0, coverHide = false, coverAnim = nil, -- cover state and its target
    grp = nil,                                                       -- { g, role = 'add'|'remove' }
    off = 0, hidden = true,                                          -- what the object has now
  }
end

-- Governor unit kind of an import: a .cdae with its own imagery texture costs more (texture load,
-- ~3 ms in-game) than one on the shared static material (~1.9 ms); with FORMAT 4 the texture size
-- picks the kind (cdaeTex128/256/512, governor.texKind).
local function kindOf(inst)
  if not inst.shape:lower():match('%.cdae$') then return 'dae' end
  return inst.material and governor.texKind(inst.tex) or 'cdae'
end

-- Drop the instance's material reference (an object that never spawned, or failed to).
local function releaseMat(inst)
  if inst.mat then
    satmat.unref(inst.mat)
    inst.mat = nil
  end
end

local function writePos(o, inst, off)
  if inst.rotated then
    o:setPosRot(inst.x, inst.y, inst.z + off, inst.qx, inst.qy, inst.qz, inst.qw)
  else
    o:setPosition(vec3(inst.x, inst.y, inst.z + off))
  end
end

local function spawn(inst)
  ensureMaterial()
  if not FS:fileExists(inst.shape) then
    log('W', 'beamngEarth', 'far tile shape missing: ' .. inst.shape)
    inst.failed = true
    return
  end
  -- TSStatic setup as in probes/p5_tsstatic.lua / util/procTrack.lua:1278-1291. collisionType 'None'
  -- keeps it out of the static collision rebuild. Created hidden (P6), placed at its true height.
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, inst.shape)
  o:setField('collisionType', 0, 'None')
  o:setField('decalType', 0, 'None')
  o.canSave = false
  o.hidden = true
  o:setPosition(vec3(inst.x, inst.y, inst.z))
  serial = serial + 1
  o:registerObject('bngEarthFar_' .. serial .. '_' .. (inst.key:gsub('[^%w_]', '_')))
  scenetree.MissionGroup:addObject(o)
  inst.id, inst.loaded, inst.off, inst.hidden = o:getID(), true, 0, true
  counters.imports = counters.imports + 1
  if watcher then watcher.onAdd(inst) end
end

-- Cover state of a footprint against the committed block: 'none', 'edge' or 'interior'. In mesh
-- mode the block is invisible, so nothing is covered.
local function coverOf(inst)
  local b = block
  if not b or nearVisual == 'mesh' then return 'none' end
  local e = 0.01
  if inst.x0 < b.x0 - e or inst.y0 < b.y0 - e or inst.x1 > b.x1 + e or inst.y1 > b.y1 + e then return 'none' end
  local d = min(inst.x0 - b.x0, inst.y0 - b.y0, b.x1 - inst.x1, b.y1 - inst.y1)
  return d < M.EDGE_BAND and 'edge' or 'interior'
end

-- Target z offset and hidden flag for a cover state.
local function coverTarget(inst, cover)
  if cover == 'none' then return 0, false end
  if inst.sink then return -inst.sink, cover == 'interior' end
  return -M.HIDE_DEPTH, true -- no sink from the server: hide rather than risk poking through
end

-- Current cover offset (ends a finished cover animation).
local function coverOffNow(inst)
  local a = inst.coverAnim
  if not a then return inst.coverOff end
  local t = clock - a.t0
  if t >= a.dur then
    inst.coverAnim = nil
    return inst.coverOff
  end
  return crossover.ramp(a.from, inst.coverOff, t, a.dur)
end

local function groupOffNow(inst)
  local r = inst.grp
  if not r then return 0 end
  local t = clock - r.g.t0
  if r.role == 'add' then return crossover.addOffset(r.g.dIn, t) end
  return crossover.removeOffset(r.g.dOut, t)
end

-- Bring the object in line with the instance state: z offset, then visibility.
local function refresh(inst)
  if inst.doomed then return end
  local o = getObj(inst)
  if not o then return end
  local off = coverOffNow(inst) + groupOffNow(inst)
  local hide = (not inst.shown) or (inst.coverHide and not inst.coverAnim)
  if abs(off - inst.off) > 1e-4 then
    writePos(o, inst, off)
    inst.off = off
  end
  if hide ~= inst.hidden then
    setHidden(o, hide)
    inst.hidden = hide
  end
end

-- Take an instance out of the field; its object goes through the delete queue.
local function doom(inst, hideNow)
  if inst.doomed then return end
  if hideNow and inst.id and not inst.hidden then
    local o = getObj(inst)
    if o then setHidden(o, true) end
    inst.hidden = true
  end
  inst.doomed, inst.grp, inst.coverAnim = true, nil, nil
  moving[inst] = nil
  if inst.queued then inst.queued = false; nQueued = nQueued - 1 end
  if inst.id then
    doomed[#doomed + 1] = inst.id
    doomedAnchor[inst.id] = inst.anchor
    if inst.mat then matOf[inst.id], inst.mat = inst.mat, nil end -- released once the object is deleted
  else
    releaseMat(inst)
  end
end

-- Re-evaluate the cover state of a shown instance. animate: a tile that was visible and becomes
-- covered sinks over COVER_SINK_S (design 2.5 "terrain<->tile transitions use phase 2 only");
-- every other change is instant (a newly uncovered tile must be there in the commit frame).
local function applyCover(inst, animate)
  local c = coverOf(inst)
  if c == inst.cover then return false end
  local fromOff, wasVisible = coverOffNow(inst), (inst.cover == 'none' and not inst.hidden)
  inst.cover = c
  inst.coverOff, inst.coverHide = coverTarget(inst, c)
  if animate and wasVisible and c ~= 'none' then
    inst.coverAnim = { from = fromOff, t0 = clock, dur = crossover.COVER_SINK_S }
    moving[inst] = true
  else
    inst.coverAnim = nil
  end
  refresh(inst)
  return true
end

------------------------------------------------------------------------------------------
-- swap groups

local function minPendingSeq()
  local m = huge
  for _, inst in pairs(pending) do
    if not inst.prefetch and inst.seq < m then m = inst.seq end
  end
  return m
end

-- Every add loaded (or failed). An add never received makes the group wait (no hole); only once
-- the server said far_done and nothing is loading does such a group run without it (server error).
local function groupReady(g)
  for _, key in ipairs(g.add) do
    local a = pending[key]
    if a then
      if not a.loaded and not a.failed then return false end
    elseif not live[key] and not (doneReceived and nQueued == 0) then
      return false
    end
  end
  return true
end

-- In view (camera cone + margin, at any distance) and the nearest distance, over instances.
local function inView(list, cam, delta)
  if not cam then return true, 0 end
  local any, nearest = false, huge
  for _, inst in ipairs(list) do
    local cx, cy, cz, r = crossover.tileSphere(inst.x0, inst.y0, inst.x1, inst.y1, inst.z, delta)
    local vis, d = crossover.sphereInView(cam, cx, cy, cz, r)
    if d < nearest then nearest = d end
    if vis then any = true end
  end
  return any, nearest
end

-- Mesh mode, after a rebase: the previous anchor's tiles stay until the server far_removes them,
-- and a far_remove waits until every tile received before it is shown - often the whole reload
-- (~10 s). Near the player that is a second ground z-fighting with the new one (the old L-1 / L-2
-- surfaces are not the new physics lattice). So a previous anchor's tile that settled
-- current-anchor tiles (shown at their true height, not failed, not in a crossover) cover
-- completely goes at once. Safe: the current anchor's covered area never shrinks (every swap group
-- replaces its own footprint). Called with the tiles that just settled.
local function dropCoveredOld(adds)
  if oldCount == 0 or nearVisual ~= 'mesh' or #adds == 0 then return end
  local drop = {}
  for k, o in pairs(old) do
    local hit = false
    for _, a in ipairs(adds) do
      if a.x0 < o.x1 and o.x0 < a.x1 and a.y0 < o.y1 and o.y0 < a.y1 then hit = true; break end
    end
    if hit and not o.doomed and o.grp == nil then
      local area = 0
      for _, l in pairs(live) do
        if l.shown and not l.failed and l.grp == nil then
          local w = min(l.x1, o.x1) - max(l.x0, o.x0)
          local h = min(l.y1, o.y1) - max(l.y0, o.y0)
          if w > 0 and h > 0 then area = area + w * h end
        end
      end
      if area >= (o.x1 - o.x0) * (o.y1 - o.y0) * (1 - 1e-9) then drop[#drop + 1] = { k = k, o = o } end
    end
  end
  -- in arrival order (deterministic: the delete queue order does not depend on pairs())
  table.sort(drop, function(a, b) return a.o.seq < b.o.seq end)
  for _, d in ipairs(drop) do
    old[d.k] = nil
    oldCount = oldCount - 1
    doom(d.o, true)
    counters.oldCovered = counters.oldCovered + 1
  end
end

local function finishGroup(g)
  for _, a in ipairs(g.adds) do
    if a.grp and a.grp.g == g then
      a.grp = nil
      refresh(a)
      if not a.coverAnim then moving[a] = nil end
    end
  end
  for _, r in ipairs(g.rems) do doom(r, true) end
  if g.state == 'run' then activeGroups = activeGroups - 1 end
  g.state = 'done'
  dropCoveredOld(g.adds)
end

-- Start a ready group. Returns false (nothing changed) when it must wait: it would cross over in
-- view within NEAR_POP_M while MAX_GROUPS crossovers already run (v1.3 D: the cap stays, but a near
-- in-view swap waits a few frames for a free slot instead of popping).
local function startGroup(g, cam)
  -- 1. plan, without changing any state
  -- what goes: the live instances of the removed keys, and any live instance an add replaces
  local rems, seen = {}, {}
  for _, key in ipairs(g.remove) do
    local r = live[key]
    if r and not seen[r] then rems[#rems + 1], seen[r] = r, true end
  end
  local adds, unknown = {}, nil
  for _, key in ipairs(g.add) do
    local a = pending[key]
    if a then
      local cur = live[key]
      if cur and not seen[cur] then rems[#rems + 1], seen[cur] = cur, true end
      adds[#adds + 1] = a
    elseif not live[key] then
      unknown = key
    end
  end
  -- adds take their cover state at once; covered (or hidden) removes go at once (nothing to see)
  local visAdds, visRems, errN, errO = {}, {}, 0, 0
  for _, a in ipairs(adds) do
    if coverOf(a) == 'none' and not a.failed then
      visAdds[#visAdds + 1] = a
      errN = max(errN, a.err)
    end
  end
  for _, r in ipairs(rems) do
    if r.cover == 'none' and not r.hidden and r.grp == nil then
      visRems[#visRems + 1] = r
      errO = max(errO, r.err)
    end
  end
  local mode, dIn, dOut = 'instant', 0, 0
  if #visAdds > 0 or #visRems > 0 then
    local bound = crossover.delta(errN, errO)
    if #visAdds > 0 and #visRems > 0 and g.msgIn and g.msgOut then
      dIn, dOut = g.msgIn, g.msgOut
    else
      dIn, dOut = bound, bound
    end
    local all = {}
    for _, a in ipairs(visAdds) do all[#all + 1] = a end
    for _, r in ipairs(visRems) do all[#all + 1] = r end
    local vis, nearest = inView(all, cam, max(dIn, dOut))
    if #visRems == 0 and oldCount == 0 then
      mode = 'instant' -- nothing visible to cross over with (first fill)
    elseif not vis then
      mode = 'instant' -- C1: off-screen
    elseif activeGroups >= crossover.MAX_GROUPS then
      if nearest <= crossover.NEAR_POP_M then
        counters.capWaits = counters.capWaits + 1
        return false -- wait for a free slot (a crossover ends within 0.5 s)
      end
      mode = 'instant' -- over the cap and far: C1, a visible pop (stats; v1.3 D: every in-view swap should cross over)
      counters.pops = counters.pops + 1
    else
      mode = (#visRems == 0) and 'rise' or 'cross' -- rise: over a previous anchor's tiles
    end
  end

  -- 2. apply
  if unknown then log('W', 'beamngEarth', 'far_swap ' .. tostring(g.id) .. ': unknown tile ' .. unknown) end
  for _, a in ipairs(adds) do
    pending[a.key] = nil
    if a.queued then a.queued = false; nQueued = nQueued - 1 end -- failed before its import
    live[a.key] = a
    a.shown = true
    a.cover = coverOf(a)
    a.coverOff, a.coverHide = coverTarget(a, a.cover)
  end
  for _, r in ipairs(rems) do
    if live[r.key] == r then live[r.key] = nil end
  end
  local isVis = {}
  for _, r in ipairs(visRems) do isVis[r] = true end
  for _, r in ipairs(rems) do
    if not isVis[r] then doom(r, true) end
  end
  g.adds, g.rems, g.dIn, g.dOut = visAdds, visRems, dIn, dOut
  if mode == 'instant' then
    for _, a in ipairs(adds) do refresh(a) end
    for _, r in ipairs(visRems) do doom(r, true) end
    g.state = 'done'
    counters.instant = counters.instant + 1
    dropCoveredOld(visAdds)
    return true
  end
  g.t0, g.dur, g.mode, g.state = clock, crossover.duration(mode == 'cross'), mode, 'run'
  for _, a in ipairs(visAdds) do a.grp = { g = g, role = 'add' }; moving[a] = true end
  for _, r in ipairs(visRems) do r.grp = { g = g, role = 'remove' }; moving[r] = true end
  for _, a in ipairs(adds) do refresh(a) end -- unhidden at z - Delta: under the old surface
  activeGroups = activeGroups + 1
  counters.crossovers = counters.crossovers + 1
  return true
end

-- End every running transition at once (before a rebase transform or an anchor switch).
local function finishAll()
  for _, g in ipairs(groups) do
    if g.state == 'run' then finishGroup(g) end
  end
  for inst in pairs(moving) do
    inst.coverAnim = nil
    refresh(inst)
  end
  moving = {}
end

------------------------------------------------------------------------------------------
-- messages

function M.onTile(msg)
  if msg.anchorId ~= anchorId then -- late tile of a previous anchor (sent before our commit reached the server)
    if msg.anchorId ~= nil then lossy[msg.anchorId] = true end
    return
  end
  if type(msg.pos) ~= 'table' or type(msg.shape) ~= 'string' or msg.key == nil then
    log('W', 'beamngEarth', 'bad far_tile ignored: ' .. tostring(msg.key))
    return
  end
  local key = tostring(msg.key)
  local prev = pending[key]
  if prev then doom(prev, true) end -- re-sent before it was shown: the newer one wins
  seqN = seqN + 1
  local inst = newInst(msg, seqN)
  if inst.material then counters.textured = counters.textured + 1 end
  pending[key] = inst
  nQueued = nQueued + 1
  if inst.prefetch then
    pT = pT + 1; preQ[pT] = inst
  else
    mT = mT + 1; mainQ[mT] = inst
  end
end

function M.onSwap(msg)
  if msg.anchorId ~= anchorId then return end
  local g = { id = msg.id, add = {}, remove = {}, state = 'wait', msgIn = tonumber(msg.dIn), msgOut = tonumber(msg.dOut) }
  for _, k in ipairs(type(msg.add) == 'table' and msg.add or {}) do
    k = tostring(k)
    g.add[#g.add + 1] = k
    local p = pending[k]
    if p and p.prefetch then -- a prefetched tile is needed now: import it with the group's tiles
      p.prefetch = false
      if p.queued then mT = mT + 1; mainQ[mT] = p end
    end
  end
  for _, k in ipairs(type(msg.remove) == 'table' and msg.remove or {}) do g.remove[#g.remove + 1] = tostring(k) end
  groups[#groups + 1] = g
end

function M.onRemove(msg)
  local key = tostring(msg.key)
  if msg.anchorId ~= anchorId then
    -- a previous anchor's tile, swapped out now that its area is covered in the new frame
    local ok = oldKey(msg.anchorId, key)
    local inst = old[ok]
    if inst and not inst.removing and not lossy[msg.anchorId] then
      inst.removing = true
      rT = rT + 1; removals[rT] = { inst = inst, seq = seqN, oldKey = ok }
    end
    return
  end
  local p = pending[key]
  if p then -- prefetch eviction (or an add withdrawn before it was shown)
    pending[key] = nil
    doom(p, true)
    return
  end
  local l = live[key]
  if l and not l.removing then
    l.removing = true
    rT = rT + 1; removals[rT] = { inst = l, seq = seqN }
  end
end

function M.onDone(msg)
  if msg.anchorId == anchorId then doneReceived = true end
end

------------------------------------------------------------------------------------------
-- terrain block

local function setBlock(cx, cy)
  local x0, y0, x1, y1 = lattice.coverBox(cx, cy)
  block = { cx = cx, cy = cy, x0 = x0, y0 = y0, x1 = x1, y1 = y1 }
end

-- Near visual from the server's hello: 'mesh' (tiles are the whole visible ground, the block is
-- physics-only) or 'terrain' (v1.2). A change re-applies every shown tile's cover state at once
-- (mesh: covered tiles come back at their true height).
function M.setNearVisual(mode)
  mode = (mode == 'mesh') and 'mesh' or 'terrain'
  if mode == nearVisual then return end
  nearVisual = mode
  for _, inst in pairs(live) do applyCover(inst, false) end
  for _, inst in pairs(old) do applyCover(inst, false) end
end

function M.nearVisual() return nearVisual end

-- The terrain block moved (terrain-follow commit frame, after importMaps + setPosition). Covers and
-- uncovers far tiles; about 48 setPosition/setHidden calls for a 256 m move. Returns the changes.
function M.onBlockCommit(cx, cy)
  setBlock(cx, cy)
  local n = 0
  for _, inst in pairs(live) do if applyCover(inst, true) then n = n + 1 end end
  for _, inst in pairs(old) do if applyCover(inst, true) then n = n + 1 end end
  return n
end

-- Is every 256 m cell that a move of the block from (ocx, ocy) to (ncx, ncy) uncovers shown by a
-- loaded tile? (commit gating, design 2.3; the server streams these first.) Also false while a
-- coarser-than-L0 tile overlaps the new block: the server forces L0 under the pending block, and
-- until that split has run here, a coarse tile across the block's edge would stay shown at its
-- true height over the new terrain (coverOf only covers tiles fully inside the block), and one
-- inside it would be hidden (it has no `sink`), leaving the 2 m N/E strip uncovered.
function M.uncoverReady(ocx, ocy, ncx, ncy)
  if nearVisual == 'mesh' then return true end -- the block is invisible: a move uncovers nothing
  local ox0, oy0, ox1, oy1 = lattice.coverBox(ocx, ocy)
  local nx0, ny0, nx1, ny1 = lattice.coverBox(ncx, ncy)
  local L = lattice.L0
  local e = 0.01
  for _, inst in pairs(live) do
    if not inst.failed and inst.x0 < nx1 - e and nx0 + e < inst.x1 and inst.y0 < ny1 - e and ny0 + e < inst.y1
      and (inst.level > 0 or inst.x0 < nx0 - e or inst.y0 < ny0 - e or inst.x1 > nx1 + e or inst.y1 > ny1 + e) then
      return false
    end
  end
  local idx = {}
  for _, inst in pairs(live) do
    if inst.level == 0 and not inst.failed then idx[floor(inst.x0 / L + 0.5) .. ',' .. floor(inst.y0 / L + 0.5)] = true end
  end
  local function anyCovers(px, py, set)
    for _, inst in pairs(set) do
      if not inst.failed and not inst.doomed and px > inst.x0 and px < inst.x1 and py > inst.y0 and py < inst.y1 then
        return true
      end
    end
    return false
  end
  for gx = ox0, ox1 - 1, L do
    for gy = oy0, oy1 - 1, L do
      local px, py = gx + L / 2, gy + L / 2
      if not (px > nx0 and px < nx1 and py > ny0 and py < ny1)
        and not idx[floor(gx / L + 0.5) .. ',' .. floor(gy / L + 0.5)]
        and not anyCovers(px, py, live) and not anyCovers(px, py, old) then
        return false
      end
    end
  end
  return true
end

------------------------------------------------------------------------------------------
-- anchors

local function setTransform(inst)
  local o = getObj(inst)
  if o then o:setPosRot(inst.x, inst.y, inst.z + inst.off, inst.qx, inst.qy, inst.qz, inst.qw) end
end

-- Origin rebase: map every tile into the new frame, T_new = R*T_old + t (xf from
-- geodesy Anchor:transformTo). TSStatic:setPosRot(x,y,z, qx,qy,qz,qw) takes a BeamNG quaternion
-- (editor/tech/roadArchitect/staticMesh.lua:151, tech/techCore.lua handleTeleportScenarioObject).
-- The tile's own rotation is applied first, then R: q_new = q_old * Rq (BeamNG order).
-- Running transitions end first. Call before switchAnchor(..., true).
function M.transform(xf)
  finishAll()
  if xf.inverse then
    local inv = xf:inverse()
    for _, f in ipairs(oldFrames) do table.insert(f.invs, 1, inv) end
    table.insert(oldFrames, 1, { anchor = anchorId, invs = { inv } })
    oldFrames[5] = nil -- a tile older than four rebases is long gone
  end
  local bx, by, bz, bw = xf:beamngQuat()
  local function move(inst)
    if inst.doomed then return end
    local s = inst.x1 - inst.x0
    -- footprint: AABB of the moved corners (slightly larger than the turned square)
    local ax, ay = xf:apply(inst.x0, inst.y0, inst.z)
    local bx_, by_ = xf:apply(inst.x0 + s, inst.y0, inst.z)
    local cx, cy = xf:apply(inst.x0, inst.y0 + s, inst.z)
    local dx, dy = xf:apply(inst.x0 + s, inst.y0 + s, inst.z)
    inst.x0, inst.y0 = min(ax, bx_, cx, dx), min(ay, by_, cy, dy)
    inst.x1, inst.y1 = max(ax, bx_, cx, dx), max(ay, by_, cy, dy)
    inst.x, inst.y, inst.z = xf:apply(inst.x, inst.y, inst.z)
    inst.qx, inst.qy, inst.qz, inst.qw = geodesy.quatMul(inst.qx, inst.qy, inst.qz, inst.qw, bx, by, bz, bw)
    inst.rotated = true
    setTransform(inst)
  end
  for _, inst in pairs(live) do move(inst) end
  for _, inst in pairs(old) do move(inst) end
  if watcher then watcher.onReset() end
end

-- New committed anchor. keepOld=true (rebase): current tiles stay until the server removes them or
-- the new far_done. keepOld=false (teleport): everything is hidden at once and deleted progressively.
-- blk: the new anchor's terrain block {cx, cy}; the kept tiles' cover state follows it at once.
function M.switchAnchor(newAnchorId, keepOld, blk)
  finishAll()
  ensureMaterial()
  local keep = {}
  if keepOld then
    local due = minPendingSeq()
    -- removals whose adds are all shown stay valid in the new frame
    for i = rH, rT do
      local r = removals[i]
      if r.seq < due and not r.inst.doomed then
        keep[#keep + 1] = { inst = r.inst, seq = 0, oldKey = r.oldKey or oldKey(anchorId, r.inst.key) }
      end
    end
    local dropped = false
    for _, inst in pairs(pending) do
      if not inst.prefetch then dropped = true end
      doom(inst, true)
    end
    if dropped and anchorId ~= nil then lossy[anchorId] = true end
    for key, inst in pairs(live) do
      old[oldKey(anchorId, key)] = inst
      oldCount = oldCount + 1
    end
  else
    for _, set in ipairs({ live, old, pending }) do
      for _, inst in pairs(set) do doom(inst, true) end
    end
    old, oldCount, lossy, oldFrames = {}, 0, {}, {}
  end
  pending, live = {}, {}
  mainQ, mH, mT, preQ, pH, pT, nQueued = {}, 1, 0, {}, 1, 0, 0
  groups, activeGroups, moving = {}, 0, {}
  removals, rH, rT = keep, 1, #keep
  anchorId = newAnchorId
  doneReceived = false
  if blk then
    setBlock(blk.cx, blk.cy)
    for _, inst in pairs(old) do applyCover(inst, false) end
  end
end

------------------------------------------------------------------------------------------
-- per frame

-- The next tile to import: group tiles in the server's order, then prefetch tiles. Mesh mode
-- (cam given): the queued group tile nearest the camera instead. The near ground IS these tiles
-- there, and after a rebase (or a teleport) hundreds of tiles queue up faster than the budget
-- imports them (~0.7 textured tiles per frame): in server order the ground the car drives onto
-- would wait behind distant tiles for seconds (the old anchor's or a coarser tile showing
-- meanwhile). Terrain mode keeps the server's order (v1.2).
local function peekImport(cam)
  while mH <= mT do
    local inst = mainQ[mH]
    if pending[inst.key] == inst and inst.queued then break end
    mainQ[mH] = nil
    mH = mH + 1
  end
  if mH <= mT then
    if nearVisual ~= 'mesh' or not cam then return mainQ[mH], true end
    local best, bd = nil, huge
    for i = mH, mT do
      local inst = mainQ[i]
      if pending[inst.key] == inst and inst.queued then
        local dx = max(inst.x0 - cam.x, 0, cam.x - inst.x1)
        local dy = max(inst.y0 - cam.y, 0, cam.y - inst.y1)
        local d = dx * dx + dy * dy
        if d < bd then best, bd = inst, d end
      end
    end
    return best, true
  end
  while pH <= pT do
    local inst = preQ[pH]
    if pending[inst.key] == inst and inst.queued then return inst, false end
    preQ[pH] = nil
    pH = pH + 1
  end
end

local function hasWaitingGroups()
  for _, g in ipairs(groups) do if g.state == 'wait' then return true end end
  return false
end

-- Streaming work waiting (for the governor's budget ramp).
function M.hasWork()
  return nQueued > 0 or #doomed > 0 or rH <= rT or hasWaitingGroups() or satmat.hasDeletes()
end

local function pendingMainCount()
  local n = 0
  for _, inst in pairs(pending) do if not inst.prefetch then n = n + 1 end end
  return n
end

-- The shown tile containing (x, y) in `set` (live, or old with anchor a: keys 'a/L_i_j'), finest
-- level first, from the exact level up: at most MAX_LEVEL - exactLevel + 1 table lookups.
local function shownAt(set, a, x, y)
  for l = exactLevel, M.MAX_LEVEL do
    local s = lattice.L0 * 2 ^ l
    local k = l .. '_' .. floor(x / s) .. '_' .. floor(y / s)
    local inst = set[a ~= nil and oldKey(a, k) or k]
    if inst and inst.shown and not inst.failed and not inst.doomed then return inst end
  end
end

-- The ground under the player this frame (after this frame's reveals): see the module header.
local function updateUnder(player)
  local px, py, pz = tonumber(player.x), tonumber(player.y), tonumber(player.z) or 0
  if not px or not py or anchorId == nil then return end
  local inst, isOld = shownAt(live, nil, px, py), false
  if not inst then
    for _, f in ipairs(oldFrames) do
      local x, y, z = px, py, pz
      for _, inv in ipairs(f.invs) do x, y, z = inv:apply(x, y, z) end
      inst = shownAt(old, f.anchor, x, y)
      if inst then isOld = true; break end
    end
  end
  local miss = false
  if nearVisual == 'mesh' then -- terrain mode: the TerrainBlock itself is the visible near ground
    miss = not inst or inst.level ~= exactLevel or abs(inst.off) > M.EXACT_OFF
    counters.exactFrames = counters.exactFrames + 1
    if miss then counters.exactMiss = counters.exactMiss + 1 end
  end
  under.level, under.old, under.miss = inst and inst.level, isOld, miss
end

-- gov: governor instance (governor.lua); dt: s since the last frame (wall); cam: nil or
-- {x,y,z,fx,fy,fz,fovDeg} for the off-screen test; player: nil or {x,y,z} (the exactMiss metric).
function M.update(gov, dt, cam, player)
  clock = clock + min(max(dt or 0, 0), M.MAX_DT)
  local now = gov.clock
  local units = 0

  -- 1. motion: finish crossovers that are over, then move what moves (time based, always runs)
  local t0 = now()
  local nMoving = 0
  for _, g in ipairs(groups) do
    if g.state == 'run' and clock - g.t0 >= g.dur then finishGroup(g) end
  end
  for inst in pairs(moving) do
    refresh(inst)
    nMoving = nMoving + 1
    if not inst.grp and not inst.coverAnim then moving[inst] = nil end
  end
  gov:chargeMotion(now() - t0)

  -- 2. start swap groups whose adds are all loaded, in order; groups sharing a key wait for the
  --    earlier one to finish
  local busy = {}
  for _, g in ipairs(groups) do
    if g.state == 'run' then
      for _, k in ipairs(g.add) do busy[k] = true end
      for _, k in ipairs(g.remove) do busy[k] = true end
    end
  end
  local stop = false
  for _, g in ipairs(groups) do
    if g.state == 'wait' then
      local blocked = stop or frozen
      if not blocked then
        for _, k in ipairs(g.add) do if busy[k] then blocked = true; break end end
      end
      if not blocked then
        for _, k in ipairs(g.remove) do if busy[k] then blocked = true; break end end
      end
      if not blocked and groupReady(g) then
        if gov:try('reveal') then
          local t1 = now()
          if startGroup(g, cam) then
            gov:charge('reveal', now() - t1)
            units = units + 1
          end -- else: waits for a crossover slot; its keys stay busy, other groups go on
        else
          stop = true -- out of budget: keep the order, try again next frame
        end
      end
      if g.state ~= 'done' then
        for _, k in ipairs(g.add) do busy[k] = true end
        for _, k in ipairs(g.remove) do busy[k] = true end
      end
    end
  end
  local kept = {}
  for _, g in ipairs(groups) do if g.state ~= 'done' then kept[#kept + 1] = g end end
  groups = kept

  -- 3. far_removes whose earlier adds are all shown
  if rH <= rT then
    local due = minPendingSeq()
    while rH <= rT and removals[rH].seq < due do
      local r = removals[rH]
      if not r.inst.doomed then
        if not gov:try('reveal') then break end
        local t1 = now()
        if r.oldKey then
          if old[r.oldKey] == r.inst then old[r.oldKey] = nil; oldCount = oldCount - 1 end
        elseif live[r.inst.key] == r.inst then
          live[r.inst.key] = nil
        end
        doom(r.inst, true)
        gov:charge('reveal', now() - t1)
        units = units + 1
      end
      removals[rH] = nil
      rH = rH + 1
    end
    if rH > rT then removals, rH, rT = {}, 1, 0 end
  end

  -- 4. imports (hidden), group tiles first, prefetch only when nothing else waits. A tile with its
  --    own imagery material gets the Material first, as a separate unit (satmat.lua); a re-sent key
  --    or a sibling sharing the name re-uses it for free.
  local imported = false
  while true do
    local inst = peekImport(cam)
    if not inst then break end
    if inst.material and not inst.mat and not inst.matFailed then
      if not satmat.exists(inst.material) then
        if not gov:try('material') then break end
        local t1 = now()
        local ok, err = pcall(satmat.create, inst.material, inst.texture, inst.detail)
        if not ok then log('E', 'beamngEarth', 'far tile material failed: ' .. tostring(err)) end
        gov:charge('material', now() - t1)
        units = units + 1
        if satmat.exists(inst.material) then counters.materials = counters.materials + 1 end
      end
      if satmat.ref(inst.material, inst.texture) then
        inst.mat = inst.material
      else
        inst.matFailed = true -- spawn anyway: better a tile without imagery than a hole
      end
    else
      local kind = kindOf(inst)
      if not gov:try(kind) then break end
      local t1 = now()
      local ok, err = pcall(spawn, inst)
      if not ok then
        inst.failed = true
        log('E', 'beamngEarth', 'far tile spawn failed: ' .. tostring(err))
      end
      if inst.failed then
        counters.failed = counters.failed + 1
        releaseMat(inst)
      end
      inst.queued = false
      nQueued = nQueued - 1
      inst.importMs = now() - t1 -- shown by the dev tools' mini-map
      gov:charge(kind, inst.importMs)
      units = units + 1
      imported = true
    end
  end

  -- 5. after a rebase: once far_done came and everything received is shown, the previous anchor's
  --    tiles go (progressively, through the delete queue)
  if doneReceived and oldCount > 0 and #groups == 0 and pendingMainCount() == 0 then
    for _, inst in pairs(old) do doom(inst, false) end
    old, oldCount, oldFrames = {}, 0, {}
  end

  -- 6. deletes; then the materials no object uses any more (never before their last object).
  --    Not in a frame that imported a tile (unless under pressure): a textured import (~3 ms) plus
  --    its material and a few deletes would make a frame more than 4 ms long.
  if (nQueued == 0 and not imported) or #doomed > M.DELETE_PRESSURE then
    while #doomed > 0 and gov:try('delete') do
      local t1 = now()
      local id = table.remove(doomed)
      deleteById(id)
      doomedAnchor[id] = nil
      local mat = matOf[id]
      if mat then
        matOf[id] = nil
        satmat.unref(mat)
      end
      gov:charge('delete', now() - t1)
      units = units + 1
    end
    while satmat.hasDeletes() and gov:try('matDelete') do
      local t1 = now()
      satmat.deleteNext()
      gov:charge('matDelete', now() - t1)
      units = units + 1
    end
  end
  lastFrame.units, lastFrame.moving = units, nMoving
  if player then updateUnder(player) end
end

-- The tile level under the player at the last update (nil: none), whether it is a previous
-- anchor's tile, and whether that frame was an exact miss (mesh mode).
function M.exactState() return under.level, under.old, under.miss end

-- The exact (physics lattice) level of a tile grid: the level whose tile is `cells` lattice squares
-- wide (32 cells: L-2 = 64 m; 64 cells: L-1 = 128 m).
function M.exactLevelFor(cells)
  cells = tonumber(cells) or 32
  return floor(ln(cells * lattice.SQUARE / lattice.L0) / ln(2) + 0.5)
end

-- The exact (physics lattice) level: -2 for the v1.3.1 32-cell layout (default), -1 for 64 cells (M1).
function M.setExactLevel(level)
  exactLevel = tonumber(level) or M.EXACT_LEVEL
  return exactLevel
end

-- Delete everything right away (extension unload / level end).
function M.clear()
  finishAll() -- tiles sinking in a crossover are in no set but the delete queue afterwards
  for _, set in ipairs({ live, old, pending }) do
    for _, inst in pairs(set) do if inst.id then deleteById(inst.id) end end
  end
  for _, id in ipairs(doomed) do deleteById(id) end
  M.forget() -- deletes the per-tile materials, now that no tile uses them
end

-- Forget object ids without touching the scene (the level already deleted them). Per-tile
-- materials are not level objects: they are deleted here (no tile uses them any more).
function M.forget()
  pending, live, old, oldCount, lossy, doomed, moving = {}, {}, {}, 0, {}, {}, {}
  oldFrames, under.level, under.old, under.miss = {}, nil, false, false
  mainQ, mH, mT, preQ, pH, pT, nQueued = {}, 1, 0, {}, 1, 0, 0
  groups, activeGroups, removals, rH, rT = {}, 0, {}, 1, 0
  anchorId, doneReceived, block = nil, false, nil
  matOf, doomedAnchor = {}, {}
  satmat.clearAll()
  materialChecked = false
  if watcher then watcher.onReset() end
end

-- Previous anchors whose tile objects still exist here (shown as old tiles, or waiting in the
-- delete queue), sorted. Sent in every pose: the server keeps those anchors' shape and texture
-- files until they are gone. A file deleted under a live TSStatic makes the engine's file watcher
-- reload it ("Could not open '...far_<a>_....cdae'", seen in-game after a rebase whose reload
-- outlasted the server's fixed 30 s prune delay).
function M.heldAnchors()
  local set = {}
  for _, inst in pairs(old) do if inst.anchor ~= nil and inst.anchor ~= anchorId then set[inst.anchor] = true end end
  for _, a in pairs(doomedAnchor) do if a ~= anchorId then set[a] = true end end
  local out = {}
  for a in pairs(set) do out[#out + 1] = a end
  table.sort(out)
  return out
end

function M.stats()
  local n, covered, hiddenLoaded = 0, 0, 0
  for _, inst in pairs(live) do
    n = n + 1
    if inst.cover ~= 'none' then covered = covered + 1 end
  end
  for _, inst in pairs(pending) do if inst.loaded then hiddenLoaded = hiddenLoaded + 1 end end
  local waiting = 0
  for _, g in ipairs(groups) do if g.state == 'wait' then waiting = waiting + 1 end end
  return { live = n, covered = covered, preloaded = hiddenLoaded, queued = nQueued, groups = waiting,
    transitions = activeGroups, old = oldCount, deleting = #doomed, done = doneReceived,
    imports = counters.imports, failed = counters.failed, crossovers = counters.crossovers,
    instant = counters.instant, pops = counters.pops, capWaits = counters.capWaits, frameUnits = lastFrame.units,
    moving = lastFrame.moving, materials = satmat.count(), materialsCreated = counters.materials,
    textured = counters.textured, oldCovered = counters.oldCovered, nearVisual = nearVisual,
    waitingGroups = waiting, exactLevel = exactLevel, underLevel = under.level, underOld = under.old,
    exactMiss = counters.exactMiss, exactFrames = counters.exactFrames }
end

------------------------------------------------------------------------------------------
-- dev tools (dev.lua): see the module header

function M.setWatcher(w) watcher = w end
function M.setFrozen(on) frozen = on and true or false; return frozen end
function M.isFrozen() return frozen end
function M.anchorId() return anchorId end
function M.counters() return counters end -- read-only

-- Every tile instance with an object (pending, live and previous anchors'; not the delete queue).
function M.eachTile(fn)
  for _, set in ipairs({ pending, live, old }) do
    for _, inst in pairs(set) do if inst.id and not inst.doomed then fn(inst) end end
  end
end

-- The instances still to import, in queue order: group tiles, then prefetch tiles (peekImport
-- picks among the group tiles, nearest the camera first in mesh mode).
function M.queuedTiles()
  local main, pre = {}, {}
  for i = mH, mT do
    local inst = mainQ[i]
    if inst and pending[inst.key] == inst and inst.queued and not inst.prefetch then main[#main + 1] = inst end
  end
  for i = pH, pT do
    local inst = preQ[i]
    if inst and pending[inst.key] == inst and inst.queued and inst.prefetch then pre[#pre + 1] = inst end
  end
  return main, pre
end

-- for tests
function M._tile(key) return live[key] end
function M._pending(key) return pending[key] end
function M._oldId(a, key) local inst = old[oldKey(a, key)]; return inst and inst.id end
function M._old(a, key) return old[oldKey(a, key)] end
function M._clock() return clock end

return M
