#!/bin/sh
# Uninstaller for Saito's AutoSwitcher/Recorder for Input Remapper.
#
#   ./uninstall.sh              remove the program, keep your config
#   ./uninstall.sh --purge      remove the config and settings too
#   ./uninstall.sh --prefix DIR if you installed somewhere non-default
#   ./uninstall.sh --config-dir D  ditto for the config
#
# Where things went is read back from the installed launcher, so normally
# neither option is needed.
#
# After an `install.sh --here`, this removes the service, launchers and menu
# entry but never the folder you are running from — delete that yourself.

set -eu

# Reopen in a terminal when launched from a file manager — see install.sh.
SELF="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")"
if [ -z "${AUTOSWITCH_IN_TERMINAL:-}" ] && [ ! -t 1 ] \
   && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  AUTOSWITCH_IN_TERMINAL=1
  export AUTOSWITCH_IN_TERMINAL
  for term in ${TERMINAL:-} konsole gnome-terminal ptyxis xfce4-terminal \
              mate-terminal tilix alacritty kitty foot x-terminal-emulator xterm; do
    command -v "$term" >/dev/null 2>&1 || continue
    case "$term" in
      gnome-terminal|ptyxis|tilix) exec "$term" -- /bin/sh "$SELF" "$@" ;;
      kitty|foot)                  exec "$term" /bin/sh "$SELF" "$@" ;;
      *)                           exec "$term" -e /bin/sh "$SELF" "$@" ;;
    esac
  done
fi
if [ -n "${AUTOSWITCH_IN_TERMINAL:-}" ]; then
  trap 'printf "\nPress Enter to close this window… "; read -r _ 2>/dev/null </dev/tty || true' EXIT
fi

APP_NAME="input-remapper-autoswitch"
XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}

BIN_DIR="$HOME/.local/bin"
PURGE=0

# Work out where it actually went by reading the wrapper the installer wrote —
# it holds both paths, so this is right whether the install was in place, into
# the XDG directories, or somewhere custom.
PREFIX=""
CONFIG_DIR=""
if [ -r "$BIN_DIR/$APP_NAME" ]; then
  PREFIX=$(sed -n 's|^exec "\(.*\)/auto-switch.sh".*|\1|p' "$BIN_DIR/$APP_NAME" | head -1)
  CONFIG_DIR=$(sed -n 's|.*--config "\(.*\)/config.txt".*|\1|p' "$BIN_DIR/$APP_NAME" | head -1)
fi
# Fall back to this folder, matching install.sh's default.
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -n "$PREFIX" ] || PREFIX="$SELF_DIR"
[ -n "$CONFIG_DIR" ] || CONFIG_DIR="$SELF_DIR"

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    --config-dir=*) CONFIG_DIR="${1#*=}"; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

if have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user disable --now "$APP_NAME.service" >/dev/null 2>&1 || true
  rm -f "$XDG_CONFIG_HOME/systemd/user/$APP_NAME.service"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  ok "service stopped and removed"
fi

# Catch a non-systemd install that's still running.
[ -x "$PREFIX/auto-switch.sh" ] && "$PREFIX/auto-switch.sh" stop >/dev/null 2>&1 || true

rm -f "$BIN_DIR/$APP_NAME" "$BIN_DIR/$APP_NAME-gui"
rm -f "$XDG_DATA_HOME/applications/$APP_NAME.desktop"
rm -f "$XDG_DATA_HOME/icons/hicolor/scalable/apps/$APP_NAME.svg"
rm -f "$XDG_CONFIG_HOME/autostart/$APP_NAME.desktop"
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/$APP_NAME"

# Never delete the directory this script is running from. With an in-place
# install that is the user's own folder, holding the source and quite possibly
# their config; removing it would be a nasty surprise.
if [ "$PREFIX" = "$SELF_DIR" ]; then
  ok "launchers, menu entry and service removed"
  printf '  \033[33m!\033[0m installed in place — %s was left alone; delete it yourself\n' "$PREFIX"
elif [ -d "$PREFIX" ]; then
  rm -rf "$PREFIX"
  ok "program files, launchers and menu entry removed"
else
  ok "launchers, menu entry and service removed"
fi

if [ "$PURGE" -eq 1 ]; then
  if [ "$CONFIG_DIR" = "$SELF_DIR" ]; then
    rm -f "$CONFIG_DIR/config.txt" "$CONFIG_DIR/config.txt.bak" \
          "$CONFIG_DIR/settings.conf"
    ok "config.txt and settings.conf removed from $CONFIG_DIR"
  else
    rm -rf "$CONFIG_DIR"
    ok "config removed ($CONFIG_DIR)"
  fi
else
  printf '  \033[33m!\033[0m config kept at %s (use --purge to remove)\n' "$CONFIG_DIR"
fi

printf '\nDone. Your Input Remapper presets were not touched.\n\n'
