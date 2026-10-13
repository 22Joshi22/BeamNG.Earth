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
