#!/usr/bin/env python3
"""Performance on a real drive: the car drives itself along a GPS route (the map's Autodrive, mod gps.lua: the car's
own AI on our road graph, in lane, at the legal speed, minding traffic) while the frame times are sampled every
second. Unlike tools/drive.py (the car moved along a profile by a script) the car really drives: streaming,
traffic, physics and the GPS all run as in play, and the same route gives the same drive again.

  python3 tools/autodrive_perf.py --to 39.2634,-76.5797 --minutes 5 --out /tmp/ad
  python3 tools/autodrive_perf.py --from 39.2834,-76.5934 --to 39.2634,-76.5797 --minutes 5 --out /tmp/ad --obs

--from teleports there first and waits for the world around it. --obs records the screen with OBS Studio (the
Flatpak, its saved scene) and writes <out>/drive.mp4 without its audio track (the microphone is in it); only when
OBS is not already open (the user's own OBS is never touched: a second instance only shows "already running", and
stopping "obs" by name once closed theirs).

Checks (the user, 2026-10-14: "add a check to see if the car crashes or stops"): a crash (the car's damage jumps by
CRASH_DAMAGE between polls), a stop (slower than STALL_MS for STOP_S while the destination is still ahead) and
leaving the route by OFF_ROUTE_M are events: events.jsonl, a screenshot each (event_<n>.jpg), listed in the
summary. A crash or a stop ends the run (--keep-going drives on).
Writes <out>/samples.jsonl (one poll per line) and <out>/summary.json; prints the summary. The game's MCP server
must be on (Options > Advanced) and the companion server running.
"""
import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcp  # noqa: E402

POLL_S = 1.0
STALL_MS = 0.5          # slower than this (m/s) counts as standing
STOP_S = 45.0           # standing this long, not arrived: a stop (longer than a red light)
CRASH_DAMAGE = 1500.0   # the damage rising this much between two polls: a crash (a curb scrape is a few hundred)
OFF_ROUTE_M = 25.0
ARRIVED_M = 60.0
OBS_APP = "com.obsproject.Studio"
OBS_VIDEOS = os.path.expanduser("~/Videos")

LUA_POLL = r'''
if not beamngEarth_perfmon then extensions.load('beamngEarth_perfmon') end
local pm = beamngEarth_perfmon
if pm and pm.tagHarness then pm.tagHarness() end
local p = pm and pm.snapshot() or {}
local g = require('beamngEarth/gps').status() or {}
local v = getPlayerVehicle(0)
local pos, vel = v:getPosition(), v:getVelocity()
local s = loadstring('return ' .. beamngEarth_core.status())() or {}
local pl = s.player or {}
local ob = map and map.objects and map.objects[v:getId()]
local rs = require('beamngEarth/roads').status() or {}
return jsonEncode({ roadsQ = rs.walksQueued, roadMeshes = rs.roadMeshes, t = os.clock(), x = pos.x, y = pos.y, z = pos.z, v = math.sqrt(vel.x ^ 2 + vel.y ^ 2), lat = pl.lat,
  lon = pl.lon, damage = ob and ob.damage or 0,
  perf = { frames = p.frames, avgMs = p.avgMs, p50 = p.p50, p95 = p.p95, p99 = p.p99, maxMs = p.maxMs,
           over33 = p.over33, over50 = p.over50, over100 = p.over100 },
  gps = { remaining = g.remaining, off = g.off, arrows = g.arrows, autodrive = g.autodrive, planning = g.planning },
  pending = s.pending, anchor = s.anchorId,
  hitches = beamngEarth_core.hitches and beamngEarth_core.hitches() or {} })
'''


def lua(code):
    return mcp.call("run_lua", {"code": code})


def lua_json(code):
    return json.loads(lua(code))


def latlon(text):
    a, b = (float(x) for x in text.split(","))
    return a, b


def obs_pids():
    r = subprocess.run(["pgrep", "-x", "obs"], capture_output=True, text=True)
    return {int(x) for x in r.stdout.split()}


def obs_start():
    """Launch OBS recording; (files before, our OBS pids) or None when OBS is open already or did not record."""
    if obs_pids():
        print("OBS is already open: not recording (close it to let the drive be recorded)", file=sys.stderr)
        return None
    before = set(glob.glob(os.path.join(OBS_VIDEOS, "*")))
    subprocess.Popen(["flatpak", "run", OBS_APP, "--startrecording", "--minimize-to-tray", "--disable-shutdown-check"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(1)
        if set(glob.glob(os.path.join(OBS_VIDEOS, "*"))) - before:
            return before, obs_pids()
    print("OBS did not start recording", file=sys.stderr)
    for pid in obs_pids():
        os.kill(pid, 9)
    return None


SHARE_MB = 29.0         # the small copy for sending (a chat upload limit of 30 MB)


def share_copy(video):
    """A 720p 30 fps copy of video beside it (drive_small.mp4) under SHARE_MB, or None."""
    out = os.path.join(os.path.dirname(video), "drive_small.mp4")
    for crf in (30, 34, 38):
        subprocess.run(["nice", "ffmpeg", "-v", "error", "-y", "-i", video, "-vf", "scale=1280:-2,fps=30", "-c:v",
                        "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags",
                        "+faststart", out], check=True)
        if os.path.getsize(out) <= SHARE_MB * 1024 * 1024:
            return out
    return None


def obs_stop(started, out_path):
    """Stop the OBS we launched (SIGTERM: it ends the recording and quits) and write its file without audio."""
    before, pids = started
    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    for _ in range(30):
        if not (obs_pids() & pids):
            break
        time.sleep(1)
    new = sorted(set(glob.glob(os.path.join(OBS_VIDEOS, "*"))) - before, key=os.path.getmtime)
    if not new:
        return None
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", new[-1], "-map", "0:v", "-c", "copy", out_path], check=True)
    os.remove(new[-1])
    return out_path


def shot(out_dir, name):
    """A screenshot of the game into out_dir/name (best effort)."""
    try:
        mcp.quiet_ui()
        p = mcp.call("screenshot", {"jpg": True}).strip()
        for _ in range(20):
            time.sleep(0.25)
            if os.path.exists(p) and os.path.getsize(p) > 0:
                break
        time.sleep(0.5)
        shutil.move(p, os.path.join(out_dir, name))
        return name
    except (Exception, SystemExit) as e:
        return f"no screenshot: {e}"


class Checks:
    """The run's events from the polls (pure but for the screenshot callback): crash, stop, off route."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.prev = None
        self.still_since = None
        self.off = False
        self.events = []

    def feed(self, r):
        g = r["gps"]
        rem = g.get("remaining")
        ahead = rem is None or rem > ARRIVED_M
        ev = None
        if self.prev is not None and (r.get("damage") or 0) - (self.prev.get("damage") or 0) > CRASH_DAMAGE:
            ev = {"kind": "crash", "damage": round(r["damage"]), "mph_before": round(self.prev["v"] * 2.23694, 1)}
        if r["v"] < STALL_MS and ahead and not g.get("planning"):
            self.still_since = self.still_since if self.still_since is not None else r["t"]
            if ev is None and r["t"] - self.still_since >= STOP_S:
                ev = {"kind": "stop", "seconds": round(r["t"] - self.still_since)}
                self.still_since = float("inf")     # once per stop
        else:
            self.still_since = None
        off = (g.get("off") or 0) > OFF_ROUTE_M
        if ev is None and off and not self.off:
            ev = {"kind": "off_route", "m": round(g.get("off"), 1)}
        self.off = off
        self.prev = r
        if ev is not None:
            ev.update({"t": round(r["t"], 1), "x": round(r["x"], 1), "y": round(r["y"], 1), "lat": r.get("lat"),
                       "lon": r.get("lon"), "arrows": g.get("arrows")})
            ev["screenshot"] = self.on_event(len(self.events), ev)
            self.events.append(ev)
        return ev


def hitch_cause(h):
    """The part a hitch frame went to: ours (mod.<part>) when it took 40 % of the frame, else the engine's largest."""
    mod = h.get("mod") or {}
    if mod:
        k, v = max(mod.items(), key=lambda kv: kv[1])
        if v >= 0.4 * h["ms"]:
            return "mod." + k
    eng = h.get("engine") or {}
    if eng:
        return "engine." + max(eng.items(), key=lambda kv: kv[1])[0]
    return "unknown"


def summarize(rows):
    """The drive's statistics from the polls (frames weighted by their count)."""
    frames = sum(r["perf"].get("frames") or 0 for r in rows)
    def wavg(key):
        n = sum((r["perf"].get("frames") or 0) for r in rows if r["perf"].get(key) is not None)
        return sum((r["perf"].get(key) or 0) * (r["perf"].get("frames") or 0) for r in rows) / n if n else None
    dist = sum(math.hypot(b["x"] - a["x"], b["y"] - a["y"]) for a, b in zip(rows, rows[1:])
               if a.get("anchor") == b.get("anchor") and math.hypot(b["x"] - a["x"], b["y"] - a["y"]) < 200)
    dur = rows[-1]["t"] - rows[0]["t"] if len(rows) > 1 else 0
    moving = [r for r in rows if r["v"] >= STALL_MS]
    avg_ms = wavg("avgMs")
    return {
        "polls": len(rows), "seconds": round(dur, 1), "frames": frames,
        "fps": round(1000 / avg_ms, 1) if avg_ms else None,
        "frame_ms": {"avg": avg_ms and round(avg_ms, 2), "p50": wavg("p50") and round(wavg("p50"), 2),
                     "p95": wavg("p95") and round(wavg("p95"), 2), "p99": wavg("p99") and round(wavg("p99"), 2),
                     "max": max((r["perf"].get("maxMs") or 0) for r in rows)},
        "frames_over": {k: sum(r["perf"].get(k) or 0 for r in rows) for k in ("over33", "over50", "over100")},
        "miles": round(dist / 1609.344, 2),
        "avg_mph_moving": round(sum(r["v"] for r in moving) / len(moving) * 2.23694, 1) if moving else 0,
        "standing_s": round(sum(POLL_S for r in rows if r["v"] < STALL_MS), 1),
        "off_route_max_m": round(max((r["gps"].get("off") or 0) for r in rows), 1),
        "arrows_fallback_polls": sum(1 for r in rows if not str(r["gps"].get("arrows") or "").startswith(("r", "final"))),
        "remaining_m": rows[-1]["gps"].get("remaining"),
        "hitch_causes": _causes(rows),
    }


def _causes(rows):
    """{cause: [count, worst ms]} of the hitch frames (core.lua HITCH_MS), most frequent first."""
    out = {}
    for r in rows:
        hs = r.get("hitches") or []
        for h in (hs.values() if isinstance(hs, dict) else hs):
            c = hitch_cause(h)
            n, w = out.get(c, (0, 0))
            out[c] = (n + 1, max(w, h["ms"]))
    return {k: list(v) for k, v in sorted(out.items(), key=lambda kv: -kv[1][0])}


# The car on the nearest road-graph node (a teleport lands it where the point is, a sidewalk or a field: the AI
# then drove along a sidewalk into a pole, 2026-10-15), facing the linked node that points most toward the goal.
SNAP_LUA = r"""
local tlat, tlon = %r, %r
local m = map.getMap()
local veh = getPlayerVehicle(0)
if not (m and m.nodes and veh) then return 'snap: no graph' end
local p = veh:getPosition()
local best, bd = nil, math.huge
for id, n in pairs(m.nodes) do
  if n.pos and next(n.links or {}) then
    local d = (n.pos.x - p.x) ^ 2 + (n.pos.y - p.y) ^ 2
    if d < bd then best, bd = id, d end
  end
end
if not best then return 'snap: no node' end
local st = beamngEarth_core and beamngEarth_core._state and beamngEarth_core._state()
local a = st and st.anchor
local n = m.nodes[best]
local gx, gy = 0, 0
if a and a.geodeticToEnu then gx, gy = a:geodeticToEnu(tlat, tlon, 0) end
local dir, bs = nil, -math.huge
for other in pairs(n.links) do
  local o = m.nodes[other]
  if o and o.pos then
    local dx, dy = o.pos.x - n.pos.x, o.pos.y - n.pos.y
    local L = math.sqrt(dx * dx + dy * dy)
    local tx, ty = gx - n.pos.x, gy - n.pos.y
    local T = math.sqrt(tx * tx + ty * ty)
    local sc = (L > 0 and T > 0) and (dx * tx + dy * ty) / (L * T) or 0
    if sc > bs then bs, dir = sc, vec3(dx / L, dy / L, 0) end
  end
end
dir = dir or vec3(0, 1, 0)
local rot = quatFromDir(dir, vec3(0, 0, 1))
local z = be:getSurfaceHeightBelow(n.pos + vec3(0, 0, 3))
veh:setPositionRotation(n.pos.x, n.pos.y, ((z and z > -1e6) and z or n.pos.z) + 0.5, rot.x, rot.y, rot.z, rot.w)
return string.format('snap: node %%s %%.1f m away', tostring(best), math.sqrt(bd))
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=latlon, help="lat,lon to teleport to first")
    ap.add_argument("--to", required=True, type=latlon, help="lat,lon of the destination")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--settle", type=float, default=45.0, help="seconds to wait after the teleport")
    ap.add_argument("--obs", action="store_true", help="record the screen with OBS (writes drive.mp4, no audio)")
    ap.add_argument("--keep-going", action="store_true", help="drive on after a crash or a stop")
    ap.add_argument("--rec", action="store_true", help="record the game's own frames (tools/gamerec.py: no screen "
                    "capture, no permission dialog; ~15 ms a frame, so the frame times are not the game's then)")
    ap.add_argument("--rec-fps", type=float, default=15.0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    mcp.quiet_ui()          # (the recording too: our welcome page closed and kept away this session)
    if a.start:
        lua("return tostring(beamngEarth_core.teleport(%r, %r, 'autodrive_perf start', {record = false}))" % a.start)
        # the game teleports once the server answers (after a server restart that was ~85 s later: the snap and the
        # drive ran at the old place, Cluj 2026-10-15): the settle counts from the arrival
        t_arrive = time.time() + 300
        while time.time() < t_arrive:
            try:
                d = float(lua("local a=beamngEarth_core._state().anchor; if not a then return '1e9' end "
                              "return tostring(math.abs(a.lat-%r)+math.abs(a.lon-%r))" % a.start))
            except (Exception, SystemExit):
                d = 1e9
            if d < 0.02:
                break
            time.sleep(2)
        else:
            print("the teleport did not happen within 300 s", file=sys.stderr)
        time.sleep(a.settle)
    if a.start:                     # on the road: the nearest graph node, facing along its edge toward the goal
        print(lua(SNAP_LUA % a.to), flush=True)
        time.sleep(3)
    lua("beamngEarth_settings.uiMapGps(%r, %r, 'autodrive_perf') return 'ok'" % a.to)
    lua("return tostring(beamngEarth_settings.uiGpsAutodrive(true).autodrive)")
    obs = obs_start() if a.obs else None
    grec = None
    if a.rec:                       # the game's own frames (no screen capture: works unattended on Wayland)
        import gamerec
        grec = gamerec.start(a.rec_fps)
    rows, end = [], time.time() + a.minutes * 60
    evf = open(os.path.join(a.out, "events.jsonl"), "w")
    checks = Checks(lambda n, ev: shot(a.out, f"event_{n}_{ev['kind']}.jpg"))
    try:
        with open(os.path.join(a.out, "samples.jsonl"), "w") as f:
            while time.time() < end:
                t0 = time.time()
                try:
                    r = lua_json(LUA_POLL)
                except (Exception, SystemExit) as e:    # a busy frame, a tool error: the next poll
                    print(f"poll: {e}", file=sys.stderr)
                    r = None
                if r:
                    rows.append(r)
                    f.write(json.dumps(r) + "\n")
                    f.flush()
                    ev = checks.feed(r) if len(rows) > 2 else None
                    if ev is not None:
                        print("event: " + json.dumps(ev), flush=True)
                        evf.write(json.dumps(ev) + "\n")
                        evf.flush()
                        if ev["kind"] in ("crash", "stop") and not a.keep_going:
                            break
                    if r["gps"].get("autodrive") is False and rows[0]["gps"].get("autodrive"):
                        break                   # arrived (the GPS clears itself) or stopped by hand
                time.sleep(max(0.0, POLL_S - (time.time() - t0)))
    finally:
        video = obs_stop(obs, os.path.join(a.out, "drive.mp4")) if obs is not None else None
        if grec is not None:
            video = grec.stop(os.path.join(a.out, "drive.mp4"))
        lua("beamngEarth_settings.uiGpsAutodrive(false) beamngEarth_settings.uiMapGps(nil) return 'ok'")
    evf.close()
    summary = summarize(rows[1:]) if len(rows) > 2 else {"polls": len(rows)}   # the first poll covers the setup
    summary["events"] = checks.events
    if video:
        summary["video"] = video
        summary["video_small"] = share_copy(video)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
