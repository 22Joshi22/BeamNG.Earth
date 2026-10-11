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
  '/levels/driver_training/art/road/main.materials.json' }         -- m_metal_grid_drain (storm drains)
M.FADE = '450 150'          -- distanceFade: start, range (m)
M.enabled = true
M.RELOAD_EVERY_S = 0.5      -- be:reloadCollision() at most this often

local tiles = {}            -- key "a:tx_ty" -> { anchor, objs = { DecalRoad ids }, queued = n, walk = TSStatic id }
local walkQ = {}            -- { key, walk = {shape, pos} } FIFO
local createQ = {}          -- { key, chunk } FIFO
local deleteQ = {}          -- object ids
local loadedMaterials = false
local anchorId = nil
local lastError = nil
local sinceChange = stopwatch.new()  -- ms since a tile arrived / went or a road was made / deleted
local sinceReload = stopwatch.new()
local collisionDirty = false

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

local function tileKey(aid, k) return tostring(aid) .. ':' .. tostring(k) end

local function now() return os.clock() * 1000 end

local function dropTile(key)
  local t = tiles[key]
  if not t then return end
  for _, id in ipairs(t.objs) do deleteQ[#deleteQ + 1] = id end
  if t.walk then deleteQ[#deleteQ + 1] = t.walk end
  tiles[key] = nil
end

-- Every road not of anchor aid goes (a new anchor: the server re-sends its tiles).
local function switchAnchor(aid)
  if aid == anchorId then return end
  anchorId = aid
  for key, t in pairs(tiles) do
    if t.anchor ~= aid then dropTile(key) end
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

local function isCollider(o)
  local c = o.class or (o.getClassName and o:getClassName())
  return c == 'TSStatic' or c == 'MeshRoad'
end

local function createWalk(walk)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, walk.shape)
  o:setField('collisionType', 0, 'Visible Mesh')
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
  tiles[key] = { anchor = msg.anchorId, objs = {}, queued = #data.chunks }
  sinceChange = stopwatch.new()
  for _, c in ipairs(data.chunks) do createQ[#createQ + 1] = { key = key, chunk = c } end
  if type(data.walk) == 'table' and data.walk.shape and type(data.walk.pos) == 'table' then
    walkQ[#walkQ + 1] = { key = key, walk = data.walk }
  end
  return true
end

-- Server message `roads_drop` {anchorId, key}.
function M.onDrop(msg)
  dropTile(tileKey(msg.anchorId, msg.key))
  sinceChange = stopwatch.new()
end

-- Once per frame (core onUpdate): deletions, then creations, within the governor's budget.
function M.update(gov, currentAnchor)
  if currentAnchor then switchAnchor(currentAnchor) end
  while #deleteQ > 0 and gov:try('roadDelete') do
    local t0 = now()
    local o = scenetree.findObjectById(table.remove(deleteQ))
    if o then
      if isCollider(o) then collisionDirty = true end
      o:delete()
    end
    gov:charge('roadDelete', now() - t0)
    sinceChange = stopwatch.new()
  end
  while #createQ > 0 and gov:try('roadChunk') do
    local q = table.remove(createQ, 1)
    local t = tiles[q.key]
    if t then
      local t0 = now()
      if not loadedMaterials then
        for _, f in ipairs(M.MATERIAL_FILES) do loadJsonMaterialsFile(f) end
        loadedMaterials = true
      end
      local ok, id = pcall(createChunk, q.chunk)
      if ok then t.objs[#t.objs + 1] = id else lastError = tostring(id) end
      t.queued = t.queued - 1
      gov:charge('roadChunk', now() - t0)
      sinceChange = stopwatch.new()
    end
  end
  while #walkQ > 0 and gov:try('roadWalk') do
    local q = table.remove(walkQ, 1)
    local t = tiles[q.key]
    if t then
      local t0 = now()
      local ok, id = pcall(createWalk, q.walk)
      if ok then t.walk = id; collisionDirty = true else lastError = tostring(id) end
      gov:charge('roadWalk', now() - t0)
      sinceChange = stopwatch.new()
    end
  end
  if collisionDirty and sinceReload() > M.RELOAD_EVERY_S * 1000 then
    local t0 = now()
    be:reloadCollision()
    collisionDirty, sinceReload = false, stopwatch.new()
    gov:charge('roadWalk', now() - t0)
  end
end

function M.hasWork() return #createQ > 0 or #deleteQ > 0 or #walkQ > 0 or collisionDirty end

-- Every road removed now (setting off, mod unload).
function M.clear()
  for key in pairs(tiles) do dropTile(key) end
  createQ = {}
  for _, id in ipairs(deleteQ) do
    local o = scenetree.findObjectById(id)
    if o then
      if isCollider(o) then collisionDirty = true end
      o:delete()
    end
  end
  deleteQ = {}
  walkQ = {}
  if collisionDirty then be:reloadCollision(); collisionDirty = false end
end

-- The level ended: its objects are gone with it.
function M.reset()
  tiles, createQ, deleteQ, walkQ, anchorId, loadedMaterials = {}, {}, {}, {}, nil, false
  collisionDirty = false
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
  local nt, no, nw = 0, 0, 0
  for _, t in pairs(tiles) do
    nt = nt + 1; no = no + #t.objs
    if t.walk then nw = nw + 1 end
  end
  return { enabled = M.enabled, tiles = nt, roads = no, queued = #createQ, deleting = #deleteQ, anchor = anchorId,
    walks = nw, walksQueued = #walkQ,
    quietS = sinceChange() / 1000, error = lastError }
end

local okSweep, swept = pcall(M.sweep)
if okSweep and swept > 0 then log('W', 'beamngEarth', 'roads: deleted ' .. swept .. ' road objects left by a previous Lua state') end

return M
