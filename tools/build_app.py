#!/usr/bin/env python3
"""Build the one-file player downloads of launcher.py (the mod, the world server and its Python inside):

    Linux:   dist/BeamNG.Earth-<VERSION>-x86_64.AppImage   (PyInstaller one-folder app in an AppImage)
    Windows: dist/BeamNG.Earth-<VERSION>.exe               (PyInstaller one-file app)

    python tools/build_app.py [--appimagetool PATH]

Needs, in the Python that runs it: pyinstaller and the server's packages (server/requirements.txt). The Rust kernels
(server/earthfast.abi3.so / .pyd) and, on Linux, tools/gcpatch/build/libbngearth_gcpatch.so go in when they exist
(tools/build_native.py, tools/gcpatch/build.sh); the app runs without them. The AppImage step needs appimagetool
(https://github.com/AppImage/appimagetool); GitHub Actions builds both (.github/workflows/release.yml).
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "BeamNG.Earth"
IS_WIN = os.name == "nt"
SEP = ";" if IS_WIN else ":"


def version():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def make_icons(work):
    """icon.png (256 px, the level's preview cropped square) and icon.ico."""
    from PIL import Image
    im = Image.open(os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "beamng_earth_preview.jpg"))
    s = min(im.size)
    im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2))
    im = im.convert("RGBA").resize((256, 256), Image.LANCZOS)
    png, ico = os.path.join(work, "icon.png"), os.path.join(work, "icon.ico")
    im.save(png)
    im.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return png, ico


def pyinstaller(work, ico, onefile):
    server = os.path.join(ROOT, "server")
    mods = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(server, "*.py")))
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--console", "--name", NAME,
           "--distpath", os.path.join(work, "dist"), "--workpath", os.path.join(work, "build"),
           "--specpath", work, "--paths", server, "--paths", ROOT, "--icon", ico,
           "--collect-submodules", "fsspec",
           "--add-data", f"{os.path.join(ROOT, 'mod', 'beamng_earth')}{SEP}mod/beamng_earth",
           "--add-data", f"{os.path.join(ROOT, 'VERSION')}{SEP}."]
    cmd += ["--onefile"] if onefile else ["--onedir"]
    for m in mods:                                  # the server runs from launcher.py --server: all of it
        cmd += ["--hidden-import", m]
    for lib in ("earthfast.pyd", "earthfast.abi3.so"):
        p = os.path.join(server, lib)
        if os.path.isfile(p) and (lib.endswith(".pyd") == IS_WIN):
            cmd += ["--add-binary", f"{p}{SEP}."]
    gc = os.path.join(ROOT, "tools", "gcpatch", "build", "libbngearth_gcpatch.so")
    if not IS_WIN and os.path.isfile(gc):
        cmd += ["--add-binary", f"{gc}{SEP}gcpatch"]
    cmd.append(os.path.join(ROOT, "launcher.py"))
    subprocess.run(cmd, check=True, cwd=ROOT)
    return os.path.join(work, "dist")


def appimage(dist, png, out, tool):
    appdir = os.path.join(os.path.dirname(dist), "AppDir")
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(os.path.join(dist, NAME), os.path.join(appdir, "usr", "lib", NAME), symlinks=True)
    with open(os.path.join(appdir, "AppRun"), "w") as f:
        f.write('#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
                f'exec "$HERE/usr/lib/{NAME}/{NAME}" "$@"\n')
    os.chmod(os.path.join(appdir, "AppRun"), 0o755)
    with open(os.path.join(appdir, "beamng-earth.desktop"), "w") as f:
        f.write(f"[Desktop Entry]\nType=Application\nName={NAME}\nComment=Drive the real world in BeamNG.drive\n"
                "Exec=BeamNG.Earth\nIcon=beamng-earth\nCategories=Game;\nTerminal=false\n")
    shutil.copyfile(png, os.path.join(appdir, "beamng-earth.png"))
    shutil.copyfile(png, os.path.join(appdir, ".DirIcon"))
    env = dict(os.environ, ARCH="x86_64")
    subprocess.run([tool, "--appimage-extract-and-run", appdir, out] if tool.endswith(".AppImage") else
                   [tool, appdir, out], check=True, env=env)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--appimagetool", default=shutil.which("appimagetool") or "appimagetool",
                    help="Linux: the appimagetool executable (or its .AppImage)")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args()
    work = os.path.join(ROOT, "build", "app")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    os.makedirs(a.out, exist_ok=True)
    png, ico = make_icons(work)
    ver = version()
    if IS_WIN:
        dist = pyinstaller(work, ico, onefile=True)
        out = os.path.join(a.out, f"{NAME}-{ver}.exe")
        shutil.copyfile(os.path.join(dist, NAME + ".exe"), out)
    else:
        dist = pyinstaller(work, ico, onefile=False)
        out = os.path.join(a.out, f"{NAME}-{ver}-x86_64.AppImage")
        appimage(dist, png, out, a.appimagetool)
    print(f"built {out} ({os.path.getsize(out) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
