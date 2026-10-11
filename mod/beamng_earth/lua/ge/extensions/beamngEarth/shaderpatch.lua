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
M.TARGETS = {
  '/shaders/common/clouds/include/cloudCommon.h.hlsl',
  '/shaders/common/clouds/cloudVrtReconstruct.cs.hlsl',
}
M.NEEDLE = 'depthNdc <= 1e-6f'
M.REPLACEMENT = 'depthNdc <= 1e-9f'
-- (the header must not contain NEEDLE itself)
M.HEADER = '// Patched by the BeamNG.Earth mod (lua/ge/extensions/beamngEarth/shaderpatch.lua): the sky\n'
  .. '// test threshold 1e-6 -> 1e-9, so terrain beyond 100 km is not taken for sky. Regenerated from the\n'
  .. "// game's file at every start; removed with the mod.\n"

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

-- Pure: the patched source and the number of replacements (0: leave the file alone).
function M.patch(src)
  if type(src) ~= 'string' or src:sub(1, #M.HEADER) == M.HEADER then return nil, 0 end
  local out, n = plainReplace(src, M.NEEDLE, M.REPLACEMENT)
  if n == 0 then return nil, 0 end
  return M.HEADER .. out, n
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
    local out, n = M.patch(src)
    if not out then
      logWarn('cloud shader patch: "' .. M.NEEDLE .. '" not found in ' .. path .. ' (game update?), left as is')
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
