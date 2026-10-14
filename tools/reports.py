#!/usr/bin/env python3
"""List / show the user's in-game reports (mod report.lua: Ctrl+Shift+N notes, Ctrl+Shift+B benchmarks).

  tools/reports.py              one line per report (newest last)
  tools/reports.py show N       the Nth report (1-based, from the list) in detail
  tools/reports.py since DATE   reports at or after 'YYYY-MM-DD HH:MM'
"""
import json
import os
import sys

ROOT = os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current/beamngearth/reports")


def load():
    out = []
    for name in sorted(os.listdir(ROOT)) if os.path.isdir(ROOT) else []:
        p = os.path.join(ROOT, name, "report.json")
        if os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
            d["_dir"] = os.path.join(ROOT, name)
            out.append(d)
    return out


def line(i, d):
    fps = (d.get("bench") or {}).get("fps") or (d.get("recentFrames") or {}).get("fps")
    loc = f"{d['lat']:.5f},{d['lon']:.5f}" if d.get("lat") else "-"
    return f"{i:3d} {d.get('time')} {d.get('kind'):5s} {loc:22s} fps {fps or 0:5.1f}  {(d.get('note') or '')[:70]}"


def show(d):
    keys = ("time", "kind", "lat", "lon", "anchorId", "note", "vehicle", "camera", "recentFrames", "fpsLimit")
    for k in keys:
        if k in d:
            print(f"{k}: {d[k]}")
    b = d.get("bench")
    if b:
        print("bench:", {k: v for k, v in b.items() if k != "hitches"})
        for h in b.get("hitches", [])[:20]:
            print("  hitch", h)
    m = d.get("engineMetrics") or {}
    top = sorted(((v, k) for k, v in m.items() if isinstance(v, (int, float)) and k not in ("physAvailable", "physUsed", "virtAvailable", "virtUsed")), reverse=True)[:10]
    print("engine (ms):", ", ".join(f"{k} {v:.2f}" for v, k in top))
    mod = d.get("mod") or {}
    print("mod:", {k: mod.get(k) for k in ("anchorId", "commits", "sea", "trees", "far")})
    print("screenshot:", os.path.join(d["_dir"], "screenshot.jpg"))


def main(argv):
    reps = load()
    if not argv:
        for i, d in enumerate(reps, 1):
            print(line(i, d))
    elif argv[0] == "show":
        show(reps[int(argv[1]) - 1])
    elif argv[0] == "since":
        for i, d in enumerate(reps, 1):
            if d.get("time", "") >= argv[1]:
                print(line(i, d))


if __name__ == "__main__":
    main(sys.argv[1:])
