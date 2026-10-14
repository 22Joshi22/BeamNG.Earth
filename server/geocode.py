"""Address and place search for the in-game map's search box (the user, 2026-10-14: "look up real addresses and
places as you type, autocomplete style").

Photon (https://photon.komoot.io, komoot; OpenStreetMap data, ODbL) is built for search-as-you-type; Nominatim's
usage policy forbids autocomplete, and Google is not used (project rule). The mod sends the typed text and the
player's position (a location bias: nearer results first); requests are debounced by the map (300 ms, 3+
characters), limited and cached by netfetch (photon.komoot.io policy). The map credits OpenStreetMap / Photon.
"""
import json
import urllib.parse

import netfetch

PHOTON_URL = "https://photon.komoot.io/api/"
LIMIT = 7
MIN_CHARS = 3
ATTRIBUTION = "Search: Photon by komoot, data (c) OpenStreetMap contributors (ODbL)"


def url(q, lat=None, lon=None, limit=LIMIT, lang="en"):
    """The Photon request for text q, biased toward (lat, lon) when given. Pure."""
    params = [("q", q.strip()), ("limit", str(int(limit))), ("lang", lang)]
    if lat is not None and lon is not None:
        params += [("lat", f"{float(lat):.4f}"), ("lon", f"{float(lon):.4f}")]
    return PHOTON_URL + "?" + urllib.parse.urlencode(params)


def label(props):
    """(name, detail) of one Photon feature's properties: the place or the street address, then the town,
    state and country. Pure."""
    p = props or {}
    street = " ".join(v for v in (p.get("housenumber"), p.get("street")) if v)
    name = p.get("name") or street or p.get("city") or p.get("state") or p.get("country") or "?"
    parts = []
    if street and street != name:
        parts.append(street)
    for k in ("city", "district", "county", "state", "country"):
        v = p.get(k)
        if v and v != name and v not in parts:
            parts.append(v)
        if len(parts) >= 3:
            break
    kind = p.get("osm_value") or p.get("type") or ""
    return name, ", ".join(parts), kind


def parse(data):
    """[{name, detail, kind, lat, lon}] of a Photon GeoJSON response. Pure."""
    out = []
    for f in (data or {}).get("features") or []:
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
            lat, lon = float(lat), float(lon)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        name, detail, kind = label(f.get("properties"))
        out.append({"name": name, "detail": detail, "kind": kind, "lat": round(lat, 6), "lon": round(lon, 6)})
    return out


def search(q, lat=None, lon=None, net=None):
    """Results for q (blocking; [] for a short query). Raises netfetch.FetchError on network trouble."""
    q = (q or "").strip()
    if len(q) < MIN_CHARS:
        return []
    net = net if net is not None else netfetch.default_fetcher()
    return parse(json.loads(net.get(url(q, lat, lon), kind="demand").body.decode("utf-8")))
