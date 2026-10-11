-- beamngEarth/shaderpatch.lua: the sky-test patch of the cloud shaders (pure part, and apply() on a
-- fake file system: delete the old copy, read the game's file, write the patched copy in the mod).
package.path = '../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local T = require('testlib')
local P = require('beamngEarth/shaderpatch')
local check = T.check

local GAME = [[
// Shared helpers
float f(float depthNdc) {
    if (depthNdc <= 1e-6f) {
        return 200.0f;
    }
    if (w > 1e-6f) { return 1.0f; }
    return 0.0f;
}
]]
local out, n = P.patch(GAME)
check(n == 1 and out:find('depthNdc <= 1e-9f', 1, true) and not out:find('depthNdc <= 1e-6f', 1, true), 'the sky test is patched')
check(out:find('w > 1e-6f', 1, true) ~= nil, 'other 1e-6 constants are left alone')
check(out:sub(1, #P.HEADER) == P.HEADER and out:sub(#P.HEADER + 1) == GAME:gsub('depthNdc <= 1e%-6f', 'depthNdc <= 1e-9f'), 'only the header and the line change')
check(select(2, P.patch(out)) == 0, 'an already patched file is not patched again')
check(select(2, P.patch('float g() { return 1.0f; }')) == 0, 'a changed game file (no such line) is left alone')
check(select(2, P.patch(nil)) == 0, 'no file: nothing')

-- apply() on a fake VFS: the mod's copy overlays the game's file
local game, own = {}, {}
for _, t in ipairs(P.TARGETS) do game[t] = GAME end
own[P.TARGETS[1]] = P.HEADER .. 'stale copy from an older game version'
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
check(r:find('1 line', 1, true) ~= nil, 'apply reports the patched lines: ' .. r)
for _, t in ipairs(P.TARGETS) do
  check(own[t] and own[t]:find('depthNdc <= 1e-9f', 1, true) and not own[t]:find('stale', 1, true),
    'regenerated from the game file, not the stale copy: ' .. t)
end
game[P.TARGETS[2]] = 'float g() { return 1.0f; }'   -- a game update changed that file
P.apply()
check(own[P.TARGETS[2]] == nil and own[P.TARGETS[1]] ~= nil, 'a changed file loses its patch, the other keeps it')
FS.directoryExists = function() return false end
check(P.apply():find('not unpacked', 1, true) ~= nil, 'a packed mod: skipped')

T.done('test_shaderpatch')
