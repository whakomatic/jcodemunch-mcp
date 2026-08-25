#!/usr/bin/env bash
# Run ONE jcodemunch-mcp server, shared by every MCP client, over HTTP.
#
#   ./scripts/jcodemunch-proxy.sh start     launch in the background
#   ./scripts/jcodemunch-proxy.sh stop      terminate it
#   ./scripts/jcodemunch-proxy.sh restart   stop, then start
#   ./scripts/jcodemunch-proxy.sh status    is it listening, and what does it cost
#   ./scripts/jcodemunch-proxy.sh logs      tail the server log
#   ./scripts/jcodemunch-proxy.sh foreground  run attached, Ctrl-C to quit
#
# WHY THIS EXISTS
#
# A stdio MCP server is spawned per client, so N concurrent Claude sessions
# cost N jcodemunch processes. Measured on one Windows box: ~586 MB private
# per server, three sessions holding ~1,758 MB between them.
#
# This repo already DIAGNOSES that: storage/process_registry.sprawl_report,
# surfaced in get_session_stats["processes"], exists because a user found 25+
# live instances against one store holding ~17 GB (jcm#375 follow-up). The
# report tells you the sprawl is there; it cannot stop it, because with stdio
# the client decides how many servers exist. This script removes the cause.
#
# `mcp-proxy` enters stdio_client() on its AsyncExitStack once, at startup,
# before Starlette begins serving (see mcp_proxy/mcp_server.py). Every HTTP
# client routes through that one session, so the cost is paid ONCE no matter
# how many clients connect.
#
# WHAT IT COSTS YOU
#
# - The proxy must be running BEFORE a client starts; an HTTP-transport client
#   connects, it does not spawn. Nothing here starts on demand.
# - Per-process state becomes per-MACHINE state. get_session_stats reports "the
#   current MCP session"; under the proxy every client shares one, so session
#   figures aggregate across clients. All-time figures are unaffected -- they
#   were already per-store, not per-process.
# - sprawl_report becomes uninformative by construction: it will report one
#   process, because that is now the design rather than a finding.
# - Clients see the wrong server version: mcp_proxy/proxy_server.py builds
#   Server(name=...) with no version argument, so the handshake reports the mcp
#   library default rather than this package's __version__.
# - Head-of-line blocking is a RISK, not a measurement. It is much smaller here
#   than in jdocmunch-mcp: server.py makes ~90 to_thread/run_in_executor calls
#   (jdocmunch-mcp makes 1), so most tool bodies already leave the event loop.
#
# CONFIGURATION, highest precedence first: environment, then the optional
# ~/.jcodemunch-proxy/config.json, then the defaults below.
#
#   JCODEMUNCH_PROXY_PORT     default 8097
#   JCODEMUNCH_PROXY_HOST     default 127.0.0.1
#   JCODEMUNCH_PROXY_CMD      default: jcodemunch-mcp on PATH
#   JCODEMUNCH_PROXY_HOME     default ~/.jcodemunch-proxy (config + log live here)
#
# Point clients at http://HOST:PORT/mcp (streamable HTTP) or /sse (legacy SSE).
# For Claude Code that is an mcpServers entry of
# {"type": "http", "url": "http://127.0.0.1:8097/mcp"}.
#
# REQUIREMENTS: mcp-proxy on PATH. Install with
#   uv tool install mcp-proxy --with "mcp<2"
# The `mcp<2` pin is load-bearing: mcp-proxy 0.12.0 imports request_ctx from
# mcp.server.lowlevel.server, which mcp 2.x removed.

set -euo pipefail

PROXY_HOME="${JCODEMUNCH_PROXY_HOME:-$HOME/.jcodemunch-proxy}"
CONFIG="$PROXY_HOME/config.json"
LOG="$PROXY_HOME/proxy.log"
PIDFILE="$PROXY_HOME/proxy.pid"

# --- configuration -------------------------------------------------------- #

# Read one key out of the optional config.json. Absent file, absent key or no
# python all resolve to empty, and the caller falls back to its default.
config_get() {
    [ -f "$CONFIG" ] || return 0
    command -v python >/dev/null 2>&1 || return 0
    python - "$CONFIG" "$1" <<'PY' 2>/dev/null || true
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
val = cfg.get(sys.argv[2])
# `args` is a JSON list in an MCP client config; flatten it to a word list so
# the caller can split it. Printing a list's repr would pass "['serve']".
if isinstance(val, (list, tuple)):
    val = " ".join(str(v) for v in val)
if val not in (None, ""):
    print(val)
PY
}

PORT="${JCODEMUNCH_PROXY_PORT:-$(config_get port)}"
PORT="${PORT:-8097}"
HOST="${JCODEMUNCH_PROXY_HOST:-$(config_get host)}"
HOST="${HOST:-127.0.0.1}"
SERVER_CMD="${JCODEMUNCH_PROXY_CMD:-$(config_get command)}"
SERVER_CMD="${SERVER_CMD:-jcodemunch-mcp}"
# jcodemunch-mcp needs its `serve` subcommand; jdocmunch-mcp takes none.
SERVER_ARGS="${JCODEMUNCH_PROXY_ARGS:-$(config_get args)}"
SERVER_ARGS="${SERVER_ARGS:-serve}"

URL="http://$HOST:$PORT/mcp"

# --- helpers -------------------------------------------------------------- #

die() { printf '%s\n' "$*" >&2; exit 1; }

require_proxy() {
    command -v mcp-proxy >/dev/null 2>&1 || die \
        "mcp-proxy is not on PATH. Install it with:
    uv tool install mcp-proxy --with \"mcp<2\"
The mcp<2 pin matters: 0.12.0 does not import against mcp 2.x."
}

# An MCP initialize is the only honest liveness check. A TCP connect proves
# something holds the port; it does not prove the stdio child came up, and the
# child is the part that can fail while uvicorn happily serves 404s.
is_up() {
    command -v curl >/dev/null 2>&1 || return 1
    curl -sf -o /dev/null --max-time 5 -X POST "$URL" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"jcodemunch-proxy.sh","version":"0"}}}' \
        2>/dev/null
}

wait_until_up() {
    local deadline=$((SECONDS + ${1:-60}))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if is_up; then return 0; fi
        sleep 0.25
    done
    return 1
}

# Export env from config.json into this shell, so the child inherits it via
# --pass-environment. Values live in the config file rather than on a command
# line, which keeps any token out of the process table.
export_config_env() {
    [ -f "$CONFIG" ] || return 0
    command -v python >/dev/null 2>&1 || return 0
    local exports
    exports="$(python - "$CONFIG" <<'PY' 2>/dev/null || true
import json, shlex, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
for k, v in (cfg.get("env") or {}).items():
    if k.replace("_", "").isalnum():
        print(f"export {k}={shlex.quote(str(v))}")
PY
)"
    [ -n "$exports" ] && eval "$exports"
    return 0
}

proxy_args() {
    # mcp-proxy serves BOTH /mcp and /sse from one --port; there is no
    # transport flag to choose between them on the server side.
    printf '%s\n' --port "$PORT" --host "$HOST" --pass-environment "$SERVER_CMD"
    # jcodemunch-mcp needs its `serve` subcommand. Unquoted on purpose: each
    # word becomes its own argv entry, which is what mcp-proxy expects.
    # shellcheck disable=SC2086
    [ -n "$SERVER_ARGS" ] && printf '%s\n' $SERVER_ARGS
    return 0
}

# --- commands ------------------------------------------------------------- #

cmd_start() {
    require_proxy
    if is_up; then
        echo "already running: $URL"
        return 0
    fi
    mkdir -p "$PROXY_HOME"
    export_config_env

    local args=(); while IFS= read -r a; do args+=("$a"); done < <(proxy_args)
    echo "starting: mcp-proxy ${args[*]}"

    # setsid where available so the proxy outlives this shell; nohup otherwise.
    if command -v setsid >/dev/null 2>&1; then
        setsid mcp-proxy "${args[@]}" >"$LOG" 2>&1 < /dev/null &
    else
        nohup mcp-proxy "${args[@]}" >"$LOG" 2>&1 < /dev/null &
    fi
    echo $! > "$PIDFILE"

    if wait_until_up 90; then
        echo "up: $URL"
        echo "log: $LOG"
    else
        echo "did NOT come up within 90s. Last log lines:" >&2
        tail -n 20 "$LOG" >&2 2>/dev/null || true
        exit 1
    fi
}

cmd_foreground() {
    require_proxy
    is_up && die "already running at $URL -- stop it first"
    mkdir -p "$PROXY_HOME"
    export_config_env
    local args=(); while IFS= read -r a; do args+=("$a"); done < <(proxy_args)
    exec mcp-proxy "${args[@]}"
}

cmd_stop() {
    local stopped=0

    if [ -f "$PIDFILE" ]; then
        local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
        if [ -n "$pid" ]; then
            # Windows: taskkill /T is what actually reaps the stdio child and
            # the embedding worker. A bare kill leaves both orphaned holding
            # ~2 GB, which is the entire problem this script exists to solve.
            if command -v taskkill >/dev/null 2>&1; then
                taskkill //PID "$pid" //T //F >/dev/null 2>&1 && stopped=1 || true
            fi
            kill "$pid" >/dev/null 2>&1 && stopped=1 || true
        fi
        rm -f "$PIDFILE"
    fi

    # Fall back to matching the command line: the pidfile is gone after a
    # reboot, and a scheduled task or service never wrote one.
    if [ "$stopped" -eq 0 ] && command -v powershell >/dev/null 2>&1; then
        # ⚠ Same self-match hazard as in status, and here it is dangerous: this
        # PowerShell's own command line contains the pattern, so without the
        # Name filter the query can select itself and taskkill /T its own tree.
        powershell -NoProfile -Command "
            \$p = Get-CimInstance Win32_Process |
                  Where-Object { \$_.Name -match '^(python|pythonw|mcp-proxy)' -and
                                 \$_.CommandLine -match 'mcp-proxy.*--port $PORT' } |
                  Select-Object -First 1
            if (\$p) { taskkill /PID \$p.ProcessId /T /F | Out-Null; 'stopped' }
        " 2>/dev/null | grep -q stopped && stopped=1 || true
    fi
    if [ "$stopped" -eq 0 ] && command -v pkill >/dev/null 2>&1; then
        pkill -f "mcp-proxy.*--port $PORT" >/dev/null 2>&1 && stopped=1 || true
    fi

    if [ "$stopped" -eq 0 ]; then
        echo "was not running"
        return 0
    fi

    # ⚠ taskkill and kill are both ASYNCHRONOUS: they return before the socket
    # closes. Reporting "stopped" at that point is a lie the next command acts
    # on -- `restart` is stop-then-start, and start bails with "already
    # running" if the dying proxy still answers. Wait for the port to go quiet.
    local deadline=$((SECONDS + 30))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if ! is_up; then echo "stopped"; return 0; fi
        sleep 0.25
    done
    echo "kill was issued but $URL still answers after 30s" >&2
    return 1
}

cmd_status() {
    if is_up; then
        echo "UP    $URL"
    else
        echo "DOWN  $URL"
    fi
    echo "conf  $CONFIG$([ -f "$CONFIG" ] || echo ' (absent, using defaults)')"
    echo "log   $LOG"

    # Memory is the reason this exists, so status reports it where it can.
    if command -v powershell >/dev/null 2>&1; then
        # ⚠ The Name filter is load-bearing, not tidiness. This PowerShell
        # process's OWN command line contains the pattern below, so matching on
        # CommandLine alone makes the query find itself: it reports a running
        # proxy when there is none, and inflates the tree when there is one.
        powershell -NoProfile -Command "
            \$all  = Get-CimInstance Win32_Process |
                     Where-Object { \$_.Name -match '^(python|pythonw|mcp-proxy|jcodemunch)' -or \$_.Name -eq 'conhost.exe' }
            \$root = \$all | Where-Object { \$_.CommandLine -match 'mcp-proxy.*--port $PORT' } |
                     Select-Object -First 1
            if (-not \$root) { 'proc  (no proxy process found)'; exit }
            \$desc = @(); \$q = @(\$root.ProcessId)
            while (\$q) {
                \$c = \$q[0]; \$q = @(\$q | Select-Object -Skip 1)
                foreach (\$k in \$all | Where-Object { \$_.ParentProcessId -eq \$c }) {
                    \$desc += \$k; \$q += \$k.ProcessId
                }
            }
            \$tree = @(\$root) + \$desc
            \$mb = [math]::Round((\$tree | Measure-Object PrivatePageCount -Sum).Sum / 1MB, 1)
            'proc  ' + \$tree.Count + ' processes, ' + \$mb + ' MB private (shared by all clients)'
        " 2>/dev/null || true
    fi
}

cmd_logs() {
    [ -f "$LOG" ] || die "no log at $LOG"
    tail -n "${2:-40}" -f "$LOG"
}

case "${1:-status}" in
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart)     cmd_stop; cmd_start ;;
    status)      cmd_status ;;
    logs)        cmd_logs "$@" ;;
    foreground)  cmd_foreground ;;
    -h|--help|help)
        sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)
        die "unknown command: $1 (try: start stop restart status logs foreground)"
        ;;
esac
