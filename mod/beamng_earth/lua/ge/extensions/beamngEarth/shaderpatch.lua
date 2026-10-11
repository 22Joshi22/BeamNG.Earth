-- BeamNG.Earth: a one-line patch of the game's volumetric cloud shaders for the real horizon.
-- Called once by scripts/beamng_earth/modScript.lua when the mod is mounted (before any level).
--
-- The cloud shaders decide "this pixel is sky" from the reversed-Z depth: depthNdc <= 1e-6. With the
-- camera's 0.1 m near plane that is everything beyond 0.1 / 1e-6 = 100 km, so terrain farther than
-- 100 km (the far field reaches 400+ km with the real horizon) counted as sky and clouds up to the
-- sky fallback distance (200 km) were drawn over it (in-game 2026-10-10: cloud patches on the
-- far terrain from a plane at 2.9 km, unchanged by view distance or cloud altitude, gone with the
-- CloudLayer hidden). The patch lowers the threshold to 1e-9 (100,000 km); the clear depth is exactly
-- 0, so real sky stays sky.
--
-- Every start: the old patched copies are deleted, the game's current files are read, the line is
-- replaced only where it is found exactly (a game update that changes it is left alone and logged),
-- and the result is written into this mod's own folder at the same path, so the mod's mount puts it
-- over the game's file and disabling the mod removes it. A packed (zip) mod cannot write into
-- itself: the patch is skipped there (follow-up in docs/ROADMAP.md).

local M = {}

M.MOD_ROOT = '/mods/unpacked/beamng_earth'
-- Per file: the exact lines to replace, all of which must be found (else the file is left alone).
--  1. the sky test: depthNdc <= 1e-6 -> 1e-9 (sky only at the clear depth);
--  2. the scene distance from the depth: depthNdc + DEPTH_EPS, i.e. the nearest distance the stored
--     depth allows. At 150-250 km the depth (~0.1 m / d = 4e-7..7e-7) is coarse; a distance read too
--     far let clouds 200-230 km away show through the terrain just below the horizon, as flipped
--     copies of the clouds above it (in-game 2026-10-11, from a plane at 3 km). Reading it
--     nearer only clips clouds that may lie behind the terrain.
M.DEPTH_EPS = '1.2e-7f'
M.EDITS = {
  ['/shaders/common/clouds/include/cloudCommon.h.hlsl'] = {
    { 'depthNdc <= 1e-6f', 'depthNdc <= 1e-9f' },
    { 'cloudCommon::screenSpaceToWorldPos0Space(ssPos, depthNdc, size)',
      'cloudCommon::screenSpaceToWorldPos0Space(ssPos, depthNdc + ' .. M.DEPTH_EPS .. ', size)' },
  },
  ['/shaders/common/clouds/cloudVrtReconstruct.cs.hlsl'] = {
    { 'depthNdc <= 1e-6f', 'depthNdc <= 1e-9f' },
    { 'float3((uv * 2.0f - 1.0f) * float2(1.0f, -1.0f), depthNdc);',
      'float3((uv * 2.0f - 1.0f) * float2(1.0f, -1.0f), depthNdc + ' .. M.DEPTH_EPS .. ');' },
  },
}
M.TARGETS = {}
for path in pairs(M.EDITS) do M.TARGETS[#M.TARGETS + 1] = path end
table.sort(M.TARGETS)
-- (the header must not contain any of the searched lines itself)
M.HEADER = '// Patched by the BeamNG.Earth mod (lua/ge/extensions/beamngEarth/shaderpatch.lua): the sky\n'
  .. '// test threshold 1e-6 -> 1e-9 and the scene distance read at its nearest, so clouds are not drawn\n'
  .. "// over terrain beyond 100 km. Regenerated from the game's file at every start; removed with the mod.\n"

local function plainReplace(s, needle, repl)
  local out, n, i = {}, 0, 1
  while true do
    local a, b = s:find(needle, i, true)
    if not a then break end
    out[#out + 1] = s:sub(i, a - 1)
    out[#out + 1] = repl
    n = n + 1
    i = b + 1
  end
  out[#out + 1] = s:sub(i)
  return table.concat(out), n
end

-- Pure: the patched source of `path` and the number of replaced lines (0: leave the file alone,
-- because it is already patched, missing, or a searched line is not there exactly once).
function M.patch(src, path)
  if type(src) ~= 'string' or src:sub(1, #M.HEADER) == M.HEADER then return nil, 0 end
  local edits = M.EDITS[path or M.TARGETS[1]]
  if not edits then return nil, 0 end
  local out, total = src, 0
  for _, e in ipairs(edits) do
    local n
    out, n = plainReplace(out, e[1], e[2])
    if n ~= 1 then return nil, 0 end
    total = total + n
  end
  return M.HEADER .. out, total
end

local function logInfo(msg) if log then log('I', 'beamngEarth.shaderpatch', msg) else print(msg) end end
local function logWarn(msg) if log then log('W', 'beamngEarth.shaderpatch', msg) else print(msg) end end

-- Returns a short result line (also logged).
function M.apply()
  if not (FS and FS.directoryExists and FS:directoryExists(M.MOD_ROOT)) then
    local r = 'cloud shader patch skipped: the mod is not unpacked at ' .. M.MOD_ROOT
    logWarn(r)
    return r
  end
  local done = {}
  for _, path in ipairs(M.TARGETS) do
    local own = M.MOD_ROOT .. path
    if FS:fileExists(own) then FS:removeFile(own) end
    local src = readFile(path)
    local out, n = M.patch(src, path)
    if not out then
      logWarn('cloud shader patch: the expected lines are not all in ' .. path .. ' (game update?), left as is')
      done[#done + 1] = path .. ': skipped'
    elseif writeFile(own, out) == false then
      logWarn('cloud shader patch: could not write ' .. own)
      done[#done + 1] = path .. ': write failed'
    else
      done[#done + 1] = path .. ': ' .. n .. ' line(s)'
    end
  end
  local r = 'cloud shader patch: ' .. table.concat(done, '; ')
  logInfo(r)
  return r
end

return M
