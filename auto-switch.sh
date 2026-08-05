#!/usr/bin/env bash
# Saito's AutoSwitcher/Recorder for Input Remapper
# Auto profile switcher for Input Remapper.
# Watches for running processes and applies the matching Input Remapper preset.
#
# Usage: auto-switch.sh [--config FILE] {start|stop|status|restart|paths|run}
#
# Paths are resolved in this order (first hit wins):
#   --config FILE
#   $AUTOSWITCH_CONFIG
#   $AUTOSWITCH_CONFIG_DIR/config.txt
#   <directory containing this script>/config.txt   (the default — the folder is
#                                                    self-contained wherever it is)

set -uo pipefail

# --- locate this script, following symlinks (no readlink -f on some systems) ---
resolve_self() {
  local src="${BASH_SOURCE[0]}" dir
  while [[ -L "$src" ]]; do
    dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="${dir}/${src}"
  done
  cd -P "$(dirname "$src")" && pwd
}
SCRIPT_DIR="$(resolve_self)"

XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
XDG_STATE_HOME="${XDG_STATE_HOME:-${HOME}/.local/state}"
XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

APP_NAME="input-remapper-autoswitch"
DEFAULT_CONFIG_DIR="${XDG_CONFIG_HOME}/${APP_NAME}"

# --- argument parsing -------------------------------------------------------
CONFIG_FILE="${AUTOSWITCH_CONFIG:-}"
ACTION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="${2:-}"; shift 2 ;;
    --config=*) CONFIG_FILE="${1#*=}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) ACTION="$1"; shift ;;
  esac
done

if [[ -z "$CONFIG_FILE" ]]; then
  if [[ -n "${AUTOSWITCH_CONFIG_DIR:-}" ]]; then
    CONFIG_FILE="${AUTOSWITCH_CONFIG_DIR}/config.txt"
  else
    # Beside this script, so the folder is self-contained wherever it's put.
    # The installed copy passes --config explicitly, which takes precedence.
    CONFIG_FILE="${SCRIPT_DIR}/config.txt"
  fi
fi
CONFIG_DIR_RESOLVED="$(dirname "$CONFIG_FILE")"

# --- defaults, overridable from settings.conf next to config.txt ------------
CHECK_INTERVAL=5          # seconds between process checks
RELOAD_INTERVAL=60        # seconds between config reloads
ENABLE_NOTIFICATIONS=1
REMAPPER_CONFIG_DIR=""    # auto-detected below when empty

SETTINGS_FILE="${CONFIG_DIR_RESOLVED}/settings.conf"
if [[ -f "$SETTINGS_FILE" ]]; then
  # Parsed line by line rather than sourced: never execute the settings file.
  while IFS='=' read -r key value; do
    key="${key%%#*}"; key="${key// /}"
    value="${value%%$'\r'}"; value="${value#\"}"; value="${value%\"}"
    case "$key" in
      check_interval)             [[ "$value" =~ ^[0-9]+$ ]] && CHECK_INTERVAL="$value" ;;
      reload_interval)            [[ "$value" =~ ^[0-9]+$ ]] && RELOAD_INTERVAL="$value" ;;
      notifications)              [[ "$value" =~ ^[01]$ ]] && ENABLE_NOTIFICATIONS="$value" ;;
      input_remapper_config_dir)  [[ -n "$value" ]] && REMAPPER_CONFIG_DIR="$value" ;;
    esac
  done < "$SETTINGS_FILE"
fi

# Input Remapper 2.x uses input-remapper-2; 1.x uses input-remapper.
if [[ -n "${INPUT_REMAPPER_CONFIG_DIR:-}" ]]; then
  REMAPPER_CONFIG_DIR="${INPUT_REMAPPER_CONFIG_DIR}"
elif [[ -z "$REMAPPER_CONFIG_DIR" ]]; then
  for candidate in "${XDG_CONFIG_HOME}/input-remapper-2" "${XDG_CONFIG_HOME}/input-remapper"; do
    [[ -d "$candidate" ]] && { REMAPPER_CONFIG_DIR="$candidate"; break; }
  done
  REMAPPER_CONFIG_DIR="${REMAPPER_CONFIG_DIR:-${XDG_CONFIG_HOME}/input-remapper-2}"
fi

NOTIFY_APPNAME="Saito's AutoSwitcher/Recorder for Input Remapper"
STATE_DIR="${XDG_STATE_HOME}/${APP_NAME}"
PIDFILE="${STATE_DIR}/auto-switch.pid"
LOCKFILE="${XDG_RUNTIME_DIR}/${APP_NAME}.lock"

log(){ printf '[autoswitch] %s %s\n' "$(date +'%F %T')" "$*" >&2; }

notify() {
  [[ "${ENABLE_NOTIFICATIONS}" -eq 1 ]] || return 0
  command -v notify-send >/dev/null 2>&1 || return 0
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
  notify-send -a "${NOTIFY_APPNAME}" "$1" "$2" 2>/dev/null || true
}

ensure_state(){ mkdir -p "${STATE_DIR}"; }

is_running(){
  [[ -f "$PIDFILE" ]] || return 1
  local p; p="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ -n "${p:-}" ]] && kill -0 "$p" 2>/dev/null
}

# Input Remapper 2.x lets an active session talk to the daemon directly; older
# setups need root. Work out once which form actually succeeds.
REMAPPER_CMD=()
detect_privilege() {
  local control; control="$(command -v input-remapper-control 2>/dev/null)"
  if [[ -z "$control" ]]; then
    log "input-remapper-control not found in PATH — is Input Remapper installed?"
    return 1
  fi
  if "$control" --command hello >/dev/null 2>&1; then
    REMAPPER_CMD=("$control")
  elif [[ "$(id -u)" -eq 0 ]]; then
    REMAPPER_CMD=("$control")
  elif command -v sudo >/dev/null 2>&1 && sudo -n "$control" --command hello >/dev/null 2>&1; then
    REMAPPER_CMD=(sudo -n "$control")
    log "Using passwordless sudo for input-remapper-control."
  else
    REMAPPER_CMD=("$control")
    log "WARNING: could not reach the Input Remapper daemon unprivileged, and sudo"
    log "         needs a password. Profile switching will probably fail. Either add"
    log "         a NOPASSWD sudoers rule for input-remapper-control, or make sure"
    log "         input-remapper.service is running for this session."
  fi
  return 0
}

start(){
  # Under systemd, run in the foreground so the unit can supervise us.
  if [[ -n "${INVOCATION_ID-}" ]]; then
    exec "$0" --config "$CONFIG_FILE" run
  fi
  if is_running; then log "Already running (pid $(cat "$PIDFILE"))."; return 0; fi
  ensure_state
  "$0" --config "$CONFIG_FILE" run &
  echo $! > "$PIDFILE"
  disown 2>/dev/null || true
  log "Started (pid $(cat "$PIDFILE"))."
}

stop(){
  if ! is_running; then log "Not running."; rm -f "$PIDFILE"; return 0; fi
  local p; p="$(cat "$PIDFILE")"
  kill "$p" 2>/dev/null || true
  sleep 1
  kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null || true
  rm -f "$PIDFILE"
  log "Stopped."
}

status(){
  if is_running; then log "Running (pid $(cat "$PIDFILE"))."; else log "Not running."; fi
}

paths(){
  printf 'script dir:          %s\n' "$SCRIPT_DIR"
  printf 'config file:         %s%s\n' "$CONFIG_FILE" \
    "$([[ -f "$CONFIG_FILE" ]] || printf ' (missing)')"
  printf 'settings file:       %s%s\n' "$SETTINGS_FILE" \
    "$([[ -f "$SETTINGS_FILE" ]] || printf ' (not present, using defaults)')"
  printf 'input-remapper dir:  %s%s\n' "$REMAPPER_CONFIG_DIR" \
    "$([[ -d "$REMAPPER_CONFIG_DIR" ]] || printf ' (missing)')"
  printf 'state dir:           %s\n' "$STATE_DIR"
  printf 'check interval:      %ss\n' "$CHECK_INTERVAL"
  printf 'reload interval:     %ss\n' "$RELOAD_INTERVAL"
  printf 'notifications:       %s\n' "$ENABLE_NOTIFICATIONS"
}

# ---------------- worker ----------------
run_worker(){
  trap 'log "Exiting."; exit 0' INT TERM HUP
  local last_reload=0
  declare -A LAST_PRESET   # device -> preset currently applied
  declare -a CONFIG_LINES

  load_config(){
    CONFIG_LINES=()
    if [[ -f "$CONFIG_FILE" ]]; then
      while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        # trim without spawning sed
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        CONFIG_LINES+=("$line")
      done < "$CONFIG_FILE"
      log "Loaded ${#CONFIG_LINES[@]} mapping(s) from ${CONFIG_FILE}"
    else
      log "Config not found: ${CONFIG_FILE}"
    fi
    last_reload="$(date +%s)"
  }

  apply_profile(){
    local device="${1:-}" preset="${2:-}"
    [[ -z "$device" || -z "$preset" ]] && return 0
    # Remembered per device, so switching one doesn't re-apply the others.
    local key="${device//[^a-zA-Z0-9]/_}"
    local previous="${LAST_PRESET[$key]:-}"
    [[ "$preset" == "$previous" ]] && return 0

    log "Switching to preset '${preset}' on device '${device}'"
    if "${REMAPPER_CMD[@]}" --command start \
        --config-dir "${REMAPPER_CONFIG_DIR}" \
        --device "${device}" \
        --preset "${preset}" >/dev/null 2>&1; then
      LAST_PRESET[$key]="$preset"
      notify "Profile applied" "Device: ${device}
Preset: ${preset}"
    else
      log "Failed to apply preset '${preset}' for device '${device}'"
      notify "Profile switch failed" "Device: ${device}
Preset: ${preset}"
    fi
  }

  # Devices mentioned anywhere in the config, deduplicated.
  configured_devices(){
    local line proc device preset
    for line in "${CONFIG_LINES[@]}"; do
      IFS='|' read -r proc device preset <<< "$line"
      [[ -n "${device:-}" ]] && printf '%s\n' "$device"
    done | awk '!seen[$0]++'
  }

  # The preset one device should be on right now: the first running process
  # mapped to *that* device, else its DEFAULT. Each device is decided
  # independently, so a keypad and a mouse can be on different presets at once.
  choose_for_device(){
    local wanted="${1:-}" line proc device preset
    for line in "${CONFIG_LINES[@]}"; do
      IFS='|' read -r proc device preset <<< "$line"
      [[ -z "${proc:-}" || -z "${device:-}" || -z "${preset:-}" ]] && continue
      [[ "$device" != "$wanted" ]] && continue
      [[ "$proc" == "DEFAULT" ]] && continue
      if pgrep -fi -- "$proc" >/dev/null 2>&1; then
        printf '%s\n' "$preset"
        return 0
      fi
    done
    for line in "${CONFIG_LINES[@]}"; do
      IFS='|' read -r proc device preset <<< "$line"
      [[ -z "${proc:-}" || -z "${device:-}" || -z "${preset:-}" ]] && continue
      [[ "$device" == "$wanted" && "$proc" == "DEFAULT" ]] && {
        printf '%s\n' "$preset"; return 0; }
    done
    return 1
  }

  # Single instance, when flock is available (util-linux).
  if command -v flock >/dev/null 2>&1; then
    mkdir -p "$(dirname "$LOCKFILE")" 2>/dev/null || true
    exec 9>"$LOCKFILE"
    if ! flock -n 9; then log "Another instance is already running. Exiting."; exit 1; fi
  fi

  detect_privilege || exit 1
  load_config
  log "Watching processes every ${CHECK_INTERVAL}s; reloading config every ${RELOAD_INTERVAL}s"
  log "Config file: ${CONFIG_FILE}"
  log "Input Remapper config dir: ${REMAPPER_CONFIG_DIR}"

  while true; do
    local now; now="$(date +%s)"
    if (( now - last_reload >= RELOAD_INTERVAL )); then
      load_config
    fi

    local device preset
    while IFS= read -r device; do
      [[ -z "$device" ]] && continue
      preset="$(choose_for_device "$device" || true)"
      [[ -n "${preset:-}" ]] && apply_profile "$device" "$preset"
    done < <(configured_devices)

    sleep "$CHECK_INTERVAL"
  done
}

case "${ACTION:-}" in
  start)    start ;;
  stop)     stop ;;
  status)   status ;;
  restart)  stop; start ;;
  paths)    paths ;;
  run|__run) run_worker ;;   # __run kept for older service files
  *) echo "Usage: $0 [--config FILE] {start|stop|status|restart|paths|run}"; exit 1 ;;
esac
