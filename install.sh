#!/bin/sh
# Installer for Saito's AutoSwitcher/Recorder for Input Remapper.
#
# POSIX sh on purpose: it has to run before we know anything about the machine.
# Everything is per-user by default — no root, no distro packages, no writes
# outside $HOME.
#
#   ./install.sh                 set up from this folder: nothing is copied and
#                                config.txt is created here, so the whole thing
#                                stays self-contained and can be moved
#   ./install.sh --prefix DIR    copy the program into DIR and run it from there;
#                                the config lives in DIR too. Everything the
#                                program needs is always in one folder.
#   ./install.sh --no-service    skip the systemd user unit
#   ./install.sh --no-desktop    skip the application menu entry
#   ./install.sh --no-start      install but don't enable/start anything
#   ./install.sh --no-install-deps  never offer to install missing packages
#   ./install.sh --yes           don't ask for confirmation
#   ./install.sh --check         only report what's missing, change nothing

set -eu

# --- reopen in a terminal when launched from a file manager ------------------
# Dolphin (and most file managers) run scripts with no terminal attached, so the
# dependency report and the confirmation prompt would go nowhere and the user
# would see nothing happen at all. Re-exec inside a terminal emulator once.
SELF="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")"

if [ -z "${AUTOSWITCH_IN_TERMINAL:-}" ] && [ ! -t 1 ] \
   && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  AUTOSWITCH_IN_TERMINAL=1
  export AUTOSWITCH_IN_TERMINAL
  # $TERMINAL first if the user set one, then the desktop's usual choice.
  for term in ${TERMINAL:-} konsole gnome-terminal ptyxis xfce4-terminal \
              mate-terminal tilix alacritty kitty foot x-terminal-emulator xterm; do
    command -v "$term" >/dev/null 2>&1 || continue
    case "$term" in
      gnome-terminal|ptyxis|tilix) exec "$term" -- /bin/sh "$SELF" "$@" ;;
      kitty|foot)                  exec "$term" /bin/sh "$SELF" "$@" ;;
      *)                           exec "$term" -e /bin/sh "$SELF" "$@" ;;
    esac
  done
  # Nothing to reopen in — carry on without a prompt rather than doing nothing.
  echo "No terminal emulator found; continuing without prompting." >&2
fi

# Keep the window up at the end so the result is readable, however we exit.
if [ -n "${AUTOSWITCH_IN_TERMINAL:-}" ]; then
  trap 'printf "\nPress Enter to close this window… "; read -r _ 2>/dev/null </dev/tty || true' EXIT
fi

APP_NAME="input-remapper-autoswitch"
DISPLAY_NAME="Saito's AutoSwitcher/Recorder for Input Remapper"
SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}

PREFIX=""            # resolved below: an existing install, --prefix, or here
CONFIG_DIR=""        # always the same as PREFIX
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$XDG_CONFIG_HOME/systemd/user"
DESKTOP_DIR="$XDG_DATA_HOME/applications"
ICON_DIR="$XDG_DATA_HOME/icons/hicolor/scalable/apps"
AUTOSTART_DIR="$XDG_CONFIG_HOME/autostart"

WANT_SERVICE=1; WANT_DESKTOP=1; WANT_START=1; CHECK_ONLY=0
# In place by default: the folder stays self-contained wherever the user put it,
# and both halves read the config.txt sitting beside them.
ASSUME_YES=0; PREFIX_SET=0; WANT_DEPS=1; FOLLOWED_EXISTING=0

while [ $# -gt 0 ]; do
  case "$1" in
    --here|--in-place) PREFIX="$SRC_DIR"; PREFIX_SET=1; shift ;;
    --no-install-deps) WANT_DEPS=0; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --prefix) PREFIX="$2"; PREFIX_SET=1; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; PREFIX_SET=1; shift ;;
    --no-service) WANT_SERVICE=0; shift ;;
    --no-desktop) WANT_DESKTOP=0; shift ;;
    --no-start) WANT_START=0; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^#\{0,1\} \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# An existing install wins over the default: re-running this should update what
# is already there, never quietly relocate it and orphan the config.
if [ "$PREFIX_SET" -eq 0 ] && [ -r "$BIN_DIR/$APP_NAME" ]; then
  OLD_PREFIX=$(sed -n 's|^exec "\(.*\)/auto-switch.sh".*|\1|p' "$BIN_DIR/$APP_NAME" | head -1)
  if [ -n "$OLD_PREFIX" ] && [ -d "$OLD_PREFIX" ]; then
    PREFIX="$OLD_PREFIX"
    FOLLOWED_EXISTING=1
  fi
fi

# The program and its config are always in the same folder — the script looks
# for config.txt beside itself and nowhere else, so splitting them would leave
# it reading a file nobody edits.
[ -n "${PREFIX:-}" ] || PREFIX="$SRC_DIR"
CONFIG_DIR="$PREFIX"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- dependencies
# Identify the distro family from os-release (authoritative), then confirm with
# the package manager that is actually present. Probing binaries alone is
# unreliable — plenty of systems carry a stray one.
distro_id() {
  [ -r /etc/os-release ] || { echo unknown; return; }
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "${ID_LIKE:-${ID:-unknown}}" | cut -d' ' -f1
}

detect_pm() {
  case "$(distro_id)" in
    arch*|manjaro*|cachyos*|endeavouros*) have pacman && { echo pacman; return; } ;;
    debian*|ubuntu*)   have apt-get && { echo apt; return; } ;;
    fedora*|rhel*|centos*) have dnf && { echo dnf; return; }; have yum && { echo yum; return; } ;;
    suse*|opensuse*)   have zypper && { echo zypper; return; } ;;
    alpine*)           have apk && { echo apk; return; } ;;
    void*)             have xbps-install && { echo xbps; return; } ;;
    gentoo*)           have emerge && { echo emerge; return; } ;;
    solus*)            have eopkg && { echo eopkg; return; } ;;
  esac
  # Unknown or mismatched distro: fall back to whatever is installed.
  for probe in pacman apt-get dnf yum zypper apk xbps-install emerge eopkg; do
    if have "$probe"; then
      case "$probe" in
        apt-get) echo apt ;; xbps-install) echo xbps ;; *) echo "$probe" ;;
      esac
      return
    fi
  done
  echo unknown
}

PM="$(detect_pm)"

# Package names per manager. Verified on Arch; the rest are best-known names —
# the exact command is always shown before it runs, so a wrong guess is visible
# and correctable rather than silent.
pkg_name() {
  case "$PM:$1" in
    pacman:pyqt6)      echo python-pyqt6 ;;
    pacman:evdev)      echo python-evdev ;;
    pacman:remapper)   echo "" ;;              # AUR only — handled separately
    pacman:procps)     echo procps-ng ;;
    pacman:libnotify)  echo libnotify ;;
    pacman:python)     echo python ;;

    apt:pyqt6)         echo python3-pyqt6 ;;
    apt:evdev)         echo python3-evdev ;;
    apt:remapper)      echo input-remapper ;;
    apt:procps)        echo procps ;;
    apt:libnotify)     echo libnotify-bin ;;
    apt:python)        echo python3 ;;

    dnf:pyqt6|yum:pyqt6)         echo python3-pyqt6 ;;
    dnf:evdev|yum:evdev)         echo python3-evdev ;;
    dnf:remapper|yum:remapper)   echo input-remapper ;;
    dnf:procps|yum:procps)       echo procps-ng ;;
    dnf:libnotify|yum:libnotify) echo libnotify ;;
    dnf:python|yum:python)       echo python3 ;;

    zypper:pyqt6)      echo python3-qt6 ;;
    zypper:evdev)      echo python3-evdev ;;
    zypper:remapper)   echo input-remapper ;;
    zypper:procps)     echo procps ;;
    zypper:libnotify)  echo libnotify-tools ;;
    zypper:python)     echo python3 ;;

    apk:pyqt6)         echo py3-qt6 ;;
    apk:evdev)         echo py3-evdev ;;
    apk:procps)        echo procps-ng ;;
    apk:libnotify)     echo libnotify ;;
    apk:python)        echo python3 ;;

    xbps:pyqt6)        echo python3-PyQt6 ;;
    xbps:evdev)        echo python3-evdev ;;
    xbps:procps)       echo procps-ng ;;
    xbps:libnotify)    echo libnotify ;;
    xbps:python)       echo python3 ;;

    emerge:pyqt6)      echo dev-python/PyQt6 ;;
    emerge:evdev)      echo dev-python/python-evdev ;;
    emerge:procps)     echo sys-process/procps ;;
    emerge:libnotify)  echo x11-libs/libnotify ;;
    emerge:python)     echo dev-lang/python ;;

    eopkg:pyqt6)       echo python3-pyqt6 ;;
    eopkg:evdev)       echo python3-evdev ;;
    eopkg:procps)      echo procps-ng ;;
    eopkg:libnotify)   echo libnotify ;;
    eopkg:python)      echo python3 ;;

    *) echo "" ;;
  esac
}

install_command() {   # $* = package names
  case "$PM" in
    pacman) echo "pacman -S --needed $*" ;;
    apt)    echo "apt-get install -y $*" ;;
    dnf)    echo "dnf install -y $*" ;;
    yum)    echo "yum install -y $*" ;;
    zypper) echo "zypper --non-interactive install $*" ;;
    apk)    echo "apk add $*" ;;
    xbps)   echo "xbps-install -Sy $*" ;;
    emerge) echo "emerge --noreplace $*" ;;
    eopkg)  echo "eopkg install -y $*" ;;
    *)      echo "" ;;
  esac
}

# How to become root, if we aren't already.
ROOT_PREFIX=""
if [ "$(id -u)" -eq 0 ]; then
  ROOT_PREFIX=""
elif have sudo;   then ROOT_PREFIX="sudo"
elif have doas;   then ROOT_PREFIX="doas"
elif have run0;   then ROOT_PREFIX="run0"
elif have pkexec; then ROOT_PREFIX="pkexec"
fi

pkg_hint() {
  name="$(pkg_name "$1")"
  if [ -z "$name" ]; then
    case "$1" in
      remapper) echo "install Input Remapper: https://github.com/sezanzeb/input-remapper" ;;
      pyqt6)    echo "install PyQt6 for python3 (pip install --user PyQt6)" ;;
      evdev)    echo "install python-evdev (pip install --user evdev)" ;;
      *)        echo "install $1" ;;
    esac
    return
  fi
  echo "${ROOT_PREFIX:+$ROOT_PREFIX }$(install_command "$name")"
}

MISSING_REQUIRED=0
say ""
say "Checking dependencies"

NEED=""        # components we could install for them
need_add() { NEED="$NEED $1"; }

if have bash; then ok "bash"; else bad "bash is required"; MISSING_REQUIRED=1; fi
if have pgrep; then ok "pgrep"; else bad "pgrep is required"; need_add procps; MISSING_REQUIRED=1; fi

if have input-remapper-control; then
  ok "input-remapper-control"
else
  bad "input-remapper-control not found"
  need_add remapper
  MISSING_REQUIRED=1
fi

if have python3; then
  if python3 -c "import PyQt6.QtWidgets" 2>/dev/null; then
    ok "python3 + PyQt6 (GUI available)"
  else
    warn "PyQt6 missing — the GUI won't start, the switcher still will"
    need_add pyqt6
  fi
  if python3 -c "import evdev" 2>/dev/null; then
    ok "python-evdev (recording available)"
  else
    warn "python-evdev missing — the GUI can't record keys or detect devices"
    need_add evdev
  fi
else
  warn "python3 missing — the GUI won't start, the switcher still will"
  need_add python
fi

have flock       && ok "flock" || warn "flock missing — single-instance locking disabled"
if have notify-send; then ok "notify-send"; else
  warn "notify-send missing — desktop notifications off"
  need_add libnotify
fi

HAVE_SYSTEMD=0
if have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
  HAVE_SYSTEMD=1; ok "systemd user session"
else
  warn "no systemd user session — will install an XDG autostart entry instead"
fi

# Input Remapper's own daemon: reachable unprivileged, or does it need root?
NEEDS_ROOT=0
if have input-remapper-control; then
  if input-remapper-control --command hello >/dev/null 2>&1; then
    ok "Input Remapper daemon reachable without root"
  elif have sudo && sudo -n input-remapper-control --command hello >/dev/null 2>&1; then
    ok "Input Remapper daemon reachable via passwordless sudo"
  else
    NEEDS_ROOT=1
    warn "can't reach the Input Remapper daemon"
    warn "  start it first:  sudo systemctl enable --now input-remapper.service"
    warn "  if it still fails, the switcher needs a NOPASSWD sudoers rule:"
    warn "  echo \"\$USER ALL=(ALL) NOPASSWD: \$(command -v input-remapper-control)\" |"
    warn "    sudo tee /etc/sudoers.d/input-remapper-autoswitch"
  fi
fi

# ---------------------------------------------------------- install missing
if [ -n "$NEED" ] && [ "$CHECK_ONLY" -eq 0 ] && [ "$WANT_DEPS" -eq 1 ]; then
  PKGS=""; UNAVAILABLE=""
  for component in $NEED; do
    name="$(pkg_name "$component")"
    if [ -n "$name" ]; then PKGS="$PKGS $name"; else UNAVAILABLE="$UNAVAILABLE $component"; fi
  done
  PKGS="${PKGS# }"; UNAVAILABLE="${UNAVAILABLE# }"

  if [ -n "$PKGS" ] && [ -n "$(install_command $PKGS)" ]; then
    say ""
    say "Missing packages can be installed for you with:"
    say "    ${ROOT_PREFIX:+$ROOT_PREFIX }$(install_command $PKGS)"
    if [ -z "$ROOT_PREFIX" ] && [ "$(id -u)" -ne 0 ]; then
      warn "no sudo/doas/run0/pkexec found — run that command as root yourself"
    else
      do_it=1
      if [ "$ASSUME_YES" -eq 0 ]; then
        printf 'Install them now? [Y/n] '
        if [ -r /dev/tty ] && [ -c /dev/tty ] && read -r a 2>/dev/null </dev/tty; then :
        elif [ -t 0 ]; then read -r a || a=""; else a=""; fi
        case "$a" in [Nn]*) do_it=0 ;; esac
      fi
      if [ "$do_it" -eq 1 ]; then
        say ""
        # apt needs its lists refreshed or install can fail on a fresh system.
        [ "$PM" = apt ] && ${ROOT_PREFIX:+$ROOT_PREFIX }apt-get update || true
        if ${ROOT_PREFIX:+$ROOT_PREFIX }$(install_command $PKGS); then
          ok "packages installed"
        else
          warn "the package install did not succeed — carrying on"
          warn "you may need to install these by hand: $PKGS"
        fi
        say ""
        say "Re-checking"
        MISSING_REQUIRED=0
        have pgrep || { bad "pgrep still missing"; MISSING_REQUIRED=1; }
        if have input-remapper-control; then ok "input-remapper-control"
        else bad "input-remapper-control still missing"; MISSING_REQUIRED=1; fi
        python3 -c "import PyQt6.QtWidgets" 2>/dev/null && ok "PyQt6" || \
          warn "PyQt6 still missing — the GUI won't start"
        python3 -c "import evdev" 2>/dev/null && ok "python-evdev" || \
          warn "python-evdev still missing — recording unavailable"
      fi
    fi
  fi

  # Input Remapper isn't in the official repos everywhere.
  case " $UNAVAILABLE " in
    *" remapper "*)
      say ""
      if [ "$PM" = pacman ]; then
        AUR_HELPER=""
        for helper in yay paru pikaur trizen; do
          have "$helper" && { AUR_HELPER="$helper"; break; }
        done
        if [ -n "$AUR_HELPER" ]; then
          say "Input Remapper is in the AUR, not the official Arch repos."
          say "It can be built and installed with:"
          say "    $AUR_HELPER -S input-remapper"
          do_aur=1
          if [ "$ASSUME_YES" -eq 0 ]; then
            printf 'Do that now? [Y/n] '
            if [ -r /dev/tty ] && [ -c /dev/tty ] && read -r a 2>/dev/null </dev/tty; then :
            elif [ -t 0 ]; then read -r a || a=""; else a=""; fi
            case "$a" in [Nn]*) do_aur=0 ;; esac
          fi
          if [ "$do_aur" -eq 1 ]; then
            say ""
            # AUR helpers must not run as root; they call sudo themselves.
            if "$AUR_HELPER" -S --needed input-remapper; then
              have input-remapper-control && { ok "Input Remapper installed"; MISSING_REQUIRED=0; } \
                || warn "the build finished but input-remapper-control still isn't on PATH"
            else
              warn "the AUR build did not succeed — install it by hand:"
              warn "  $AUR_HELPER -S input-remapper"
            fi
          fi
        else
          warn "Input Remapper is in the AUR, not the official Arch repos."
          warn "  install an AUR helper (yay or paru) and run:  yay -S input-remapper"
          warn "  or build it by hand from https://aur.archlinux.org/packages/input-remapper"
        fi
      else
        warn "Input Remapper isn't packaged for this distro as far as this"
        warn "installer knows — install it from"
        warn "  https://github.com/sezanzeb/input-remapper#installation"
      fi ;;
  esac
fi

if [ "$MISSING_REQUIRED" -eq 1 ]; then
  say ""
  say "Required dependencies are still missing."
  say "Install them and run this again — or use --no-install-deps to skip this step."
  exit 1
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  say ""
  say "Check only; nothing was installed."
  exit 0
fi

# ---------------------------------------------------------------- confirm
say ""
say "This will set up $DISPLAY_NAME as follows:"
say ""
if [ "$PREFIX" = "$SRC_DIR" ]; then
  say "  Program files   $PREFIX"
  say "                  (run from here — nothing is copied)"
else
  say "  Program files   $PREFIX"
  say "                  (copied from $SRC_DIR)"
fi
say "  Config          $CONFIG_DIR/config.txt"
if [ "$FOLLOWED_EXISTING" -eq 1 ]; then
  say ""
  say "  (matching the install already set up — --here or --prefix moves it)"
fi
if [ "$WANT_SERVICE" -eq 1 ] && [ "$HAVE_SYSTEMD" -eq 1 ]; then
  say "  Service         $UNIT_DIR/$APP_NAME.service"
  [ "$WANT_START" -eq 1 ] && say "                  (enabled and started now)"
elif [ "$WANT_SERVICE" -eq 1 ]; then
  say "  Autostart       $AUTOSTART_DIR/$APP_NAME.desktop"
fi
[ "$WANT_DESKTOP" -eq 1 ] && say "  Menu entry      $DESKTOP_DIR/$APP_NAME.desktop"
say "  Commands        $BIN_DIR/$APP_NAME, $BIN_DIR/$APP_NAME-gui"
say ""
say "Nothing outside your home directory is touched, and no root is needed."
if [ "$PREFIX" != "$SRC_DIR" ]; then
  say "Tip: --here installs into this folder instead, so you can keep the whole"
  say "     thing wherever you like and move it later."
fi

if [ "$ASSUME_YES" -eq 0 ]; then
  say ""
  printf 'Continue? [Y/n] '
  # Prefer the terminal so `curl … | sh` doesn't swallow the script as input;
  # fall back to stdin, and only assume yes when there is no way to ask.
  if [ -r /dev/tty ] && [ -c /dev/tty ] && read -r answer 2>/dev/null </dev/tty; then
    :
  elif [ -t 0 ]; then
    read -r answer || answer=""
  else
    answer=""
    say "(no terminal to ask on — continuing; use --yes to silence this)"
  fi
  case "$answer" in
    [Nn]*) say ""; say "Nothing was changed."; exit 0 ;;
  esac
fi

# ---------------------------------------------------------------- install
say ""
say "Installing"

mkdir -p "$PREFIX" "$CONFIG_DIR" "$BIN_DIR"

for f in auto-switch.sh autoswitch-gui.py; do
  [ -f "$SRC_DIR/$f" ] || { bad "missing source file: $f"; exit 1; }
  [ "$SRC_DIR" = "$PREFIX" ] || cp -f "$SRC_DIR/$f" "$PREFIX/$f"
  chmod +x "$PREFIX/$f"
done
if [ "$SRC_DIR" = "$PREFIX" ]; then
  ok "running in place from $PREFIX"
else
  # Take the whole lot so the destination is self-contained and can update
  # or uninstall itself without needing the folder it came from.
  for f in install.sh uninstall.sh config.example.txt README.md LICENSE; do
    [ -f "$SRC_DIR/$f" ] && cp -f "$SRC_DIR/$f" "$PREFIX/$f"
  done
  [ -d "$SRC_DIR/icons" ] && { mkdir -p "$PREFIX/icons"; cp -f "$SRC_DIR/icons/"* "$PREFIX/icons/" 2>/dev/null; }
  chmod +x "$PREFIX/install.sh" "$PREFIX/uninstall.sh" 2>/dev/null || true
  ok "program files -> $PREFIX"
fi

# Config: never clobber an existing one; migrate a legacy in-place config.
if [ -f "$CONFIG_DIR/config.txt" ]; then
  ok "kept existing config -> $CONFIG_DIR/config.txt"
elif [ -f "$SRC_DIR/config.txt" ]; then
  cp "$SRC_DIR/config.txt" "$CONFIG_DIR/config.txt"
  ok "migrated config -> $CONFIG_DIR/config.txt"
elif [ -f "$PREFIX/config.example.txt" ]; then
  cp "$PREFIX/config.example.txt" "$CONFIG_DIR/config.txt"
  ok "starter config from the example -> $CONFIG_DIR/config.txt"
else
  cat > "$CONFIG_DIR/config.txt" <<'EOF'
# process_name_or_unique_fragment | device name | preset name (without .json)
# Checked top to bottom; the first running process wins.
# DEFAULT applies when nothing else matches.
EOF
  ok "created starter config -> $CONFIG_DIR/config.txt"
fi

if [ ! -f "$CONFIG_DIR/settings.conf" ]; then
  cat > "$CONFIG_DIR/settings.conf" <<'EOF'
# Optional settings. Delete a line to use its default.
check_interval=5
reload_interval=60
notifications=1
# Leave empty to auto-detect ~/.config/input-remapper-2 or ~/.config/input-remapper
input_remapper_config_dir=
EOF
  ok "created settings -> $CONFIG_DIR/settings.conf"
fi

# Wrappers so both halves are on PATH and always see the same config.
cat > "$BIN_DIR/$APP_NAME" <<EOF
#!/bin/sh
exec "$PREFIX/auto-switch.sh" --config "$CONFIG_DIR/config.txt" "\$@"
EOF
chmod +x "$BIN_DIR/$APP_NAME"

cat > "$BIN_DIR/$APP_NAME-gui" <<EOF
#!/bin/sh
exec python3 "$PREFIX/autoswitch-gui.py" --config "$CONFIG_DIR/config.txt" "\$@"
EOF
chmod +x "$BIN_DIR/$APP_NAME-gui"
ok "commands -> $BIN_DIR/$APP_NAME, $BIN_DIR/$APP_NAME-gui"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH — add it to your shell profile" ;;
esac

# ---------------------------------------------------------------- autostart
if [ "$WANT_SERVICE" -eq 1 ] && [ "$HAVE_SYSTEMD" -eq 1 ]; then
  mkdir -p "$UNIT_DIR"
  # %t is XDG_RUNTIME_DIR — no hardcoded uid, unlike a literal /run/user/1000.
  cat > "$UNIT_DIR/$APP_NAME.service" <<EOF
[Unit]
Description=$DISPLAY_NAME
After=graphical-session.target

[Service]
Type=simple
Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus"
Environment="XDG_RUNTIME_DIR=%t"
ExecStart=$PREFIX/auto-switch.sh --config $CONFIG_DIR/config.txt run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  ok "systemd unit -> $UNIT_DIR/$APP_NAME.service"
  if [ "$WANT_START" -eq 1 ]; then
    systemctl --user enable --now "$APP_NAME.service" >/dev/null 2>&1 \
      && ok "service enabled and started" \
      || warn "could not start the service — check: systemctl --user status $APP_NAME"
  fi
elif [ "$WANT_SERVICE" -eq 1 ]; then
  mkdir -p "$AUTOSTART_DIR"
  cat > "$AUTOSTART_DIR/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$DISPLAY_NAME
Exec=$PREFIX/auto-switch.sh --config $CONFIG_DIR/config.txt start
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
  ok "autostart entry -> $AUTOSTART_DIR/$APP_NAME.desktop"
  [ "$WANT_START" -eq 1 ] && "$PREFIX/auto-switch.sh" --config "$CONFIG_DIR/config.txt" start
fi

# ---------------------------------------------------------------- menu entry
if [ "$WANT_DESKTOP" -eq 1 ]; then
  mkdir -p "$DESKTOP_DIR"
  # Ship our own icon rather than relying on a theme having a suitable one —
  # icon names differ wildly between icon themes and distros.
  if [ -f "$PREFIX/icons/$APP_NAME.svg" ] || [ -f "$SRC_DIR/icons/$APP_NAME.svg" ]; then
    mkdir -p "$ICON_DIR"
    cp -f "$PREFIX/icons/$APP_NAME.svg" "$ICON_DIR/$APP_NAME.svg" 2>/dev/null || \
      cp -f "$SRC_DIR/icons/$APP_NAME.svg" "$ICON_DIR/$APP_NAME.svg"
    have gtk-update-icon-cache && \
      gtk-update-icon-cache -q -t "$XDG_DATA_HOME/icons/hicolor" 2>/dev/null || true
    ok "icon -> $ICON_DIR/$APP_NAME.svg"
  fi
  cat > "$DESKTOP_DIR/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$DISPLAY_NAME
Comment=Switch Input Remapper presets per game, and record macros
Exec=$BIN_DIR/$APP_NAME-gui
Icon=input-remapper-autoswitch
Terminal=false
Categories=Settings;HardwareSettings;
EOF
  have update-desktop-database && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
  ok "menu entry -> $DESKTOP_DIR/$APP_NAME.desktop"
fi

say ""
say "Done."
say "  Configure:  $APP_NAME-gui        (or the “$DISPLAY_NAME” menu entry)"
say "  Control:    $APP_NAME {start|stop|status|restart|paths}"
say "  Config:     $CONFIG_DIR/config.txt"
[ "$HAVE_SYSTEMD" -eq 1 ] && [ "$WANT_SERVICE" -eq 1 ] && \
  say "  Logs:       journalctl --user -u $APP_NAME -f"
[ "$NEEDS_ROOT" -eq 1 ] && \
  say "  NOTE: the Input Remapper daemon wasn't reachable — see the warnings above."
say ""
