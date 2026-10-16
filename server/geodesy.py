"""WGS84 geodesy in float64: geodetic <-> ECEF <-> local ENU.

All functions take scalars or numpy arrays (degrees for lat/lon, metres for the
rest) and broadcast like numpy ufuncs.
"""
import math

import numpy as np

try:
    import earthfast as _ef     # Rust kernels (server/fast.py): the point-list conversion below
except ImportError:
    _ef = None

A = 6378137.0                    # semi-major axis
F = 1 / 298.257223563            # flattening
B = A * (1 - F)                  # semi-minor axis
E2 = F * (2 - F)                 # first eccentricity squared
EP2 = E2 / (1 - E2)              # second eccentricity squared


def geodetic_to_ecef(lat, lon, h=0.0):
    lat, lon = np.radians(lat), np.radians(lon)
    sl, cl = np.sin(lat), np.cos(lat)
    n = A / np.sqrt(1 - E2 * sl * sl)
    r = (n + h) * cl
    return r * np.cos(lon), r * np.sin(lon), (n * (1 - E2) + h) * sl


def ecef_to_geodetic(x, y, z):
    """Heikkinen's closed form (exact, no iteration). Returns (lat, lon, h)."""
    x, y, z = (np.asarray(v, dtype=np.float64) for v in (x, y, z))
    p2 = x * x + y * y
    p = np.sqrt(p2)
    z2 = z * z
    with np.errstate(divide="ignore", invalid="ignore"):
        f_ = 54 * B * B * z2
        g = p2 + (1 - E2) * z2 - E2 * (A * A - B * B)
        c = E2 * E2 * f_ * p2 / (g * g * g)
        s = np.cbrt(1 + c + np.sqrt(c * c + 2 * c))
        k = s + 1 + 1 / s
        pp = f_ / (3 * k * k * g * g)
        q = np.sqrt(1 + 2 * E2 * E2 * pp)
        r0 = -pp * E2 * p / (1 + q) + np.sqrt(np.maximum(
            0.5 * A * A * (1 + 1 / q) - pp * (1 - E2) * z2 / (q * (1 + q)) - 0.5 * pp * p2, 0.0))
        t = (p - E2 * r0) ** 2
        u = np.sqrt(t + z2)
        v = np.sqrt(t + (1 - E2) * z2)
        z0 = B * B * z / (A * v)
        h = u * (1 - B * B / (A * v))
        lat = np.degrees(np.arctan2(z + EP2 * z0, p))
    # On the polar axis the closed form degenerates; the answer is trivial there.
    pole = p < 1e-6
    if np.any(pole):
        lat = np.where(pole, np.copysign(90.0, z), lat)
        h = np.where(pole, np.abs(z) - B, h)
    lon = np.degrees(np.arctan2(y, x))
    return lat, lon, h


class EnuFrame:
    """Local East-North-Up frame tangent to the ellipsoid at (lat0, lon0, h0)."""

    def __init__(self, lat0, lon0, h0=0.0):
        self.lat0, self.lon0, self.h0 = float(lat0), float(lon0), float(h0)
        self.origin = np.array(geodetic_to_ecef(lat0, lon0, h0))
        la, lo = np.radians(lat0), np.radians(lon0)
        sla, cla, slo, clo = np.sin(la), np.cos(la), np.sin(lo), np.cos(lo)
        # Rows are the E, N, U unit vectors expressed in ECEF.
        self.rot = np.array([
            [-slo, clo, 0.0],
            [-sla * clo, -sla * slo, cla],
            [cla * clo, cla * slo, sla],
        ])

    def enu_to_ecef(self, e, n, u=0.0):
        r = self.rot
        o = self.origin
        return (o[0] + r[0, 0] * e + r[1, 0] * n + r[2, 0] * u,
                o[1] + r[0, 1] * e + r[1, 1] * n + r[2, 1] * u,
                o[2] + r[0, 2] * e + r[1, 2] * n + r[2, 2] * u)

    def ecef_to_enu(self, x, y, z):
        dx, dy, dz = x - self.origin[0], y - self.origin[1], z - self.origin[2]
        r = self.rot
        return (r[0, 0] * dx + r[0, 1] * dy + r[0, 2] * dz,
                r[1, 0] * dx + r[1, 1] * dy + r[1, 2] * dz,
                r[2, 0] * dx + r[2, 1] * dy + r[2, 2] * dz)

    def enu_to_geodetic(self, e, n, u=0.0):
        return ecef_to_geodetic(*self.enu_to_ecef(e, n, u))

    def geodetic_to_enu(self, lat, lon, h=0.0):
        if _ef is not None and type(lat) is np.ndarray and lat.ndim == 1 and np.shape(lon) == lat.shape:
            # the Rust kernel for point lists (a road tile made ~60k small calls, ~35 us each in numpy)
            f64 = np.float64
            hh = np.asarray(h, f64)
            hh = np.full(lat.shape, float(hh)) if hh.ndim == 0 else hh
            if hh.shape == lat.shape:
                return _ef.geodetic_to_enu(lat.astype(f64, copy=False), np.asarray(lon, f64), hh, self.origin,
                                           self.rot)
        return self.ecef_to_enu(*geodetic_to_ecef(lat, lon, h))

    def surface_enu(self, e, n, elev_fn):
        """Spec steps 1-5: drop the plane point (e, n, 0) onto the DEM surface.

        Returns (lat, lon, elev, E, N, U): the geodetic position under the plane
        point, its DEM elevation, and the true ENU position of the surface point.
        """
        lat, lon, _ = self.enu_to_geodetic(e, n, 0.0)
        elev = elev_fn(lat, lon)
        return (lat, lon, elev) + self.geodetic_to_enu(lat, lon, elev)


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance (m) on the WGS84 mean sphere between two points in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))
