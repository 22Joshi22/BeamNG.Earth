-- luajit mod/tests/test_hudapps.lua  (our HUD as BeamNG UI apps: mounting, payload, throttled sends)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local H = require('beamngEarth/hudapps')
local sent = {}
guihooks = { trigger = function(name, p) sent[#sent + 1] = { name = name, p = p } end }
H._reset()
local calls = 0
local function get() calls = calls + 1; return { status = 'Elm St | 71 fps', credits = 'Imagery: x', banner = { text = '', level = 'ok', visible = false } } end
T.check(not H.update(1, get) and calls == 0, 'no app on screen: nothing computed or sent')
T.check(H.setMounted('status', true) and H.mounted('status') and H.any(), 'the status app mounted')
T.check(H.setMounted('nonsense', true) == false, 'an unknown app is refused')
T.check(H.update(1, get) and #sent == 1 and sent[1].name == 'BeamNGEarthHud' and sent[1].p.status == 'Elm St | 71 fps', 'sent once mounted')
T.check(sent[1].p.banner == '' and sent[1].p.hidden == false, 'no banner while all is well')
T.check(not H.update(1.1, get), 'throttled within SEND_S')
T.check(not H.update(2, get) and #sent == 1, 'unchanged: not sent again')
T.check(H.update(8, get) and #sent == 2, 'unchanged but stale: sent again (an app opened since)')
H.setMounted('banner', true)
T.check(H.update(8.5, get) and #sent == 3, 'a new app: the current state at once')
local p = H.payload({ banner = { text = 'World server not running', level = 'error', visible = true } })
T.check(p.banner == 'World server not running' and p.bannerLevel == 'error' and p.status == '', 'the banner when there is one')
H.setMounted('status', false); H.setMounted('banner', false)
T.check(not H.any() and not H.mounted('status'), 'all apps gone')
T.done('test_hudapps')
