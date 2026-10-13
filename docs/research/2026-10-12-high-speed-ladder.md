# High-speed ladder, detail near-visual mode (2026-10-12)

Simulated drives and flights with `tools/drive.py` from Fells Point (39.2834, -76.5934), heading north,
server `earthd -v --status-interval 1 --deshadow full --near-visual detail` (512² block at 2 m, 64-cell
tiles). Speeds are increased until something breaks. "Edge margin" is the smallest distance from the
player to the block's collidable edge (negative: past it; the mod's edge warning).

## What broke, and the fixes

| Run | Result | Cause found |
|---|---|---|
| ground 58 m/s, before | fell after 0.5-0.8 km | blocks took 3-13 s to build; the block must move every 128 m (2.2 s at 58 m/s) |
| | | `roadwidth` ray casts: one Python call per ray (vectorised: same results, 4x) |
| | | every block recomputed all 262k heights (`heightmap.ChunkCache`: 128 m chunks, bit-exact, a moved block computes 1/8) |
| | | a map cell's first road plans and widths: 2-10 s (planned ahead along the velocity and within 2 km) |
| | | one interpreter: three far-tile workers made a block 3-10x slower (blocks in their own processes, `blockworker.py`) |
| ground 58 m/s, after | **pass**, 10 km, 0 falls, blocks 0.6-0.9 s | |
| flight 90 m/s, 40 m | edge margin -120 m | the event loop stalled 2.5-3.5 s (stack-sampling watchdog): `surface.prefetch` read whole `.npz` files per pose (now a file test), `group_deltas` ran on the loop (now a thread) |
| | | a request queued behind a prefetch in one block process (now two lanes, 4 waiting threads) |
| | | follow commits waited for a governor heavy slot past the hard commit distance (now urgent there) |
| flight 90 m/s, after | **pass**, 12 km, margin +62 m, 0 stalls | |
| flight 134 m/s, 60 m | **pass**, 16 km, margin +149 m | far-tile backlog grows (+101/min) |
| flight 200 m/s, 80 m | **pass**, 20 km, margin +69 m | far-tile backlog +131/min |
| flight 300 m/s, 400 m | **fail**: margin -730 m | the block requests outrun the answers (p50 1.3 s at altitude); fps 44 |

The ground ladder above 58 m/s is not meaningful on this line: the driver holds the car on the static
collision along a straight line through city blocks, and at 88 m/s it blew up (NaN) on the buildings /
curbs at 0.8 km. The flights hold a fixed height above the start ground: below ~100 m the hills north
of Baltimore come through the car (the user saw it clip and wreck; the streaming numbers stand, the
driver places it every frame). Fly the high rungs at 400 m with a climb (a jump to altitude in one frame
also blows the car up).

## The mod's follow wait counters

`status().followWait` (frames past the commit distance without a commit, by cause): at 200 m/s
`{server 1567, gate 775, governor 0}`; at 300 m/s `{server 2788, gate 41}`. The remaining limit is the
server's answer time against the block's 128 m grid, then the far tiles (the uncover gate).

## Next

- Far tiles: the backlog grows from 134 m/s; they share one interpreter with the server (processes like
  the blocks), and at altitude the horizon adds many.
- The base photo is still rendered in the server process (imagery corrections live there): 0.4-1.3 s of
  a block's answer.
- Commit frames of 60-80 ms in the game (37-51 per run).
- Lazy unloading (the user, 2026-10-12): keep tiles and blocks around when turning back; deletes are not
  a measured cost today.

## Frame rate (2026-10-12/13)

**Roads and sidewalks** (`earthd --sidewalks off`, `--roads off`; a 58 m/s drive north of Fells Point and 30 s
creeping there, one run each):

| | all on | sidewalks off | no roads |
|---|---|---|---|
| drive fps mean / worst second | 42.7 / 27 | 42.5 / 28 | 44.0 / 32 |
| frames over 50 ms | 111 | 89 | 55 |
| worst commit frame | 80 ms | 83 ms | 48 ms |
| creeping fps | 61.4 | 57.7 | 57.9 |

Sidewalks cost nothing measurable; roads add about half the hard dips. `be:reloadCollision()` takes 10 ms
with 27 sidewalk meshes + 30 road meshes + a deck (1.6 ms without, 2026-10-10); the roads module calls it
at most every 0.5 s while tiles arrive, the follow commit once more.

**The block processes are not the frame cost**: a 90 m/s flight at 150 m, `--block-process on` 39.4 fps,
`off` 36.5 fps (and the block fell 3.6 km behind). At 150-400 m the settled far field draws at ~50 fps
against ~90-100 on the ground.

**Where a frame goes** (stationary, toggles): hiding the far tiles, the block, grass, particles, rain, clouds
or SSAO changed nothing; sun shadows off saved ~6 ms. The game's main thread was at 96 % CPU. The game's MCP
`get_performance_metrics` gives a per-subsystem frame breakdown (RenderShadows, Render, Lua, FramePresent,
...); `get_fps_limit_reason` the caps (vsync, 60/30 fps foreground/background limits).

**The game slows down with session age.** The same four fixed free-camera views (`fixedviews.py` in the
session scratch; the orbit camera and the time of day changed the old "stationary baseline" by several ms):
12-13 ms per frame after ~1.5 h of tests, 6.9 ms after a restart (the 144 Hz vsync cap: the fresh game is
faster still). Not the cause: particle emitters (they leak from ground-drive crashes, ~25 per drive; deleting
all of them changed nothing), decals (`decalManagerClear()` changed nothing), scene objects or live
materials (flat). Four 90 m/s flights alone did not age it measurably.

**Root cause of the dips and the "aging" (2026-10-13): the server's memory traffic.** A time series of the
game's own frame breakdown at a fixed view showed the cost alternating between two states every
~15-20 s (shadows 3.4 vs 7-10 ms, ticking 0.75 vs 1.5-2.2 ms, reflections 1.8 vs 4-5.5 ms) with the player
parked and nothing streaming. Not the vehicle (physics paused: same cycle), not the weather (clear, no
precipitation). It followed the server's CPU exactly: the colour-seam analysis (`colormatch`, ~24 s per
region, region after region while the player explores) ran at 100 % of a core in those phases, and
with the server frozen (SIGSTOP) the game stayed in the low state for 40 s. Not SMT sharing (the threads
were on different physical cores): a pure CPU loop changed nothing, one numpy thread streaming large
arrays made the shadow pass 5-6x slower (3.4 -> 16-20 ms). The game's renderer is that sensitive to memory
bandwidth. The "aging" was the analyses' backlog growing with the explored area (plus the test harness's
own numpy work); the leaked particle emitters (BNGP_*, the stock vehicle particles, ~11 per km of
scraping) were a red herring: deleting them changed nothing once measured at a fixed view.

Fix: `pacer.py`, the background analyses run at most `--background-duty` (0.25) of the time. Measured at a
fresh location while they ran: the shadow pass 4.5-6.5 ms instead of 7-10 (baseline 4.0). The remaining
bumps are single ~0.5 s numpy steps. The far-tile geometry moved to worker processes too
(`blockworker.far_geometry`), and the shadow pass is about 1.2 ms cheaper with no roads (the road and
sidewalk meshes cast sun shadows).

**The block-move frame** (`status().commits[i].engine`, the engine's breakdown of the commit frame, 58 m/s
drive): 70-94 ms = ~30 ms Lua (`importMaps` 19-21 ms + `reloadCollision` 8-17 ms), ~20 ms physics (the
collision rebuild), ~13 ms shadows (the changed terrain). The `.ter` swap of §7 of the engine-limits note
(7-11 ms, no collision rebuild) is the next step for it; it changes how the physics ground under the car
loads, so it waits for the user's OK.

**Road collision rebuilds**: every `reloadCollision` is a ~20 ms physics frame; the roads module ran one up
to every 0.5 s while sidewalk tiles arrived. At most every 3 s, and none right after a terrain commit:
p99 frame 38.2 -> 17.2 ms on a 58 m/s drive.

**Lazy unloading** (the user's suggestion): an out-and-back flight imported 15.5 tiles/s on the way back
against 5.6 on the way out. Removed tiles now wait hidden as spares (120 s, at most 400) and are revived:
7.8 tiles/s on the way back, 369 of ~1,770 tile loads revived.

**After tonight** (fresh game, monitors off): a 58 m/s drive through Fells Point at 101-103 fps mean,
p99 17 ms; flights at 134 m/s (400 m) 86 fps with a flat far-tile backlog, 300 m/s 80 fps (it was 44)
but the block still falls behind there (~1 s per block answer).

**2026-10-13, with the user's OK:**
- **`.ter` swap.** The server writes every block as a `.ter` (`heightmap.write_ter`, a new name per build:
  the engine skips a name it already has). Terrain-follow moves load it with `terrainFile` + `postApply`
  (`terrain.swap`) instead of `importMaps` + `reloadCollision`. Heights are identical to the import (9
  points, in-game).
  - Result, 58 m/s drive: every commit swapped, 11-14 ms of Lua, commit frames 32-42 ms (were 70-94),
    p50 9.4 ms, p99 18.5 ms, 101 fps mean, no falls.
  - Teleports, rebases and the first block still import.
- **Altitude rule** (the user: stop following at high altitude, bring it back low).
  - The far tiles do not collide, so the server answers each pose with the highest ground near the
    straight path for the next 10 s (`groundahead.py`, `ground_ahead`).
  - The block holds above 300 m over that ground and resumes below 150 m, counting 8 s of the current
    descent. The server builds no block ahead while it holds (pose `follow`).
  - 300 m/s at 400 m: passes (it failed before), 119 fps mean, worst frame 36 ms.
  - Descent from 400 m at 200 m/s to 60 m at 90 m/s: resumed at ~320 m, block under the car by 234 m;
    25 km pass, min edge margin afterwards 89 m.
- **Terrain detail and altitude.** Nothing chooses detail from the height above sea level: far-tile
  levels follow the horizontal distance only. The horizon radius uses the eye's height above sea level,
  which is right for the curvature (the terrain heights it compares with are above sea level too).
- **What is left of the block-move frame** (~37 ms): Lua ~12 ms (the `.ter` parse), and the shadow pass
  ~14 ms instead of ~1.5 (the frame after `postApply` on a shadow-casting block; physics paused, fixed
  view, 4 of 4 swaps).
  - With the block's `castShadows` off in the swap frame and back on in the next one, the spike goes and
    the swapped heights load (8 of 8). The shadows a few seconds later match a normal swap (synthetic
    120 m ridge, low sun: mean difference < 1 grey level, 17 between two ridge positions).
  - Not used: that frame draws no terrain-cast shadows (west of the ridge the ground goes visibly
    brighter), a one-frame flash in hills at low sun; a slow frame is the smaller evil.
  - Turning it off and on in the same frame keeps the spike. Re-setting `castShadows` to 1 before
    `postApply` once left the old terrain drawn: not safe.
