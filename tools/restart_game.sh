#!/bin/sh
# Restart BeamNG.drive (Linux native, Steam app 284160) and load the BeamNG.Earth level, for unattended
# testing after a crash or a freeze. The MCP server must be enabled in the game (Options > Advanced;
# the setting persists). The companion server (server/earthd.py) is not touched.
#
#   tools/restart_game.sh            graceful stop (SIGTERM, 30 s), SIGKILL if frozen, start, load
#   tools/restart_game.sh --start    only start + load (the game is not running)
#
# The main game process is matched exactly by its executable path with the launch option, so this
# never matches the shell running it (a loose pkill pattern once killed its own shell).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
EXE="$HOME/.local/share/Steam/steamapps/common/BeamNG.drive/BinLinux/BeamNG.drive.x64"
APPID=284160

main_pid() {
    # the main thread renames itself ("===>>> BeamNGMa"), so match the command line in /proc
    for d in /proc/[0-9]*; do
        args=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
        case "$args" in
            "$EXE "*--type=*) ;;            # CEF helper processes
            "$EXE "*|"$EXE") echo "${d#/proc/}"; return 0 ;;
        esac
    done
    return 1
}

if [ "${1:-}" != "--start" ]; then
    pid=$(main_pid) || pid=""
    if [ -n "$pid" ]; then
        echo "stopping BeamNG (pid $pid)"
        kill -TERM "$pid"
        i=0
        while kill -0 "$pid" 2>/dev/null && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done
        if kill -0 "$pid" 2>/dev/null; then
            echo "not stopping (frozen?): SIGKILL"
            kill -KILL "$pid"
            sleep 3
        fi
    fi
fi

echo "starting BeamNG via Steam"
steam -applaunch "$APPID" >/dev/null 2>&1 &
i=0
until python3 "$HERE/mcp.py" call run_lua '{"code":"return 1"}' 2>/dev/null | grep -q '^1$'; do
    sleep 5; i=$((i + 5))
    if [ $i -ge 300 ]; then echo "the game's MCP server did not answer within 300 s"; exit 1; fi
done
echo "MCP up after ${i} s; loading the BeamNG.Earth level"
sleep 10    # let the main menu finish loading
python3 "$HERE/mcp.py" call load_level '{"name":"beamng_earth"}'
i=0
until python3 "$HERE/mcp.py" call run_lua '{"code":"return tostring(beamngEarth_core ~= nil and getPlayerVehicle(0) ~= nil)"}' 2>/dev/null | grep -q true; do
    sleep 5; i=$((i + 5))
    if [ $i -ge 300 ]; then echo "the level did not load within 300 s"; exit 1; fi
done
echo "level loaded after ${i} s"
