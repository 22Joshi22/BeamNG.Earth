-- The trees' leaves in the season (the user, 2026-10-14: "it's currently fall right now and trees are changing
-- colour"). The ground's photo is graded by the server (server/season.py, hello.season); the 3D trees are stock
-- shapes, whose leaf materials take a tint in our level: each species its own fall colour (oak rust-orange, beech
-- bronze, birch / aspen gold; conifers stay green), so mixed woods vary. The tint grows with the season's amount
-- (the same curve as season.py: ~Oct 27 at 40 N). The materials are the stock maps' too: their original factors
-- come back when our level ends (M.restore).
local M = {}

M.CHECK_S = 5           -- leaf materials load with the first tree of their shape: looked for this often
-- species: the leaf materials (v1.5 baseColorFactor), the old-style merged billboards (diffuseColor) and the tint at
-- full fall (multiplies the material's own factor; above 1 on red: a green leaf texture cannot turn red otherwise)
M.SPECIES = {
  { mats = { 'oak_leaves_01', 'oak_leaves_02', 'oak_leaves_03', 'm_oak_leaves_distant' },
    merged = { 'm_oak_merged_foliage' }, tint = { 1.55, 0.72, 0.32 }, days = -12 },     -- oaks turn last
  { mats = { 'beech_leaves', 'm_beech_leaves_distant' }, merged = { 'm_beech_merged_foliage' },
    tint = { 1.6, 1.0, 0.35 }, days = 0 },
  { mats = { 'Birch_Leaves_01', 'm_birch_leaves_distant' }, merged = {}, tint = { 1.7, 1.3, 0.3 }, days = 10 },
}
M.EASE = 1.6            -- tint = amount ^ EASE: a tree holds its green early in the turn

-- Pure: how far the leaves have turned (0..1) on day-of-year doy at latitude lat (server/season.py
-- season_amounts: none in the tropics, the southern hemisphere half a year later).
function M.fallAmount(doy, lat)
  if math.abs(lat) < 23 then return 0 end
  if lat < 0 then doy = (doy + 182) % 365 end
  local peak = 300 - (math.abs(lat) - 40) * 2.5
  return math.max(0, 1 - math.abs(doy - peak) / 28)
end

-- Pure: a factor {r, g, b, a} times the tint blended by amount t.
function M.blend(orig, tint, t)
  return { orig[1] * (1 + (tint[1] - 1) * t), orig[2] * (1 + (tint[2] - 1) * t), orig[3] * (1 + (tint[3] - 1) * t),
           orig[4] or 1 }
end

local function parse(s)
  if type(s) ~= 'string' or s == '' then return { 1, 1, 1, 1 } end
  if s == 'White' then return { 1, 1, 1, 1 } end
  local v = {}
  for x in s:gmatch('[-%d%.eE]+') do v[#v + 1] = tonumber(x) end
  return { v[1] or 1, v[2] or 1, v[3] or 1, v[4] or 1 }
end

local mode = nil        -- the server's hello.season ('auto', 'fall'), nil = off
local orig = {}         -- material name -> { field, factor } as found
local applied = {}      -- material name -> the amount applied
local lastCheck = -math.huge

function M.setMode(m) mode = m end

-- the species' tint amount now: its own timing (days ahead of the season curve), eased
local function amountNow(lat, days)
  if mode == 'fall' then return 1 end
  if mode ~= 'auto' or not lat then return 0 end
  return M.fallAmount(tonumber(os.date('*t').yday) + (days or 0), lat) ^ M.EASE
end

local function tintOne(name, field, tint, t)
  local m = scenetree.findObject(name)
  if not m then return end
  if not orig[name] then orig[name] = { field, parse(m:getField(field, 0)) } end
  if applied[name] and math.abs(applied[name] - t) < 0.01 then return end
  local c = M.blend(orig[name][2], tint, t)
  m:setField(field, 0, string.format('%.3f %.3f %.3f %.3f', c[1], c[2], c[3], c[4]))
  m:flush()
  m:reload()
  applied[name] = t
end

-- Every frame in our level (lat: the anchor's latitude): the leaf materials tinted to the season, checked every CHECK_S.
function M.update(inOurLevel, lat)
  if not inOurLevel or not scenetree then return end
  local now = os.clock()
  if now - lastCheck < M.CHECK_S then return end
  lastCheck = now
  for _, sp in ipairs(M.SPECIES) do
    local t = amountNow(lat, sp.days)
    for _, n in ipairs(sp.mats) do pcall(tintOne, n, 'baseColorFactor', sp.tint, t) end
    for _, n in ipairs(sp.merged) do pcall(tintOne, n, 'diffuseColor', sp.tint, t) end
  end
end

-- Leaving our level: the stock factors back (other levels share the materials while the game runs).
function M.restore()
  for name, o in pairs(orig) do
    local m = scenetree and scenetree.findObject(name)
    if m then
      pcall(function()
        m:setField(o[1], 0, string.format('%.3f %.3f %.3f %.3f', o[2][1], o[2][2], o[2][3], o[2][4]))
        m:flush()
        m:reload()
      end)
    end
  end
  orig, applied = {}, {}
end

return M
