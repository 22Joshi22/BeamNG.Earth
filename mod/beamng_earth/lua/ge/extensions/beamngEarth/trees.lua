-- Trees, bushes and rocks from the server's vegetation tiles (server/vegetation.py, --trees on; the user,
-- 2026-10-13: "trees, rocks and other decoration"). Each tile file lists items
-- {group, x, y, z, yaw, scale, height, rank} in the anchor frame; they become Forest items of stock
-- East Coast USA shapes (they have LODs, billboards and collision meshes), the variant nearest the
-- item's height, scaled to it. A tile shows the items whose fixed rank is under its distance's threshold
-- (ROADMAP "deterministic generation": a level shows the same trees, thinned), made and removed a few
-- hundred per frame (~19 us each, 2026-10-13 probe).
local M = {}

M.PER_FRAME = 250       -- items made or removed per frame at most
M.ROWS_PER_FRAME = 12000 -- rows of changing tiles looked at per frame at most (Lua time: 30000 averaged 2.5 ms)
-- 2026-10-14: was { 300, 1 }, { 550, 0.5 }, { 900, 0.25 }, { 1e9, 0.12 }; the engine already draws far trees as
-- billboards (their LODs), so our extra thinning made far forest sparse. Columbia: 48k -> 78k items, no frame cost seen.
M.RINGS = { { 300, 1.0 }, { 700, 0.6 }, { 1500, 0.35 }, { 1e9, 0.2 } }   -- tile distance (m): the rank shown below
-- small props only near the player (2026-10-14, Fells Point at street level: ~10 ms a frame for the Forest
-- with a door on every house out to 300 m): the tile distance (m) beyond which a group is not shown
M.GROUP_MAX_M = { shrub = 250, desert_shrub = 250, door = 120, hydrant = 200, bin = 200, bench = 200, polelamp = 400,
  stop = 300, signal = 500, fence = 150, wallsign_logistics = 800, wallsign_rentabox = 800, wallsign_lemon = 800, wallsign_turboburger = 500,
  speed30 = 300, speed40 = 300, speed50 = 300, speed60 = 300, speed70 = 300,
  speedk40 = 300, speedk60 = 300, speedk80 = 300, speedk100 = 300 }
M.FOREST_NAME = 'bngEarthForest'      -- a prefix: small Forests from a pool ('bngEarthForest_<n>')
-- A small Forest per tile, from a pool (2026-10-15, 100 mph drives: one shared Forest with ~240k items made and ~220k
-- removed over a session made frames with any tree change cost ~100 ms in the engine's tick, and a fresh game ran the
-- same drive at 71 fps instead of 30-42). A dropped tile's items are removed (cheap in its small Forest) and the empty
-- Forest goes back to the pool: deleting a Forest whose items are in the physics' static collision hung the game.
-- Only tiles this near the player get colliding items (their trees, rocks and street furniture), the rest are made of
-- item types that never collide, a tile's items re-made when it crosses (back beyond SOLID_M + SOLID_HYST_M).
-- Solid items made ask for a collision rebuild (roads.lua, throttled): a Forest item collides only after
-- be:reloadCollision, and where no road tile arrives (the Sahara's rocks, 2026-10-14) none came at all.
-- 2026-10-15 probes: with ~150k trees all colliding, a frame changing 10 items cost the engine's tick ~124 ms, ~68 ms
-- when the changed items' type did not collide: much of a Forest change's cost grew with the colliding items.
M.SOLID_M = 300
M.SOLID_HYST_M = 100
M.NO_COLLIDE = { shrub = true, desert_shrub = true, door = true, wallsign_logistics = true, wallsign_rentabox = true,
  wallsign_lemon = true, wallsign_turboburger = true }
-- The 100 mph stutter (2026-10-15 probes): a frame that adds or removes Forest items costs the engine's tick ~0.5 us
-- for every item in the level of each SHAPE touched (whatever its item type or Forest), so a frame making 300 items of
-- ~20 shapes among ~130k items cost ~100 ms. A frame touches shapes whose items add up to SHAPE_ITEMS_PER_FRAME at most
-- (at least one shape), and bushes (half of all items, one shape alone 34k) show near the player only.
M.SHAPE_ITEMS_PER_FRAME = 4000
-- The engine tells shapes apart by their path, so a tile's items use one of SHAPE_VARIANTS spellings of each shape's
-- path ('dir/./name', 'dir/././name', ...: the same file, a shape of its own), which splits the big populations (one
-- beech ~7.7k items: a frame touching it cost ~4-9 ms). A variant's first items cost one ~12 ms frame (its load).
M.SHAPE_VARIANTS = 4

local ECA = '/levels/east_coast_usa/art/shapes/'
local WCA = '/levels/west_coast_usa/art/shapes/'
local UT = '/levels/Utah/art/shapes/'
local JV = '/levels/johnson_valley/art/shapes/'
local JRI = '/levels/jungle_rock_island/art/shapes/'
local IT = '/levels/italy/art/shapes/'
-- the stock shapes per group with their measured heights (m; TSStatic world boxes, 2026-10-13/14)
M.CATALOG = {
  deciduous = {
    { ECA .. 'trees/trees_oak/tree_oak_sml_a', 8.6 }, { ECA .. 'trees/trees_oak/tree_oak_sml_b', 9.5 },
    { ECA .. 'trees/trees_aspen/tree_aspen_small_a', 8.3 }, { ECA .. 'trees/trees_aspen/tree_aspen_small_c', 9.7 },
    { ECA .. 'trees/trees_beech/tree_beech_small_c', 9.9 }, { ECA .. 'trees/trees_oak/tree_oak_large_a', 13.2 },
    { ECA .. 'trees/trees_oak/tree_oak_large_b', 15.9 }, { ECA .. 'trees/trees_oak/tree_oak_large_c', 15.5 },
    { ECA .. 'trees/trees_oak/tree_oak_forest_a', 15.9 }, { ECA .. 'trees/trees_oak/tree_oak_forest_b', 16.0 },
    { ECA .. 'trees/trees_beech/tree_beech_large_c', 17.4 }, { ECA .. 'trees/trees_aspen/tree_aspen_forest_b', 18.5 },
    { ECA .. 'trees/trees_beech/tree_beech_forest_a', 20.2 }, { ECA .. 'trees/trees_beech/tree_beech_large_b', 21.1 },
  },
  conifer = {
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_small_b', 11.2 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_small_a', 13.4 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_c', 18.9 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_b', 21.4 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7 },
    { IT .. 'trees/trees_italy/scots_pine', 18.9 },
  },
  shrub = {
    { ECA .. 'trees/ECA_coast_bush/generibush', 1.8 }, { ECA .. 'trees/trees_aspen/tree_aspen_bush_a', 2.2 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_bush_a', 2.7 }, { ECA .. 'trees/trees_oak/tree_oak_bush_c', 3.1 },
    { ECA .. 'trees/trees_beech/tree_beech_bush_b', 3.3 }, { ECA .. 'trees/trees_beech/tree_beech_bush_c', 3.9 },
    { ECA .. 'trees/trees_oak/tree_oak_bush_b', 4.1 },
  },
  rock = {
    { ECA .. 'rocks/eca_rock_small', 0.6 }, { ECA .. 'rocks/rock_01_a_large_a', 0.7 }, { ECA .. 'rocks/eca_rock_small_2', 0.9 },
    { ECA .. 'rocks/eca_rock_medium_1', 1.5 }, { ECA .. 'rocks/eca_rock_medium_2', 1.6 }, { ECA .. 'rocks/eca_rock_big_narrow', 1.9 },
    { ECA .. 'rocks/eca_rock_big_wide', 1.9 },
  },
  pine_west = {
    { WCA .. 'trees/trees_conifer/pine_radiata_small', 8.6 }, { WCA .. 'trees/trees_conifer/pine_radiata_forest', 13.3 },
    { WCA .. 'trees/trees_conifer/pine_radiata_large', 13.2 }, { WCA .. 'trees/trees_conifer/pine_radiata_weathered', 13.2 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7 },
    { IT .. 'trees/trees_italy/scots_pine', 18.9 }, { WCA .. 'trees/trees_conifer/redwood_tall', 38.5 },
    { WCA .. 'trees/trees_conifer/redwood_split', 38.1 }, { WCA .. 'trees/trees_conifer/redwood_broad', 38.5 },
  },
  oak_west = {
    { WCA .. 'trees/trees_oak/oak_d', 7.1 }, { WCA .. 'trees/trees_oak/oak_b', 8.2 }, { WCA .. 'trees/trees_oak/oak_c', 8.4 },
    { WCA .. 'trees/trees_oak/oak_a', 9.6 }, { WCA .. 'trees/trees_eucalyptus/eucalyptus_double', 14.7 },
    { WCA .. 'trees/trees_eucalyptus/eucalyptus_tall', 22.5 },
  },
  palm = {
    { JRI .. 'trees/trees_palm/palm_small', 8.9 }, { JRI .. 'trees/trees_palm/palm_short', 9.1 },
    { WCA .. 'trees/trees_palm/fanpalm_short', 10.6 }, { WCA .. 'trees/trees_palm/fanpalm_skirt', 11.7 },
    { WCA .. 'trees/trees_palm/fanpalm_full', 13.8 }, { JRI .. 'trees/trees_palm/palm_medium', 18.0 },
    { WCA .. 'trees/trees_palm/fanpalm_tall', 18.5 }, { WCA .. 'trees/trees_palm/fanpalm_slim', 20.8 },
    { JRI .. 'trees/trees_palm/palm_tall', 25.3 },
  },
  juniper = {
    { UT .. 'trees/Juniper/Juniper_bush_large_01', 2.6 }, { UT .. 'trees/Juniper/Juniper_tree_04', 4.5 },
    { UT .. 'trees/Juniper/Juniper_tree_03', 5.2 }, { UT .. 'trees/Juniper/Juniper_tree_01', 6.3 },
    { UT .. 'trees/Juniper/Juniper_tree_02', 7.4 }, { UT .. 'trees/Juniper/Juniper_ancient_01', 7.5 },
  },
  desert_shrub = {
    { UT .. 'groundcover/s_gc_utah_grey_bush_01', 0.7 }, { JV .. 'groundcover/s_gc_jv_dry_bush_01', 0.7 },
    { JV .. 'trees/deserttrees/deserttree_bush_tiny', 0.7 }, { UT .. 'groundcover/s_gc_utah_grey_bush_03', 1.0 },
    { JV .. 'groundcover/s_gc_jv_dry_bush_03', 1.0 }, { UT .. 'trees/Juniper/Juniper_bush_small_01', 1.2 },
    { UT .. 'trees/Juniper/Juniper_bush_small_dry_01', 2.0 }, { UT .. 'trees/Juniper/Juniper_bush_medium_01', 2.4 },
    { JV .. 'trees/deserttrees/deserttree_bush_medium', 2.9 },
  },
  desert_rock = {
    { UT .. 'rocks/rock_01_a_small_a', 0.3 }, { UT .. 'rocks/utah_rock_flat_a', 0.5 }, { UT .. 'rocks/utah_boulder_5', 0.6 },
    { JV .. 'rocks/s_rocks_desert_small_01', 0.9 }, { JV .. 'rocks/s_rocks_desert_medium_03', 1.4 },
    { UT .. 'rocks/utah_boulder_3', 1.7 }, { UT .. 'rocks/utah_boulder_1', 1.9 }, { JV .. 'rocks/s_rocks_desert_medium_01', 2.1 },
  },
  door = {     -- the stock Building Architect kit (art_shapes.zip; global materials)
    { '/art/shapes/common/building_architect_modules/doors/R_HS_A_DR_01_100x210', 2.2 },
    { '/art/shapes/common/building_architect_modules/doors/R_HS_B_DR_03_100x210', 2.2 },
  },
  signal = { { '/art/shapes/objects/s_trafficlight_boom_sn', 9.4 } },
  stop = { { '/levels/east_coast_usa/art/shapes/signs_usa/sign_stop', 3.0 } },
  -- a low wooden yard fence, 5.4 m along its x (server buildings._front_fence)
  fence = { { ECA .. 'buildings/eca_bld_wood_fence_c', 1.3 } },
  -- company signs on big boxes, warehouses and restaurants (server buildings._wall_sign; West Coast USA's firms)
  wallsign_logistics = { { WCA .. 'objects/sign_logistics', 5.8 } }, wallsign_rentabox = { { WCA .. 'objects/sign_rentabox', 5.7 } },
  wallsign_lemon = { { WCA .. 'objects/sign_lemon', 3.6 } }, wallsign_turboburger = { { WCA .. 'objects/sign_turboburger', 2.4 } },
  -- speed-limit signs (server furniture.speed_signs: the road graph's limit, where a model exists)
  speed30 = { { UT .. 'ut_roadsigns/ut_30_speed_limit', 2.6 } }, speed40 = { { UT .. 'ut_roadsigns/ut_40_speed_limit', 2.6 } },
  speed50 = { { UT .. 'ut_roadsigns/ut_50_speed_limit', 2.6 } }, speed60 = { { UT .. 'ut_roadsigns/ut_60_speed_limit', 2.6 } },
  speed70 = { { UT .. 'ut_roadsigns/ut_70_speed_limit', 2.6 } },
  -- ... and the round km/h signs (furniture.SIGN_KMH, outside the mph countries: Driver Training's)
  speedk40 = { { '/levels/driver_training/art/shapes/race/sign_speed40kph', 2.47 } },
  speedk60 = { { '/levels/driver_training/art/shapes/race/sign_speed60kph', 2.47 } },
  speedk80 = { { '/levels/driver_training/art/shapes/race/sign_speed80kph', 2.47 } },
  speedk100 = { { '/levels/driver_training/art/shapes/race/sign_speed100kph', 2.47 } },
  hydrant = { { '/levels/west_coast_usa/art/shapes/objects/hydrant1', 1.5 } },
  bench = { { ECA .. 'clutter/clutter_city_bench_wood', 0.9 } },
  bin = { { '/art/shapes/garage_and_dealership/Clutter/clutter_city_bin_round', 1.1 }, { WCA .. 'objects/garbage_bin_a', 1.1 } },
  polelamp = { { ECA .. 'buildings/electrical_pole_lamp', 1.1 } },
  streetlight = { { '/art/shapes/garage_and_dealership/Clutter/eca_lamppost', 9.0 } },
  pole = { { '/levels/automation_test_track/art/shapes/objects/s_powerpole_3wire', 9.2 } },
  tropical = {
    { JRI .. 'trees/bush_tropical_1/tro_bush_huge', 5.5 }, { JRI .. 'trees/bush_tropical_2/tro_bush_2_small', 6.1 },
    { JRI .. 'trees/trees_tropical_2/tro_tree_2_filler', 20.2 }, { JRI .. 'trees/trees_tropical_1/tro_tree_1_huge', 20.6 },
    { JRI .. 'trees/trees_tropical_1/tro_tree_1_filler', 33.0 },
  },
}
local MATERIALS = {
  ECA .. 'trees/trees_oak', ECA .. 'trees/trees_beech', ECA .. 'trees/trees_aspen', ECA .. 'trees/trees_douglasfir',
  ECA .. 'trees/ECA_coast_bush', ECA .. 'trees/common', ECA .. 'trees', ECA .. 'rocks',
  WCA .. 'trees/trees_conifer', WCA .. 'trees/trees_oak', WCA .. 'trees/trees_eucalyptus', WCA .. 'trees/trees_palm',
  WCA .. 'trees', UT .. 'trees/Juniper', UT .. 'trees', UT .. 'groundcover', UT .. 'rocks', JV .. 'groundcover',
  JV .. 'rocks', JV .. 'trees/deserttrees', JV .. 'trees', JRI .. 'trees/trees_palm', JRI .. 'trees/trees_tropical_1',
  JRI .. 'trees/trees_tropical_2', JRI .. 'trees/bush_tropical_1', JRI .. 'trees/bush_tropical_2', JRI .. 'trees',
  IT .. 'trees/trees_italy', IT .. 'trees', '/levels/automation_test_track/art/shapes/objects',
  '/levels/automation_test_track/art/shapes/buildings',     -- the utility pole's wood_trim / painted metal
  ECA .. 'signs_usa', '/art/shapes/objects', '/levels/east_coast_usa/art/shapes', WCA .. 'objects', ECA .. 'buildings',
  UT .. 'ut_roadsigns', '/levels/driver_training/art/shapes/race',
}

local tiles = {}        -- key "a:tx_ty" -> { anchor, cx, cy, rows, made = { i -> item }, shown = rank }
local types = {}        -- "shape" / "shape#n" (not solid) -> TSForestItemData
local pop = {}          -- shape -> its items in the level
local ready = false
M.made, M.removed, M.errors = 0, 0, 0

local pool = {}          -- ids of empty Forests to reuse
local forestCount = 0
M.forests = 0

-- Pure: the n-th pool Forest's name.
function M.forestName(n)
  return M.FOREST_NAME .. '_' .. tostring(n)
end

-- The tile's Forest: its own, else one from the pool, else a new one.
local function forest(t)
  local f = t.forestId and scenetree.findObjectById(t.forestId)
  while not f and #pool > 0 do f = scenetree.findObjectById(table.remove(pool)) end
  if not f then
    forestCount = forestCount + 1
    f = createObject('Forest')
    f:registerObject(M.forestName(forestCount))
    if scenetree.MissionGroup then scenetree.MissionGroup:addObject(f) end
    M.forests = forestCount
  end
  t.forestId = f:getID()
  return f
end

-- An emptied tile's Forest back to the pool.
local function releaseForest(t)
  if t.forestId then pool[#pool + 1] = t.forestId end
  t.forestId = nil
end

local function setup()
  if ready then return true end
  for _, m in ipairs(MATERIALS) do
    local p = m .. '/main.materials.json'
    if not FS or FS:fileExists(p) then pcall(loadJsonMaterialsFile, p) end
  end
  ready = true
  return true
end

-- Pure: the item type's name of a shape, solid (collides) or not.
function M.typeName(shape, solid)
  return 'bngEarthTree_' .. shape:gsub('[^%w]', '_') .. (solid and '' or '_n')
end

local function itemType(shape, solid)
  local key = solid and shape or (shape .. '#n')
  local d = types[key]
  if d then return d end
  local name = M.typeName(shape, solid)
  d = scenetree.findObject(name)
  if not d then
    d = (worldEditorCppApi and worldEditorCppApi.createObject) and worldEditorCppApi.createObject('TSForestItemData')
      or createObject('TSForestItemData')
    if not d then return nil end
    d:setField('shapeFile', 0, shape .. '.dae')
    d:setField('collidable', 0, solid and '1' or '0')
    d:setField('windScale', 0, '0.4'); d:setField('branchAmp', 0, '0.1'); d:setField('detailAmp', 0, '0.2')
    d:registerObject(name)
    if Sim and Sim.getDataBlockSet then Sim.getDataBlockSet():addObject(d) end
  end
  types[key] = d
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
local groupBand

-- Pure: the variant v's spelling of a shape path (0: itself).
function M.variant(shape, v)
  if not v or v <= 0 then return shape end
  return (shape:gsub('/([^/]+)$', '/' .. string.rep('./', v) .. '%1'))
end

-- Pure: a tile's shape variant from its key ("a:tx_ty" / "a:ptx_ty"): neighbours differ.
function M.tileVariant(key)
  local tx, ty = tostring(key):match('(%-?%d+)_(%-?%d+)$')
  if not tx then return 0 end
  return (tonumber(tx) * 3 + tonumber(ty) * 5) % math.max(M.SHAPE_VARIANTS, 1)
end

-- The shape (the tile's variant) and scale of a tile's row i (cached: M.pick makes tables).
local function shapeOf(t, i, row)
  local s = t.shapes[i]
  if s then return s, t.scales[i] end
  local sc
  s, sc = M.pick((t.groups or GROUPS)[(row[1] or 0) + 1] or 'deciduous', row[7] or 10, row[8] or 0)
  s = M.variant(s, t.variant)
  t.shapes[i], t.scales[i] = s, sc
  return s, sc
end

local function makeItem(fd, row, shape, sc, solid)
  local d = itemType(shape, solid)
  if not d then M.errors = M.errors + 1; return nil end
  local m = MatrixF(true)
  m:setFromEuler(vec3(0, 0, math.rad(row[5] or 0)))
  m:setPosition(vec3(row[2], row[3], row[4]))
  local ok, it = pcall(function() return fd:createNewItem(d, m, sc) end)
  if not ok then M.errors = M.errors + 1; return nil end
  M.made = M.made + 1
  pop[shape] = (pop[shape] or 0) + 1
  return it
end

local function removeItem(fd, it, shape)
  pcall(function() fd:removeItem(it) end)
  M.removed = M.removed + 1
  if shape then pop[shape] = math.max((pop[shape] or 1) - 1, 0) end
end

-- This frame's shapes: may an item of this shape change now? (see SHAPE_ITEMS_PER_FRAME)
local touched, touchedItems = {}, 0
local function allow(shape)
  if touched[shape] then return true end
  local n = pop[shape] or 0
  if next(touched) ~= nil and touchedItems + n > M.SHAPE_ITEMS_PER_FRAME then return false end
  touched[shape], touchedItems = true, touchedItems + n
  return true
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
  M.onRows(msg.anchorId, msg.key, data.items, data.groups)
end

-- Rows of a tile from elsewhere (a road tile's street furniture: roads.lua, key "p<tx>_<ty>").
function M.onRows(anchorId, tileKey, rows, groups)
  local key = anchorId .. ':' .. tostring(tileKey)
  local old = tiles[key]
  local tx, ty = tostring(tileKey):match('(%-?%d+)_(%-?%d+)$')
  local t = { anchor = anchorId, rows = rows, groups = type(groups) == 'table' and groups or nil,
    made = old and old.made or {}, solid = old and old.solid or {}, near = old and old.near,
    shapes = old and old.rows == rows and old.shapes or {}, scales = old and old.rows == rows and old.scales or {},
    madeShape = old and old.madeShape or {}, shown = old and old.shown or 0, forestId = old and old.forestId or nil,
    variant = M.tileVariant(key), cx = (tonumber(tx) or 0) * 256 + 128, cy = (tonumber(ty) or 0) * 256 + 128, drop = false }
  tiles[key] = t
end

-- The tile "anchorId:key" goes (roads.lua's furniture).
function M.dropKey(key)
  local t = tiles[key]
  if t then t.drop = true end
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

-- M.suppress(group, x, y) -> true hides that item (set by navgraph.lua); M.refresh() re-runs every tile's rows
-- after it changes.
M.suppress = nil
function M.refresh()
  for _, t in pairs(tiles) do t.pending = true end
end

-- Which of the capped groups a tile at distance d still shows (changes re-run its rows).
groupBand = function(t, d)
  local k = 0
  for _, cap in pairs(M.GROUP_MAX_M) do if d <= cap then k = k + cap end end
  return k
end

-- Every frame: tiles of other anchors and dropped ones emptied, the others filled / thinned to their
-- distance's rank, M.PER_FRAME items at most.
-- Tree work every EVERY_N-th frame, N times the items: a frame with any Forest change costs the engine's tick a few
-- ms whatever the count (2026-10-15, 100 mph forest drive: tick 3.5 -> 1.65 ms a frame on average, 50 -> 54 fps)
M.EVERY_N = 3
local frameN = 0
function M.update(anchorId, player)
  if not next(tiles) then return end
  if not setup() then return end
  frameN = frameN + 1
  if frameN % M.EVERY_N ~= 0 then return end
  local budget = M.PER_FRAME * M.EVERY_N
  M.lastShapeItems = touchedItems        -- the previous frame's (diagnostics)
  touched, touchedItems = {}, 0
  for key, t in pairs(tiles) do
    if t.anchor ~= anchorId or t.drop then
      local f = t.forestId and scenetree.findObjectById(t.forestId)
      local fd = f and f:getData()
      local left = false
      for i, it in pairs(t.made) do
        if budget <= 0 then return end
        local sh = t.madeShape[i]
        if not sh or allow(sh) then
          if fd then removeItem(fd, it, sh) end
          t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
          budget = budget - 1
        else left = true end
      end
      if not left then
        releaseForest(t)
        tiles[key] = nil
      end
    end
  end
  if not player then return end
  -- nearest tiles first
  local order = {}
  for key, t in pairs(tiles) do
    if t.anchor ~= anchorId or t.drop then goto continue end     -- still being emptied
    local dx = math.max(math.abs(player.x - t.cx) - 128, 0)
    local dy = math.max(math.abs(player.y - t.cy) - 128, 0)
    order[#order + 1] = { key, math.sqrt(dx * dx + dy * dy) }
    ::continue::
  end
  table.sort(order, function(a, b) return a[2] < b[2] end)
  local nearMade = false
  local rowsScanned = 0
  local function finish()
    if nearMade then
      local ok, roads = pcall(require, 'beamngEarth/roads')
      if ok and roads.requestCollision then roads.requestCollision() end
    end
  end
  for _, o in ipairs(order) do
    local t = tiles[o[1]]
    local fd = nil                          -- the tile's Forest data, made when it first needs an item
    local function data() fd = fd or forest(t):getData(); return fd end
    local want = threshold(o[2])
    local band = groupBand(t, o[2])
    local near = o[2] <= M.SOLID_M or (t.near and o[2] <= M.SOLID_M + M.SOLID_HYST_M) or false
    if want ~= t.shown or band ~= t.band or near ~= t.near or t.pending then
      t.pending = false
      if rowsScanned > M.ROWS_PER_FRAME then finish(); return end
      rowsScanned = rowsScanned + #t.rows
      for i, row in ipairs(t.rows) do
        local r = row[8] or 0
        local g = (t.groups or GROUPS)[(row[1] or 0) + 1]
        local cap = g and M.GROUP_MAX_M[g]
        if cap and o[2] > cap then r = 2 end     -- this group is not shown this far out
        -- an item something else stands in for (navgraph.lua's switchable signal heads)
        if g and M.suppress and M.suppress(g, row[2], row[3]) then r = 2 end
        local solid = near and not M.NO_COLLIDE[g or '']
        if r < want and (not t.made[i] or (t.solid[i] == true) ~= solid) then
          local sh, sc = shapeOf(t, i, row)
          if budget <= 0 then t.pending = true; finish(); return end
          if allow(sh) then
            if t.made[i] then removeItem(data(), t.made[i], t.madeShape[i]); budget = budget - 1 end  -- solid <-> not
            t.made[i] = makeItem(data(), row, sh, sc, solid)
            t.solid[i] = (t.made[i] and solid) or nil      -- true or nil
            t.madeShape[i] = t.made[i] and sh or nil
            if solid then nearMade = true end
            budget = budget - 1
          else t.pending = true end                -- another frame (this one's shapes are full)
        elseif r >= want and t.made[i] then
          if budget <= 0 then t.pending = true; finish(); return end
          if allow(t.madeShape[i] or '') then
            removeItem(data(), t.made[i], t.madeShape[i])
            t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
            budget = budget - 1
          else t.pending = true end
        end
      end
      t.shown = want
      t.band = band
      t.near = near
    end
  end
  finish()
end

-- Disconnect, a new server session, mod unload: every item removed.
function M.clear()
  for _, t in pairs(tiles) do
    local f = t.forestId and scenetree.findObjectById(t.forestId)
    local fd = f and f:getData()
    for i, it in pairs(t.made) do if fd then removeItem(fd, it, t.madeShape[i]) end end
    releaseForest(t)
  end
  tiles = {}
end

-- Level end: the level deletes its objects itself.
function M.reset()
  tiles, types, ready, pool, forestCount, pop = {}, {}, false, {}, 0, {}
end

function M.status()
  local n, items, pending = 0, 0, 0
  for _, t in pairs(tiles) do
    n = n + 1
    if t.pending or t.shown == 0 then pending = pending + 1 end
    for _ in pairs(t.made) do items = items + 1 end
  end
  return { tiles = n, items = items, pending = pending, made = M.made, removed = M.removed, errors = M.errors }
end

return M
