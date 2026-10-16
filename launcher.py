#!/usr/bin/env python3
"""BeamNG.Earth launcher: one start for everything (the AppImage and the .exe run this; from the source folder:
python launcher.py).

  1. copies the mod into the game's user folder when it differs (one zip, one version),
  2. starts the world server (server/earthd.py) with the recommended options,
  3. starts BeamNG.drive unless it already runs. Linux: the native game directly, with tools/gcpatch preloaded
     (keeps the grass when the physics block moves; it checks the game build and does nothing on another), so no
     Steam launch options are needed; through Steam when that is not possible (Steam not running, the Windows
     build under Proton). Windows: through Steam.
  4. stops the server when the game closes (or on Ctrl+C / closing the window).

    launcher.py                  everything
    launcher.py --no-game        only the server (start the game yourself)
    launcher.py --no-gcpatch     the game without the grass patch
    launcher.py -- --verbose     anything after -- goes to the server
"""

import argparse
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import time

FROZEN = getattr(sys, "frozen", False)
ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
APP_ID = "284160"
GAME_EXE_LINUX = os.path.join("BinLinux", "BeamNG.drive.x64")
GAME_EXE_WIN = "BeamNG.drive.x64.exe"
GCPATCH_NAME = "libbngearth_gcpatch.so"
# the recommended server options (start.py's)
RECOMMENDED = ["--deshadow", "full", "--near-visual", "detail", "--airports", "on", "--sea", "on", "--trees", "on",
               "--buildings", "on"]
GAME_START_S = 180          # waiting for the game's process after asking Steam
DIRECT_FAIL_S = 20          # a direct start that ends this soon did not work (no Steam client): through Steam


def say(msg=""):
    print(f"[BeamNG.Earth] {msg}" if msg else "", flush=True)


def data_dir():
    """Where the launcher keeps its files (logs, the gcpatch copy): the server's cache folder's parent."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "beamng-earth")
    return os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")


# --- the server -----------------------------------------------------------------------------------

def run_server(argv):
    """In this process: server/earthd.py with argv (the --server mode of the packaged app)."""
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import earthd
    sys.argv = ["earthd"] + argv
    earthd.main()


def start_server(user_dir, extra, log_path):
    cmd = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)])
    cmd += ["--server", "--user-dir", user_dir] + RECOMMENDED + extra
    kw = {"cwd": ROOT}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True          # its worker processes with it: stopped as one group
    out = None
    if not (sys.stdout and sys.stdout.isatty()):  # no terminal (the AppImage from a file manager): a log file
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        out = open(log_path, "w", encoding="utf-8")
        kw.update(stdout=out, stderr=subprocess.STDOUT)
        say(f"server log: {log_path}")
    return subprocess.Popen(cmd, **kw)


def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    say("stopping the world server")
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(15)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if IS_WIN:
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


# --- the game -------------------------------------------------------------------------------------

def linux_game_exe():
    import install
    for lib in install._steam_roots():
        exe = os.path.join(lib, "steamapps", "common", "BeamNG.drive", GAME_EXE_LINUX)
        if os.path.isfile(exe):
            return exe
    return None


def game_pids():
    """The game's main processes (not its CEF helpers)."""
    if IS_WIN:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE_WIN}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True)
        return [int(line.split('","')[1]) for line in r.stdout.splitlines() if line.startswith(f'"{GAME_EXE_WIN}"')]
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        exe = args[0].decode(errors="replace") if args else ""
        if (exe.endswith("/" + GAME_EXE_LINUX.replace(os.sep, "/")) or exe.endswith(GAME_EXE_WIN)) and \
                not any(a.startswith(b"--type=") for a in args):
            out.append(int(d))
    return out


def steam_running():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".steam", "steam.pid")) as f:
            pid = int(f.read().strip())
        return os.path.exists(f"/proc/{pid}")
    except (OSError, ValueError):
        return False


def gcpatch_lib():
    """A copy of the gcpatch library outside the bundle (an AppImage's files go when it closes), or None."""
    for src in (os.path.join(ROOT, "gcpatch", GCPATCH_NAME),
                os.path.join(ROOT, "tools", "gcpatch", "build", GCPATCH_NAME)):
        if os.path.isfile(src):
            dst = os.path.join(data_dir(), "gcpatch", GCPATCH_NAME)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.isfile(dst) or open(dst, "rb").read() != open(src, "rb").read():
                shutil.copyfile(src, dst + ".tmp")
                os.replace(dst + ".tmp", dst)
            return dst
    return None


def clean_env():
    """The environment for the game: without the frozen app's library path (PyInstaller keeps the original)."""
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH",):
        orig = env.pop(k + "_ORIG", None)
        if FROZEN:
            if orig is not None:
                env[k] = orig
            else:
                env.pop(k, None)
    for k in ("APPDIR", "APPIMAGE", "ARGV0", "OWD"):      # AppImage runtime variables
        env.pop(k, None)
    return env


def steam_launch():
    if IS_WIN:
        os.startfile(f"steam://rungameid/{APP_ID}")
    else:
        steam = shutil.which("steam")
        cmd = [steam, "-applaunch", APP_ID] if steam else ["xdg-open", f"steam://rungameid/{APP_ID}"]
        subprocess.Popen(cmd, env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def start_game(use_gcpatch):
    """Start the game; returns a Popen when this process started it directly, else None (through Steam)."""
    if not IS_WIN:
        exe = linux_game_exe()
        if exe and steam_running():
            env = clean_env()
            env["SteamAppId"] = env["SteamGameId"] = APP_ID
            lib = gcpatch_lib() if use_gcpatch else None
            if lib:
                env["LD_PRELOAD"] = lib + (":" + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else "")
            say("starting BeamNG.drive" + (" (with the grass patch, tools/gcpatch)" if lib else ""))
            proc = subprocess.Popen([exe], cwd=os.path.dirname(os.path.dirname(exe)), env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            t0 = time.time()
            while time.time() - t0 < DIRECT_FAIL_S:
                if proc.poll() is not None:
                    break
                time.sleep(1)
            if proc.poll() is None:
                return proc
            say(f"the direct start ended (code {proc.returncode}); starting the game through Steam instead")
        elif exe:
            say("Steam is not running: starting the game through Steam (without the grass patch)")
    say("starting BeamNG.drive through Steam")
    steam_launch()
    return None


def wait_for_game(proc):
    """Until the game closes."""
    if proc is not None:
        proc.wait()
        return
    t0 = time.time()
    while not game_pids():
        if time.time() - t0 > GAME_START_S:
            say("the game did not start (no BeamNG.drive process); the server keeps running, Ctrl+C stops it")
            while True:
                time.sleep(3600)
        time.sleep(2)
    while game_pids():
        time.sleep(3)


# --- main -----------------------------------------------------------------------------------------

def user_folder():
    import install
    found = install.candidate_user_dirs()
    if found:
        return found[0]
    # the game never ran: its default folder (the game uses it on its first start)
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        d = os.path.join(base, "BeamNG", "BeamNG.drive", "current")
    else:
        d = os.path.join(os.path.expanduser("~"), ".local", "share", "BeamNG", "BeamNG.drive", "current")
    os.makedirs(d, exist_ok=True)
    return d


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--server":
        run_server(sys.argv[2:])
        return
    ap = argparse.ArgumentParser(description="Start the BeamNG.Earth world server and BeamNG.drive.")
    ap.add_argument("--no-game", action="store_true", help="only the server (start the game yourself)")
    ap.add_argument("--no-gcpatch", action="store_true", help="Linux: start the game without the grass patch")
    ap.add_argument("--keep-mod", action="store_true", help="leave the game's copy of the mod as it is")
    ap.add_argument("--user-dir", help="BeamNG.drive user folder (default: found)")
    ap.add_argument("extra", nargs=argparse.REMAINDER, help="after --: more options for the server")
    args = ap.parse_args()
    if not (sys.stdout and sys.stdout.isatty()) and not IS_WIN:    # no terminal (from a file manager): a log
        log = os.path.join(data_dir(), "logs", "launcher.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        sys.stdout = sys.stderr = open(log, "w", encoding="utf-8")
    import install
    say(f"BeamNG.Earth {install.version()}")
    user_dir = args.user_dir or user_folder()
    say(f"BeamNG.drive user folder: {user_dir}")
    running = bool(game_pids())
    if not args.keep_mod:
        if running:
            say("the game is already running: the mod is copied now, the game loads it on its next start")
        install.install_mod(user_dir, True, dry_run=False)
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    server = start_server(user_dir, extra, os.path.join(data_dir(), "logs", "server.log"))
    say("world server started")
    try:
        if args.no_game:
            say("Start BeamNG.drive and pick the 'BeamNG.Earth' level in Freeroam. Ctrl+C stops the server.")
            server.wait()
            return
        if running:
            say("BeamNG.drive is already running")
            proc = None
        else:
            proc = start_game(not args.no_gcpatch)
        say("In the game: Freeroam > 'BeamNG.Earth'. The server stops when the game closes.")
        wait_for_game(proc)
        say("the game closed")
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(server)


if __name__ == "__main__":
    multiprocessing.freeze_support()        # the server's worker processes in the packaged app
    main()
