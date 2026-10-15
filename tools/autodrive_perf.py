#!/usr/bin/env python3
"""Performance on a real drive: the car drives itself along a GPS route (the map's Autodrive, mod gps.lua: the car's
own AI on our road graph, in lane, at the legal speed, minding traffic) while the frame times are sampled every
second. Unlike tools/drive.py (the car moved along a profile by a script) the car really drives: streaming,
traffic, physics and the GPS all run as in play, and the same route gives the same drive again.

  python3 tools/autodrive_perf.py --to 39.2634,-76.5797 --minutes 5 --out /tmp/ad
  python3 tools/autodrive_perf.py --from 39.2834,-76.5934 --to 39.2634,-76.5797 --minutes 5 --out /tmp/ad --obs

--from teleports there first and waits for the world around it. --obs records the screen with OBS Studio (the
Flatpak, its saved scene) and writes <out>/drive.mp4 without its audio track (the microphone is in it).
Writes <out>/samples.jsonl (one poll per line) and <out>/summary.json; prints the summary. The game's MCP server
must be on (Options > Advanced) and the companion server running.
"""
import argparse
import glob
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcp  # noqa: E402

POLL_S = 1.0
STALL_MS = 0.5          # slower than this (m/s) counts as standing
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
return jsonEncode({ t = os.clock(), x = pos.x, y = pos.y, v = math.sqrt(vel.x ^ 2 + vel.y ^ 2), lat = pl.lat, lon = pl.lon,
  perf = { frames = p.frames, avgMs = p.avgMs, p50 = p.p50, p95 = p.p95, p99 = p.p99, maxMs = p.maxMs,
           over33 = p.over33, over50 = p.over50, over100 = p.over100 },
  gps = { remaining = g.remaining, off = g.off, arrows = g.arrows, autodrive = g.autodrive, planning = g.planning },
  pending = s.pending, anchor = s.anchorId })
'''


def lua(code):
    return mcp.call("run_lua", {"code": code})


def lua_json(code):
    return json.loads(lua(code))


def latlon(text):
    a, b = (float(x) for x in text.split(","))
    return a, b


def obs_start():
    before = set(glob.glob(os.path.join(OBS_VIDEOS, "*")))
    subprocess.Popen(["flatpak", "run", OBS_APP, "--startrecording", "--minimize-to-tray", "--disable-shutdown-check"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return before


def obs_stop(before, out_path):
    """Stop OBS (SIGTERM: it ends the recording and quits cleanly) and write its file to out_path without audio."""
    subprocess.run(["pkill", "-TERM", "-x", "obs"])
    for _ in range(30):
        if subprocess.run(["pgrep", "-x", "obs"], capture_output=True).returncode != 0:
            break
        time.sleep(1)
    new = sorted(set(glob.glob(os.path.join(OBS_VIDEOS, "*"))) - before, key=os.path.getmtime)
    if not new:
        return None
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", new[-1], "-map", "0:v", "-c", "copy", out_path], check=True)
    os.remove(new[-1])
    return out_path


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
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=latlon, help="lat,lon to teleport to first")
    ap.add_argument("--to", required=True, type=latlon, help="lat,lon of the destination")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--settle", type=float, default=45.0, help="seconds to wait after the teleport")
    ap.add_argument("--obs", action="store_true", help="record the screen with OBS (writes drive.mp4, no audio)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.start:
        lua("return tostring(beamngEarth_core.teleport(%r, %r, 'autodrive_perf start', {record = false}))" % a.start)
        time.sleep(a.settle)
    lua("beamngEarth_settings.uiMapGps(%r, %r, 'autodrive_perf') return 'ok'" % a.to)
    lua("return tostring(beamngEarth_settings.uiGpsAutodrive(true).autodrive)")
    obs = obs_start() if a.obs else None
    rows, end = [], time.time() + a.minutes * 60
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
                    if r["gps"].get("autodrive") is False and rows[0]["gps"].get("autodrive"):
                        break                   # arrived (the GPS clears itself) or stopped by hand
                time.sleep(max(0.0, POLL_S - (time.time() - t0)))
    finally:
        video = obs_stop(obs, os.path.join(a.out, "drive.mp4")) if obs is not None else None
        lua("beamngEarth_settings.uiGpsAutodrive(false) beamngEarth_settings.uiMapGps(nil) return 'ok'")
    summary = summarize(rows[1:]) if len(rows) > 2 else {"polls": len(rows)}   # the first poll covers the setup
    if video:
        summary["video"] = video
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
