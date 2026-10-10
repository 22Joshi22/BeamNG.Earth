# Weather probe results (in-game, 2026-10-10, ravine at 36.4946,-118.4057, ground 2134 m)
Tool: tools/probe_weather.py (+ manual horizon shots). The game window was unfocused (30 fps cap),
so every frame-cost number of this run is invalid; redo costs with the window focused.

- core_environment's CloudLayer cache: getCloudCover() = 0.5 before and after the reset, i.e. it
  already sees our runtime cloud object in this session (created before environment first looked).
  Keep the onEditorDeactivated() reset after creating clouds anyway (load order can differ).
- Coverage, camera straight up (cloud base ~2.1 km above the ground): sky fraction 0 at coverage
  0 ... 0.53, 0.1 % at 0.67, 7 % at 0.79, 19 % at 0.90, and the clouds are thin and wispy overhead.
  So the shader-based estimate (0.53 -> 50 % sky) is far off for this layer: visible cloud needs
  coverage >= ~0.75. Horizon views in this narrow canyon only showed small cumulus near the horizon
  at 0.7-0.85. Redo on open ground (Central Valley / Baltimore) looking 15-30 deg up at several
  headings, and try `height` (layer thickness, km; 4 today) and baseColor alpha.
- cirrusDensity defaults to 0: cirrus is invisible whatever cirrusCoverage says. Set density
  (0.3-0.6 to test) when real weather reports high cloud.
- Temperature: the vehicle sees 1.13 C and 83.1 kPa at 2134 m: the game already has a default
  sea-level temperature (~15 C) and lapse rate (15 - 6.5 x 2.13 = 1.1). LevelInfo has a
  temperature curve table. be:getSeaLevelTemperatureK / set... are not exposed to GE Lua in this
  form ('n/a'): set the temperature through the LevelInfo curve (core_environment) instead.
- Ground wind 20 m/s on a parked Vivace (neutral, brakes released by input events): velocity stayed
  exactly 0 after 6 s. Inconclusive: arcade auto-parking-brake or a sleeping physics body can hold
  the car; redo with the car rolling slowly (e.g. 2 m/s coasting) and compare drift with/without wind.
- Rain (Precipitation, rain_medium) created and destroyed fine; cost unmeasured (unfocused window).
