"""Update check (the user, 2026-10-15: "add an update system where the mod will check GitHub for any updates").

The project's VERSION file on GitHub's main branch is read (raw.githubusercontent.com, netfetch's policy: a few
requests a day, cached for hours) and compared with this copy's VERSION; when GitHub's is newer the server tells the
mod ('update' message), which says so once in game and on its settings page, with the link to the download.

    newer("1.4.1-preview", "1.4.0-preview") -> True
    check(local_version, net) -> {"current", "latest", "url"} or None (up to date, offline, no VERSION there)
"""
import os
import re
import sys

import netfetch

REPO = "TecnaGamer/BeamNG.Earth"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/VERSION"
PAGE_URL = f"https://github.com/{REPO}/releases"      # the downloads (.exe / AppImage)
# the folder with VERSION: above server/, or the packaged app's bundle (launcher.py frozen by PyInstaller, where
# every module sits at the bundle's top)
ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def local_version(root=ROOT):
    """This copy's VERSION (the folder above server/), or None."""
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            v = f.read().strip()
        return v or None
    except OSError:
        return None


def parse(v):
    """(major, minor, patch, release) of a version like "1.4.0-preview" ("-..." is a pre-release: below the same
    numbers without it); None when it is not one."""
    m = re.fullmatch(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?\s*", str(v or ""))
    if not m:
        return None
    nums = tuple(int(g or 0) for g in m.groups()[:3])
    return nums + ((0, m.group(4)) if m.group(4) else (1, ""),)


def newer(latest, current):
    """Is version latest newer than current? (False when either is not a version.)"""
    a, b = parse(latest), parse(current)
    return a is not None and b is not None and a > b


def check(current, net=None):
    """The update GitHub has for version current, or None. Blocking (a worker thread)."""
    if not current:
        return None
    net = net if net is not None else netfetch.default_fetcher()
    try:
        latest = net.get(VERSION_URL, kind="background").body.decode("utf-8", "replace").strip()
    except (netfetch.FetchError, OSError):
        return None
    if not newer(latest, current):
        return None
    return {"current": current, "latest": latest, "url": PAGE_URL}
