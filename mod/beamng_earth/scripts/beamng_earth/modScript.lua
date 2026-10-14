-- Executed by core/modmanager.lua:704-711 (FS:findFiles('/scripts/', 'modScript.lua') + dofile)
-- once the mod is mounted. 'manual' keeps the extension loaded across level changes.
setExtensionUnloadMode('beamngEarth_core', 'manual')
extensions.load('beamngEarth_core')
-- The pause-menu settings page's Lua side (settings.lua) and the dev tools (dev.lua: everything off
-- until the player turns something on; its hooks return at their first line meanwhile).
setExtensionUnloadMode('beamngEarth_settings', 'manual')
extensions.load('beamngEarth_settings')
setExtensionUnloadMode('beamngEarth_dev', 'manual')
extensions.load('beamngEarth_dev')
-- The in-game reports (report.lua: Ctrl+Shift+N a report / GitHub issue, Ctrl+Shift+B the 30 s performance profiler).
setExtensionUnloadMode('beamngEarth_report', 'manual')
extensions.load('beamngEarth_report')
