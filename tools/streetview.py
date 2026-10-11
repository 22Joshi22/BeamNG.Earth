#!/usr/bin/env python3
"""Street-level comparison: Mapillary photos of a real place next to the same view in the game
(user, 2026-10-11: "use street view images to compare the roads"; Mapillary rather than Google
Street View, whose terms forbid this use).

    python3 tools/streetview.py fetch --lat 39.2834 --lon -76.5934 --name fells_point [--radius 250] [--n 12]
    python3 tools/streetview.py run --name fells_point            (needs the game; the benchmark's safe runner)
    python3 tools/streetview.py compare --name fells_point        (side by side: photo | game, index.html)

fetch: the Mapillary Graph API v4 (images in a bbox around the point: perspective cameras only, no
panoramas; spread over distinct spots and headings; newest first). It downloads each photo's
2048 px thumbnail and writes <dir>/photos.json and <dir>/set.json, a benchmark set (docs/benchmarks.json
format) with one place and one view per photo: the photo's (computed) position, 2 m above the ground,
its compass heading, level, and the horizontal field of view from its camera parameters; one place
per photo, so the car (and the detailed ground around it) is at the photo, as when driving there.
run: tools/benchmark.py run --benchmarks <dir>/set.json (teleport, wait for the world, clear sky at
the photos' local hour, the free camera per view, screenshots, everything put back after).
compare: each photo next to its screenshot, the screenshot cropped to the photo's aspect ratio
around the centre, plus <dir>/index.html. The photos are © their Mapillary contributors (CC BY-SA
4.0): credited on the page, kept local (git-ignored).

The access token is read from ~/.config/beamng-earth/mapillary.token (or $MAPILLARY_TOKEN) and only
sent in the Authorization header; it is never printed or written anywhere else.
Data: ~/Documents/BeamNG.Earth/docs/research/streetview/<name>/ (git-ignored).
"""
import argparse
import datetime
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
from netfetch import USER_AGENT  # noqa: E402

API = "https://graph.mapillary.com/images"
TOKEN_FILE = os.path.expanduser("~/.config/beamng-earth/mapillary.token")
DATA = os.path.join(ROOT, "docs", "research", "streetview")
FIELDS = ("id,computed_geometry,geometry,computed_compass_angle,compass_angle,captured_at,camera_type,"
          "camera_parameters,is_pano,thumb_2048_url,width,height,creator,sequence")
CAMERA_AGL = 2.0            # m: most street photos are taken from a car roof or a dashboard
MIN_SPACING_M = 25.0        # chosen photos are at least this far apart, or look another way
MIN_TURN_DEG = 45.0
PER_SEQUENCE = 3            # photos from one capture run at most (variety across the place)
MIN_SEQUENCE = 3            # a capture run with fewer photos here is skipped: street captures come in
                            # runs; single uploads are often indoor or mislocated (a wedding at Thames St)
M_PER_DEG = 111320.0


def token():
    t = os.environ.get("MAPILLARY_TOKEN")
    if not t:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
    if not t.startswith("MLY|"):
        raise SystemExit(f"no Mapillary client token in {TOKEN_FILE} (it starts with MLY|)")
    return t


def get(url, tok=None, timeout=30):
    hd = {"User-Agent": USER_AGENT}
    if tok:
        hd["Authorization"] = f"OAuth {tok}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=hd), timeout=timeout) as r:
        return r.read()


def hfov(img):
    """Horizontal field of view (deg) from Mapillary's perspective camera parameters [focal, k1, k2]
    (focal relative to the larger image side), else 70."""
    p = img.get("camera_parameters") or []
    w, h = img.get("width") or 4, img.get("height") or 3
    if p and p[0] and p[0] > 0.1:
        f = float(p[0]) * max(w, h) / w             # relative to the width
        return max(30.0, min(110.0, math.degrees(2 * math.atan(0.5 / f))))
    return 70.0


def search(lat, lon, radius, tok, limit=500):
    dlat = radius / M_PER_DEG
    dlon = radius / (M_PER_DEG * math.cos(math.radians(lat)))
    q = urllib.parse.urlencode({"fields": FIELDS, "bbox": f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}",
                                "limit": limit})
    return json.loads(get(f"{API}?{q}", tok)).get("data", [])


def pick(images, lat, lon, n):
    """Perspective, non-panorama photos with a position and a heading, newest first, spread out."""
    ok = []
    for im in images:
        g = (im.get("computed_geometry") or im.get("geometry") or {}).get("coordinates")
        hd = im.get("computed_compass_angle", im.get("compass_angle"))
        if not g or hd is None or im.get("is_pano") or im.get("camera_type") not in (None, "perspective", "brown"):
            continue
        if not im.get("thumb_2048_url"):
            continue
        ok.append((im, g[1], g[0], float(hd) % 360.0))
    runs = {}
    for im in images:
        runs[im.get("sequence")] = runs.get(im.get("sequence"), 0) + 1
    ok = [t for t in ok if runs.get(t[0].get("sequence"), 0) >= MIN_SEQUENCE]
    ok.sort(key=lambda t: -(t[0].get("captured_at") or 0))
    chosen = []
    m_lon = M_PER_DEG * math.cos(math.radians(lat))
    per_seq = {}
    for im, la, lo, hd in ok:
        seq = im.get("sequence")
        if per_seq.get(seq, 0) >= PER_SEQUENCE:
            continue
        far = True
        for _, la2, lo2, hd2 in chosen:
            d = math.hypot((la - la2) * M_PER_DEG, (lo - lo2) * m_lon)
            turn = abs((hd - hd2 + 180) % 360 - 180)
            if d < MIN_SPACING_M and turn < MIN_TURN_DEG:
                far = False
                break
        if far:
            chosen.append((im, la, lo, hd))
            per_seq[seq] = per_seq.get(seq, 0) + 1
        if len(chosen) >= n:
            break
    return chosen


def place_dir(name):
    return os.path.join(DATA, name)


def cmd_fetch(a):
    tok = token()
    d = place_dir(a.name)
    os.makedirs(d, exist_ok=True)
    images = search(a.lat, a.lon, a.radius, tok)
    chosen = pick(images, a.lat, a.lon, a.n)
    photos, views = [], []
    for im, la, lo, hd in chosen:
        fn = f"mly_{im['id']}.jpg"
        path = os.path.join(d, fn)
        if not os.path.exists(path):
            with open(path + ".tmp", "wb") as f:
                f.write(get(im["thumb_2048_url"]))
            os.replace(path + ".tmp", path)
        when = datetime.datetime.fromtimestamp((im.get("captured_at") or 0) / 1000, datetime.timezone.utc)
        creator = (im.get("creator") or {}).get("username", "unknown")
        photos.append({"id": im["id"], "file": fn, "lat": la, "lon": lo, "heading": round(hd, 1),
                       "fov": round(hfov(im), 1), "captured": when.isoformat(), "creator": creator,
                       "width": im.get("width"), "height": im.get("height")})
        views.append({"name": f"mly_{im['id']}", "lat": la, "lon": lo, "agl": CAMERA_AGL, "heading": round(hd, 1),
                      "pitch": 0, "fov": round(hfov(im), 1)})
        time.sleep(0.2)
    with open(os.path.join(d, "photos.json"), "w") as f:
        json.dump({"name": a.name, "lat": a.lat, "lon": a.lon, "radius": a.radius, "found": len(images),
                   "photos": photos, "credit": "Photos © Mapillary contributors, CC BY-SA 4.0"}, f, indent=1)
    hours = [datetime.datetime.fromisoformat(p["captured"]).hour + a.lon / 15.0 for p in photos] or [10.0]
    hour = round(sorted(h % 24 for h in hours)[len(hours) // 2], 1)
    hour = min(max(hour, 8.0), 17.0)            # daylight, like most street photos
    setj = {"version": 1, "about": f"street view comparison set for {a.name} (tools/streetview.py)",
            "defaults": {"date": "2026-06-21", "hour": hour, "weather": "clear",
                         # required by the set format; the place's own views are the ones used
                         "views": [{"name": "ground", "agl": 1.7, "pitch": -3, "fov": 70}]},
            # one place per photo: the car (and so the detailed ground) at the photo, as when driving there
            "places": [{"id": f"{a.name}_{k:02d}", "name": f"{a.name} photo {k}", "lat": v["lat"], "lon": v["lon"],
                        "heading": v["heading"], "group": "streetview", "heldout": False, "tags": ["streetview"],
                        "why": "street view comparison", "views": [v]} for k, v in enumerate(views)]}
    with open(os.path.join(d, "set.json"), "w") as f:
        json.dump(setj, f, indent=1)
    print(f"{len(images)} images found, {len(photos)} chosen -> {d}")


def cmd_run(a):
    d = place_dir(a.name)
    game = os.path.join(d, "game")
    if os.path.exists(game):                    # keep the previous shots: game_<time of that run>
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(game)))
        os.rename(game, os.path.join(d, f"game_{stamp}"))
    ids = ",".join(p["id"] for p in json.load(open(os.path.join(d, "set.json")))["places"])
    cmd = [sys.executable, os.path.join(ROOT, "tools", "benchmark.py"), "run", "--benchmarks", os.path.join(d, "set.json"),
           "--places", ids, "--label", f"streetview-{a.name}", "--out", os.path.join(d, "game"), "--weather", "clear"]
    if a.force:
        cmd.append("--force")
    sys.exit(subprocess.call(cmd))


def cmd_compare(a):
    from PIL import Image
    d = place_dir(a.name)
    meta = json.load(open(os.path.join(d, "photos.json")))
    run = json.load(open(os.path.join(d, "game", "run.json")))
    shots = {}
    for pl in run.get("places", []):
        for v in pl.get("views", []):
            if v.get("file"):
                shots[v["name"]] = os.path.join(d, "game", v["file"])
    rows = []
    for p in meta["photos"]:
        g = shots.get(f"mly_{p['id']}")
        if not g or not os.path.exists(g):
            continue
        ph = Image.open(os.path.join(d, p["file"])).convert("RGB")
        gm = Image.open(g).convert("RGB")
        # game shot cropped to the photo's aspect around the centre (same horizontal fov), same height
        ar = ph.width / ph.height
        if gm.width / gm.height > ar:
            w = int(gm.height * ar)
            gm = gm.crop(((gm.width - w) // 2, 0, (gm.width - w) // 2 + w, gm.height))
        else:
            h = int(gm.width / ar)
            gm = gm.crop((0, (gm.height - h) // 2, gm.width, (gm.height - h) // 2 + h))
        H = 600
        ph = ph.resize((int(H * ar), H))
        gm = gm.resize((int(H * ar), H))
        out = Image.new("RGB", (ph.width * 2 + 10, H), (255, 255, 255))
        out.paste(ph, (0, 0))
        out.paste(gm, (ph.width + 10, 0))
        fn = f"pair_{p['id']}.jpg"
        out.save(os.path.join(d, fn), quality=88)
        rows.append((fn, p))
    html = ["<!doctype html><meta charset=utf-8><title>Street view: %s</title>" % a.name,
            "<style>body{font:14px sans-serif;margin:16px;background:#111;color:#ddd}img{max-width:100%}"
            "figure{margin:0 0 24px}</style>",
            f"<h1>{a.name}: Mapillary photo | BeamNG.Earth</h1><p>{meta['credit']}</p>"]
    for fn, p in rows:
        html.append(f"<figure><img src='{fn}'><figcaption>{p['lat']:.6f}, {p['lon']:.6f} heading {p['heading']}° "
                    f"fov {p['fov']}° · {p['captured'][:10]} · photo by {p['creator']} (Mapillary, CC BY-SA 4.0) · "
                    f"<a href='https://www.mapillary.com/app/?pKey={p['id']}' style='color:#8cf'>view</a></figcaption></figure>")
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write("\n".join(html))
    print(f"{len(rows)} pairs -> {os.path.join(d, 'index.html')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--lat", type=float, required=True)
    f.add_argument("--lon", type=float, required=True)
    f.add_argument("--name", required=True)
    f.add_argument("--radius", type=float, default=250.0)
    f.add_argument("--n", type=int, default=12)
    r = sub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--force", action="store_true")
    c = sub.add_parser("compare")
    c.add_argument("--name", required=True)
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    main()
