import { lua } from "@/bridge"

// BeamNG.Earth: a button in the shared "Mods" tab of the Pause Menu opening the settings page
// (same pattern as the taa_standalone mod). Lua side: lua/ge/extensions/beamngEarth/settings.lua.
export async function onLoad() {
  await lua.extensions.ui_pause_actions.registerModButton({
    id: "beamngEarth-settings",
    tabId: "mods",
    label: "BeamNG.Earth",
    icon: "globe",
    componentName: "/ui/ui-vue/mods/beamngEarth/EarthSettings.vue",
  })
  // tell the mod the page exists: its imgui window becomes a compact HUD (settings.lua)
  if (window.bngApi && window.bngApi.engineLua) {
    window.bngApi.engineLua("if beamngEarth_settings then beamngEarth_settings.setPageExists(true) end")
  }
}

export async function onUnload() {
  await lua.extensions.ui_pause_actions.unregisterModButton("beamngEarth-settings")
  if (window.bngApi && window.bngApi.engineLua) {
    window.bngApi.engineLua("if beamngEarth_settings then beamngEarth_settings.setPageExists(false) end")
  }
}
