# gcpatch: keep the grass when the physics block moves (optional, Linux)

BeamNG.Earth moves its hidden physics TerrainBlock under the player every ~128 m. The engine
then frees every grass cell on that block. The mod hides this by regenerating the grass in the
same frame, at the cost of one slower frame per move (docs/ARCHITECTURE.md "Ground surfaces").
This preload library removes the cause instead. It changes one byte of the engine's GroundCover
terrain-update handler in the game's memory at startup, so that height and layer updates no longer
free grass. The mod still rebuilds the grass itself where the world really moves: teleports and
floating-origin rebases.

- **Only BeamNG.drive 0.39.4, Linux x86-64.** It checks the handler's first 54 bytes and does
  nothing on any other build, for example after a game update.
- Nothing on disk changes. Without the launch option the game is stock.
- It acts only in the main `BeamNG.drive.x64` process, not in the CEF helpers or Steam.
- Unsupported by BeamNG. A wrong patch crashes the game, which a restart without the launch
  option fixes.

## Install
```sh
tools/gcpatch/build.sh
```
Then go to Steam > BeamNG.drive > Properties > Launch options and enter the line that
`build.sh` prints:
```
LD_PRELOAD="/path/to/tools/gcpatch/build/libbngearth_gcpatch.so:$LD_PRELOAD" %command%
```
Then restart the game.

## Check
After the game starts, look at
`~/.local/share/BeamNG/BeamNG.drive/current/temp/beamngEarth_gcpatch.txt`.
It contains one line, such as `applied: grass kept across terrain block moves`, or the reason
it did nothing.

## Turn off
Remove the launch option, or add `BNGEARTH_GCPATCH_OFF=1` in front of it.
