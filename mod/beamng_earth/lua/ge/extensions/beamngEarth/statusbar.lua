-- A slim status bar at the top of the screen (the user, 2026-10-15: "replace the current status lua window with a
-- status bar at the top of the screen, like BeamMP ... level loading / performance and location info like street,
-- city, country ... simple and precise"). One line, top centre:
--   Thames Street  |  Baltimore, Maryland, United States  |  Loading 120/433  |  71 fps
-- The street comes from the server's map data (geocode.nearest_street), the town / state / country from Photon's
-- reverse geocoding with the position rounded to ~1 km (setting placeNamesOnline); asked with a 'where' message when
-- the car moved WHERE_STREET_M (street) / WHERE_PLACE_M (place), at most every WHERE_MIN_S. Loading shows while the
-- world fills in. Drawn like credits.lua (a background draw-list rectangle and one text, measured only when the text
-- or the viewport changed); hidden in the pause menu and on the map (core passes credits.hiddenByUi()).
local M = {}

M.enabled = true
M.TOP = 6                  -- px from the top of the viewport
M.PAD_X, M.PAD_Y = 10, 4
M.SEP = '  |  '
M.TEXT_RGBA = { 1, 1, 1, 0.92 }
M.BACK_RGBA = { 0.05, 0.06, 0.08, 0.55 }
M.WHERE_STREET_M = 30
M.WHERE_PLACE_M = 1500
M.WHERE_MIN_S = 2.0
M.WHERE_PLACE_S = 120
M.FPS_ALPHA = 0.05

local loc = { street = '', place = '', region = '', country = '' }
local last = nil           -- { x, y, t, px, py, pt } of the last request (street / place)
local nextId = 0
local fps = nil
local layout, textU32, backU32, vecs = nil, nil, nil, nil
M.draws, M.measures, M.requests = 0, 0, 0

-- Pure: the bar's text from the location, the loading line (nil when loaded) and the fps (nil: unknown).
function M.compose(l, loading, f)
  local parts = {}
  if l and l.street ~= nil and l.street ~= '' then parts[#parts + 1] = l.street end
  local where = {}
  for _, k in ipairs({ 'place', 'region', 'country' }) do
    local v = l and l[k]
    if v ~= nil and v ~= '' and v ~= where[#where] then where[#where + 1] = v end
  end
  if #where > 0 then parts[#parts + 1] = table.concat(where, ', ') end
  if loading and loading ~= '' then parts[#parts + 1] = loading end
  if f then parts[#parts + 1] = string.format('%d fps', math.floor(f + 0.5)) end
  return table.concat(parts, M.SEP)
end

-- Pure: the bar's short loading line from core's status text, nil once the world is ready.
function M.loadingLine(status)
  if type(status) ~= 'string' then return nil end
  local done, total = status:match('loading:%s*(%d+)/(%d+)')
  if done then return string.format('Loading %s/%s', done, total) end
  if status:find('Preparing terrain') or status:find('Building terrain') or status:find('Elevation tiles') then
    return 'Building terrain'
  end
  return nil
end

-- Every frame: the fps average (frameMs: the frame's wall time).
function M.frame(frameMs)
  if not frameMs or frameMs <= 0 then return end
  local f = 1000 / frameMs
  fps = fps and (fps + M.FPS_ALPHA * (f - fps)) or f
end

-- Every frame: the 'where' request due now, or nil. now: s; player {x, y, lat, lon}; online: place names allowed.
function M.request(now, player, online)
  if not M.enabled or not player or not player.lat then return nil end
  local moved = last and math.sqrt((player.x - last.x) ^ 2 + (player.y - last.y) ^ 2) or math.huge
  if last and (now - last.t < M.WHERE_MIN_S or moved < M.WHERE_STREET_M) then return nil end
  local placeDue = online and (not last or not last.pt or now - last.pt >= M.WHERE_PLACE_S
    or math.sqrt((player.x - last.px) ^ 2 + (player.y - last.py) ^ 2) >= M.WHERE_PLACE_M)
  last = { x = player.x, y = player.y, t = now, px = last and last.px, py = last and last.py, pt = last and last.pt }
  if placeDue then last.px, last.py, last.pt = player.x, player.y, now end
  nextId = nextId + 1
  M.requests = M.requests + 1
  return { type = 'where', id = nextId, lat = player.lat, lon = player.lon, online = placeDue and true or false }
end

-- The server's where_result: the street always, the place parts when it sent them.
function M.onResult(msg)
  if type(msg) ~= 'table' then return end
  if msg.street ~= nil then loc.street = tostring(msg.street) end
  if msg.place ~= nil or msg.country ~= nil then
    loc.place, loc.region, loc.country = msg.place or '', msg.region or '', msg.country or ''
  end
end

function M.location() return loc end

-- A teleport or a new level: the old place goes.
function M.reset()
  loc = { street = '', place = '', region = '', country = '' }
  last = nil
end

local function u32(im, c) return im.GetColorU322(im.ImVec4(c[1], c[2], c[3], c[4])) end

local function drawImgui(im, text)
  local vp = im.GetMainViewport()
  if not vp then return false end
  local vw = vp.Size.x
  local fsz = im.GetFontSize()
  local L = layout
  if not L or L.text ~= text or L.vw ~= vw or L.fsz ~= fsz then
    local ts = im.CalcTextSize(text)
    L = { text = text, vw = vw, fsz = fsz, w = ts.x, h = ts.y }
    layout = L
    M.measures = M.measures + 1
  end
  if not textU32 then textU32, backU32 = u32(im, M.TEXT_RGBA), u32(im, M.BACK_RGBA) end
  if not vecs then vecs = { im.ImVec2(0, 0), im.ImVec2(0, 0), im.ImVec2(0, 0) } end
  local x = vp.Pos.x + (vw - L.w) / 2
  local y = vp.Pos.y + M.TOP + M.PAD_Y
  local p, a, b = vecs[1], vecs[2], vecs[3]
  p.x, p.y = x, y
  a.x, a.y = x - M.PAD_X, y - M.PAD_Y
  b.x, b.y = x + L.w + M.PAD_X, y + L.h + M.PAD_Y
  local dl = im.GetBackgroundDrawList1()
  im.ImDrawList_AddRectFilled(dl, a, b, backU32, 4)
  im.ImDrawList_AddText1(dl, p, textU32, text, nil)
  return true
end

-- Once per frame: draw the bar. active: our world is shown; hidden: the game's UI covers it; status: core's status
-- text. An error in the imgui calls turns the bar off (logged once).
function M.draw(active, hidden, status)
  if not M.enabled or not active or hidden then return false end
  local text = M.compose(loc, M.loadingLine(status), fps)
  if text == '' then return false end
  local im = rawget(_G, 'ui_imgui')
  if not im or not im.GetMainViewport then return false end
  local ok, drawn = pcall(drawImgui, im, text)
  if not ok then
    M.enabled, M.failed = false, tostring(drawn)
    if log then log('E', 'beamngEarth.statusbar', 'status bar turned off: ' .. M.failed) end
    return false
  end
  if drawn then M.draws = M.draws + 1 end
  return drawn
end

return M
