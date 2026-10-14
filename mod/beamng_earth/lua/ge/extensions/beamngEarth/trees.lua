-- Trees, bushes and rocks from the server's vegetation tiles (server/vegetation.py, --trees on; the user,
-- 2026-10-13: "trees, rocks and other decoration"). Each tile file lists items
-- {group, x, y, z, yaw, scale, height, rank} in the anchor frame; they become Forest items of stock
-- East Coast USA shapes (they have LODs, billboards and collision meshes), the variant nearest the
-- item's height, scaled to it. A tile shows the items whose fixed rank is under its distance's threshold
-- (ROADMAP "deterministic generation": a level shows the same trees, thinned), made and removed a few
-- hundred per frame (~19 us each, 2026-10-13 probe).
local M = {}

M.PER_FRAME = 300       -- items made or removed per frame at most
M.RINGS = { { 300, 1.0 }, { 550, 0.5 }, { 1e9, 0.25 } }    -- tile distance (m): the rank shown below
M.FOREST_NAME = 'bngEarthForest'

local ECA = '/levels/east_coast_usa/art/shapes/'
-- the stock shapes per group with their measured heights (m; TSStatic world boxes, 2026-10-13)
M.CATALOG = {
  deciduous = {
    { 'trees/trees_oak/tree_oak_sml_a', 8.6 }, { 'trees/trees_oak/tree_oak_sml_b', 9.5 },
    { 'trees/trees_aspen/tree_aspen_small_a', 8.3 }, { 'trees/trees_aspen/tree_aspen_small_c', 9.7 },
    { 'trees/trees_beech/tree_beech_small_c', 9.9 }, { 'trees/trees_oak/tree_oak_large_a', 13.2 },
    { 'trees/trees_oak/tree_oak_large_b', 15.9 }, { 'trees/trees_oak/tree_oak_large_c', 15.5 },
    { 'trees/trees_oak/tree_oak_forest_a', 15.9 }, { 'trees/trees_oak/tree_oak_forest_b', 16.0 },
    { 'trees/trees_beech/tree_beech_large_c', 17.4 }, { 'trees/trees_aspen/tree_aspen_forest_b', 18.5 },
    { 'trees/trees_beech/tree_beech_forest_a', 20.2 }, { 'trees/trees_beech/tree_beech_large_b', 21.1 },
  },
  conifer = {
    { 'trees/trees_douglasfir/tree_douglasfir_small_b', 11.2 }, { 'trees/trees_douglasfir/tree_douglasfir_small_a', 13.4 },
    { 'trees/trees_douglasfir/tree_douglasfir_large_c', 18.9 }, { 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6 },
    { 'trees/trees_douglasfir/tree_douglasfir_large_b', 21.4 }, { 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7 },
  },
  shrub = {
    { 'trees/ECA_coast_bush/generibush', 1.8 }, { 'trees/trees_aspen/tree_aspen_bush_a', 2.2 },
    { 'trees/trees_douglasfir/tree_douglasfir_bush_a', 2.7 }, { 'trees/trees_oak/tree_oak_bush_c', 3.1 },
    { 'trees/trees_beech/tree_beech_bush_b', 3.3 }, { 'trees/trees_beech/tree_beech_bush_c', 3.9 },
    { 'trees/trees_oak/tree_oak_bush_b', 4.1 },
  },
  rock = {
    { 'rocks/eca_rock_small', 0.6 }, { 'rocks/rock_01_a_large_a', 0.7 }, { 'rocks/eca_rock_small_2', 0.9 },
    { 'rocks/eca_rock_medium_1', 1.5 }, { 'rocks/eca_rock_medium_2', 1.6 }, { 'rocks/eca_rock_big_narrow', 1.9 },
    { 'rocks/eca_rock_big_wide', 1.9 },
  },
}
local MATERIALS = { 'trees/trees_oak', 'trees/trees_beech', 'trees/trees_aspen', 'trees/trees_douglasfir',
  'trees/ECA_coast_bush', 'trees/common', 'trees', 'rocks' }

local tiles = {}        -- key "a:tx_ty" -> { anchor, cx, cy, rows, made = { i -> item }, shown = rank }
local types = {}        -- shape path -> TSForestItemData
local ready = false
M.made, M.removed, M.errors = 0, 0, 0

local function forest()
  local f = scenetree.findObject(M.FOREST_NAME)
  if not f then
    f = createObject('Forest')
    f:registerObject(M.FOREST_NAME)
    if scenetree.MissionGroup then scenetree.MissionGroup:addObject(f) end
  end
  return f
end

local function setup()
  if ready then return true end
  for _, m in ipairs(MATERIALS) do
    local p = ECA .. m .. '/main.materials.json'
    if not FS or FS:fileExists(p) then pcall(loadJsonMaterialsFile, p) end
  end
  ready = true
  return true
end

local function itemType(shape)
  local d = types[shape]
  if d then return d end
  local name = 'bngEarthTree_' .. shape:gsub('[^%w]', '_')
  d = scenetree.findObject(name)
  if not d then
    d = (worldEditorCppApi and worldEditorCppApi.createObject) and worldEditorCppApi.createObject('TSForestItemData')
      or createObject('TSForestItemData')
    if not d then return nil end
    d:setField('shapeFile', 0, ECA .. shape .. '.dae')
    d:setField('windScale', 0, '0.4'); d:setField('branchAmp', 0, '0.1'); d:setField('detailAmp', 0, '0.2')
    d:registerObject(name)
    if Sim and Sim.getDataBlockSet then Sim.getDataBlockSet():addObject(d) end
  end
  types[shape] = d
  return d
end

-- The variant of a group for an item of height h: those within 0.8 .. 1.25 of it (else 0.6 .. 1.6), picked by the
-- item's rank; else the nearest. Returns shape, scale.
function M.pick(group, h, rank)
  local list = M.CATALOG[group] or M.CATALOG.deciduous
  local cands = {}
  for _, range in ipairs({ { 0.8, 1.25 }, { 0.6, 1.6 } }) do      -- near its size first, then wider
    for _, v in ipairs(list) do
      if v[2] >= h * range[1] and v[2] <= h * range[2] then cands[#cands + 1] = v end
    end
    if #cands > 0 then break end
  end
  if #cands == 0 then
    local best, bd
    for _, v in ipairs(list) do
      local d = math.abs(math.log(v[2] / math.max(h, 0.1)))
      if not bd or d < bd then best, bd = v, d end
    end
    cands = { best }
  end
  local v = cands[math.floor(rank * 7919) % #cands + 1]
  return v[1], math.max(0.3, math.min(2.5, h / v[2]))
end

local GROUPS = { 'deciduous', 'conifer', 'shrub', 'rock' }

local function makeItem(fd, row)
  local shape, sc = M.pick(GROUPS[(row[1] or 0) + 1] or 'deciduous', row[7] or 10, row[8] or 0)
  local d = itemType(shape)
  if not d then M.errors = M.errors + 1; return nil end
  local m = MatrixF(true)
  m:setFromEuler(vec3(0, 0, math.rad(row[5] or 0)))
  m:setPosition(vec3(row[2], row[3], row[4]))
  local ok, it = pcall(function() return fd:createNewItem(d, m, sc) end)
  if not ok then M.errors = M.errors + 1; return nil end
  M.made = M.made + 1
  return it
end

local function removeItem(fd, it)
  pcall(function() fd:removeItem(it) end)
  M.removed = M.removed + 1
end

-- A 'veg_tile' message: { anchorId, key, path, n }.
function M.onTile(msg, currentAnchor)
  if msg.anchorId ~= currentAnchor or type(msg.path) ~= 'string' then return end
  local text = readFile(msg.path)
  local ok, data = pcall(jsonDecode, text or '')
  if not ok or type(data) ~= 'table' or type(data.items) ~= 'table' then
    M.errors = M.errors + 1
    return
  end
  local key = msg.anchorId .. ':' .. tostring(msg.key)
  local old = tiles[key]
  local tx, ty = tostring(msg.key):match('^(%-?%d+)_(%-?%d+)$')
  local t = { anchor = msg.anchorId, rows = data.items, made = old and old.made or {}, shown = old and old.shown or 0,
    cx = (tonumber(tx) or 0) * 256 + 128, cy = (tonumber(ty) or 0) * 256 + 128, drop = false }
  tiles[key] = t
end

-- A 'veg_drop' message: the tile's items go.
function M.onDrop(msg)
  local t = tiles[msg.anchorId .. ':' .. tostring(msg.key)]
  if t then t.drop = true end
end

local function threshold(d)
  for _, r in ipairs(M.RINGS) do
    if d <= r[1] then return r[2] end
  end
  return 0
end

-- Every frame: tiles of other anchors and dropped ones emptied, the others filled / thinned to their
-- distance's rank, M.PER_FRAME items at most.
function M.update(anchorId, player)
  if not next(tiles) then return end
  if not setup() then return end
  local fd = forest():getData()
  local budget = M.PER_FRAME
  for key, t in pairs(tiles) do
    if t.anchor ~= anchorId or t.drop then
      for i, it in pairs(t.made) do
        if budget <= 0 then return end
        removeItem(fd, it)
        t.made[i] = nil
        budget = budget - 1
      end
      tiles[key] = nil
    end
  end
  if not player then return end
  -- nearest tiles first
  local order = {}
  for key, t in pairs(tiles) do
    local dx = math.max(math.abs(player.x - t.cx) - 128, 0)
    local dy = math.max(math.abs(player.y - t.cy) - 128, 0)
    order[#order + 1] = { key, math.sqrt(dx * dx + dy * dy) }
  end
  table.sort(order, function(a, b) return a[2] < b[2] end)
  for _, o in ipairs(order) do
    local t = tiles[o[1]]
    local want = threshold(o[2])
    if want ~= t.shown or t.pending then
      t.pending = false
      for i, row in ipairs(t.rows) do
        local r = row[8] or 0
        if r < want and not t.made[i] then
          if budget <= 0 then t.pending = true; return end
          t.made[i] = makeItem(fd, row)
          budget = budget - 1
        elseif r >= want and t.made[i] then
          if budget <= 0 then t.pending = true; return end
          removeItem(fd, t.made[i])
          t.made[i] = nil
          budget = budget - 1
        end
      end
      t.shown = want
    end
  end
end

-- Disconnect, a new server session, mod unload: every item removed.
function M.clear()
  local f = scenetree.findObject(M.FOREST_NAME)
  local fd = f and f:getData()
  for _, t in pairs(tiles) do
    for _, it in pairs(t.made) do if fd then removeItem(fd, it) end end
  end
  tiles = {}
end

-- Level end: the level deletes its objects itself.
function M.reset()
  tiles, types, ready = {}, {}, false
end

function M.status()
  local n, items = 0, 0
  for _, t in pairs(tiles) do
    n = n + 1
    for _ in pairs(t.made) do items = items + 1 end
  end
  return { tiles = n, items = items, made = M.made, removed = M.removed, errors = M.errors }
end

return M
