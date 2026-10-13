"""Data credits for what is around the player (docs/DEVTOOLS.md "Data credits overlay").

The mod draws a small line of text in a screen corner naming the sources that built the world the
player sees right now (CC BY and similar licences ask for visible attribution). The server knows
them per tile:

  - imagery: the sources whose texels a tile's texture took (Imagery.render's per-source counts;
    "none" = the fallback colour and "deshadow" = a relit share are not sources; "colormatch" =
    texels whose colours the seam feather corrected names its S2 references, as hello.attribution
    does with --colormatch s2). Capture dates
    are not in the tiles (USGS ImageryOnly carries none; EOX's year is the layer's own, part of
    its credit);
  - elevation: the provider of every DEM tile under the tile's footprint at its DEM zoom
    (dem.Dem.served: Mapterhorn or the AWS Terrain Tiles fallback). Mapterhorn's underlying
    dataset (USGS 3DEP, swissALTI3D, ...) is not in the tiles it serves, so the credit names
    Mapterhorn with its attribution page;
  - weather: Open-Meteo once a weather message went to the mod (weather.CREDIT).

Credits.message() splits the shown tiles (the connection's `shown`, previous anchors' tiles
included) into those within NEAR_M of the player and the rest (a source already near stays near
while one of its tiles is within NEAR_M + NEAR_HYST_M: a player rocking across the radius does not
flip it every check), and builds the 'credits' message, once some tile's sources are known (the
weather alone would replace the mod's full attribution line with one name):
`near` (the line the overlay shows first), `far` (the sources only the far field uses, short),
`ids` / `farIds` (source ids, for tests and tools). Tracker sends it only when it changed, after
at most one check per INTERVAL_S (pokes from poses and swaps), and again on a hello.

Off unless the server has a Credits object (earthd --credits on, the command line's default;
Server() has none, so the golden message streams of test_cli_default / test_v131_default are
unchanged).
"""
import logging
import math
import time

import numpy as np

import colormatch as colormatchmod
import dem as demmod
import weather as weathermod

log = logging.getLogger("earthd.credits")

VERSION = 1
NEAR_M = 2000.0                 # m from the player: tiles this close are "near"
NEAR_HYST_M = 200.0             # ... and a near source stays near until its tiles are this much further
INTERVAL_S = 1.0                # at most one check (and so one message) per second
MAX_DEM_KEYS = 64               # DEM tiles looked up per far tile (a footprint needs 1-4)

# Overlay wording per source id, word for word the attribution CREDITS.md lists (test_credits checks
# it): EOX's is their prescribed attribution (docs/research/2026-10-10-rate-limits.md, imagery.SOURCES
# without the licence name); the others as in imagery.SOURCES / earthd.ELEVATION_CREDIT. The full
# credits stay in hello.attribution (the settings page) and CREDITS.md.
IMAGERY = {
    "naipusda": "USDA NAIP",
    "naip": "USDA NAIP via USGS The National Map",
    "usgs": "USGS The National Map",
    "eox2023": "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
               "(Contains modified Copernicus Sentinel data 2023)",
    "eox2016": "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
               "(Contains modified Copernicus Sentinel data 2016 & 2017)",
}
ELEVATION = {
    demmod.MAPTERHORN["name"]: "Mapterhorn (see mapterhorn.com/attribution)",
    demmod.AWS["name"]: "AWS Terrain Tiles / Mapzen",
}
WEATHER_ID = "wx:open-meteo"
# Display order of the ids (unknown imagery sources follow, by name)
ORDER = ([f"img:{n}" for n in IMAGERY] + [f"dem:{n}" for n in ELEVATION] + [WEATHER_ID])


# Imagery.render's counts that are no source: the fallback colour, the relit and colour-matched shares
NOT_SOURCES = ("none", "deshadow", "colormatch")


def _rank(i):
    return (ORDER.index(i), "") if i in ORDER else (len(ORDER), i)


def ordered(ids):
    return sorted(ids, key=_rank)


def label(i):
    kind, _, name = i.partition(":")
    if kind == "img":
        return IMAGERY.get(name, name)
    if kind == "dem":
        return ELEVATION.get(name, name)
    if i == WEATHER_ID:
        return weathermod.CREDIT
    return name


def line(ids, far=False):
    """'Imagery: a; b | Elevation: c | Weather data by Open-Meteo.com' (far: 'Far imagery: ...
    | Far elevation: ...'), in ORDER; '' for no ids."""
    groups = {"img": [], "dem": [], "wx": []}
    for i in ordered(ids):
        groups.setdefault(i.partition(":")[0], []).append(label(i))
    out = []
    if groups["img"]:
        out.append(("Far imagery: " if far else "Imagery: ") + "; ".join(groups["img"]))
    if groups["dem"]:
        out.append(("Far elevation: " if far else "Elevation: ") + "; ".join(groups["dem"]))
    out += groups["wx"]
    return " | ".join(out)


def dem_keys(frame, bbox, zoom):
    """The (z, x, y) DEM tiles (dem.TILE px Web-Mercator grid) under an ENU footprint of `frame`,
    from a 3 x 3 grid of points on it (the footprint's edges bow on large tiles); at most
    MAX_DEM_KEYS, none beyond the Mercator limit."""
    x0, y0, x1, y1 = bbox
    E, N = np.meshgrid(np.linspace(x0, x1, 3), np.linspace(y0, y1, 3))
    lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
    lat, lon = np.ravel(lat), np.ravel(lon)
    if np.abs(lat).max() > demmod.MAX_LAT:
        return []
    n = 2 ** zoom
    fx = (lon / 360.0 + 0.5) * n
    fx = fx - n * np.round((fx - fx[0]) / n)                # unwrap across the antimeridian
    fy = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * n
    tx0, tx1 = int(math.floor(fx.min())), int(math.floor(fx.max()))
    ty0, ty1 = max(0, int(math.floor(fy.min()))), min(n - 1, int(math.floor(fy.max())))
    if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > MAX_DEM_KEYS:
        return []
    return [(zoom, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]


def tile_sources(frame, bbox, dem, zoom, used):
    """Source ids of one built tile: 'img:<source>' for every imagery source with texels in
    `used` (Imagery.render's counts; None = no imagery), 'dem:<provider>' for every DEM tile
    under the footprint at `zoom` whose provider the Dem recorded (dem.served; a DEM without it,
    e.g. a test's synthetic one, gives none)."""
    used = used or {}
    ids = {f"img:{name}" for name, n in used.items() if name not in NOT_SOURCES and n > 0}
    if used.get("colormatch", 0) > 0:       # --colormatch: the S2 layers the correction came from
        ids |= {f"img:{name}" for name in colormatchmod.REFS}
    served = getattr(dem, "served", None)
    if served is not None and zoom is not None:
        for k in dem_keys(frame, bbox, zoom):
            p = served.get(k)
            if p:
                ids.add(f"dem:{p}")
    return frozenset(ids)


def bbox_distance(bb, x, y):
    """Distance from (x, y) to the nearest point of the box (0 inside)."""
    dx = max(bb[0] - x, 0.0, x - bb[2])
    dy = max(bb[1] - y, 0.0, y - bb[3])
    return math.hypot(dx, dy)


class Credits:
    """The server's credits setting (earthd --credits on): the near radius and its hysteresis, the
    throttle and the clock (injectable for tests)."""

    def __init__(self, near_m=NEAR_M, interval=INTERVAL_S, clock=time.monotonic, hyst_m=NEAR_HYST_M):
        self.near_m, self.interval, self.clock, self.hyst_m = near_m, interval, clock, hyst_m

    def message(self, conn, near_before=()):
        """The 'credits' message for a connection's shown tiles around its player, or None before
        anything is known (no anchor, no tile with sources). near_before: the ids near in the
        last message (they stay near within the hysteresis band)."""
        if conn.active is None:
            return None
        fx, fy = conn.focus
        tiles = conn.s.tiles
        near, far, kept = set(), set(), set()
        keep = frozenset(near_before)
        for ck, bb in list(conn.shown.items()):
            info = tiles.get(ck)
            ids = getattr(info, "credits", None)
            if not ids:
                continue
            d = bbox_distance(bb, fx, fy)
            if d <= self.near_m:
                near |= ids
            else:
                far |= ids
                if keep and d <= self.near_m + self.hyst_m:
                    kept |= ids & keep
        near |= kept
        if not near and not far:
            return None
        if conn.weather.msg is not None:
            near.add(WEATHER_ID)
        far -= near
        return {"type": "credits", "v": VERSION, "near": line(near), "far": line(far, far=True),
                "ids": ordered(near), "farIds": ordered(far)}


class Tracker:
    """Per connection: the last credits message sent (re-sent on hello) and the throttle (one
    check per Credits.interval; a poke during the wait is folded into the scheduled check)."""

    def __init__(self):
        self.msg = None
        self.last_check = -math.inf
        self.timer = None
        self.checks = 0

    def poke(self, conn):
        """Something the credits depend on may have changed (a pose, a swap): check once the
        interval since the last check has passed."""
        cr = conn.s.credits
        if cr is None or self.timer is not None:
            return
        delay = max(0.0, self.last_check + cr.interval - cr.clock())
        self.timer = conn.loop.call_later(delay, self.check, conn)

    def check(self, conn):
        self.timer = None
        cr = conn.s.credits
        if cr is None:
            return
        self.last_check = cr.clock()
        self.checks += 1
        try:
            msg = cr.message(conn, self.msg["ids"] if self.msg else ())
        except Exception:               # a bug must not stop the connection
            log.exception("credits failed")
            return
        if msg is not None and msg != self.msg:
            self.msg = msg
            conn.send(msg)

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
