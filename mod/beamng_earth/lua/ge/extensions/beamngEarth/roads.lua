-- Roads near the player (docs/ROADMAP.md v1.6 "Roads", server/roads.py): the server announces road
-- tiles (`roads_tile`: a JSON file of chunks in the anchor frame) and drops them (`roads_drop`); each
-- chunk becomes one DecalRoad drawn on the physics ground (engine probes P1-P3, 2026-10-11: they
-- render on the hidden block, re-clip by themselves when it is re-imported or moved, and cost
-- ~0.45 ms per 100-node road with insertNodeNoRegen + regenerate). Creation and deletion run under the
-- frame governor (kinds roadChunk / roadDelete). A new anchor makes every road of the old one go
-- (the server sends the new frame's tiles again).
--
-- Materials are stock ones from official levels, loaded at runtime from the player's own install
-- (nothing copied): West Coast USA's asphalt strip, sidewalks and lines, Johnson Valley's dirt road
-- tracks, Italy's cobblestone.
--
-- Raised sidewalks (curbs, sidewalk slabs): one mesh per tile (server/sidewalks.py, the way West
-- Coast USA builds its sidewalks: one modelled mesh per district, with its `curbs` and `sidewalk1`
-- materials), announced as the tile's "walk" {shape, pos} and shown as a TSStatic that collides with
-- its visible mesh. A TSStatic only collides after be:reloadCollision() (asynchronous: in effect a
-- frame later; ~1.8 ms), so that runs once per RELOAD_EVERY_S at most while sidewalks come and go.
-- (First cut, replaced: a MeshRoad per sidewalk piece, 3 draw calls each and no curb profile.)
-- Storm drains are DecalRoad chunks like the rest (Driver Training's m_metal_grid_drain).
-- Console: beamngEarth_roads.status(), .setEnabled(bool)

local stopwatch = require('beamngEarth/stopwatch')

local M = {}

M.MATERIAL_FILES = { '/levels/west_coast_usa/art/road/main.materials.json',
  '/levels/west_coast_usa/art/shapes/roads/main.materials.json',   -- curbs, sidewalk1 (the sidewalk mesh)
  '/levels/johnson_valley/art/decalroad/main.materials.json',
  '/levels/italy/art/shapes/buildings/main.materials.json',        -- italy_cobblestone (paving stones)
  '/levels/driver_training/art/road/main.materials.json',          -- m_metal_grid_drain (storm drains)
  '/levels/beamng_earth/art/shapes/beamngEarth/buildings.materials.json' }  -- buildings (server/buildings.py)
M.FADE = '450 150'          -- distanceFade: start, range (m)
M.enabled = true
-- be:reloadCollision() at most this often: every rebuild is a ~20 ms physics frame (2026-10-13, the
-- engine's frame metrics), and while driving new sidewalk tiles arrive all the time. They arrive ~640 m
-- ahead (11 s at 58 m/s), so a few seconds before they collide do not matter. A terrain commit rebuilds
-- collision anyway: M.collisionRebuilt() clears the pending rebuild.
-- 2026-10-14: a rebuild had grown to ~56 ms (1.1 M collision vertices with the buildings) and ran every 3 s
-- while driving, most of the hitches of a 34 mph drive: new colliders now wait up to 6 s (still 11 s of margin
-- at 130 mph), and colliders deleted far behind the car (their stale collision harms nobody) up to 30 s.
M.RELOAD_EVERY_S = 6
-- Collision only near the car (2026-10-15: the rebuild cost scales with every colliding triangle loaded, ~40 ms in
-- Fells Point with all 36 tiles' buildings and sidewalks): a tile's colliders (sidewalks, decks, the buildings'
-- lean collision mesh "bldcol") are made once its centre is within COLLIDE_LEAD_S of driving (COLLIDE_M min / max)
-- and dropped again UNCOLLIDE_M beyond that. The drawn building meshes only render when the tile has a bldcol.
M.TILE_M = 256
M.COLLIDE_M = { 320, 900 }
M.COLLIDE_LEAD_S = 14
M.UNCOLLIDE_M = 350
M.COLLIDE_CHECK_S = 0.25
M.RELOAD_DELETES_S = 30
M.DELETE_NEAR_M = 200       -- a collider deleted this close to the player counts as new (no ghost walls)

local newColliders = {}    -- { id, walkQ entry } of the colliders made since the last reloadCollision
local parked = {}          -- walkQ entries of colliders whose tile is not near enough yet
local sinceCollideCheck = stopwatch.new()
local held = {}            -- walkQ entries of colliding meshes held while the player is in their boxes
local noteNewColliders, playerIn, releaseHeld
local tiles = {}            -- key "a:tx_ty" -> { anchor, objs = { DecalRoad ids }, queued = n, walk = TSStatic id,
                            --   road = TSStatic id }
local walkQ = {}            -- { key, walk = {shape, pos}, what = 'walk' | 'road' } FIFO
local createQ = {}          -- { key, chunk } FIFO
local deleteQ = {}          -- object ids
local loadedMaterials = false
local anchorId = nil
local far = {}              -- far buildings (server buildings.far_tile): key "a:tx_ty" -> { anchor, id, shape, pos, hidden }
local farQ = {}             -- keys of far tiles to make
local lastError = nil
local sinceChange = stopwatch.new()  -- ms since a tile arrived / went or a road was made / deleted
local sinceReload = stopwatch.new()
local collisionDirty = false
local deletesDirty = false  -- only far colliders deleted since the last rebuild (RELOAD_DELETES_S)

-- Every road object lives in one SimGroup. A Lua reload (Lua:requestReload) rebuilds the VM without
-- unloading the extensions, so nothing deletes the old objects: 2026-10-11, after a few reloads, 3,558
-- DecalRoads and 120 sidewalk MeshRoads were in the scene for 1,011 + 51 tracked (the user saw fps drop).
-- The group is emptied when this module loads (see the end of the file).
M.GROUP = 'bngEarthRoads'

local function group()
  local g = scenetree.findObject(M.GROUP)
  if not g then
    g = createObject('SimGroup')
    g.canSave = false
    g:registerObject(M.GROUP)
    scenetree.MissionGroup:addObject(g)
  end
  return g
end

-- Objects of a previous Lua state deleted. Returns how many.
function M.sweep()
  local g = scenetree.MissionGroup and scenetree.findObject(M.GROUP)
  if not g then return 0 end
  local n = g:getCount()
  if n > 0 then
    g:deleteAllObjects()
    be:reloadCollision()
  end
  return n
end

-- The road surfaces drawn opaque (2026-10-14): the stock decal materials are all translucent, and inside
-- one road mesh the markings showed only while the engine happened to draw them after the asphalt; with
-- many more translucent objects in view (buildings' windows, signals) they vanished. Opaque asphalt is
-- drawn first, always, and the translucent lines, crossings and gutters over it.
M.OPAQUE = { 'road_asphalt_2lane', 'italy_cobblestone' }

local function loadMaterials()
  if loadedMaterials then return end
  for _, f in ipairs(M.MATERIAL_FILES) do loadJsonMaterialsFile(f) end
  for _, name in ipairs(M.OPAQUE) do
    local m = scenetree.findObject(name)
    if m then
      pcall(function() m:setField('translucent', 0, '0'); m:flush(); m:reload() end)
    end
  end
  local pad = scenetree.findObject('tactile_indicator')    -- curb ramp pads: US truncated domes are yellow
  if pad then pcall(function() pad:setField('baseColorFactor', 0, '0.95 0.78 0.18 1'); pad:flush(); pad:reload() end) end
  loadedMaterials = true
end

local function tileKey(aid, k) return tostring(aid) .. ':' .. tostring(k) end

-- Pooled shape files (server/matslots.py, 2026-10-16: the engine's frame cost grows with every distinct shape file it
-- ever loaded): roads_tile / bldfar_tile carry `slots` {what = {path, serial}}, shape files the server re-uses for
-- later tiles once we report [slot, serial] gone (core.lua pose.slotsGone). A slot goes when its tile is dropped AND
-- its last object is deleted (a collider deleted and made again while the tile lives keeps it).
local session = nil        -- the server session the slots belong to (setSession)
local objSlot = {}         -- object id -> slot number of the pooled file it loaded
local slotLive = {}        -- slot number -> objects alive with it
local slotDropped = {}     -- slot number -> { serial, session } of a dropped tile's slot, reported at its last delete
local slotsGone = {}       -- { n, serial, session } to report
local function slotOf(path) return tonumber(type(path) == 'string' and path:match('/slot_(%d+)%.cdae$') or nil) end

function M.setSession(s) session = s end

-- An object with a pooled shape was made / is deleted.
local function slotMade(id, path)
  local n = slotOf(path)
  if not n then return end
  objSlot[id] = n
  slotLive[n] = (slotLive[n] or 0) + 1
end
local function slotObjGone(id)
  local n = objSlot[id]
  if not n then return end
  objSlot[id] = nil
  slotLive[n] = (slotLive[n] or 1) - 1
  if slotLive[n] <= 0 then
    slotLive[n] = nil
    local d = slotDropped[n]
    if d then slotDropped[n] = nil; slotsGone[#slotsGone + 1] = { n, d[1], d[2] } end
  end
end
-- A tile's slot { n, serial, session } is let go: at once if no object has it, else at its last delete.
local function slotDrop(sl)
  if not sl then return end
  if (slotLive[sl[1]] or 0) > 0 then slotDropped[sl[1]] = { sl[2], sl[3] }
  else slotsGone[#slotsGone + 1] = { sl[1], sl[2], sl[3] } end
end
-- The slots of msg.slots[what] as { n, serial, session } (nil: not pooled).
local function msgSlot(msg, what)
  local e = type(msg.slots) == 'table' and msg.slots[what]
  if type(e) ~= 'table' then return nil end
  local n, serial = slotOf(e[1]), tonumber(e[2])
  if not n or not serial then return nil end
  return { n, serial, session, path = e[1] }
end

-- The [slot, serial] pairs gone since the last call, for server session `sess`.
function M.takeSlotsGone(sess)
  if #slotsGone == 0 then return {} end
  local out = {}
  for _, g in ipairs(slotsGone) do if g[3] ~= nil and g[3] == sess then out[#out + 1] = { g[1], g[2] } end end
  slotsGone = {}
  return out
end

-- Every [slot, serial] held for session `sess` (hello after a reconnect): live tiles' slots and dropped ones whose
-- objects are not deleted yet.
function M.slotsHeld(sess)
  local out = {}
  for _, t in pairs(tiles) do
    for _, sl in pairs(t.slots or {}) do if sl[3] == sess then out[#out + 1] = { sl[1], sl[2] } end end
  end
  for _, f in pairs(far) do if f.slot and f.slot[3] == sess then out[#out + 1] = { f.slot[1], f.slot[2] } end end
  for n, d in pairs(slotDropped) do if d[2] == sess then out[#out + 1] = { n, d[1] } end end
  return out
end

local function now() return os.clock() * 1000 end

local function dropTile(key)
  local t = tiles[key]
  if not t then return end
  for _, sl in pairs(t.slots or {}) do slotDrop(sl) end
  for _, id in ipairs(t.objs) do deleteQ[#deleteQ + 1] = id end
  if t.walk then deleteQ[#deleteQ + 1] = t.walk end
  if t.road then deleteQ[#deleteQ + 1] = t.road end
  if t.deck then deleteQ[#deleteQ + 1] = t.deck end
  for _, w in ipairs({ 'bld', 'bld2', 'bld3', 'bld4', 'bldcol', 'walkcol', 'tunnel' }) do if t[w] then deleteQ[#deleteQ + 1] = t[w] end end
  if t.wire then deleteQ[#deleteQ + 1] = t.wire end
  local keep = {}
  for _, q in ipairs(parked) do if q.key ~= key then keep[#keep + 1] = q end end
  parked = keep
  if t.props then require('beamngEarth/trees').dropKey((key:gsub(':', ':p', 1))) end
  if t.park then require('beamngEarth/parking').dropKey(key) end
  tiles[key] = nil
end

-- Every road not of anchor aid goes (a new anchor: the server re-sends its tiles).
local function switchAnchor(aid)
  if aid == anchorId then return end
  anchorId = aid
  for key, t in pairs(tiles) do
    if t.anchor ~= aid and not t.ghost then dropTile(key) end
  end
  for key, f in pairs(far) do
    if f.anchor ~= aid and not f.ghost then
      if f.id then deleteQ[#deleteQ + 1] = f.id end
      slotDrop(f.slot)
      far[key] = nil
    end
  end
  local keep = {}
  for _, q in ipairs(createQ) do if tiles[q.key] then keep[#keep + 1] = q end end
  createQ = keep
  keep = {}
  for _, q in ipairs(walkQ) do if tiles[q.key] then keep[#keep + 1] = q end end
  walkQ = keep
end

-- Pure: the DecalRoad fields of a chunk (name, value), in order.
function M.fieldsFor(chunk)
  return {
    { 'material', chunk.m }, { 'textureLength', tostring(chunk.tl or 10) },
    { 'renderPriority', tostring(chunk.p or 20) }, { 'drivability', '-1' }, { 'improvedSpline', 'true' },
    { 'overObjects', 'false' }, { 'distanceFade', M.FADE }, { 'decalBias', '0.1' },
  }
end

-- The collision radius around the player (m): COLLIDE_LEAD_S of driving at its speed, within COLLIDE_M.
function M.collideRadius(speed)
  return math.max(M.COLLIDE_M[1], math.min(M.COLLIDE_M[2], (speed or 0) * M.COLLIDE_LEAD_S))
end

local lastRadius = M.COLLIDE_M[1]
-- Is tile t's centre within the collision radius of the player? (no car or no centre: yes)
function M.collideNear(t, extra)
  if not t or not t.cx then return true end
  return M.nearPlayer(vec3(t.cx, t.cy, 0), lastRadius + (extra or 0) + M.TILE_M * 0.71)
end

-- Every COLLIDE_CHECK_S: parked colliders whose tile came near queued; far tiles' colliders dropped.
local function updateCollideRange()
  if sinceCollideCheck() < M.COLLIDE_CHECK_S * 1000 then return end
  sinceCollideCheck = stopwatch.new()
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  local v = veh and veh.getVelocity and veh:getVelocity()
  lastRadius = M.collideRadius(v and v:length() or 0)
  if #parked > 0 then
    local keep = {}
    for _, q in ipairs(parked) do
      local t = tiles[q.key]
      if t then
        if M.collideNear(t) then walkQ[#walkQ + 1] = q else keep[#keep + 1] = q end
      end
    end
    parked = keep
  end
  for key, t in pairs(tiles) do
    if t.cq and not M.collideNear(t, M.UNCOLLIDE_M) then
      for what, q in pairs(t.cq) do
        if t[what] then deleteQ[#deleteQ + 1] = t[what]; t[what] = nil end
        if not t.ghost then parked[#parked + 1] = q end   -- a ghost's queue entries are the old frame's
      end
      t.cq = nil
    end
  end
end

local function isCollider(o)
  local c = o.class or (o.getClassName and o:getClassName())
  if c == 'MeshRoad' then return true end
  return c == 'TSStatic' and o:getField('collisionType', 0) ~= 'None'
end

-- collide: true ('Visible Mesh'), a collision type name, or false / nil (render only)
local function createWalk(walk, collide)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, walk.shape)
  o:setField('collisionType', 0, type(collide) == 'string' and collide or (collide and 'Visible Mesh' or 'None'))
  o:setField('decalType', 0, 'None')
  o.canSave = false
  o:setPosition(vec3(walk.pos[1], walk.pos[2], walk.pos[3]))
  o:registerObject('')
  group():addObject(o)
  return o:getID()
end

local function createChunk(chunk)
  local r = createObject('DecalRoad')
  for _, f in ipairs(M.fieldsFor(chunk)) do r:setField(f[1], 0, f[2]) end
  r.canSave = false
  r:registerObject('')
  group():addObject(r)
  for _, n in ipairs(chunk.n) do r:insertNodeNoRegen(vec3(n[1], n[2], n[3]), chunk.w, -1) end
  r:regenerate()
  return r:getId()
end

-- Server message `roads_tile` {anchorId, key, path, n}.
function M.onTile(msg, currentAnchor)
  if not M.enabled then return false end
  switchAnchor(currentAnchor)
  if msg.anchorId ~= currentAnchor then return false end
  local key = tileKey(msg.anchorId, msg.key)
  if tiles[key] then dropTile(key) end
  local text = readFile(msg.path)
  local ok, data = pcall(jsonDecode, text or '')
  if not ok or type(data) ~= 'table' or type(data.chunks) ~= 'table' then
    lastError = 'unreadable road tile ' .. tostring(msg.path)
    return false
  end
  -- pooled shape files: the server's slot paths instead of the tile's own files
  local slots = {}
  for _, what in ipairs({ 'road', 'deck', 'walk', 'walkcol', 'bld', 'bld2', 'bld3', 'bld4', 'bldcol', 'wire', 'tunnel' }) do
    local sl = msgSlot(msg, what)
    if sl then
      if type(data[what]) == 'table' and type(data[what].pos) == 'table' then
        data[what] = { shape = sl.path, pos = data[what].pos }
        slots[what] = sl
      else
        slotDrop(sl)                -- nothing will load it
      end
    end
  end
  local road = type(data.road) == 'table' and data.road.shape and type(data.road.pos) == 'table' and data.road
  tiles[key] = { anchor = msg.anchorId, objs = {}, queued = road and 0 or #data.chunks,
    colliders = type(data.colliders) == 'table' and data.colliders or {}, slots = slots }
  sinceChange = stopwatch.new()
  if road then                -- the tile's road mesh replaces its DecalRoads (server/roadmesh.py)
    walkQ[#walkQ + 1] = { key = key, walk = road, what = 'road' }
  else
    for _, c in ipairs(data.chunks) do createQ[#createQ + 1] = { key = key, chunk = c } end
  end
  if type(data.props) == 'table' and type(data.props.items) == 'table' then   -- street lights, poles (trees.lua)
    require('beamngEarth/trees').onRows(msg.anchorId, 'p' .. tostring(msg.key), data.props.items, data.props.groups)
    tiles[key].props = true
  end
  if type(data.park) == 'table' and data.park[1] then      -- curbside parking spots (parking.lua)
    require('beamngEarth/parking').onRows(msg.anchorId, key, data.park)
    tiles[key].park = true
  end
  if type(data.wire) == 'table' and data.wire.shape and type(data.wire.pos) == 'table' then    -- pole wires
    walkQ[#walkQ + 1] = { key = key, walk = data.wire, what = 'wire' }
  end
  -- sidewalks, bridge decks, buildings (split in parts under the engine's 16-bit index limit), the buildings'
  -- lean collision mesh: colliders only near the car (M.COLLIDE_M); with a bldcol the drawn buildings only render
  local tx, ty = tostring(msg.key):match('^(%-?%d+)_(%-?%d+)$')
  tiles[key].cx = tx and (tonumber(tx) + 0.5) * M.TILE_M or nil
  tiles[key].cy = ty and (tonumber(ty) + 0.5) * M.TILE_M or nil
  -- a lean collision mesh ("bldcol", "walkcol": server cdae collision_only) makes the drawn ones render-only
  local lean = type(data.bldcol) == 'table' and data.bldcol.shape ~= nil
  local leanWalk = type(data.walkcol) == 'table' and data.walkcol.shape ~= nil
  -- tunnels (server roadmesh.tunnel: tubes and portals) collide like the decks
  for _, what in ipairs({ 'walk', 'deck', 'bld', 'bld2', 'bld3', 'bld4', 'bldcol', 'walkcol', 'tunnel' }) do
    local w = data[what]
    if type(w) == 'table' and w.shape and type(w.pos) == 'table' then
      local q = { key = key, walk = w, what = what,
        renderOnly = (lean and what:sub(1, 3) == 'bld' and what ~= 'bldcol') or (leanWalk and what == 'walk') }
      if q.renderOnly or M.collideNear(tiles[key]) then walkQ[#walkQ + 1] = q else parked[#parked + 1] = q end
    end
  end
  return true
end

-- Server message `bldfar_tile` {anchorId, key, path, n}: a far tile's buildings (render-only, the lite mesh);
-- shown only while the road tile of the same square has no building mesh of its own.
function M.onFarTile(msg, currentAnchor)
  if not M.enabled then return false end
  switchAnchor(currentAnchor)
  if msg.anchorId ~= currentAnchor then return false end
  local ok, data = pcall(jsonDecode, readFile(msg.path) or '')
  if not ok or type(data) ~= 'table' then lastError = 'unreadable far building tile ' .. tostring(msg.path); return false end
  local key = tileKey(msg.anchorId, msg.key)
  local old = far[key]
  if old and old.id then deleteQ[#deleteQ + 1] = old.id end
  if old then slotDrop(old.slot) end
  far[key] = nil
  local sl = msgSlot(msg, 'shape')
  if data.shape and type(data.pos) == 'table' then
    far[key] = { anchor = msg.anchorId, shape = sl and sl.path or data.shape, pos = data.pos, slot = sl }
    farQ[#farQ + 1] = key
  else
    slotDrop(sl)
  end
  return true
end

-- Server message `bldfar_drop` {anchorId, key}.
function M.onFarDrop(msg)
  local key = tileKey(msg.anchorId, msg.key)
  local f = far[key]
  if f and f.ghost then return end
  if f and f.id then deleteQ[#deleteQ + 1] = f.id end
  if f then slotDrop(f.slot) end
  far[key] = nil
end

-- A far tile is hidden while its square's road tile shows buildings (they are the same, colliding).
local function updateFar(gov)
  for key, f in pairs(far) do
    if f.id then
      local hide = tiles[key] ~= nil and tiles[key].bld ~= nil
      if hide ~= (f.hidden or false) then
        local o = scenetree.findObjectById(f.id)
        if o then o:setHidden(hide) end
        f.hidden = hide
      end
    end
  end
  while #farQ > 0 and #walkQ == 0 and gov:try('roadWalk') do
    local key = table.remove(farQ, 1)
    local f = far[key]
    if f and not f.id then
      local t0 = now()
      loadMaterials()
      local ok, id = pcall(createWalk, { shape = f.shape, pos = f.pos }, false)
      if ok then f.id = id; slotMade(id, f.shape) else lastError = tostring(id) end
      gov:charge('roadWalk', now() - t0)
    end
  end
end

-- Server message `roads_drop` {anchorId, key}. (A ghost goes when covered: releaseGhosts.)
function M.onDrop(msg)
  local t = tiles[tileKey(msg.anchorId, msg.key)]
  if t and t.ghost then return end
  dropTile(tileKey(msg.anchorId, msg.key))
  sinceChange = stopwatch.new()
end

-- Origin rebase (core.lua finishRebase): the shown tiles move into the new frame (p_new = R*p + t: their TSStatics
-- turned by R, as farfield.transform) and stay as "ghosts" until the new anchor's tiles over them are made
-- (ghostsCovered). Before (2026-10-17, forced rebase at Fells Point) every building, road mesh and sidewalk went at
-- once and came back over ~26 s as the server re-sent the tiles. DecalRoads (no rotation) and anything still queued go
-- now: their positions are the old frame's. A ghost's colliders are not made again (updateCollideRange).
M.GHOST_MAX_S = 60          -- ghosts left after this long go anyway
M.GHOST_QUIET_S = 6         -- ... or once no new tile arrived for this long (places with no tile over a ghost)
local ghostSince = nil      -- stopwatch since the last rebase that left ghosts

local WHATS = { 'walk', 'road', 'deck', 'bld', 'bld2', 'bld3', 'bld4', 'bldcol', 'walkcol', 'wire', 'tunnel' }

function M.transform(xf, geodesy)
  local bx, by, bz, bw = xf:beamngQuat()
  local function moveObj(id, q)
    local o = id and scenetree.findObjectById(id)
    if not o then return end
    local p = o:getPosition()
    local x, y, z = xf:apply(p.x, p.y, p.z)
    o:setPosRot(x, y, z, q[1], q[2], q[3], q[4])
  end
  local function newQ(q)
    local x, y, z, w = geodesy.quatMul(q[1], q[2], q[3], q[4], bx, by, bz, bw)
    return { x, y, z, w }
  end
  local any = false
  for key, t in pairs(tiles) do
    t.q = newQ(t.q or { 0, 0, 0, 1 })
    for _, what in ipairs(WHATS) do moveObj(t[what], t.q) end
    for _, id in ipairs(t.objs) do deleteQ[#deleteQ + 1] = id end
    t.objs, t.queued, t.cq = {}, 0, nil
    if t.cx then t.cx, t.cy = xf:apply(t.cx, t.cy, 0) end
    t.ghost = true
    any = true
  end
  for _, f in pairs(far) do
    if f.id then
      f.q = newQ(f.q or { 0, 0, 0, 1 })
      moveObj(f.id, f.q)
      f.ghost = true
      any = true
    end
  end
  local function keepNew(list)
    local keep = {}
    for _, q in ipairs(list) do if tiles[q.key] and not tiles[q.key].ghost then keep[#keep + 1] = q end end
    return keep
  end
  walkQ, createQ, parked = keepNew(walkQ), keepNew(createQ), keepNew(parked)
  local fq = {}
  for _, k in ipairs(farQ) do if far[k] and not far[k].ghost then fq[#fq + 1] = k end end
  farQ = fq
  if any then ghostSince = stopwatch.new(); sinceChange = stopwatch.new(); collisionDirty = true end
end

-- Is the square of side M.TILE_M around (cx, cy) covered by made tiles of the current anchor (every one of the new
-- grid's squares it overlaps has a tile with nothing queued)?
-- done: the new grid's squares whose tile is made (madeSquares).
local function madeSquares(aid, busy)
  local n, done = M.TILE_M, {}
  for key, t in pairs(tiles) do
    if t.anchor == aid and not t.ghost and t.cx then
      done[math.floor(t.cx / n) .. '_' .. math.floor(t.cy / n)] = t.queued <= 0 and not busy[key]
    end
  end
  return done
end
local function covered(done, cx, cy)
  local h, n = M.TILE_M / 2, M.TILE_M
  for gx = math.floor((cx - h) / n), math.floor((cx + h - 1e-6) / n) do
    for gy = math.floor((cy - h) / n), math.floor((cy + h - 1e-6) / n) do
      if not done[gx .. '_' .. gy] then return false end
    end
  end
  return true
end

local function releaseGhosts()
  if not ghostSince then return end
  local busyKeys = {}
  for _, q in ipairs(walkQ) do busyKeys[q.key] = true end
  local all = ghostSince() > M.GHOST_MAX_S * 1000 or (sinceChange() > M.GHOST_QUIET_S * 1000 and #walkQ == 0)
  local left = false
  local done = madeSquares(anchorId, busyKeys)
  for key, t in pairs(tiles) do
    if t.ghost then
      if all or (t.cx and covered(done, t.cx, t.cy)) then
        dropTile(key)
        collisionDirty = true
      else left = true end
    end
  end
  for key, f in pairs(far) do
    if f.ghost then
      if all then
        if f.id then deleteQ[#deleteQ + 1] = f.id end
        slotDrop(f.slot)
        far[key] = nil
      else left = true end
    end
  end
  if not left then ghostSince = nil end
end

-- Once per frame (core onUpdate): deletions, then creations, within the governor's budget.
function M.update(gov, currentAnchor)
  if currentAnchor then switchAnchor(currentAnchor) end
  releaseGhosts()
  while #deleteQ > 0 and gov:try('roadDelete') do
    local t0 = now()
    local did = table.remove(deleteQ)
    local o = scenetree.findObjectById(did)
    if o then
      if isCollider(o) then
        if M.nearPlayer(o:getPosition(), M.DELETE_NEAR_M) then collisionDirty = true else deletesDirty = true end
      end
      o:delete()
    end
    slotObjGone(did)
    gov:charge('roadDelete', now() - t0)
    sinceChange = stopwatch.new()
  end
  while #createQ > 0 and gov:try('roadChunk') do
    local q = table.remove(createQ, 1)
    local t = tiles[q.key]
    if t then
      local t0 = now()
      loadMaterials()
      local ok, id = pcall(createChunk, q.chunk)
      if ok then t.objs[#t.objs + 1] = id else lastError = tostring(id) end
      t.queued = t.queued - 1
      gov:charge('roadChunk', now() - t0)
      sinceChange = stopwatch.new()
    end
  end
  releaseHeld()
  updateCollideRange()
  while #walkQ > 0 and gov:try('roadWalk') do
    local q = table.remove(walkQ, 1)
    local t = tiles[q.key]
    if t and q.what ~= 'road' and playerIn(t.colliders[q.what]) then
      held[#held + 1] = q           -- the car is on it: made once the car is clear (playerIn)
      t = nil
    end
    if t then
      local t0 = now()
      loadMaterials()
      local isRoad = q.what == 'road' or q.what == 'wire' or q.renderOnly   -- render-only meshes
      -- the buildings' and sidewalks' lean meshes are a collision detail (server cdae collision_only): never drawn
      local ok, id = pcall(createWalk, q.walk, (q.what == 'bldcol' or q.what == 'walkcol') and 'Collision Mesh' or not isRoad)
      if ok then slotMade(id, q.walk.shape) end
      if ok then
        if not isRoad then t.cq = t.cq or {}; t.cq[q.what] = q end
        if isRoad then t[q.what] = id else t[q.what] = id; collisionDirty = true; newColliders[#newColliders + 1] = { id, q } end
      else lastError = tostring(id) end
      gov:charge('roadWalk', now() - t0)
      sinceChange = stopwatch.new()
    end
  end
  updateFar(gov)                -- far buildings: only with the near meshes all made (they share the budget)
  if (collisionDirty and sinceReload() > M.RELOAD_EVERY_S * 1000)
      or (deletesDirty and sinceReload() > M.RELOAD_DELETES_S * 1000) then
    local t0 = now()
    noteNewColliders()
    be:reloadCollision()
    collisionDirty, deletesDirty, sinceReload = false, false, stopwatch.new()
    gov:charge('roadWalk', now() - t0)
  end
end

-- A colliding mesh (sidewalks, bridge decks) never turns solid around the player's car (2026-10-13: the
-- intermittent NaN drives; a late curb or deck appearing around a vehicle). The server's road tile lists
-- boxes around its pieces (roads.collider_boxes); a mesh the car is in is not made (held) or, made since
-- the last reloadCollision, is removed again before it, and made once the car is clear.
M.PLAYER_R = 3.0    -- m around the player's position (half a car's length, and some)
M.PLAYER_DZ = 2.5   -- m above / below

-- Is world position p within r m (horizontally) of the player's car? (no car: yes, to be safe)
function M.nearPlayer(p, r)
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh or not p then return true end
  local q = veh:getPosition()
  local dx, dy = p.x - q.x, p.y - q.y
  return dx * dx + dy * dy <= r * r
end

playerIn = function(boxes)
  if type(boxes) ~= 'table' or #boxes == 0 then return false end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh then return false end
  local p = veh:getPosition()
  local r, dz = M.PLAYER_R, M.PLAYER_DZ
  for _, b in ipairs(boxes) do
    if p.x >= b[1] - r and p.x <= b[4] + r and p.y >= b[2] - r and p.y <= b[5] + r
        and p.z >= b[3] - dz and p.z <= b[6] + dz then
      return true
    end
  end
  return false
end

releaseHeld = function()
  if #held == 0 then return end
  local keep = {}
  for _, q in ipairs(held) do
    local t = tiles[q.key]
    if t then
      if playerIn(t.colliders[q.what]) then keep[#keep + 1] = q else walkQ[#walkQ + 1] = q end
    end
  end
  held = keep
end

noteNewColliders = function()
  local removed = 0
  for _, c in ipairs(newColliders) do
    local q = c[2]
    local t = tiles[q.key]
    if t and t[q.what] == c[1] and playerIn(t.colliders[q.what]) then
      local o = scenetree.findObjectById(c[1])
      if o then o:delete() end
      t[q.what] = nil
      held[#held + 1] = q
      removed = removed + 1
      M.solidAround = (M.solidAround or 0) + 1
      log('I', 'beamngEarth', 'roads: a ' .. tostring(q.what) .. ' mesh around the player held until it is clear')
    end
  end
  newColliders = {}
  return removed
end
M._noteNewColliders = noteNewColliders

-- Something else made colliding objects near the player (trees.lua's Forest items): rebuild collision at the
-- next throttled slot.
function M.requestCollision() collisionDirty = true end

function M.hasWork() return #createQ > 0 or #deleteQ > 0 or #walkQ > 0 or #farQ > 0 or collisionDirty end

-- Somebody else just ran be:reloadCollision() (the terrain commit): the sidewalks made so far collide.
function M.collisionRebuilt()
  -- that rebuild already made them solid: one held now leaves the physics at the next one
  local removed = noteNewColliders()
  collisionDirty, deletesDirty, sinceReload = removed > 0, false, stopwatch.new()
end

-- Every road removed now (setting off, mod unload).
function M.clear()
  for key in pairs(tiles) do dropTile(key) end
  for _, f in pairs(far) do
    if f.id then deleteQ[#deleteQ + 1] = f.id end
    slotDrop(f.slot)
  end
  far, farQ = {}, {}
  createQ, held, newColliders = {}, {}, {}
  for _, id in ipairs(deleteQ) do
    local o = scenetree.findObjectById(id)
    if o then
      if isCollider(o) then collisionDirty = true end
      o:delete()
    end
    slotObjGone(id)
  end
  deleteQ = {}
  walkQ, parked = {}, {}
  if collisionDirty then be:reloadCollision(); collisionDirty = false end
end

-- The level ended: its objects are gone with it.
function M.reset()
  -- (the level took the objects: every slot goes)
  for _, t in pairs(tiles) do for _, sl in pairs(t.slots or {}) do slotsGone[#slotsGone + 1] = { sl[1], sl[2], sl[3] } end end
  for _, f in pairs(far) do if f.slot then slotsGone[#slotsGone + 1] = { f.slot[1], f.slot[2], f.slot[3] } end end
  for n, d in pairs(slotDropped) do slotsGone[#slotsGone + 1] = { n, d[1], d[2] } end
  objSlot, slotLive, slotDropped = {}, {}, {}
  tiles, createQ, deleteQ, walkQ, anchorId, loadedMaterials = {}, {}, {}, {}, nil, false
  far, farQ = {}, {}
  collisionDirty, deletesDirty = false, false
end

-- On / off (the settings page, through core.setRoadsEnabled, which asks the server for the tiles
-- again when switched back on). Returns true if it was off and is on now.
function M.setEnabled(on)
  local was = M.enabled
  M.enabled = on and true or false
  if not M.enabled then M.clear() end
  return M.enabled and not was
end

function M.status()
  local nt, no, nw, nr, nd, nb = 0, 0, 0, 0, 0, 0
  for _, t in pairs(tiles) do
    nt = nt + 1; no = no + #t.objs
    if t.walk then nw = nw + 1 end
    if t.road then nr = nr + 1 end
    if t.deck then nd = (nd or 0) + 1 end
    if t.bld then nb = nb + 1 end
  end
  local nf = 0
  for _, f in pairs(far) do if f.id and not f.hidden then nf = nf + 1 end end
  return { enabled = M.enabled, tiles = nt, roads = no, queued = #createQ, deleting = #deleteQ, anchor = anchorId,
    walks = nw, roadMeshes = nr, decks = nd, buildings = nb, farBuildings = nf, walksQueued = #walkQ, solidAround = M.solidAround or 0,
    quietS = sinceChange() / 1000, error = lastError }
end

local okSweep, swept = pcall(M.sweep)
if okSweep and swept > 0 then log('W', 'beamngEarth', 'roads: deleted ' .. swept .. ' road objects left by a previous Lua state') end

return M
