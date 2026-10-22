-- luajit mod/tests/test_pauseowner.lua
-- Who holds the game's pause: the mod's waits (drop hold, edge guard) and the player's own pause.
local dir = (arg and arg[0] or ''):match('^(.*)[/\\]') or '.'
local T = dofile(dir .. '/testlib.lua')
log = function() end

simTimeAuthority = { paused = false }
function simTimeAuthority.pause(on) simTimeAuthority.paused = on and true or false end
function simTimeAuthority.getPause() return simTimeAuthority.paused end
local function playerPauseKey() simTimeAuthority.pause(not simTimeAuthority.paused) end   -- the game's toggle

local P = require('beamngEarth/pauseowner')
local function fresh() P.unload(); simTimeAuthority.paused = false end

-- 1. a wait pauses and resumes the game
fresh()
T.check(P.take('drophold') and simTimeAuthority.paused, 'a wait pauses the game')
P.release('drophold')
T.check(not simTimeAuthority.paused, 'loading done: resumed')

-- 2. the player paused before: never our pause, never resumed by us
fresh()
simTimeAuthority.pause(true)
T.check(not P.take('edgeguard'), 'already paused by the player: not ours')
P.release('edgeguard')
T.check(simTimeAuthority.paused, '... and the game stays paused')

-- 3. the player pauses while we hold the pause (the user 2026-10-22): kept when loading is done
fresh()
P.take('drophold')
simTimeAuthority.pause(true)               -- e.g. a pause call from the game's pause action / another mod
T.check(P.claimed(), 'the player\'s pause while we hold it is noticed')
P.release('drophold')
T.check(simTimeAuthority.paused, 'loading done: the game stays paused for the player')
T.check(not P.claimed(), '... and the pause is the player\'s alone now')
playerPauseKey()
T.check(not simTimeAuthority.paused, 'the player resumes it themselves')

-- 4. the player resumes during the wait (pause key): the owner lets go, nothing touched
fresh()
P.take('drophold')
playerPauseKey()
P.drop('drophold')
T.check(not simTimeAuthority.paused and not P.holds('drophold'), 'resumed by the player: the wait lets go')
-- ... and pauses again themselves: theirs, a new wait does not take it
playerPauseKey()
T.check(not P.take('edgeguard'), 'paused again by the player: not ours')

-- 5. the pause menu open when loading is done: paused while it is open, resumed when it closes
fresh()
P.take('drophold')
P.setMenu(true)
P.release('drophold')
T.check(simTimeAuthority.paused, 'loading done with the pause menu open: still paused')
P.setMenu(false)
T.check(not simTimeAuthority.paused, 'the menu closed: resumed')

-- 6. the menu opened and closed during the wait: as before (resumed when loading is done)
fresh()
P.take('drophold')
P.setMenu(true)
P.setMenu(false)
P.release('drophold')
T.check(not simTimeAuthority.paused, 'menu opened and closed during the wait: resumed when loading is done')

-- 7. the game's own pause call as the menu opens is not the player's pause key
fresh()
P.take('drophold')
simTimeAuthority.pause(true)               -- the menu's call, just before the menu state arrives
P.setMenu(true)
T.check(not P.claimed(), 'the menu\'s own pause call is not a claim')
P.setMenu(false)
P.release('drophold')
T.check(not simTimeAuthority.paused, '... resumed when loading is done')

-- 8. two waits: the game resumes only when both are done
fresh()
P.take('edgeguard')
T.check(P.take('drophold'), 'a second wait joins our pause')
P.release('edgeguard')
T.check(simTimeAuthority.paused, 'one wait done, the other still waits: paused')
P.release('drophold')
T.check(not simTimeAuthority.paused, 'both done: resumed')

-- 9. a stale hold (the player resumed, the owner not told yet) does not block a new pause
fresh()
P.take('edgeguard')
playerPauseKey()                           -- resumed; the guard has not seen it yet
T.check(P.take('edgeguard') and simTimeAuthority.paused, 'a new wait pauses again')
P.release('edgeguard')
T.check(not simTimeAuthority.paused, '... and resumes')

-- 10. unload gives the game its own pause function back
fresh()
local orig = simTimeAuthority.pause
P.take('drophold')
T.check(simTimeAuthority.pause ~= orig, 'wrapped while in use')
P.unload()
T.check(simTimeAuthority.pause == orig, 'unloaded: the game\'s own pause function back')

T.done('test_pauseowner')
