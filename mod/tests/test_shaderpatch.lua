-- beamngEarth/shaderpatch.lua: the cloud shader patch for far terrain (pure part, and apply() on a
-- fake file system: delete the old copy, read the game's file, write the patched copy in the mod).
package.path = '../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local T = require('testlib')
local P = require('beamngEarth/shaderpatch')
local check = T.check

local COMMON, RECON
for _, t in ipairs(P.TARGETS) do
  if t:find('cloudCommon', 1, true) then COMMON = t elseif t:find('Reconstruct', 1, true) then RECON = t end
end
check(#P.TARGETS == 2 and COMMON and RECON, 'two target files')

-- excerpts of the game's files (0.39.4), each with every searched line once
local GAME = {
  [COMMON] = [[
// Shared helpers
float cloudSceneDepthKmAtPixel(uint2 p, uint2 size) {
    const float depthNdc = sceneDepth[safeP];
    if (depthNdc <= 1e-6f) {
        return CLOUD_FORCE_SCENE_DEPTH_KM;
    }
    const float3 scenePos = cloudCommon::screenSpaceToWorldPos0Space(ssPos, depthNdc, size);
    if (w > 1e-6f) { return 1.0f; }
}
]],
  [RECON] = [[
float sceneDepthKmAt(int2 p) {
    const float depthNdc = sceneDepth[(uint2)p];
    if (depthNdc <= 1e-6f) return kSkySceneDepthKm; // reversed-Z: clear depth is sky
    const float3 ndc = float3((uv * 2.0f - 1.0f) * float2(1.0f, -1.0f), depthNdc);
}
]],
}

for _, path in ipairs(P.TARGETS) do
  local out, n = P.patch(GAME[path], path)
  check(n == 2, 'both lines patched in ' .. path)
  check(out:find('depthNdc <= 1e-9f', 1, true) and not out:find('depthNdc <= 1e-6f', 1, true), 'the sky test: ' .. path)
  check(out:find('depthNdc + ' .. P.DEPTH_EPS, 1, true) ~= nil, 'the distance read at its nearest: ' .. path)
  check(out:sub(1, #P.HEADER) == P.HEADER, 'the header marks the copy: ' .. path)
  check(select(2, P.patch(out, path)) == 0, 'an already patched file is not patched again: ' .. path)
end
local outC = P.patch(GAME[COMMON], COMMON)
check(outC:find('w > 1e-6f', 1, true) ~= nil, 'other 1e-6 constants are left alone')
for _, path in ipairs(P.TARGETS) do
  for _, e in ipairs(P.EDITS[path]) do
    check(not P.HEADER:find(e[1], 1, true), 'the header does not contain a searched line')
  end
end
-- a game update that changed any one of the lines: the file is left alone entirely
check(select(2, P.patch(GAME[COMMON]:gsub('depthNdc, size%)', 'depth, size)'), COMMON)) == 0, 'one line missing: not patched at all')
check(select(2, P.patch(GAME[COMMON] .. GAME[COMMON], COMMON)) == 0, 'a line twice: not patched (ambiguous)')
check(select(2, P.patch(nil, COMMON)) == 0 and select(2, P.patch('x', 'unknown')) == 0, 'no file / unknown path: nothing')

-- apply() on a fake VFS: the mod's copy overlays the game's file
local game, own = {}, {}
for p, s in pairs(GAME) do game[p] = s end
own[COMMON] = P.HEADER .. 'stale copy from an older game version'
local function lookup(path)
  if path:sub(1, #P.MOD_ROOT) == P.MOD_ROOT then return own, path:sub(#P.MOD_ROOT + 1) end
  return nil, path
end
FS = {
  directoryExists = function(_, p) return p == P.MOD_ROOT end,
  fileExists = function(_, p) local t, k = lookup(p) return t ~= nil and t[k] ~= nil end,
  removeFile = function(_, p) local t, k = lookup(p) if t then t[k] = nil end end,
}
function readFile(p) return own[p] or game[p] end
function writeFile(p, s) local t, k = lookup(p) if not t then return false end t[k] = s return true end
local r = P.apply()
check(r:find('2 line', 1, true) ~= nil, 'apply reports the patched lines: ' .. r)
for _, t in ipairs(P.TARGETS) do
  check(own[t] and own[t]:find('depthNdc <= 1e-9f', 1, true) and not own[t]:find('stale', 1, true),
    'regenerated from the game file, not the stale copy: ' .. t)
end
game[RECON] = 'float g() { return 1.0f; }'   -- a game update changed that file
P.apply()
check(own[RECON] == nil and own[COMMON] ~= nil, 'a changed file loses its patch, the other keeps it')
FS.directoryExists = function() return false end
check(P.apply():find('not unpacked', 1, true) ~= nil, 'a packed mod: skipped')

T.done('test_shaderpatch')
