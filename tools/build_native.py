#!/usr/bin/env python3
"""Build the world server's Rust kernels (native/earthfast) and put the module next to the server:
server/earthfast.abi3.so (Linux) or server/earthfast.pyd (Windows). One build works for every Python from 3.10
(the stable ABI). Needs Rust (https://rustup.rs). The server runs without it (server/fast.py: the Python code).

    python3 tools/build_native.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRATE = os.path.join(ROOT, "native", "earthfast")


def main():
    subprocess.run(["cargo", "build", "--release"], cwd=CRATE, check=True)
    rel = os.path.join(CRATE, "target", "release")
    if sys.platform == "win32":
        src, dst = os.path.join(rel, "earthfast.dll"), os.path.join(ROOT, "server", "earthfast.pyd")
    elif sys.platform == "darwin":
        src, dst = os.path.join(rel, "libearthfast.dylib"), os.path.join(ROOT, "server", "earthfast.abi3.so")
    else:
        src, dst = os.path.join(rel, "libearthfast.so"), os.path.join(ROOT, "server", "earthfast.abi3.so")
    shutil.copyfile(src, dst)
    print("built", dst)


if __name__ == "__main__":
    main()
