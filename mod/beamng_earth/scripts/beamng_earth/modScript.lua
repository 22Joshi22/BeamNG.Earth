-- Executed by core/modmanager.lua:704-711 (FS:findFiles('/scripts/', 'modScript.lua') + dofile)
-- once the mod is mounted. 'manual' keeps the extension loaded across level changes.
-- The real horizon needs the cloud shaders' sky test patched (shaderpatch.lua), before any level.
pcall(function() require('beamngEarth/shaderpatch').apply() end)
setExtensionUnloadMode('beamngEarth_core', 'manual')
extensions.load('beamngEarth_core')
-- The pause-menu settings page's Lua side (settings.lua) and the dev tools (dev.lua: everything off
-- until the player turns something on; its hooks return at their first line meanwhile).
setExtensionUnloadMode('beamngEarth_settings', 'manual')
extensions.load('beamngEarth_settings')
setExtensionUnloadMode('beamngEarth_dev', 'manual')
extensions.load('beamngEarth_dev')
