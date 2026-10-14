# BeamNG.Earth: quick start

Drive anywhere on the real Earth in BeamNG.drive. This page gets you from the download to driving
in a few minutes. No programming needed.

## What you need
- **BeamNG.drive** (tested with 0.39.x), started at least once.
- **Python 3.10 or newer** from <https://www.python.org/downloads/>. On Windows, tick
  **"Add python.exe to PATH"** on the first page of the Python installer.
- **About 2 GB of free disk space** for the map cache (it grows as you explore and is capped).
- **An internet connection**: the map (height data, aerial photos, roads, buildings) is downloaded
  while you play.

## Install (3 steps)
1. **Unzip** `BeamNG.Earth-<version>.zip` anywhere you like (for example your Documents folder).
2. **Run the installer.** Open the unzipped folder and:
   - Windows: double-click `run_server.bat`. The first time, it runs the installer for you. (Or
     open a terminal in the folder and type `py install.py`.)
   - Linux: open a terminal in the folder and run `python3 install.py`.

   The installer finds your BeamNG.drive user folder, copies the mod into it, and downloads the
   server's Python packages (a few minutes). If it finds more than one BeamNG folder it asks which
   one your game uses. Running it again later is safe: it only updates what changed.
3. **Play.**
   - Start the world server: `run_server.bat` (Windows) or `./run_server.sh` (Linux). Keep its
     window open while you play; close it when you are done.
   - Start BeamNG.drive, go to **Freeroam** and pick the **BeamNG.Earth** level.

## Going places
- Press **Esc**, open the **Mods** tab and click **BeamNG.Earth**. The **Places** section lets you
  type (or paste from Google Maps) a "latitude, longitude" and press **Go**, save your favourite
  spots, and jump back to recent ones.
- The first visit to a new area takes a little while: the map is downloaded and built while you
  watch. Going back later is much faster (it is cached).

## Traffic
Traffic is on by default. You can change it in the level-start traffic option, or while driving
with the game's **radial menu** (the traffic entry), like on any other map.

## Something wrong? Tell us
See the in-game report tool (**Ctrl+Shift+N**) to note what you saw and where. Issues go to
<https://github.com/TecnaGamer/BeamNG.Earth/issues>: say where you were (the place or its latitude,
longitude), what you expected, and add a screenshot if you can.

## Troubleshooting
- **"Looking for the world server" / "server not connected" banner** in the game: the server is not
  running. Start `run_server.bat` / `./run_server.sh` and wait a few seconds; the game reconnects
  by itself. If the server window shows an error, run the installer again.
- **Windows Firewall asks about Python**: the server only talks to the game on your own computer
  (local port 47613). You can allow it for private networks only, or cancel; the game still connects.
- **The first load is slow, or the ground looks blurry at first**: it is downloading. This gets
  faster as the cache fills up.
- **The level is not in the list**: the mod is not in the folder the game uses. Run the installer
  again and pick the right folder (in the game, Options > Other shows the user folder), or run
  `python install.py --user-dir "<that folder>"`.
- **After an update**: unzip the new version, run the installer again (or `python start.py --update-mod`).

## Credits
The map comes from free and open data sources (USGS, Sentinel-2 by EOX, Mapterhorn, OpenStreetMap
and Overture contributors, and more). The full list and their licences are in
[CREDITS.md](../CREDITS.md); the game also shows the credits on screen.
BeamNG.Earth is an unofficial fan project, not made or endorsed by BeamNG GmbH.
