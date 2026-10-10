-- The "BeamNG.Earth" imgui window (docs/V1_SPEC.md "Mod UI (v1)", docs/V1_1_SPEC.md). Engine-only module.
-- Plain strings go through TextUnformatted: Text/TextWrapped treat their argument as a
-- printf format (common/extensions/ui/imgui_luaintf.lua:291-310).

local M = {}

M.visible = true

local im = nil
local openPtr, latPtr, lonPtr, rebaseKmPtr = nil, nil, nil, nil

local function text(s) im.TextUnformatted(tostring(s)) end

local function wrapped(s)
  im.PushTextWrapPos(0)
  im.TextUnformatted(tostring(s))
  im.PopTextWrapPos()
end

local function fmtLatLon(lat, lon)
  if not lat then return 'n/a' end
  return string.format('%.6f, %.6f', lat, lon)
end

local function ms(v) return v and string.format('%.0f ms', v) or '-' end

-- view: plain table of values to show; actions.teleport(lat, lon) is called on the button,
-- actions.setRebaseDistance(m) when the rebase slider is released.
function M.draw(view, actions)
  if not M.visible then return end
  im = im or ui_imgui
  if not im then return end
  if not openPtr then
    openPtr = im.BoolPtr(true)
    latPtr = im.DoublePtr(view.defaultLat)
    lonPtr = im.DoublePtr(view.defaultLon)
    rebaseKmPtr = im.FloatPtr((view.rebaseDistance or 10000) / 1000)
  end
  openPtr[0] = true

  im.SetNextWindowSize(im.ImVec2(420, 0), im.Cond_FirstUseEver)
  if im.Begin('BeamNG.Earth', openPtr, im.WindowFlags_AlwaysAutoResize) then
    text('Server: ' .. view.connection)
    text('Anchor: ' .. (view.anchorId and ('#' .. view.anchorId .. '  ' .. fmtLatLon(view.anchorLat, view.anchorLon)) or 'none'))
    text('Player: ' .. fmtLatLon(view.playerLat, view.playerLon))
    if view.blockCx then
      text(string.format('Terrain block centre: (%d, %d)', view.blockCx, view.blockCy))
    end
    if view.toFollowPrepare then
      text(string.format('Terrain-follow: request in %.0f m, commit in %.0f m%s', math.max(0, view.toFollowPrepare),
        math.max(0, view.toFollowCommit), view.followPending and ('  - next ' .. view.followPending) or ''))
    end
    if view.dist then
      text(string.format('Distance from origin: %.0f m (rebase in %.0f m)%s', view.dist, math.max(0, view.toRebase),
        view.rebasePending and ('  - anchor ' .. view.rebasePending) or ''))
    end
    text(string.format('Last terrain-follow: %s (%d)   Last rebase: %s (%d)', ms(view.lastFollowMs), view.followCount or 0,
      ms(view.lastRebaseMs), view.rebaseCount or 0))
    if view.farText then text(view.farText) end
    local weather = rawget(_G, 'beamngEarth_weather') -- real weather (weather.lua), when loaded
    if weather and weather.statusLine then text(weather.statusLine()) end
    if rebaseKmPtr then
      im.SliderFloat('Rebase distance (km)', rebaseKmPtr, view.rebaseMin / 1000, view.rebaseMax / 1000, '%.1f km')
      if im.IsItemDeactivatedAfterEdit() and actions.setRebaseDistance then
        rebaseKmPtr[0] = actions.setRebaseDistance(rebaseKmPtr[0] * 1000) / 1000
      elseif not im.IsItemActive() then
        rebaseKmPtr[0] = view.rebaseDistance / 1000 -- follow console changes
      end
    end
    im.Separator()
    im.InputDouble('Latitude', latPtr, 0, 0, '%.6f')
    im.InputDouble('Longitude', lonPtr, 0, 0, '%.6f')
    if im.Button('Teleport') then actions.teleport(latPtr[0], lonPtr[0]) end
    im.Separator()
    wrapped('Status: ' .. (view.status ~= '' and view.status or '-'))
    im.Separator()
    wrapped(view.attribution)
  end
  im.End()
  if not openPtr[0] then M.visible = false end
end

-- Values the window edits, for callers that need them (e.g. auto-teleport on level start).
function M.getLatLon(defaultLat, defaultLon)
  if latPtr then return latPtr[0], lonPtr[0] end
  return defaultLat, defaultLon
end

function M.setLatLon(lat, lon)
  if latPtr then latPtr[0], lonPtr[0] = lat, lon end
end

return M
