"""The server's memory and CPU budget for the machine it runs on.

Every worker process keeps its own caches (decoded DEM tiles, map data cells, imagery tiles), sized for a desktop.
On a Steam Deck (16 GB shared with the GPU, 4 cores) seven processes filled the memory: 8.2 GB for the server next to
the game's 2.4 GB and the GPU's 1.9 GB, swap at 6.9 GB, and the system's out-of-memory guard killed the game in a
Baltimore drive (the user, 2026-10-20: "the game just closed"; "I want to keep world generation fast on the deck as
well"). SMALL machines (less than SMALL_GB of RAM) get smaller caches and fewer processes; the others keep the
desktop sizes.

    budget.apply()          # in each process before its caches are made (earthd main, blockworker.init)
"""
import os

SMALL_GB = 20.0             # below this much RAM: the small profile (a Steam Deck shows ~14.5 GB)
# per process, small profile (desktop sizes in the modules: dem 512 tiles, imagery 512 tiles, mapdata 64 MB)
SMALL_DEM_TILES = 96        # decoded 512² float32 DEM tiles (1 MB each)
SMALL_IMAGERY_TILES = 192   # decoded 256² RGB imagery tiles (~200 KB each), and as many colour-matched ones
SMALL_MAPDATA_MB = 32       # decoded map data cell layers
SMALL_ROAD_PROCESSES = 1    # road-tile processes (earthd ROAD_PROCESSES)
SMALL_FAR_WORKERS = 2       # far-tile worker processes (earthd --workers)
SMALL_SERVER_SHARE = 0.5    # of the physical cores for the server (cpupin.SERVER_SHARE): one of four was too few


def total_gb():
    """The machine's RAM in GB (None if unknown)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        pass
    try:                                    # Windows
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m.ullTotalPhys / 1e9
    except Exception:
        pass
    return None


def small(gb=None):
    """Is this a small machine (the small profile)? BNGE_BUDGET=small / desktop overrides."""
    force = os.environ.get("BNGE_BUDGET", "").lower()
    if force in ("small", "desktop"):
        return force == "small"
    gb = total_gb() if gb is None else gb
    return gb is not None and gb < SMALL_GB


def apply(is_small=None):
    """Set the cache sizes of this process's modules for the profile; returns whether it is the small one."""
    is_small = small() if is_small is None else is_small
    if is_small:
        import dem
        import imagery
        import mapdata
        dem.MEM_TILES = SMALL_DEM_TILES
        imagery.MEM_TILES = SMALL_IMAGERY_TILES
        mapdata.MEM_MB = SMALL_MAPDATA_MB
    return is_small
