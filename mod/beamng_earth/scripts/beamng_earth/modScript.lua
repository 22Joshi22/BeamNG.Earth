-- Executed by core/modmanager.lua:704-711 (FS:findFiles('/scripts/', 'modScript.lua') + dofile)
-- once the mod is mounted. 'manual' keeps the extension loaded across level changes.
setExtensionUnloadMode('beamngEarth_core', 'manual')
extensions.load('beamngEarth_core')
