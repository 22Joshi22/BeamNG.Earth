"""Real weather at the player: Open-Meteo current conditions turned into the 'weather' message
(docs/research/2026-10-10-real-weather.md §1, §4.1; measured facts in
docs/research/2026-10-10-weather-probe-results.md).

Open-Meteo (https://open-meteo.com/en/docs) is keyless and free for non-commercial use (< 600
calls/min, 10,000/day); its data are CC BY 4.0, so every place that shows them carries CREDIT
(Server.attribution). One GET per fetch with a generic user agent and no personal data. Its
`current` block is 15-minutely (`interval` 900 s): precipitation, rain, showers and snowfall are
sums over that interval, so a rate per hour is value * 3600 / interval. `timezone=auto` adds the
real UTC offset of the place (the mod's clock uses it instead of its longitude guess).

Per connection (Tracker): fetch at the player's lat/lon when there is no weather yet, every
REFRESH_S, or once the player moved REFETCH_M from the last fetch point; one request in flight; a
failure keeps the last weather and retries after RETRY_S. Results are cached for every
connection by (CELL_DEG cell, SLOT_S slot), the resolution of the data.

derive() keeps the physical quantities (rates, visibility, cloud base, sea-level temperature);
the mapping onto the game's controls (coverage calibration, fog density, wind vectors, rain
fraction) is the mod's (beamngEarth/weather.lua).
"""
import json
import logging
import math
import threading
import time
import urllib.parse
import urllib.request

import dem as demmod

log = logging.getLogger("earthd.weather")

API = "https://api.open-meteo.com/v1/forecast"
CURRENT = ("temperature_2m", "dew_point_2m", "relative_humidity_2m", "is_day", "precipitation", "rain",
           "showers", "snowfall", "weather_code", "cloud_cover", "cloud_cover_low", "cloud_cover_mid",
           "cloud_cover_high", "surface_pressure", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
           "visibility")
HOURLY = ("wind_speed_700hPa", "wind_direction_700hPa")      # winds aloft: where the clouds drift
CREDIT = "Weather data by Open-Meteo.com"
VERSION = 1                 # weather.v: the message layout

REFRESH_S = 600.0           # new data every 10 min (the models update hourly at best)
REFETCH_M = 25000.0         # ... or after this far from the last fetch point
RETRY_S = 120.0             # after a failed fetch
TIMEOUT_S = 10.0
CELL_DEG = 0.1              # cache cell (~11 km); also the lat/lon the message reports
SLOT_S = 900                # cache slot = Open-Meteo's 15-minute `current` step
CACHE_MAX = 256

LAPSE = 0.0065              # K/m, the standard atmosphere (the game applies the same with height)
LCL_M_PER_K = 125.0         # cloud base above ground ~ 125 m per K of dew-point spread (Espy)
MID_BASE_M = 3000.0         # a mid-level deck (3-8 km in Open-Meteo's split) starts at least here
BASE_MIN_AGL, BASE_MAX_AGL = 200.0, 6000.0
FOG_CODES = (45, 48)
FOG_VIS_M = 800.0           # fog codes with a model visibility above this mean fog anyway
SNOW_CODES = tuple(range(71, 78)) + (85, 86)
# The model's weather code with a zero 15-min sum (the code is the hour's): a light floor so a
# code that says it rains shows some rain. (code range, rain mm/h, snow cm/h)
CODE_FLOORS = ((range(51, 58), 0.3, 0.0), (range(61, 68), 1.0, 0.0), (range(80, 83), 1.0, 0.0),
               (range(71, 78), 0.0, 0.5), ((85, 86), 0.0, 0.5), (range(95, 100), 4.0, 0.0))


def url(lat, lon):
    q = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "current": ",".join(CURRENT),
         "hourly": ",".join(HOURLY), "forecast_hours": 1, "wind_speed_unit": "ms", "timeformat": "unixtime",
         "timezone": "auto", "cell_selection": "land"}
    return API + "?" + urllib.parse.urlencode(q, safe=",")


def fetch(lat, lon, timeout=TIMEOUT_S):
    """One Open-Meteo GET (blocking): the parsed JSON."""
    req = urllib.request.Request(url(lat, lon), headers={"User-Agent": demmod.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _f(v, default=0.0):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


def cell(lat, lon):
    """The cache cell (CELL_DEG grid) of a point, as its centre's (lat, lon)."""
    return (round(round(lat / CELL_DEG) * CELL_DEG, 4), round(round(lon / CELL_DEG) * CELL_DEG, 4))


def cloud_base(t, td, low, mid, elev):
    """(base ASL, base above the model ground) in m of the deck the mod draws (max(low, mid)):
    a low deck at the lifting condensation level; a mid deck at least MID_BASE_M above sea level."""
    lcl = LCL_M_PER_K * max(0.0, t - td)
    if low >= mid:
        agl = lcl
    else:
        agl = max(elev + lcl, MID_BASE_M) - elev
    agl = min(max(agl, BASE_MIN_AGL), BASE_MAX_AGL)
    return elev + agl, agl


def derive(om, lat=None, lon=None):
    """The 'weather' message for one Open-Meteo response (pure). lat/lon: the point it reports
    (default: the response's grid point)."""
    cur = om.get("current") or {}
    iv = _f(cur.get("interval"), 900.0) or 900.0
    per_h = 3600.0 / iv
    code = int(_f(cur.get("weather_code"), 0))
    t, td = _f(cur.get("temperature_2m"), 15.0), _f(cur.get("dew_point_2m"), 15.0)
    elev = _f(om.get("elevation"), 0.0)
    rain = (_f(cur.get("rain")) + _f(cur.get("showers"))) * per_h
    snow = _f(cur.get("snowfall")) * per_h                     # cm/h of snow
    if rain == 0 and snow == 0 and _f(cur.get("precipitation")) > 0:    # a sum without its split
        rain = _f(cur.get("precipitation")) * per_h
    for codes, r_min, s_min in CODE_FLOORS:
        if code in codes:
            rain, snow = max(rain, r_min), max(snow, s_min)
    vis = cur.get("visibility")
    vis = _f(vis) if isinstance(vis, (int, float)) and not isinstance(vis, bool) else None
    fog = code in FOG_CODES
    if fog:
        vis = min(vis if vis is not None else FOG_VIS_M, FOG_VIS_M)
    low, mid, high = (_f(cur.get(k)) for k in ("cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"))
    base_asl, base_agl = cloud_base(t, td, low, mid, elev)
    ms = _f(cur.get("wind_speed_10m"))
    msg = {"type": "weather", "v": VERSION, "t": int(_f(cur.get("time"), 0)),
           "lat": float(lat if lat is not None else _f(om.get("latitude"))),
           "lon": float(lon if lon is not None else _f(om.get("longitude"))),
           "code": code, "isDay": bool(_f(cur.get("is_day"), 1)),
           "cover": {"total": _f(cur.get("cloud_cover")), "low": low, "mid": mid, "high": high},
           "baseM": round(base_asl, 1), "baseAglM": round(base_agl, 1),
           "rainMmH": round(rain, 3), "snowCmH": round(snow, 3),
           "visM": None if vis is None else round(vis, 1), "fog": fog, "thunder": code >= 95,
           "wind": {"ms": ms, "fromDeg": _f(cur.get("wind_direction_10m")),
                    "gustMs": max(ms, _f(cur.get("wind_gusts_10m"), ms))},
           "tempC": t, "dewC": td, "elevM": elev, "slTempC": round(t + LAPSE * elev, 2),
           "pressureHpa": _f(cur.get("surface_pressure"), 1013.25),
           "credit": CREDIT}
    hourly = om.get("hourly") or {}
    sp, dr = hourly.get("wind_speed_700hPa"), hourly.get("wind_direction_700hPa")
    if sp and dr and isinstance(sp[0], (int, float)) and isinstance(dr[0], (int, float)):
        msg["aloft"] = {"ms": float(sp[0]), "fromDeg": float(dr[0])}
    if isinstance(om.get("utc_offset_seconds"), int):
        msg["utcOffsetS"] = om["utc_offset_seconds"]
    if isinstance(om.get("timezone"), str):
        msg["tz"] = om["timezone"]
    return msg


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance (spherical Earth; plenty for a refetch rule)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


class Service:
    """The weather source shared by every connection: fetch (injectable: tests pass a fake, no
    network) + the (cell, slot) cache. get() blocks: run it in an executor."""

    def __init__(self, fetch=fetch, refresh_s=REFRESH_S, refetch_m=REFETCH_M, retry_s=RETRY_S,
                 clock=time.time):
        self.fetch, self.clock = fetch, clock
        self.refresh_s, self.refetch_m, self.retry_s = refresh_s, refetch_m, retry_s
        self.cache = {}
        self.lock = threading.Lock()
        self.fetches = 0

    def key(self, lat, lon, now):
        return cell(lat, lon) + (int(now // SLOT_S),)

    def get(self, lat, lon):
        """The weather message for (lat, lon) now: from the cache, or fetched and derived."""
        k = self.key(lat, lon, self.clock())
        with self.lock:
            hit = self.cache.get(k)
        if hit is not None:
            return hit
        clat, clon = k[0], k[1]
        msg = derive(self.fetch(clat, clon), clat, clon)
        self.fetches += 1
        with self.lock:
            self.cache[k] = msg
            while len(self.cache) > CACHE_MAX:
                self.cache.pop(next(iter(self.cache)))
        return msg


class Tracker:
    """Per connection: when to fetch, and the last weather sent (re-sent on hello)."""

    def __init__(self):
        self.last = None            # (lat, lon, time) of the last successful fetch
        self.inflight = False
        self.retry_at = 0.0
        self.msg = None

    def due(self, svc, lat, lon, now):
        if self.inflight or now < self.retry_at:
            return False
        if self.last is None:
            return True
        llat, llon, lt = self.last
        return now - lt >= svc.refresh_s or distance_m(llat, llon, lat, lon) > svc.refetch_m

    def done(self, svc, lat, lon, now, msg):
        self.inflight = False
        if msg is None:
            self.retry_at = now + svc.retry_s
        else:
            self.last, self.msg = (lat, lon, now), msg

    def on_pose(self, conn, lat, lon):
        """earthd Connection.on_pose: start a fetch in the background when one is due; the
        message goes to the mod when it arrives."""
        svc = conn.s.weather
        if svc is None or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return
        now = svc.clock()
        if not self.due(svc, lat, lon, now):
            return
        self.inflight = True

        async def run():
            msg = None
            try:
                msg = await conn.loop.run_in_executor(None, svc.get, lat, lon)
            except Exception as e:      # network, HTTP, JSON: keep the last weather
                log.warning("weather fetch at %.3f, %.3f failed: %s: %s", lat, lon, type(e).__name__, e)
            finally:
                self.done(svc, lat, lon, svc.clock(), msg)
            if msg is not None:
                conn.send(msg)
        conn.spawn(run())
