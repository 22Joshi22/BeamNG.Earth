"""Data credits (server/credits.py, docs/DEVTOOLS.md "Data credits overlay"): which sources are
reported for which tiles (imagery per texture, the DEM provider per footprint, weather), the near /
far split around the player, the wording against the sources' own credits, change-only sending with
the one-per-second throttle, the re-send on hello, and the default server without any of it. No
network: synthetic DEM and imagery, fake weather fetches."""
import asyncio
import io
import json
import math
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import credits  # noqa: E402
import dem as demmod  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import imagery  # noqa: E402
import test_server  # noqa: E402
import test_weather  # noqa: E402
import weather  # noqa: E402

LAT, LON = test_server.LAT, test_server.LON


def lon_of_tile_x(x, z):
    return (x / 2 ** z) * 360 - 180


class ServedDem(test_server.FakeDem):
    """The synthetic DEM, with providers like dem.Dem.served: Mapterhorn west of `split_lon`,
    the AWS fallback east of it (by DEM tile)."""

    def __init__(self, split_lon=LON + 0.02):
        self.split_lon = split_lon
        self.served = self

    def get(self, key):
        z, x, y = key
        return demmod.MAPTERHORN["name"] if lon_of_tile_x(x + 0.5, z) < self.split_lon else demmod.AWS["name"]


class WordingTest(unittest.TestCase):
    def test_imagery_credits_are_the_sources_own(self):
        for name, text in credits.IMAGERY.items():
            self.assertTrue(imagery.SOURCES[name].attribution.startswith(text), name)
        # EOX: their prescribed June 2026 wording, with the layer's data year
        self.assertIn("Copernicus Sentinel data 2023)", credits.IMAGERY["eox2023"])
        self.assertIn("Copernicus Sentinel data 2016 & 2017)", credits.IMAGERY["eox2016"])
        self.assertEqual(set(credits.IMAGERY), set(imagery.SOURCES))

    def test_elevation_and_weather(self):
        self.assertEqual(set(credits.ELEVATION), {demmod.MAPTERHORN["name"], demmod.AWS["name"]})
        self.assertIn("mapterhorn.com/attribution", credits.ELEVATION["mapterhorn"])
        self.assertEqual(credits.label(credits.WEATHER_ID), weather.CREDIT)
        self.assertIn("Mapterhorn", earthd.ELEVATION_CREDIT)

    def test_line(self):
        ids = {"dem:aws-terrarium", "img:eox2023", "wx:open-meteo", "img:usgs", "dem:mapterhorn"}
        self.assertEqual(credits.ordered(ids), ["img:usgs", "img:eox2023", "dem:mapterhorn", "dem:aws-terrarium",
                                                "wx:open-meteo"])
        text = credits.line(ids)
        self.assertEqual(text, "Imagery: USGS The National Map; " + credits.IMAGERY["eox2023"]
                         + " | Elevation: Mapterhorn (mapterhorn.com/attribution); AWS Terrain Tiles / Mapzen"
                         + " | Weather data by Open-Meteo.com")
        self.assertEqual(credits.line({"img:eox2016"}, far=True), "Far imagery: " + credits.IMAGERY["eox2016"])
        self.assertEqual(credits.line({"dem:mapterhorn"}, far=True), "Far elevation: Mapterhorn (mapterhorn.com/attribution)")
        self.assertEqual(credits.line(set()), "")
        # a source outside the catalogue (a custom chain) is named, after the known ones
        self.assertEqual(credits.ordered({"img:zz", "img:usgs"}), ["img:usgs", "img:zz"])
        self.assertEqual(credits.line({"img:zz"}), "Imagery: zz")

    def test_ascii_only(self):
        """The game's UI font lacks most non-ASCII glyphs (the settings page found it): the overlay
        text is plain ASCII."""
        for text in list(credits.IMAGERY.values()) + list(credits.ELEVATION.values()) + [weather.CREDIT]:
            text.encode("ascii")


class DemKeysTest(unittest.TestCase):
    def test_keys_cover_what_sample_reads(self):
        """dem_keys(footprint, zoom) are the DEM tiles Dem.sample reads for points on the footprint."""
        frame = geodesy.EnuFrame(LAT, LON)
        dem = demmod.Dem(tempfile.mkdtemp())
        read = set()

        def tiles(keys, progress=None):
            keys = list(keys)
            read.update(keys)
            return {k: np.zeros((demmod.TILE, demmod.TILE), np.float32) for k in keys}
        dem.tiles = tiles
        for key, zoom in (("-1_3_-5", 15), ("2_0_-1", 12), ("6_0_0", 8), ("9_-1_0", 5)):
            t = ff.tile_of(key)
            read.clear()
            xs = np.linspace(t.bbox[0], t.bbox[2], 9)
            E, N = np.meshgrid(xs, np.linspace(t.bbox[1], t.bbox[3], 9))
            lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
            dem.sample(lat, lon, zoom)
            keys = credits.dem_keys(frame, t.bbox, zoom)
            self.assertTrue(keys, key)
            self.assertLessEqual(set(keys), read, key)
            # sample also reads the neighbour a pixel beyond the edge; nothing further
            self.assertLessEqual(len(read), 4 * len(keys), key)
        dem.pool.shutdown()

    def test_limits(self):
        frame = geodesy.EnuFrame(LAT, LON)
        self.assertEqual(credits.dem_keys(frame, ff.tile_of("9_0_0").bbox, 15), [])     # too many: none
        polar = geodesy.EnuFrame(89.9, 0.0)
        self.assertEqual(credits.dem_keys(polar, (-500.0, -500.0, 500.0, 500.0), 10), [])
        # across the antimeridian: tiles on both sides, wrapped
        frame = geodesy.EnuFrame(10.0, 179.999)
        keys = credits.dem_keys(frame, (-2000.0, -10.0, 2000.0, 10.0), 10)
        self.assertEqual(sorted({x for _, x, _ in keys}), [0, 1023])


class DemServedTest(unittest.TestCase):
    """dem.Dem.served: the provider of every tile it loaded."""

    def test_served(self):
        tmp = tempfile.TemporaryDirectory()
        dem = demmod.Dem(tmp.name)

        def png(size):
            b = io.BytesIO()
            Image.new("RGB", (size, size), (128, 10, 0)).save(b, "PNG")
            return b.getvalue()

        def download(src, z, x, y):
            if src is demmod.MAPTERHORN:
                return png(512) if x == 1 else None
            return png(256) if x // 2 == 2 else None        # AWS z+1 children of x == 2
        dem._download = download
        got = dem.tiles([(12, 1, 7), (12, 2, 7), (12, 3, 7)])
        self.assertEqual(dem.served, {(12, 1, 7): "mapterhorn", (12, 2, 7): "aws-terrarium", (12, 3, 7): None})
        self.assertEqual(float(got[(12, 1, 7)][0, 0]), 128 * 256 + 10 - 32768)
        dem.pool.shutdown()
        tmp.cleanup()


class TileSourcesTest(unittest.TestCase):
    def test_tile_sources(self):
        frame = geodesy.EnuFrame(LAT, LON)
        t = ff.tile_of("-1_3_-5")
        dem = ServedDem(split_lon=-1000)            # AWS everywhere
        ids = credits.tile_sources(frame, t.bbox, dem, 15, {"usgs": 60000, "eox2023": 12, "none": 5, "deshadow": 9})
        self.assertEqual(ids, {"img:usgs", "img:eox2023", "dem:aws-terrarium"})
        self.assertEqual(credits.tile_sources(frame, t.bbox, dem, 15, None), {"dem:aws-terrarium"})
        self.assertEqual(credits.tile_sources(frame, t.bbox, test_server.FakeDem(), 15, {"eox2016": 1}),
                         {"img:eox2016"})                # a DEM without providers: imagery only
        self.assertEqual(credits.tile_sources(frame, t.bbox, dem, 15, {"usgs": 0, "none": 70000}),
                         {"dem:aws-terrarium"})


class ServerTileCreditsTest(unittest.TestCase):
    """Server.make_tile records each tile's sources with --credits on, nothing without."""

    def make(self, cred):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, near_visual="mesh", cells=64,
                          credits=cred)
        s.imagery = test_server.FakeImagery(os.path.join(tmp.name, "cache", "imagery"))
        s.dem = ServedDem()
        self.addCleanup(s.imagery.close)
        self.addCleanup(s.tile_pool.shutdown)
        self.addCleanup(s.hm_pool.shutdown)
        a = s.new_anchor(LAT, LON)
        return s, a

    def test_make_tile(self):
        s, a = self.make(credits.Credits())
        # FakeImagery: USGS only where its z10 coverage probe tile's centre is east of LON, i.e. from
        # ~15 km east of the origin; DEM (ServedDem): Mapterhorn west of LON + 0.02 (~1.8 km east)
        west = s.make_tile(a, ff.tile_of("-1_-8_0"))
        east = s.make_tile(a, ff.tile_of("-1_8_0"))
        far_east = s.make_tile(a, ff.tile_of("-1_20_0"))
        usgs = s.make_tile(a, ff.tile_of("-1_160_0"))           # 20 km east
        self.assertEqual(west.credits, {"img:eox2023", "dem:mapterhorn"})
        self.assertEqual(east.credits, {"img:eox2023", "dem:mapterhorn"})
        self.assertEqual(far_east.credits, {"img:eox2023", "dem:aws-terrarium"})
        self.assertEqual(usgs.credits, {"img:usgs", "dem:aws-terrarium"})
        coarse = s.make_tile(a, ff.tile_of("9_0_0"))        # 256 m texels, z9: USGS starts at z10
        self.assertIn("img:eox2023", coarse.credits)
        self.assertNotIn("img:usgs", coarse.credits)

    def test_palette_tile_has_no_imagery_credit(self):
        s, a = self.make(credits.Credits())

        class Broken(test_server.FakeImagery):
            def render(self, *a, **kw):
                raise imagery.ImageryError("offline")
        s.imagery.close()
        s.imagery = Broken(os.path.join(s.out_dir, "broken"))
        self.addCleanup(s.imagery.close)
        info = s.make_tile(a, ff.tile_of("-1_8_0"))
        self.assertIsNone(info.material)
        self.assertEqual(info.credits, {"dem:mapterhorn"})      # no imagery: elevation only

    def test_off_by_default(self):
        import inspect
        self.assertIsNone(inspect.signature(earthd.Server).parameters["credits"].default)
        s, a = self.make(None)
        self.assertEqual(s.make_tile(a, ff.tile_of("-1_8_0")).credits, frozenset())


def conn_stub(shown, tiles, focus=(0.0, 0.0), weather_msg=None, cred=None):
    server = types.SimpleNamespace(tiles=tiles, credits=cred)
    return types.SimpleNamespace(s=server, active=1, focus=focus, shown=shown,
                                 weather=types.SimpleNamespace(msg=weather_msg), sent=[],
                                 send=None, loop=None)


class MessageTest(unittest.TestCase):
    def info(self, *ids):
        return types.SimpleNamespace(credits=frozenset(ids))

    def test_near_far_split(self):
        tiles = {(1, "a"): self.info("img:usgs", "dem:mapterhorn"),
                 (1, "b"): self.info("img:eox2023", "dem:mapterhorn"),
                 (1, "c"): self.info("img:eox2023", "dem:aws-terrarium"),
                 (0, "d"): self.info("img:eox2016"),             # a previous anchor's tile, still shown
                 (1, "e"): self.info()}                           # a palette tile on the synthetic DEM
        shown = {(1, "a"): (-64, -64, 64, 64), (1, "b"): (1900, 0, 2100, 100),   # 1.9 km: near
                 (1, "c"): (2100, 0, 2400, 100), (0, "d"): (-50000, -50000, -40000, -40000),
                 (1, "e"): (0, 0, 10, 10), (1, "gone"): (0, 0, 1, 1)}           # evicted record: skipped
        c = conn_stub(shown, tiles)
        m = credits.Credits().message(c)
        self.assertEqual(m["ids"], ["img:usgs", "img:eox2023", "dem:mapterhorn"])
        self.assertEqual(m["farIds"], ["img:eox2016", "dem:aws-terrarium"])     # near sources not repeated
        self.assertEqual(m["near"], "Imagery: USGS The National Map; " + credits.IMAGERY["eox2023"]
                         + " | Elevation: Mapterhorn (mapterhorn.com/attribution)")
        self.assertEqual(m["far"], "Far imagery: " + credits.IMAGERY["eox2016"]
                         + " | Far elevation: AWS Terrain Tiles / Mapzen")
        self.assertEqual((m["type"], m["v"]), ("credits", credits.VERSION))
        json.dumps(m)
        # the player moves east: b and c near, a still within 2 km
        c.focus = (2000.0, 0.0)
        m = credits.Credits().message(c)
        self.assertEqual(m["ids"], ["img:usgs", "img:eox2023", "dem:mapterhorn", "dem:aws-terrarium"])
        self.assertEqual(m["farIds"], ["img:eox2016"])
        # a smaller radius: only the tile under the player
        c.focus = (0.0, 0.0)
        m = credits.Credits(near_m=100).message(c)
        self.assertEqual(m["ids"], ["img:usgs", "dem:mapterhorn"])

    def test_weather_and_nothing(self):
        c = conn_stub({}, {})
        self.assertIsNone(credits.Credits().message(c))         # nothing known yet: no message
        c.weather.msg = {"type": "weather"}
        m = credits.Credits().message(c)
        self.assertEqual((m["near"], m["far"], m["ids"], m["farIds"]), (weather.CREDIT, "", ["wx:open-meteo"], []))
        c.active = None
        self.assertIsNone(credits.Credits().message(c))


class FakeLoop:
    def __init__(self):
        self.calls = []

    def call_later(self, delay, fn, *args):
        h = types.SimpleNamespace(delay=delay, fn=fn, args=args, cancelled=False)
        h.cancel = lambda: setattr(h, "cancelled", True)
        self.calls.append(h)
        return h

    def run(self):
        """Run the earliest pending call; returns its delay."""
        h = self.calls.pop(0)
        h.fn(*h.args)
        return h.delay


class TrackerTest(unittest.TestCase):
    """Change-only sending, at most one check per interval, re-send state for hello."""

    def setUp(self):
        self.now = 100.0
        self.cred = credits.Credits(clock=lambda: self.now)
        self.tiles = {(1, "a"): types.SimpleNamespace(credits=frozenset({"img:usgs"}))}
        self.c = conn_stub({(1, "a"): (0, 0, 10, 10)}, self.tiles, cred=self.cred)
        self.c.loop = FakeLoop()
        self.c.send = self.c.sent.append
        self.t = credits.Tracker()

    def test_change_only_and_throttle(self):
        t, c, loop = self.t, self.c, self.c.loop
        t.poke(c)
        t.poke(c)                                   # folded into the scheduled check
        self.assertEqual([h.delay for h in loop.calls], [0.0])
        loop.run()
        self.assertEqual([m["ids"] for m in c.sent], [["img:usgs"]])
        self.assertIs(t.msg, c.sent[-1])
        # nothing changed: checks go on (one per interval), nothing is sent
        self.now += 0.3
        t.poke(c)
        self.assertAlmostEqual(loop.calls[0].delay, 0.7)
        for _ in range(5):
            t.poke(c)
        self.assertEqual(len(loop.calls), 1)
        self.now += 0.7
        loop.run()
        self.assertEqual(len(c.sent), 1)
        # a change: sent at the next check, never sooner than the interval after the last one
        c.shown[(1, "b")] = (5000, 0, 5100, 100)
        self.tiles[(1, "b")] = types.SimpleNamespace(credits=frozenset({"img:eox2023"}))
        self.now += 0.1
        t.poke(c)
        self.assertAlmostEqual(loop.calls[0].delay, 0.9)
        self.now += 0.9
        loop.run()
        self.assertEqual(len(c.sent), 2)
        self.assertEqual(c.sent[-1]["farIds"], ["img:eox2023"])
        # a long pause: checked at once
        self.now += 10
        t.poke(c)
        self.assertEqual(loop.calls[0].delay, 0.0)
        loop.run()
        self.assertEqual((len(c.sent), t.checks), (2, 4))

    def test_off_and_close(self):
        t, c = self.t, self.c
        c.s.credits = None
        t.poke(c)
        self.assertEqual(c.loop.calls, [])
        c.s.credits = self.cred
        t.poke(c)
        h = c.loop.calls[0]
        t.close()
        self.assertTrue(h.cancelled)
        self.assertIsNone(t.timer)

    def test_error_keeps_the_connection(self):
        t, c = self.t, self.c
        self.cred.message = lambda conn: 1 / 0
        t.poke(c)
        with self.assertLogs("earthd.credits", "ERROR"):
            c.loop.run()
        self.assertEqual(c.sent, [])


class SessionCreditsTest(test_server.ServerHarness):
    """A real in-process session (mesh, 64 cells, synthetic imagery and DEM with providers, fake
    weather): credits messages arrive once tiles are shown, only on change, at most one per
    interval, and again on a hello."""
    near_visual = "mesh"
    with_imagery = True
    cells = 64
    INTERVAL = 0.25

    def setUp(self):
        super().setUp()
        self.server.dem = ServedDem(split_lon=LON + 0.3)       # AWS from ~27 km east
        self.server.credits = credits.Credits(interval=self.INTERVAL)
        self.server.weather = weather.Service(fetch=test_weather.FakeFetch())

    def credits_msgs(self, msgs):
        return [m for m in msgs if m["type"] == "credits"]

    def test_session(self):
        c = self.c
        self.hello()
        a = self.prepare_anchor(1, LAT, LON)
        aid = a["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        self.pose(aid, 0, 0)
        _, seen = c.until(lambda m: m["type"] == "far_done", 120)
        self.pose(aid, 0, 0)
        seen += c.drain(3 * self.INTERVAL)
        got = self.credits_msgs(seen)
        self.assertTrue(got)
        last = got[-1]
        # near the player: EOX 2023 (FakeImagery's USGS starts ~15 km east), Mapterhorn (AWS from
        # ~27 km east), the weather from the fake fetch
        self.assertEqual(last["ids"], ["img:eox2023", "dem:mapterhorn", "wx:open-meteo"])
        # the far field (80 km): USGS and AWS east of their limits
        self.assertEqual(last["farIds"], ["img:usgs", "dem:aws-terrarium"])
        self.assertEqual(last["near"], "Imagery: " + credits.IMAGERY["eox2023"]
                         + " | Elevation: Mapterhorn (mapterhorn.com/attribution) | Weather data by Open-Meteo.com")
        self.assertEqual(last["far"], "Far imagery: USGS The National Map | Far elevation: AWS Terrain Tiles / Mapzen")
        # only on change: every message differs from the one before
        for prev, m in zip(got, got[1:]):
            self.assertNotEqual(prev, m)
        # the throttle: no two messages closer than the interval (receive times, so a little slack)
        conn = self.conn()
        # poses at the same place: no new message
        checks = conn.credits.checks
        for _ in range(4):
            self.pose(aid, 0, 0)
        quiet = c.drain(3 * self.INTERVAL)
        self.assertEqual(self.credits_msgs(quiet), [])
        self.assertGreater(conn.credits.checks, checks)
        # re-sent on hello
        c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, before = c.until(lambda m: m["type"] == "credits", 10)
        self.assertEqual(m, last)
        self.assertEqual([x["type"] for x in before if x["type"] == "hello"], ["hello"])
        # the player 50 km east: only AWS under the near tiles once the far field there is in
        self.pose(aid, 50000, 0)
        _, moving = c.until(lambda m: m["type"] == "far_done", 120)
        self.pose(aid, 50000, 0)
        later = self.credits_msgs(moving + c.drain(3 * self.INTERVAL))
        self.assertTrue(later)
        self.assertEqual(later[-1]["ids"], ["img:usgs", "dem:aws-terrarium", "wx:open-meteo"])
        self.assertEqual(later[-1]["farIds"], ["img:eox2023", "dem:mapterhorn"])

    def test_throttle_in_session(self):
        """Many swaps and poses: messages at least one interval apart (server send times)."""
        sent = []
        conn_send = earthd.Connection.send

        def send(conn, msg):
            if msg.get("type") == "credits":
                sent.append(self.server.credits.clock())
            conn_send(conn, msg)
        earthd.Connection.send = send
        self.addCleanup(setattr, earthd.Connection, "send", conn_send)
        c = self.c
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        for i in range(30):                         # 10 poses a second, the tiles swapping under the car
            self.pose(aid, i * 60.0, 0, vx=600)
            c.drain(0.1)
        c.drain(3 * self.INTERVAL)
        self.assertGreaterEqual(len(sent), 2)
        gaps = [b - a for a, b in zip(sent, sent[1:])]
        self.assertGreaterEqual(min(gaps), self.INTERVAL * 0.95, gaps)


class DefaultServerTest(test_server.ServerHarness):
    """Without --credits (Server()'s default) a session never sends a credits message."""
    near_visual = "mesh"
    with_imagery = True
    cells = 64

    def test_no_credits(self):
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        self.c.send({"type": "commit_anchor", "anchorId": aid})
        self.pose(aid, 0, 0)
        _, seen = self.c.until(lambda m: m["type"] == "far_done", 120)
        self.pose(aid, 0, 0)
        seen += self.c.drain(0.5)
        self.assertFalse([m for m in seen if m["type"] == "credits"])
        self.assertIsNone(self.conn().credits.msg)


if __name__ == "__main__":
    unittest.main()
