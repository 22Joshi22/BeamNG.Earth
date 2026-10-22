-- Who holds the game's pause (simTimeAuthority, what the pause key does).
--
-- The mod pauses the game while it waits for the world: the drop hold (core.lua, drophold.lua) and the edge guard
-- (edgeguard.lua). Each such wait is an owner here. The game resumes when the last owner lets go, unless the player
-- wants it paused (the user 2026-10-22: "if the user pauses whilst the world is paused the game should respect that
-- and not unpause when loading/waiting for server is done"):
--   - the player pauses while we hold the pause (a pause(true) call that is not ours: simTimeAuthority.pause is
--     wrapped): the pause is theirs, it stays when loading is done, until they resume it;
--   - the pause menu is open when loading is done (core.lua's onUiChangedState, 'menu*' states): the game stays
--     paused while the menu is open and resumes when it closes (opened and closed during the wait: as before).
-- The player resuming (the pause key while paused) stays what it was: the owners see the game running and let go
-- without touching the pause (M.drop). A pause the player set before the mod wanted one is never ours (M.take
-- returns false), as before.

local M = {}

local held = {}            -- owner name -> true
local claimed = false      -- the player paused while the mod held the pause: theirs now
local claimedAt = -math.huge -- (os.clock) when; the game's own pause call as the menu opens is not the player's
local MENU_CALL_S = 0.5
local menuOpen = false     -- the pause menu is open
local resumeOnMenu = false -- loading finished with the menu open: resume when it closes
local inOwn = false        -- our own call to the game's pause (the wrapper does not count it)
local wrap = nil           -- { sta, orig, fn }: the wrapped pause function

local function sta() return rawget(_G, 'simTimeAuthority') end
local function anyHeld() return next(held) ~= nil end

-- (Re)install the wrapper on the game's pause function: a reloaded simTimeAuthority (extensions reload, tests) has a
-- fresh table, so this checks each time it is used.
local function install()
  local s = sta()
  if not s or type(s.pause) ~= 'function' then return s end
  if wrap and wrap.sta == s and s.pause == wrap.fn then return s end
  local w = { sta = s, orig = s.pause }
  w.fn = function(on, ...)
    if not inOwn then
      if on and anyHeld() and not menuOpen then claimed, claimedAt = true, os.clock() end
      resumeOnMenu = false            -- someone else decided about the pause: not ours to end any more
    end
    return w.orig(on, ...)
  end
  s.pause = w.fn
  wrap = w
  return s
end

-- Put the game's own pause function back (the extension unloads).
local function uninstall()
  if wrap and wrap.sta and wrap.sta.pause == wrap.fn then wrap.sta.pause = wrap.orig end
  wrap = nil
end

local function setPause(on)
  local s = install()
  if not (s and s.pause) then return end
  inOwn = true
  local ok, err = pcall(s.pause, on)
  inOwn = false
  if not ok and rawget(_G, 'log') then log('E', 'beamngEarth', 'pause: ' .. tostring(err)) end
end

local function info(text)
  if rawget(_G, 'log') then log('I', 'beamngEarth', text) end
end

function M.paused()
  local s = sta()
  return s and s.getPause and s.getPause() or false
end

-- An owner wants the game paused. True: it holds the pause now (it paused the game, or joins another owner of
-- ours). False: the game was already paused by the player, the pause is not ours to end.
function M.take(owner)
  -- the game runs: whatever was held was resumed by the player (an owner not told yet), start afresh
  if not M.paused() then held, claimed, resumeOnMenu = {}, false, false end
  if held[owner] then return true end
  if M.paused() and not anyHeld() then return false end
  held[owner] = true
  if not M.paused() then setPause(true) else install() end
  return true
end

-- The owner's wait is over. The game resumes when no owner is left, unless the player wants it paused.
function M.release(owner)
  if not held[owner] then return end
  held[owner] = nil
  if anyHeld() then return end
  if claimed then
    claimed = false
    info('pause: loading done, the game stays paused (paused by the player)')
    return
  end
  if menuOpen and M.paused() then
    resumeOnMenu = true
    info('pause: loading done, the game resumes when the pause menu closes')
    return
  end
  if M.paused() then setPause(false) end
end

-- The owner lets go without touching the pause (the player resumed the game themselves).
function M.drop(owner)
  held[owner] = nil
  if not anyHeld() then claimed, resumeOnMenu = false, false end
end

-- The pause menu opened or closed (core.lua's onUiChangedState).
function M.setMenu(open)
  open = open and true or false
  if open == menuOpen then return end
  menuOpen = open
  -- a pause call just before the menu state arrived is the menu's own, not the player's pause key
  if open and claimed and os.clock() - claimedAt < MENU_CALL_S then claimed = false end
  if not open and resumeOnMenu then
    resumeOnMenu = false
    if not anyHeld() and not claimed and M.paused() then setPause(false) end
  end
end

function M.holds(owner) return held[owner] == true end
function M.claimed() return claimed end

-- A new level: nothing is held any more (the pause itself is left as it is).
function M.reset()
  held, claimed, resumeOnMenu = {}, false, false
end

-- The extension unloads: also the game's own pause function back.
function M.unload()
  M.reset()
  menuOpen = false
  uninstall()
end

return M
