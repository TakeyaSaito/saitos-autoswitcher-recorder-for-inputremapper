#!/usr/bin/env python3
"""Saito's AutoSwitcher/Recorder for Input Remapper — GUI configurator.

Edits the mapping config (process|device|preset), edits Input Remapper presets,
and controls the input-remapper-autoswitch user service. Run with --help for how
the config file is located.
"""

import functools
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QEventLoop, QProcess, QTimer
from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QKeySequence,
                         QShortcut)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QInputDialog,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QToolButton,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QWidgetAction,
)

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
APP_NAME = "input-remapper-autoswitch"   # ids: unit, paths, icon
DISPLAY_NAME = "Saito's AutoSwitcher/Recorder for Input Remapper"

XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")


def resolve_config_file(explicit=None):
    """Same order as auto-switch.sh, so both halves always agree.

    The default is config.txt *beside the script*, so the whole folder can be
    dropped anywhere and stays self-contained. Whether that file exists yet makes
    no difference — it is where one gets created. An explicit path always wins,
    which is how the installed copy points at $XDG_CONFIG_HOME instead.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("AUTOSWITCH_CONFIG")
    if env:
        return Path(env).expanduser()
    env_dir = os.environ.get("AUTOSWITCH_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "config.txt"
    return SCRIPT_DIR / "config.txt"


def config_candidates():
    """Every place a config could be, in resolution order, with its size.

    Several can exist at once — a checkout's own config.txt and the XDG one, say
    — and only the first is used. Showing the others is what stops "my mappings
    vanished" being a mystery.
    """
    seen, out = set(), []
    for path in (os.environ.get("AUTOSWITCH_CONFIG"),
                 (os.environ.get("AUTOSWITCH_CONFIG_DIR") or "") and
                 os.path.join(os.environ["AUTOSWITCH_CONFIG_DIR"], "config.txt"),
                 SCRIPT_DIR / "config.txt",
                 # not used by default any more, but an installed copy keeps its
                 # config here — worth naming if it has mappings in it.
                 XDG_CONFIG_HOME / APP_NAME / "config.txt"):
        if not path:
            continue
        path = Path(path).expanduser()
        if path in seen:
            continue
        seen.add(path)
        count = 0
        if path.is_file():
            try:
                count = sum(1 for line in path.read_text().splitlines()
                            if line.split("#", 1)[0].strip())
            except OSError:
                count = -1
        out.append((path, count))
    return out


def read_settings(config_dir):
    """Parse the optional settings.conf (key=value); never executed."""
    settings = {}
    path = config_dir / "settings.conf"
    if not path.is_file():
        return settings
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return settings
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip().strip('"')
    return settings


def resolve_remapper_dir(settings):
    """Input Remapper 2.x uses input-remapper-2; 1.x uses input-remapper."""
    env = os.environ.get("INPUT_REMAPPER_CONFIG_DIR") or settings.get(
        "input_remapper_config_dir")
    if env:
        return Path(env).expanduser()
    for name in ("input-remapper-2", "input-remapper"):
        candidate = XDG_CONFIG_HOME / name
        if candidate.is_dir():
            return candidate
    return XDG_CONFIG_HOME / "input-remapper-2"


def config_from_argv(argv):
    """--config PATH or --config=PATH, without pulling in argparse."""
    for i, arg in enumerate(argv):
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
        if arg == "--config" and i + 1 < len(argv):
            return argv[i + 1]
    return None


CONFIG_FILE = resolve_config_file(config_from_argv(sys.argv[1:]))
SETTINGS = read_settings(CONFIG_FILE.parent)
REMAPPER_DIR = resolve_remapper_dir(SETTINGS)
PRESET_DIR = REMAPPER_DIR / "presets"
SERVICE = f"{APP_NAME}.service"
APP_ICON = APP_NAME            # must match Icon= in the .desktop install.sh writes
REMAPPER_SERVICE = "input-remapper.service"  # system service, needs root
DEFAULT_PRESET = "Default"

HEADER = "# process_name_or_unique_fragment | device name | preset name (without .json)\n"
DEFAULT_KEY = "DEFAULT"


# sudo -n failing for want of a password looks different from the command itself
# failing, and only the former is worth escalating to a graphical prompt.
SUDO_NEEDS_PASSWORD_RE = re.compile(
    r"password is required|no askpass|no tty present|a terminal is required|"
    r"may not run|not allowed to execute|is not in the sudoers", re.I)

PRIVILEGED_TIMEOUT_MS = 180_000  # long enough for someone to type a password


def run_command(cmd, timeout_ms=15_000):
    """Run a command, keeping the GUI responsive if one is up. (code, output)."""
    if QApplication.instance() is None:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=timeout_ms / 1000)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)
        return res.returncode, (res.stdout + res.stderr).strip()

    # QProcess + a nested event loop, so a polkit prompt doesn't freeze the window.
    proc = QProcess()
    proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
    loop = QEventLoop()
    proc.finished.connect(lambda *_: loop.quit())
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    proc.start(cmd[0], cmd[1:])
    if not proc.waitForStarted(5000):
        return 1, f"could not run {cmd[0]}"
    timer.start(timeout_ms)
    loop.exec()
    if proc.state() != QProcess.ProcessState.NotRunning:
        proc.kill()
        proc.waitForFinished(2000)
        return 1, f"timed out after {timeout_ms // 1000}s"
    output = bytes(proc.readAll()).decode("utf-8", "replace").strip()
    return proc.exitCode(), output


def run_privileged(args):
    """Run a root command, asking for credentials only when we have to.

    Tries passwordless sudo first (silent when a NOPASSWD rule exists), then
    plain systemctl — systemd asks polkit, which pops the desktop's password
    dialog — and finally pkexec. (code, output, how).
    """
    code, out = run_command(["sudo", "-n"] + args)
    if code == 0:
        return 0, out, "sudo"
    if not SUDO_NEEDS_PASSWORD_RE.search(out or ""):
        return code, out, "sudo"  # sudo worked; the command itself failed

    if args and args[0] == "systemctl":
        code, out = run_command(args, PRIVILEGED_TIMEOUT_MS)
        if code == 0:
            return 0, out, "polkit"

    if shutil.which("pkexec"):
        code2, out2 = run_command(["pkexec"] + args, PRIVILEGED_TIMEOUT_MS)
        if code2 == 0:
            return 0, out2, "pkexec"
        out = out2 or out
        code = code2
    return code or 1, out, "denied"


def passwordless_sudo():
    return run_command(["sudo", "-n", "true"], 5000)[0] == 0


def systemctl(*args, check_output=False, system=False, root=False):
    """Run systemctl. `system` drops --user; `root` escalates privileges (needed
    to change a system unit, but not to query one)."""
    cmd = ["systemctl"] + ([] if system else ["--user"]) + list(args)
    if root:
        code, out, _how = run_privileged(cmd)
        return out if check_output else (code, out)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error: {exc}" if check_output else (1, str(exc))
    if check_output:
        return (res.stdout or res.stderr).strip()
    return res.returncode, (res.stdout + res.stderr).strip()


def known_devices():
    if not PRESET_DIR.is_dir():
        return []
    return sorted(p.name for p in PRESET_DIR.iterdir() if p.is_dir())


def mappable_devices():
    """Devices Input Remapper would actually offer to map.

    Raw evdev enumeration is no good here — it includes HDMI audio nodes, power
    buttons and Input Remapper's own virtual devices. Its grouping code already
    classifies devices as keyboard/mouse/gamepad/tablet, so use that and keep
    only the ones with a type.
    """
    try:
        from inputremapper.groups import _Groups
    except ImportError:
        return []
    try:
        groups = _Groups()
        groups.refresh()
        return sorted({group.key for group in getattr(groups, "_groups", [])
                       if group.types and group.key})
    except Exception:
        return []


def all_device_names():
    """Devices that have presets, plus anything mappable that's plugged in.

    A brand new device has no preset directory yet, so it would never show up if
    we only looked at the presets folder.
    """
    return sorted(set(mappable_devices()) | set(known_devices()))


def presets_for(device):
    d = PRESET_DIR / device
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def preset_exists(device, preset):
    return (PRESET_DIR / device / f"{preset}.json").is_file()


def gui_state_path():
    return Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") \
        / APP_NAME / "gui-state.json"


def load_gui_state():
    try:
        return json.loads(gui_state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_gui_state(updates):
    """Remember small UI choices, like which device was last being edited."""
    state = load_gui_state()
    state.update(updates)
    path = gui_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


# Extensions and engine suffixes that are part of the executable name but not
# of the game's name — "Palworld-Win64-Shipping.exe" is a Palworld preset.
PROCESS_NOISE_RE = re.compile(
    r"(-(Win64|WinGDK|Win32)-Shipping)?"
    r"(\.(exe|x86_64|x86|bin|sh|jar|AppImage|bin\.x86_64))?$", re.I)


def clean_process_name(process):
    """A likely preset name for a process: 'Palworld-Win64-Shipping.exe' -> 'Palworld'."""
    name = (process or "").strip()
    previous = None
    while name and name != previous:          # strip .bin.x86_64 and friends
        previous = name
        name = PROCESS_NOISE_RE.sub("", name).strip()
    return name or (process or "").strip()


def preset_for_process(device, process):
    """Which preset a new mapping for this process should point at.

    Matches an existing preset by name where one fits, otherwise seeds the
    process's own name so the row flags itself until the preset is created —
    the same behaviour as importing from Steam. Picking whatever sorted first
    silently attached games to an unrelated preset.
    """
    presets = presets_for(device)
    if not process:
        return DEFAULT_PRESET if DEFAULT_PRESET in presets else (
            presets[0] if presets else "")
    guess = clean_process_name(process)
    by_norm = {normalize(name): name for name in presets}
    return by_norm.get(normalize(guess), safe_preset_name(guess))


def normalize(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def safe_preset_name(name):
    """Input Remapper stores presets as <name>.json, so keep it path-safe."""
    return re.sub(r"[/\\:\x00]", "-", name).strip() or "Unnamed"


# ---------------- stale origin_hash repair ----------------
#
# Input Remapper tags every mapping with an origin_hash identifying the exact
# /dev/input/eventXX it was recorded from:
#
#     md5(str(device.capabilities(absinfo=False)) + device.name)
#
# It is meant to be stable, but it is derived from the device's *capabilities* —
# so a kernel, driver or firmware update that changes what the device reports
# silently changes the hash. Every preset recorded before that keeps pointing at
# a device node that no longer exists, and those mappings quietly stop firing.
# Re-recording a key fixes it, which is why the symptom looks like "the key is
# assigned but does nothing".


def current_device_hashes():
    """{hash: {"name", "path", "codes", "count"}} for every live input device."""
    try:
        import evdev
    except ImportError:
        return None  # python-evdev missing; caller reports it
    from hashlib import md5

    found = {}
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
            capabilities = device.capabilities(absinfo=False)
        except (OSError, PermissionError):
            continue
        digest = md5((str(capabilities) + device.name).encode()).hexdigest().lower()
        codes = {(ev_type, code) for ev_type, code_list in capabilities.items()
                 for code in code_list}
        found[digest] = {"name": device.name, "path": path, "codes": codes,
                         "count": len(codes)}
    return found


def preset_paths(device):
    directory = PRESET_DIR / device
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def analyze_presets(live=None):
    """Find mappings pointing at device nodes that no longer exist.

    Returns (findings, problems). Each finding is a dict describing one preset
    file and the replacement hash chosen for it.
    """
    if live is None:
        live = current_device_hashes()
    if live is None:
        return [], ["python-evdev is not installed — cannot inspect input devices"]

    findings, problems = [], []
    for device in known_devices():
        # Hashes currently offered by nodes belonging to this device.
        device_hashes = {h: info for h, info in live.items() if info["name"] == device}
        if not device_hashes:
            problems.append(f"“{device}” is not connected — skipped")
            continue

        # Ground truth: hashes the user's own healthy mappings already use.
        healthy = {}
        parsed = {}
        for path in preset_paths(device):
            try:
                text = path.read_text()
            except OSError as exc:
                problems.append(f"{path.name}: unreadable ({exc})")
                continue
            if not text.strip():
                continue  # Input Remapper leaves empty placeholder presets around
            try:
                data = json.loads(text)
            except ValueError as exc:
                problems.append(f"{path.name}: not valid JSON ({exc})")
                continue
            parsed[path] = data
            for mapping in data if isinstance(data, list) else []:
                for combo in mapping.get("input_combination", []) or []:
                    digest = combo.get("origin_hash")
                    if digest in device_hashes:
                        healthy[digest] = healthy.get(digest, 0) + 1

        for path, data in parsed.items():
            stale = []
            for mapping in data if isinstance(data, list) else []:
                for combo in mapping.get("input_combination", []) or []:
                    digest = combo.get("origin_hash")
                    if digest and digest not in device_hashes:
                        stale.append((combo.get("type"), combo.get("code"), digest))
            if not stale:
                continue

            needed = {(t, c) for t, c, _h in stale if t is not None and c is not None}
            candidates = [h for h, info in device_hashes.items()
                          if needed <= info["codes"]]
            if not candidates:
                problems.append(
                    f"{path.stem}: no connected node of “{device}” reports all of its "
                    "keys — leaving it alone")
                continue
            # Prefer what the healthy mappings use, then the richest node.
            candidates.sort(key=lambda h: (healthy.get(h, 0), device_hashes[h]["count"]),
                            reverse=True)
            replacement = candidates[0]
            findings.append({
                "device": device,
                "preset": path.stem,
                "path": path,
                "count": len(stale),
                "old": sorted({h for _t, _c, h in stale}),
                "new": replacement,
                "node": device_hashes[replacement]["path"],
                "evidence": healthy.get(replacement, 0),
            })
    return findings, problems


def backup_dir():
    return XDG_DATA_HOME / APP_NAME / "preset-backups"


def apply_hash_fix(finding):
    """Rewrite one preset's stale origin_hash values. (ok, message)."""
    path, replacement = finding["path"], finding["new"]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return False, f"{finding['preset']}: {exc}"

    changed = 0
    for mapping in data if isinstance(data, list) else []:
        for combo in mapping.get("input_combination", []) or []:
            if combo.get("origin_hash") in finding["old"]:
                combo["origin_hash"] = replacement
                changed += 1
    if not changed:
        return False, f"{finding['preset']}: nothing to change"

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / finding["device"]
    try:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target / f"{path.name}.{stamp}")
        path.write_text(json.dumps(data, indent=4))
    except OSError as exc:
        return False, f"{finding['preset']}: {exc}"
    return True, f"{finding['preset']}: {changed} mapping(s) repaired"


def trash_dir():
    """Deleted presets are moved here rather than unlinked, so a mistaken
    delete is always recoverable."""
    return XDG_DATA_HOME / APP_NAME / "deleted-presets"


def delete_preset(device, preset):
    """Move a preset out of Input Remapper's directory. (ok, message)."""
    if preset == DEFAULT_PRESET:
        return False, f"refused to delete “{DEFAULT_PRESET}”"
    source = PRESET_DIR / device / f"{preset}.json"
    if not source.is_file():
        return False, f"“{preset}” not found"
    target_dir = trash_dir() / device
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target_dir / f"{preset}.json.{stamp}"))
    except OSError as exc:
        return False, f"“{preset}”: {exc}"
    return True, f"deleted “{preset}”"


def create_preset_from_default(device, preset):
    """Copy the device's Default preset to a new name. (ok, message)."""
    source = PRESET_DIR / device / f"{DEFAULT_PRESET}.json"
    target = PRESET_DIR / device / f"{safe_preset_name(preset)}.json"
    if target.exists():
        return False, f"“{preset}” already exists"
    if not source.is_file():
        return False, f"no {DEFAULT_PRESET}.json to copy for “{device}”"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError as exc:
        return False, f"“{preset}”: {exc}"
    return True, f"created “{target.stem}”"


BUS_TAKEN_RE = re.compile(r"already running|Name request has failed", re.I)


DAEMON_EXE = "input-remapper-service"


def _is_daemon_process(pid):
    """True only for a real daemon process — never for something that merely
    mentions the name.

    Deliberately strict: this drives a kill. Matching on the whole command line
    (pgrep -f style) also matches a shell running a grep for it, an editor with
    the file open, or this program's own helpers, and killing those would be
    catastrophic. Only argv[0]/argv[1] count, which is where an interpreter puts
    the script it is running.
    """
    try:
        argv = [a for a in Path(f"/proc/{pid}/cmdline").read_bytes()
                .decode("utf-8", "replace").split("\0") if a]
    except OSError:
        return False
    return any(arg == DAEMON_EXE or arg.endswith(f"/{DAEMON_EXE}")
               for arg in argv[:2])


def squatting_daemons():
    """PIDs of input-remapper daemons that systemd is *not* supervising.

    Closing the Input Remapper GTK app can orphan the daemon it spawned. The
    orphan keeps the inputremapper.Control bus name, so systemd's own copy dies
    with "Is the service already running?" on every start attempt, forever.
    Anything outside the input-remapper.service cgroup is such a squatter.
    """
    ours = {os.getpid(), os.getppid()}
    squatters = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in ours or not _is_daemon_process(pid):
            continue
        try:
            cgroup = (entry / "cgroup").read_text()
        except OSError:
            continue  # already gone
        if REMAPPER_SERVICE not in cgroup:
            squatters.append(str(pid))
    return squatters


def stop_injections():
    """Ask the daemon to stop injecting, which releases every key it holds.

    Always do this before a daemon dies. A daemon killed mid-press takes its
    virtual keyboard with it, so the key-up never arrives and the desktop is
    left with a key stuck down.
    """
    control = shutil.which("input-remapper-control")
    if not control:
        return False
    code, _out = run_command([control, "--command", "stop-all"], 10_000)
    return code == 0


def release_all_keys():
    """Belt and braces: send a key-up for every key code from a scratch device.

    Releasing a key that isn't held is a no-op, so this can only ever unstick
    things. Best effort — if uinput isn't writable we simply skip it.
    """
    try:
        import evdev
        from evdev import UInput, ecodes
    except ImportError:
        return False
    try:
        keys = sorted(ecodes.keys.keys())
        device = UInput({ecodes.EV_KEY: keys}, name="autoswitch-key-release-helper")
    except (OSError, PermissionError):
        return False
    try:
        time.sleep(0.4)  # let the compositor register the new device
        for code in keys:
            device.write(ecodes.EV_KEY, code, 0)
        device.syn()
        time.sleep(0.2)
    finally:
        device.close()
    return True


def clear_squatters():
    """Evict orphaned daemons holding the bus name. (killed_pids, note).

    Injections are stopped first so no key is left down, then the processes are
    terminated gently (SIGTERM) before SIGKILL, and finally every key is
    released as a safety net.
    """
    pids = squatting_daemons()
    if not pids:
        return [], ""

    stop_injections()
    time.sleep(0.3)

    code, out, _how = run_privileged(["kill"] + pids)
    if code != 0:
        return [], f"could not stop stray daemon(s) {', '.join(pids)}: {out}"

    for _ in range(20):
        time.sleep(0.25)
        if not squatting_daemons():
            break
    else:  # still there — escalate
        run_privileged(["kill", "-9"] + squatting_daemons())
        time.sleep(0.5)

    release_all_keys()
    return pids, ""


def reload_remapper():
    """Restart Input Remapper so it sees new presets, then the switcher.

    Order matters. The switcher polls every few seconds and each poll runs
    input-remapper-control, which D-Bus-activates a daemon of its own; if that
    lands while the unit is restarting it grabs the bus name and systemd's copy
    dies with "Is the service already running?". So the switcher is stopped for
    the duration. Restarting it afterwards is also what forces the profile to be
    re-applied — its last-applied cache would otherwise skip it.
    """
    messages = []
    switcher_was_up = systemctl("is-active", SERVICE, check_output=True) == "active"
    if switcher_was_up:
        systemctl("stop", SERVICE)

    # Release everything the daemon is holding *before* it is torn down, so a
    # restart can never strand a key in the down position.
    stop_injections()

    restart = ["systemctl", "restart", REMAPPER_SERVICE]
    code, out, how = run_privileged(restart)
    if code != 0 and how != "denied":
        time.sleep(1.5)  # give a lingering daemon time to drop the bus name
        code, out, how = run_privileged(restart)

    # Still refusing to start? Something outside systemd owns the bus name —
    # typically a daemon orphaned by the Input Remapper GTK app. Restarting can
    # never win that on its own, so evict the squatter and try once more.
    if code != 0 and how != "denied" and BUS_TAKEN_RE.search(
            (out or "") + systemctl("status", REMAPPER_SERVICE, check_output=True,
                                    system=True)):
        killed, note = clear_squatters()
        if note:
            messages.append(note)
        if killed:
            messages.append(f"cleared {len(killed)} orphaned daemon(s) holding the "
                            "D-Bus name")
            code, out, how = run_privileged(restart)

    if code != 0 and how == "denied":
        messages.append("Input Remapper NOT restarted (authorization declined) — "
                        f"run: sudo systemctl restart {REMAPPER_SERVICE}")
    elif code == 0:
        for _ in range(20):  # wait for the daemon to own the bus before poking it
            if systemctl("is-active", REMAPPER_SERVICE,
                         check_output=True, system=True) == "active":
                break
            time.sleep(0.25)
        messages.append("Input Remapper restarted")
    else:
        messages.append(f"Input Remapper restart FAILED: {out.splitlines()[0] if out else ''}")

    if switcher_was_up:
        code, out = systemctl("start", SERVICE)
        messages.append("auto-switch restarted" if code == 0
                        else f"auto-switch start failed: {out}")
    else:
        messages.append("auto-switch left stopped")
    return "; ".join(messages)


# ---------------- Steam library scanning ----------------

STEAM_ROOTS = [HOME / ".steam/steam", HOME / ".local/share/Steam", HOME / ".steam/root"]

# Runtimes and utilities that aren't games.
TOOL_RE = re.compile(r"^(proton|steam linux runtime|steamworks|steamvr|steam controller|"
                     r"wallpaper engine|lossless scaling)|uploader$", re.I)
# Directories that only ever hold engine helpers or redistributables. Skipping
# "engine" is what keeps CrashReportClient/EpicWebHelper out of UE results.
SKIP_DIRS = {"engine", "redist", "redistributable", "_commonredist", "commonredist",
             "directx", "vcredist", "dotnet", "easyanticheat", "eac", "battleye",
             "installers", "support", "tools", "dxsetup", "__installer"}
JUNK_RE = re.compile(r"crashreport|crashhandler|crashpad|epicwebhelper|easyanticheat|"
                     r"battleye|unins|setup|vc_redist|dxsetup|dotnet|touchup|helper|"
                     r"server|benchmark|editor|-cmd|selector|launcher|familyshare|"
                     r"restarter", re.I)
SHIPPING_RE = re.compile(r"-(Win64|WinGDK|Win32)-Shipping\.exe$", re.I)
NATIVE_RE = re.compile(r"\.(x86_64|x86|sh)$")
# Native launchers are often tiny shims (Oxygen Not Included's is 4 KB next to a
# 12 MB Restarter), so this only filters out obvious data files.
MIN_NATIVE_SIZE = 2048


def steam_libraries():
    """Every Steam library path, from libraryfolders.vdf in each Steam root."""
    libs, out, seen = [], [], set()
    for root in STEAM_ROOTS:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(errors="replace")
        except OSError:
            continue
        libs += [Path(m.group(1)) for m in re.finditer(r'"path"\s*"([^"]+)"', text)]
    for lib in libs:
        try:
            real = lib.resolve()
        except OSError:
            continue
        if real not in seen and (real / "steamapps").is_dir():
            seen.add(real)
            out.append(real)
    return out


def steam_apps():
    """Installed games as (appid, name, install_path), games only, sorted by name."""
    found = []
    for lib in steam_libraries():
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                text = acf.read_text(errors="replace")
            except OSError:
                continue

            def field(key):
                m = re.search(rf'"{key}"\s*"([^"]*)"', text)
                return m.group(1) if m else ""

            name, installdir = field("name"), field("installdir")
            if not name or not installdir or TOOL_RE.search(name):
                continue
            path = lib / "steamapps" / "common" / installdir
            if path.is_dir():
                found.append((field("appid"), name, path))
    return sorted(found, key=lambda a: a[1].lower())


def executable_candidates(root, max_depth=5):
    """Rank launchable binaries under a game dir; best first.

    Unreal games ship a stub <Game>.exe in the install root that only bounces to
    the real binary in <Game>/Binaries/Win64/<Game>-Win64-Shipping.exe, and it is
    the latter that shows up in the process list — so shipping binaries outrank
    everything else.
    """
    out = []
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root_str):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.lower().endswith("_data")]
        for filename in filenames:
            full = Path(dirpath) / filename
            windows = filename.lower().endswith(".exe")
            extensionless = "." not in filename and depth == 0
            native = bool(NATIVE_RE.search(filename)) or extensionless
            if not (windows or native):
                continue
            try:
                stat = full.stat()
            except OSError:
                continue
            if not full.is_file():
                continue
            if extensionless and (stat.st_size < MIN_NATIVE_SIZE
                                  or not os.access(full, os.X_OK)):
                continue

            score, kind = 0.0, "executable"
            if SHIPPING_RE.search(filename) and "binaries" in dirpath.lower():
                score += 100
                kind = "Unreal shipping binary"
            elif native and depth == 0:
                score += 40
                kind = "native Linux binary"
            elif windows and depth == 0:
                kind = "Windows .exe (install root)"
            # Outweighs the size bonus: the binary named after the game beats a
            # bigger helper sitting beside it.
            if normalize(full.stem) == normalize(root.name):
                score += 60
            if JUNK_RE.search(filename):
                score -= 80
                kind = "helper / tool — probably not the game"
            score += min(stat.st_size / 1e9, 5)
            out.append((score, filename, str(full), kind))
    out.sort(key=lambda c: -c[0])
    return out[:8]


class ProcessPicker(QDialog):
    """Pick a currently running process to use as a match fragment."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick a running process")
        self.resize(640, 520)
        self.choice = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Select a running process. The name is matched with <tt>pgrep -fi</tt>, "
            "so a unique fragment is enough."))

        self.filter = QLineEdit(placeholderText="Filter…")
        layout.addWidget(self.filter)

        self.list = QListWidget()
        layout.addWidget(self.list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        refresh = buttons.addButton("Refresh", QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        refresh.clicked.connect(self.reload)
        self.filter.textChanged.connect(self.apply_filter)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())

        self.reload()

    def reload(self):
        self.list.clear()
        try:
            out = subprocess.run(["ps", "-eo", "comm="], capture_output=True,
                                 text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        names = sorted({n.strip() for n in out.splitlines() if n.strip()},
                       key=str.lower)
        self.list.addItems(names)
        self.apply_filter(self.filter.text())

    def apply_filter(self, text):
        text = text.lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(text not in item.text().lower())

    def accept(self):
        item = self.list.currentItem()
        if item and not item.isHidden():
            self.choice = item.text()
        super().accept()


# ---------------- preset editing ----------------
#
# The editor drives Input Remapper's own model classes rather than hand-rolling
# the JSON, so validation, defaults and the on-disk format all come from the
# source of truth and stay correct across its versions.


def guard(method):
    """Keep an exception inside a Qt slot from aborting the process.

    PyQt6 calls abort() when an exception escapes a slot, which would take the
    whole GUI down over something as small as an unexpected field type.

    Extra positional arguments are dropped to match the wrapped method. PyQt
    inspects a slot's signature and passes as many arguments as it will accept —
    clicked() offers a `checked` bool — so a naive *args wrapper would forward an
    argument the real method never had.
    """
    accepted = len([
        p for p in inspect.signature(method).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        args = args[:max(0, accepted - 1)]  # -1 for self
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            label = getattr(self, "error_label", None)
            if label is not None:
                label.setText(f"<span style='color:#d13438'>⚠ {method.__name__}: "
                              f"{exc}</span>")
            else:
                print(f"[autoswitch-gui] {method.__name__}: {exc}", file=sys.stderr)
    return wrapper


def import_remapper():
    """Input Remapper's own config classes, or None when unavailable."""
    try:
        from inputremapper.configs.preset import Preset
        from inputremapper.configs.mapping import UIMapping, KnownUinput, MappingType
        from inputremapper.configs.input_config import InputCombination, InputConfig
    except ImportError:
        return None
    return {"Preset": Preset, "UIMapping": UIMapping, "KnownUinput": KnownUinput,
            "MappingType": MappingType, "InputCombination": InputCombination,
            "InputConfig": InputConfig}


def device_nodes(device_name):
    """Every live /dev/input node belonging to a device, with its origin hash."""
    live = current_device_hashes() or {}
    return [(info["path"], digest) for digest, info in live.items()
            if info["name"] == device_name]


def event_label(ev_type, code):
    """Name a code within its own event type — REL_X, ABS_Y, KEY_A…

    Codes are only unique per type: code 0 is KEY_RESERVED, REL_X or ABS_X
    depending on the event type it arrived with.
    """
    try:
        import evdev
    except ImportError:
        return f"type {ev_type} code {code}"
    name = evdev.ecodes.bytype.get(ev_type, {}).get(code)
    if isinstance(name, (list, tuple)):
        name = name[0]
    return name or f"type {ev_type} code {code}"


def key_name(code):
    try:
        import evdev
        name = evdev.ecodes.bytype.get(evdev.ecodes.EV_KEY, {}).get(code)
    except ImportError:
        return str(code)
    if isinstance(name, (list, tuple)):
        name = name[0]
    return name or str(code)


class Recorder(QDialog):
    """Capture a keypress from real hardware instead of typing its name.

    Two modes. "input" listens to the preset's own device — which means pausing
    injection first, because the daemon holds an exclusive grab on it while
    injecting. "output" listens to everything else, so you can press the key you
    want produced.
    """

    def __init__(self, paths, title, prompt, combination=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(460, 210)
        self.result_keys = []       # [(type, code, origin_hash)]
        self._devices = []
        self._pressed = {}
        self._combination = combination

        layout = QVBoxLayout(self)
        self.label = QLabel(prompt)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.captured = QLabel("<i>waiting…</i>")
        font = QFont("monospace"); font.setPointSize(font.pointSize() + 2)
        self.captured.setFont(font)
        layout.addWidget(self.captured)
        self.countdown = QLabel()
        layout.addWidget(self.countdown)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._open_devices(paths)

        # Hard stop: never leave the keyboard grabbed if something goes wrong.
        self._remaining = 20
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

    def _open_devices(self, paths):
        try:
            import evdev
        except ImportError:
            self.label.setText("python-evdev is not installed — cannot record.")
            return
        from PyQt6.QtCore import QSocketNotifier

        failed = []
        for path in paths:
            device = None
            # The daemon may not have let go the instant we asked it to, so keep
            # trying for a few seconds rather than silently skipping the one node
            # that actually emits the keys.
            for _attempt in range(12):
                try:
                    device = evdev.InputDevice(path)
                    device.grab()
                    break
                except OSError:
                    if device is not None:
                        device.close()
                        device = None
                    time.sleep(0.25)
                    QApplication.processEvents()
            if device is None:
                failed.append(path)
                continue
            notifier = QSocketNotifier(device.fd, QSocketNotifier.Type.Read, self)
            notifier.activated.connect(lambda _s, d=device: self._read(d))
            self._devices.append((device, notifier))

        if not self._devices:
            self.label.setText(
                "<b>Could not get exclusive access to the device.</b><br><br>"
                "Something else is holding it — usually Input Remapper still "
                "injecting, or its own GUI being open. Close the Input Remapper "
                "window and try again.")
        elif failed:
            # Partial access is the dangerous case: the dialog looks fine but the
            # node carrying the keypresses may be the one we missed.
            self.label.setText(
                self.label.text()
                + f"<br><br><span style='color:#c07000'>Note: {len(failed)} of "
                f"{len(paths)} device nodes could not be captured. If nothing is "
                "detected, close the Input Remapper window and retry.</span>")

    def _read(self, device):
        import evdev
        from hashlib import md5
        try:
            events = list(device.read())
        except OSError:
            return
        digest = md5((str(device.capabilities(absinfo=False))
                      + device.name).encode()).hexdigest().lower()
        for event in events:
            if event.type != evdev.ecodes.EV_KEY:
                continue
            if event.value == 1:      # key down
                self._pressed[(event.type, event.code)] = digest
                self._refresh()
                if not self._combination:
                    QTimer.singleShot(120, self._finish)
            elif event.value == 0 and self._combination and self._pressed:
                # Combination is complete once the user lets go.
                QTimer.singleShot(120, self._finish)

    def _refresh(self):
        names = " + ".join(key_name(code) for _t, code in self._pressed)
        self.captured.setText(names or "<i>waiting…</i>")

    def _finish(self):
        if not self._pressed:
            return
        self.result_keys = [(t, c, h) for (t, c), h in self._pressed.items()]
        self.accept()

    def _tick(self):
        self.countdown.setText(
            f"<i>cancels automatically in {self._remaining}s</i>")
        self._remaining -= 1
        if self._remaining < 0:
            self.reject()

    def closeEvent(self, event):
        self._timer.stop()
        for device, notifier in self._devices:
            notifier.setEnabled(False)
            try:
                device.ungrab()
            except OSError:
                pass
            device.close()
        self._devices = []
        super().closeEvent(event)

    def reject(self):
        self.close()
        super().reject()

    def accept(self):
        self.close()
        super().accept()


def detect_target(output_symbol=None, output_type=None, output_code=None):
    """Which virtual device can emit this output — keyboard, mouse, or both.

    Uses Input Remapper's own per-target capability tables rather than guessing
    from the KEY_/BTN_ prefix: BTN_LEFT is a mouse button, BTN_SOUTH is a
    gamepad one, and only the tables know which target covers which codes.
    Returns the narrowest target that can emit everything, or None.
    """
    try:
        from inputremapper.injection.global_uinputs import DEFAULT_UINPUTS
        import evdev
    except ImportError:
        return None

    needed = set()
    for name in re.findall(r"\b(?:KEY|BTN)_[A-Z0-9_]+", output_symbol or ""):
        code = evdev.ecodes.ecodes.get(name)
        if code is not None:
            needed.add((evdev.ecodes.EV_KEY, code))
    if output_type is not None and output_code is not None:
        needed.add((output_type, output_code))
    if not needed:
        return None

    # Narrowest first, so a keyboard-only macro doesn't claim a combined device.
    for target in ("keyboard", "mouse", "gamepad", "keyboard + mouse"):
        capabilities = DEFAULT_UINPUTS.get(target, {})
        if all(code in capabilities.get(ev_type, []) for ev_type, code in needed):
            return target
    return None


def uses_left_click_input(mapping):
    """Does this mapping consume the left mouse button?"""
    try:
        import evdev
        combination = mapping.input_combination
    except Exception:
        return False
    return any(config.type == evdev.ecodes.EV_KEY
               and config.code == evdev.ecodes.BTN_LEFT
               for config in combination)


def produces_left_click(mapping):
    """Does this mapping actually give left click back?

    The target device has to be able to emit it: a mapping that outputs
    BTN_LEFT to the *keyboard* uinput produces nothing, and would otherwise look
    like a safe way to keep clicking.
    """
    try:
        import evdev
        from inputremapper.injection.global_uinputs import DEFAULT_UINPUTS
    except ImportError:
        return False

    mentions = "btn_left" in (mapping.output_symbol or "").lower() or (
        mapping.output_type, mapping.output_code) == (
        evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT)
    if not mentions:
        return False

    capabilities = DEFAULT_UINPUTS.get(mapping.target_uinput or "", {})
    return evdev.ecodes.BTN_LEFT in capabilities.get(evdev.ecodes.EV_KEY, [])


def disables_left_click(mappings):
    """True when a preset swallows left click without handing it back.

    Losing left click makes a desktop very hard to recover — you can't click the
    Input Remapper window to undo it — so this is worth blocking on. A mapping
    that remaps left click *to* left click, or another that re-emits it, is fine.
    """
    valid = [m for m in mappings if m.is_valid()]
    if not any(uses_left_click_input(m) for m in valid):
        return False
    return not any(produces_left_click(m) for m in valid)


def leading_int(text):
    """First integer in a label like '2  (EV_REL — relative…)'; None if absent."""
    match = re.match(r"\s*(-?\d+)", text or "")
    return int(match.group(1)) if match else None


# A trailing wait inside a hold() is what paces the loop; accept both spellings.
TRAILING_WAIT_RE = re.compile(r"\.(?:wait|w)\((\d+)\)\s*$")


def split_trailing_wait(text):
    """(macro without its trailing wait, that wait in ms or None)."""
    match = TRAILING_WAIT_RE.search(text or "")
    return (text[:match.start()], int(match.group(1))) if match else (text, None)


def build_output(base, hold, delay_ms=None):
    """Assemble the output symbol from its parts.

    hold(...) loops while the key is held; a wait at the end of the loop body is
    what stops it repeating as fast as the CPU allows.
    """
    base = (base or "").strip()
    if not base:
        return ""
    if not hold:
        return base
    # Any trailing wait is the loop delay's, so drop it before adding the
    # current one — otherwise editing a macro stacks a second wait each pass.
    base, _existing = split_trailing_wait(base)
    inner = base if "(" in base else f"key({base})"
    if delay_ms:
        inner = f"{inner}.wait({delay_ms})"
    return f"hold({inner})"


def wrap_hold(text):
    """Wrap an output in hold(...) so it loops while the trigger key is held."""
    text = (text or "").strip()
    if not text or is_held(text):
        return text
    inner = text if "(" in text else f"key({text})"
    return f"hold({inner})"


def is_held(text):
    """True when the output is a hold(...) covering the whole expression."""
    text = (text or "").strip()
    if not text.startswith("hold(") or not text.endswith(")"):
        return False
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1  # the opening hold( closes at the end
    return False


def unwrap_hold(text):
    text = (text or "").strip()
    return text[len("hold("):-1].strip() if is_held(text) else text


MACRO_STEP_RE = re.compile(r"(key|wait|hold_keys)\(([^)]*)\)")


def parse_macro_steps(text):
    """Turn macro text back into editable steps, or None if we can't be sure.

    Only round-trips what this recorder produces. Anything more elaborate is
    returned as None so the existing macro is left alone rather than mangled.
    """
    text = (text or "").strip()
    if not text:
        return []
    if re.fullmatch(r"[A-Za-z0-9_]+", text):      # a bare symbol like KEY_M
        return [{"keys": [text]}]

    steps = []
    for name, args in MACRO_STEP_RE.findall(text):
        args = args.strip()
        if name == "wait":
            if not args.isdigit():
                return None
            steps.append({"wait": int(args)})
        else:
            keys = [k.strip() for k in args.split(",") if k.strip()]
            if not keys or (name == "key" and len(keys) != 1):
                return None
            steps.append({"keys": keys})

    # Only trust the parse if it reproduces the original exactly.
    rebuilt = ".".join(
        f"wait({s['wait']})" if "wait" in s
        else (f"key({s['keys'][0]})" if len(s["keys"]) == 1
              else "hold_keys(" + ", ".join(s["keys"]) + ")")
        for s in steps)
    normalise = lambda t: re.sub(r"\s+", "", t)
    return steps if steps and normalise(rebuilt) == normalise(text) else None


class MacroRecorder(QDialog):
    """Record a whole sequence — keys, chords and the pauses between them.

    Produces Input Remapper macro text: key(KEY_A).wait(250).hold_keys(KEY_B, KEY_C)
    """

    MIN_WAIT_MS = 40      # shorter gaps than this are just human imprecision
    MAX_WAIT_MS = 10_000
    # BTN_LEFT..BTN_TASK — the pointer buttons that also drive the UI.
    MOUSE_BUTTONS = set(range(0x110, 0x118))

    def __init__(self, paths, parent=None, excluded="", initial="", single=False):
        super().__init__(parent)
        # single=True captures one key or chord and closes — the common case,
        # without making the user drive the whole macro screen for it.
        self.single = single
        self.setWindowTitle("Record a key" if single else "Record macro")
        self.paths = list(paths)
        self.steps = []            # {"keys": [names]} or {"wait": ms}
        self._devices = []
        self._held = {}            # code -> name, currently down
        self._chord = []           # names accumulated for the chord in progress
        self._chord_started = None # when that chord began, for measuring the gap
        self._last_release = None
        self._ui_click_at = 0.0   # when a click last landed on this dialog

        # Carry the existing macro in so recording appends to it instead of
        # starting from scratch. Start paused when there is something to keep.
        # Opens idle: nothing is captured until Start recording is pressed.
        self.recording = False
        self.unparsed = ""
        existing = parse_macro_steps(initial)
        if existing:
            self.steps = existing
        elif initial.strip():
            self.unparsed = initial.strip()

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Press the key you want this mapping to produce — or several together "
            "for a chord. It is captured as soon as you let go.<br><br>"
            "<i>Your keyboard keeps working normally, so avoid pressing "
            "shortcuts.</i>"
            if single else
            "Press the keys you want this mapping to produce — one after another, "
            "or several together for a chord. The pauses between them are recorded "
            "too.<br><br>Press <b>Stop</b> when you're done, then trim or retime any "
            "step before accepting.<br><br>"
            "<i>Your keyboard keeps working normally while recording, so avoid "
            "pressing shortcuts.</i>")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        if excluded:
            note = QLabel(
                f"<span style='color:#c07000'>Note: keys and clicks from "
                f"<b>{excluded}</b> are not recorded here — that's the device being "
                "remapped. Use another keyboard or mouse.</span>")
            note.setWordWrap(True)
            layout.addWidget(note)

        if self.unparsed:
            warn = QLabel(
                "<span style='color:#c07000'>The current output "
                f"<tt>{self.unparsed}</tt> uses macro features this recorder "
                "can't represent, so it isn't loaded below. Recording something "
                "new will replace it; cancel to keep it.</span>")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        self.status = QLabel("<b style='color:#d13438'>● recording</b>")
        layout.addWidget(self.status)


        self.list = QListWidget()
        self.list.setMinimumHeight(150)
        self.list.setToolTip("Select a step and press Delete to remove it")
        layout.addWidget(self.list, 1)

        # Only bites while stopped: during recording the event filter swallows
        # keys, so pressing Delete records it as a step instead.
        delete = QShortcut(QKeySequence.StandardKey.Delete, self.list)
        delete.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete.activated.connect(self.delete_step)

        edit_row = QHBoxLayout()
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.toggle_recording)
        edit_row.addWidget(self.stop_button)
        delete = QPushButton("Delete step")
        delete.setToolTip("Remove the selected step  (Delete)")
        delete.clicked.connect(self.delete_step)
        edit_row.addWidget(delete)
        wait_label = QLabel("Wait (ms):")
        edit_row.addWidget(wait_label)
        self.wait_field = QLineEdit()
        self.wait_field.setPlaceholderText("select a wait step")
        self.wait_field.editingFinished.connect(self.retime_step)
        edit_row.addWidget(self.wait_field)
        layout.addLayout(edit_row)

        delay_row = QHBoxLayout()
        self.fixed_delay_check = QCheckBox("Use a fixed delay between steps:")
        self.fixed_delay_check.setToolTip(
            "Insert the same pause between every step instead of recording how "
            "long you actually took.")
        delay_row.addWidget(self.fixed_delay_check)
        self.fixed_delay_field = QLineEdit("100")
        self.fixed_delay_field.setFixedWidth(70)
        self.fixed_delay_field.setToolTip("Milliseconds")
        delay_row.addWidget(self.fixed_delay_field)
        ms_label = QLabel("ms")
        delay_row.addWidget(ms_label)
        delay_row.addStretch(1)
        layout.addLayout(delay_row)

        macro_label = QLabel("<b>Macro</b>")
        layout.addWidget(macro_label)
        self.preview = QLineEdit(readOnly=True)
        self.preview.setFont(QFont("monospace"))
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use this macro")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if single:
            self.recording = True
            for widget in (self.list, self.stop_button, delete, self.wait_field,
                           self.fixed_delay_check, self.fixed_delay_field,
                           wait_label, ms_label, macro_label, self.preview):
                widget.hide()
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use this key")
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            self.status.setText(
                "<b style='color:#d13438'>● listening</b> — press a key")
        if self.recording:
            self._open(paths)
        QApplication.instance().installEventFilter(self)

        # Belt and braces: recording stops on its own, so a wedged dialog can
        # never sit there consuming keystrokes indefinitely.
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self.stop)
        if self.recording:
            self._deadline.start(120_000)

        self.update_recording_state()
        self.refresh()

        # Opens at the smallest usable size. The layout would technically fit in
        # ~360px, but the wait field and macro preview become unreadable, so the
        # width has a floor; it's freely resizable from there.
        # The wrapped instruction text means the needed height depends on the
        # width, and minimumSizeHint() doesn't account for that — ask the layout
        # what it actually needs at the width we're going to use, or the list
        # ends up overlapping the buttons underneath it.
        self.layout().invalidate()
        self.layout().activate()
        smallest = self.minimumSizeHint()
        width = max(smallest.width(), 520)
        height = max(smallest.height(), self.layout().minimumHeightForWidth(width))
        # A hint alone doesn't stop the window being dragged smaller.
        self.setMinimumSize(width, height)
        self.resize(width, height)

    def _open(self, paths):
        """Listen to the keyboard WITHOUT grabbing it.

        Never take an exclusive grab here. This listens to every device except
        the one being remapped — which includes the mouse — and grabbing those
        leaves nothing to click Stop with. Read-only means the keys also reach
        the desktop; that is the acceptable trade, and they are swallowed by the
        dialog's event filter so they can't type into its own fields.
        """
        try:
            import evdev
        except ImportError:
            self.status.setText("python-evdev is not installed — cannot record.")
            return
        from PyQt6.QtCore import QSocketNotifier
        for path in paths:
            try:
                device = evdev.InputDevice(path)
                if evdev.ecodes.EV_KEY not in device.capabilities():
                    device.close()      # pointer-motion only; nothing to record
                    continue
            except OSError:
                continue
            notifier = QSocketNotifier(device.fd, QSocketNotifier.Type.Read, self)
            notifier.activated.connect(lambda _s, d=device: self._read(d))
            self._devices.append((device, notifier))
        if not self._devices:
            self.status.setText(
                "<b style='color:#d13438'>Could not read any keyboard.</b>")

    def _read(self, device):
        import evdev
        if not self.recording:
            return
        try:
            events = list(device.read())
        except OSError:
            return
        for event in events:
            if event.type != evdev.ecodes.EV_KEY:
                continue
            # A click that just operated this dialog's own UI.
            if event.code in self.MOUSE_BUTTONS \
                    and time.monotonic() - self._ui_click_at < 0.25:
                self._held.pop(event.code, None)
                continue
            name = key_name(event.code)
            if event.value == 1:
                if not self._held:
                    self._chord = []
                    self._chord_started = time.monotonic()
                self._held[event.code] = name
                if name not in self._chord:
                    self._chord.append(name)
            elif event.value == 0:
                self._held.pop(event.code, None)
                if not self._held and self._chord:
                    # The gap is only committed now, together with the step it
                    # precedes. Recording it when the chord *started* left an
                    # orphaned wait behind whenever the chord was later discarded
                    # — which is exactly what a click on this dialog does.
                    self._append_gap()
                    self.steps.append({"keys": list(self._chord),
                                       "at": time.monotonic()})
                    self._chord = []
                    self._last_release = time.monotonic()
                    self.refresh()
                    if self.single:
                        self.status.setText(
                            "<b style='color:#2e7d32'>captured</b> "
                            f"{self.result_text()}")
                        # Give the release a moment to settle, then hand it back.
                        QTimer.singleShot(120, self.accept)
                        return

    def fixed_delay_ms(self):
        text = self.fixed_delay_field.text().strip()
        value = int(text) if text.isdigit() else 100
        return max(1, min(value, self.MAX_WAIT_MS))

    def _append_gap(self):
        """The pause before the chord that has just completed.

        Measured from the previous release to when this chord began, so holding
        a key down doesn't count as part of the pause before it.
        """
        if self.fixed_delay_check.isChecked():
            # Same pause every time, regardless of how long the user took.
            if self.steps and "wait" not in self.steps[-1]:
                self.steps.append({"wait": self.fixed_delay_ms()})
            return
        if self._last_release is None or self._chord_started is None:
            return
        gap = int((self._chord_started - self._last_release) * 1000)
        if self.MIN_WAIT_MS <= gap <= self.MAX_WAIT_MS:
            self.steps.append({"wait": gap})

    def toggle_recording(self):
        self.stop() if self.recording else self.start()

    def start(self):
        """Resume recording, appending to the steps already listed."""
        if self.recording:
            return
        self._open(self.paths)
        if not self._devices:
            return
        self._held.clear()
        self._chord = []
        self._last_release = None      # don't turn the pause into a wait step
        self.recording = True
        self._remaining = 120
        self._deadline.start(120_000)
        self.update_recording_state()

    def update_recording_state(self):
        if self.single:
            self.status.setText(
                "<b style='color:#d13438'>● listening</b> — press a key")
            return
        self.stop_button.setText("Stop" if self.recording else "Start recording")
        self.stop_button.setToolTip(
            "Stop recording so the steps can be edited" if self.recording
            else "Record more steps, appended to the list below")
        if self.recording:
            self.status.setText("<b style='color:#d13438'>● recording</b>")
        elif self.steps:
            self.status.setText(
                "<b style='color:#2e7d32'>■ stopped</b> — edit the steps, or "
                "<b>Start recording</b> to add more")
        else:
            self.status.setText(
                "<b>■ not recording</b> — press <b>Start recording</b> to begin")

    def drop_trailing_ui_click(self):
        """Remove a click on this dialog that slipped into the macro.

        Belt and braces behind the event-filter suppression. It only fires when
        the last step was created *by* the click that landed on this dialog —
        the step's timestamp is after the moment Qt delivered that click — so a
        click the user deliberately recorded a moment earlier is never eaten.
        """
        if not self.steps or not self._ui_click_at:
            return
        if time.monotonic() - self._ui_click_at > 1.5:
            return
        last = self.steps[-1]
        keys = last.get("keys") or []
        created_by_that_click = last.get("at", 0) >= self._ui_click_at
        if created_by_that_click and keys \
                and all(name.startswith("BTN_") for name in keys):
            self.steps.pop()
            if self.steps and "wait" in self.steps[-1]:
                self.steps.pop()

    def stop(self):
        if self.recording:
            self.drop_trailing_ui_click()
        self.recording = False
        self._deadline.stop()
        self._release_devices()
        self.update_recording_state()
        self.refresh()

    def refresh(self):
        row = self.list.currentRow()
        self.list.clear()
        for step in self.steps:
            if "wait" in step:
                self.list.addItem(f"wait  {step['wait']} ms")
            else:
                keys = step["keys"]
                label = " + ".join(keys)
                self.list.addItem(f"press {label}" + ("   (chord)" if len(keys) > 1 else ""))
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        self.preview.setText(self.macro_text())

    def macro_text(self):
        parts = []
        for step in self.steps:
            if "wait" in step:
                parts.append(f"wait({step['wait']})")
            elif len(step["keys"]) == 1:
                parts.append(f"key({step['keys'][0]})")
            else:
                parts.append("hold_keys(" + ", ".join(step["keys"]) + ")")
        return ".".join(parts)

    def result_text(self):
        """A lone keypress is nicer expressed as the bare symbol."""
        if not self.steps and self.unparsed:
            return self.unparsed      # nothing recorded: keep what was there
        if len(self.steps) == 1 and "keys" in self.steps[0] \
                and len(self.steps[0]["keys"]) == 1:
            return self.steps[0]["keys"][0]
        return self.macro_text()

    def delete_step(self):
        row = self.list.currentRow()
        if not 0 <= row < len(self.steps):
            return
        del self.steps[row]
        self.refresh()
        if self.steps:   # keep a row selected so Delete can be pressed repeatedly
            self.list.setCurrentRow(min(row, len(self.steps) - 1))
            self.list.setFocus()

    def retime_step(self):
        row = self.list.currentRow()
        text = self.wait_field.text().strip()
        if not (0 <= row < len(self.steps)) or "wait" not in self.steps[row]:
            return
        if text.isdigit():
            self.steps[row]["wait"] = max(1, min(int(text), self.MAX_WAIT_MS))
            self.refresh()

    def _release_devices(self):
        for device, notifier in self._devices:
            notifier.setEnabled(False)
            device.close()   # never grabbed, so nothing to release
        self._devices = []

    def eventFilter(self, obj, event):
        """Swallow keystrokes while recording so they don't drive the dialog.

        Devices are never grabbed, so presses still reach the window; this stops
        them landing in the wait field or triggering buttons. Mouse events pass
        through untouched so Stop and Cancel stay clickable — the click that
        presses Stop is stripped from the macro afterwards instead.
        """
        if not self.recording:
            return super().eventFilter(obj, event)

        types = event.Type
        if event.type() in (types.KeyPress, types.KeyRelease, types.ShortcutOverride):
            return True
        # Mouse events pass through so the buttons keep working — but note when
        # one lands on this dialog, so the same physical click arriving from
        # evdev a moment later isn't recorded as part of the macro. This covers
        # every button (Stop, Use this macro, Delete step…), not just Stop.
        if event.type() in (types.MouseButtonPress, types.MouseButtonRelease,
                            types.MouseButtonDblClick):
            if isinstance(obj, QWidget) and self.isAncestorOf(obj):
                self._ui_click_at = time.monotonic()
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        self.recording = False
        self._deadline.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._release_devices()
        super().closeEvent(event)

    def reject(self):
        self.close()
        super().reject()

    def accept(self):
        # "Use this macro" can be pressed while still recording, and that click
        # would otherwise be the last thing in the macro.
        if self.recording:
            self.drop_trailing_ui_click()
            self.recording = False
        self.close()
        super().accept()


class PresetEditor(QDialog):
    """Edit an Input Remapper preset's mappings.

    Only the fields worth touching day to day are shown. Anything else already
    on a mapping (release_timeout, deadzone, gain, …) is carried through
    untouched on save rather than being editable here — Input Remapper's own GUI
    is the place for those.
    """

    def __init__(self, device, preset, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit preset")
        self.api = import_remapper()
        self.device = device
        self.preset_name = preset
        self.mappings = []
        self.loading = False
        self.manual_targets = set()

        # Nothing should be remapping the hardware while it's being mapped: a
        # game starting mid-edit would re-grab the device and recordings would
        # come through already translated. Held for the whole session rather
        # than per-recording, so profiles don't flicker back between takes.
        self.switcher_was_up = (
            systemctl("is-active", SERVICE, check_output=True) == "active")
        if self.switcher_was_up:
            systemctl("stop", SERVICE)
        stop_injections()

        layout = QVBoxLayout(self)
        paused = QLabel(
            "<span style='color:#c07000'>⏸ Remapping is paused while this window "
            "is open — your devices behave normally, and profiles resume when you "
            "close it.</span>")
        paused.setWordWrap(True)
        layout.addWidget(paused)
        if not self.api:
            layout.addWidget(QLabel(
                "Input Remapper's Python package could not be imported, so presets "
                "cannot be edited here."))
            return

        top = QHBoxLayout()
        top.addWidget(QLabel("Device:"))
        self.device_combo = QComboBox()
        self.device_combo.addItems(known_devices())
        self.device_combo.setCurrentText(device)
        top.addWidget(self.device_combo, 1)
        top.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        top.addWidget(self.preset_combo, 1)
        layout.addLayout(top)

        split = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Mappings</b>"))
        self.list = QListWidget()
        self.list.setMinimumHeight(200)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        for label, slot in (("Add", self.add_mapping), ("Delete", self.delete_mapping)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        left.addLayout(row)
        split.addLayout(left, 2)

        right = QVBoxLayout()
        form = QGroupBox("Mapping")
        inner = QVBoxLayout(form)

        inner.addWidget(QLabel("<b>Input</b> — the key you press on this device"))
        input_row = QHBoxLayout()
        self.input_field = QLineEdit(readOnly=True)
        input_row.addWidget(self.input_field, 1)
        self.record_input = QPushButton("Record…")
        self.record_input.clicked.connect(self.do_record_input)
        input_row.addWidget(self.record_input)
        inner.addLayout(input_row)

        inner.addWidget(QLabel("<b>Output</b> — a key name, or a macro like "
                               "<tt>hold(KEY_A)</tt>"))
        output_row = QHBoxLayout()
        self.output_field = QLineEdit()
        self.output_field.textEdited.connect(lambda _t: self.pull_form())
        output_row.addWidget(self.output_field, 1)
        self.record_key = QPushButton("Single key")
        self.record_key.setToolTip(
            "Press one key (or a chord) and use it as the output — no macro "
            "screen, no timing.")
        self.record_key.clicked.connect(self.do_record_single_key)
        output_row.addWidget(self.record_key)

        self.record_output = QPushButton("Macro")
        self.record_output.setToolTip(
            "Open the macro editor: review the existing steps, then record more "
            "keys, chords and the pauses between them.")
        self.record_output.clicked.connect(self.do_record_output)
        output_row.addWidget(self.record_output)
        inner.addLayout(output_row)

        hold_row = QHBoxLayout()
        self.hold_check = QCheckBox("Loop if held — repeat while the input key is "
                                    "down (wraps the output in hold(…))")
        self.hold_check.toggled.connect(self.toggle_hold)
        hold_row.addWidget(self.hold_check)
        hold_row.addWidget(QLabel("loop delay:"))
        self.loop_delay_field = QLineEdit()
        self.loop_delay_field.setFixedWidth(70)
        self.loop_delay_field.setPlaceholderText("ms")
        self.loop_delay_field.setToolTip(
            "Pause added at the end of each pass through the loop, so it repeats "
            "at this interval instead of as fast as possible.")
        self.loop_delay_field.textEdited.connect(lambda _t: self.rebuild_output())
        hold_row.addWidget(self.loop_delay_field)
        hold_row.addWidget(QLabel("ms"))
        hold_row.addStretch(1)
        inner.addLayout(hold_row)

        combos = QHBoxLayout()
        combos.addWidget(QLabel("Sends to:"))
        self.target_combo = QComboBox()
        self.target_combo.addItems([e.value for e in self.api["KnownUinput"]])
        self.target_combo.setToolTip(
            "Detected from the output: keyboard keys send to the keyboard, mouse "
            "buttons to the mouse, a mix to both. Choosing one yourself stops it "
            "being adjusted for this mapping.")
        self.target_combo.currentTextChanged.connect(lambda _t: self.pull_form())
        # activated fires only on real user interaction, not programmatic changes.
        self.target_combo.activated.connect(self.target_chosen_manually)
        combos.addWidget(self.target_combo, 1)
        self.target_note = QLabel()
        combos.addWidget(self.target_note)
        combos.addWidget(QLabel("Type:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["(auto)"] + [e.value for e in self.api["MappingType"]])
        self.type_combo.currentTextChanged.connect(lambda _t: self.pull_form())
        combos.addWidget(self.type_combo, 1)
        inner.addLayout(combos)

        # Analog only matters for axis mappings (a thumbstick driving the cursor
        # or the wheel), and its tuning knobs are useless without it — so the
        # whole lot lives together and only appears when Type is analog.
        self.analog_group = QGroupBox("Analog output — advanced")
        analog_layout = QVBoxLayout(self.analog_group)
        analog_layout.addWidget(QLabel(
            "<i>For axis mappings: the input's movement drives a continuous "
            "output instead of a keypress. Leave the Output field empty.</i>"))

        axis = QHBoxLayout()
        axis.addWidget(QLabel("Event type:"))
        self.output_type = QComboBox()
        self.output_type.setEditable(True)
        self.output_type.addItems(["", "2  (EV_REL — relative, e.g. mouse/wheel)",
                                   "3  (EV_ABS — absolute, e.g. joystick)"])
        self.output_type.currentTextChanged.connect(lambda _t: self.pull_form())
        axis.addWidget(self.output_type, 1)
        axis.addWidget(QLabel("code:"))
        self.output_code = QComboBox()
        self.output_code.setEditable(True)
        self.output_code.addItems(["", "0  (X)", "1  (Y)", "8  (wheel)",
                                   "6  (horizontal wheel)"])
        self.output_code.currentTextChanged.connect(lambda _t: self.pull_form())
        axis.addWidget(self.output_code, 1)
        analog_layout.addLayout(axis)

        self.analog_fields = {}
        for name, hint in (
            ("deadzone", "ignore movement smaller than this (0–1)"),
            ("gain", "output speed multiplier"),
            ("expo", "curve: 0 is linear, higher is finer near the centre"),
            ("rel_rate", "events per second for relative output"),
            ("rel_to_abs_input_cutoff", "input speed treated as full deflection"),
        ):
            if name not in self.api["UIMapping"].__fields__:
                continue
            line = QHBoxLayout()
            label = QLabel(name.replace("_", " ") + ":")
            label.setMinimumWidth(160)
            line.addWidget(label)
            widget = QLineEdit()
            widget.setPlaceholderText(hint)
            widget.textEdited.connect(lambda _t: self.pull_form())
            self.analog_fields[name] = widget
            line.addWidget(widget, 1)
            analog_layout.addLayout(line)

        inner.addWidget(self.analog_group)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        inner.addWidget(self.error_label)
        right.addWidget(form, 1)
        split.addLayout(right, 3)
        layout.addLayout(split)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save && apply")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.list.currentRowChanged.connect(self.show_mapping)
        self.device_combo.currentTextChanged.connect(self.device_changed)
        self.preset_combo.currentTextChanged.connect(self.preset_changed)
        self.device_changed(device)

    # ---------- loading ----------

    @guard
    def device_changed(self, device):
        self.device = device
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(presets_for(device))
        if self.preset_name in presets_for(device):
            self.preset_combo.setCurrentText(self.preset_name)
        self.preset_combo.blockSignals(False)
        self.preset_changed(self.preset_combo.currentText())

    @guard
    def preset_changed(self, preset):
        if not preset:
            return
        self.preset_name = preset
        path = PRESET_DIR / self.device / f"{preset}.json"
        self.mappings = []
        try:
            loaded = self.api["Preset"](path, mapping_factory=self.api["UIMapping"])
            loaded.load()
            self.mappings = list(loaded)
        except Exception as exc:
            self.error_label.setText(f"<span style='color:#d13438'>{exc}</span>")
        self.refresh_list()

    def refresh_list(self, select=0):
        self.list.blockSignals(True)
        self.list.clear()
        for mapping in self.mappings:
            self.list.addItem(self.describe(mapping))
        self.list.blockSignals(False)
        if self.mappings:
            self.list.setCurrentRow(min(select, len(self.mappings) - 1))
        else:
            self.show_mapping(-1)

    def describe(self, mapping):
        # A fresh mapping carries a placeholder combination; don't present that
        # as if the user had recorded it.
        if mapping.has_input_defined():
            keys = " + ".join(event_label(c.type, c.code)
                              for c in mapping.input_combination)
        else:
            keys = "(press Record…)"
        if mapping.output_symbol:
            output = mapping.output_symbol
        elif mapping.output_type is not None or mapping.output_code is not None:
            output = event_label(mapping.output_type, mapping.output_code)
        else:
            output = "(not set)"
        return f"{keys}   →   {output}"

    # ---------- form ----------

    def current(self):
        row = self.list.currentRow()
        return self.mappings[row] if 0 <= row < len(self.mappings) else None

    @guard
    def show_mapping(self, _row):
        mapping = self.current()
        self.loading = True
        enabled = mapping is not None
        for widget in (self.input_field, self.output_field, self.record_input,
                       self.record_output, self.target_combo, self.type_combo,
                       self.output_type, self.output_code, self.hold_check,
                       self.record_key):
            widget.setEnabled(enabled)
        if mapping is None:
            self.input_field.clear(); self.output_field.clear()
            self.loading = False
            return

        self.input_field.setText(
            " + ".join(event_label(c.type, c.code) for c in mapping.input_combination)
            if mapping.has_input_defined() else "")
        self.output_field.setText(mapping.output_symbol or "")
        symbol = mapping.output_symbol or ""
        self.hold_check.setChecked(is_held(symbol))
        _base, loop_delay = split_trailing_wait(unwrap_hold(symbol)) if is_held(symbol) \
            else (symbol, None)
        self.loop_delay_field.setText("" if loop_delay is None else str(loop_delay))
        self.target_combo.setCurrentText(mapping.target_uinput or "keyboard")
        # mapping_type comes back as an enum or a plain string depending on how
        # the preset was written, so accept either.
        mapping_type = mapping.mapping_type
        self.type_combo.setCurrentText(
            getattr(mapping_type, "value", mapping_type) or "(auto)")
        self.set_numeric_combo(self.output_type, mapping.output_type)
        self.set_numeric_combo(self.output_code, mapping.output_code)
        for name, widget in self.analog_fields.items():
            value = getattr(mapping, name, None)
            widget.setText("" if value is None else str(value))
        self.loading = False
        self.target_note.setText(
            "<i>set by hand</i>" if id(mapping) in self.manual_targets else "<i>auto</i>")
        self.update_analog_visibility()
        self.validate_mapping()

    def target_chosen_manually(self, _index):
        """Remember that this mapping's target was set by hand, and leave it be."""
        mapping = self.current()
        if mapping is not None:
            self.manual_targets.add(id(mapping))
            self.target_note.setText("<i>set by hand</i>")

    def auto_target(self, mapping):
        """Point the mapping at whichever virtual device suits its output."""
        if mapping is None or id(mapping) in self.manual_targets:
            return
        detected = detect_target(mapping.output_symbol, mapping.output_type,
                                 mapping.output_code)
        if not detected or detected == mapping.target_uinput:
            self.target_note.setText("<i>auto</i>" if detected else "")
            return
        try:
            mapping.target_uinput = detected
        except Exception:
            return
        self.target_combo.blockSignals(True)
        self.target_combo.setCurrentText(detected)
        self.target_combo.blockSignals(False)
        self.target_note.setText("<i>auto</i>")

    def set_numeric_combo(self, combo, value):
        """Show the labelled entry matching a numeric value, or the bare number."""
        combo.blockSignals(True)
        if value is None:
            combo.setCurrentText("")
        else:
            for index in range(combo.count()):
                if leading_int(combo.itemText(index)) == value:
                    combo.setCurrentIndex(index)
                    break
            else:
                combo.setCurrentText(str(value))
        combo.blockSignals(False)

    def update_analog_visibility(self):
        """Only show the analog block when it's actually in play.

        Shown when the mapping type is analog, or when the mapping already
        carries analog values — never hide something that is in use.
        """
        mapping = self.current()
        in_use = mapping is not None and (
            mapping.output_type is not None or mapping.output_code is not None)
        self.analog_group.setVisible(
            self.type_combo.currentText() == "analog" or in_use)

    @guard
    def pull_form(self):
        """Copy the form back onto the selected mapping and re-validate."""
        if self.loading:
            return
        mapping = self.current()
        if mapping is None:
            return

        def assign(field, value):
            try:
                setattr(mapping, field, value)
            except Exception:
                pass  # pydantic rejected it; validate_mapping() reports why

        assign("output_symbol", self.output_field.text().strip() or None)
        assign("target_uinput", self.target_combo.currentText())
        chosen = self.type_combo.currentText()
        assign("mapping_type", None if chosen == "(auto)" else chosen)
        for field, widget in (("output_type", self.output_type),
                              ("output_code", self.output_code)):
            assign(field, leading_int(widget.currentText()))
        for name, widget in self.analog_fields.items():
            text = widget.text().strip()
            if not text:
                continue
            try:
                default = self.api["UIMapping"].__fields__[name].default
                assign(name, int(float(text)) if isinstance(default, int)
                       and not isinstance(default, bool) else float(text))
            except (ValueError, KeyError):
                pass
        self.update_analog_visibility()
        self.auto_target(mapping)
        row = self.list.currentRow()
        if 0 <= row < self.list.count():
            self.list.blockSignals(True)
            self.list.item(row).setText(self.describe(mapping))
            self.list.blockSignals(False)
        self.validate_mapping()

    @guard
    def validate_mapping(self):
        mapping = self.current()
        if mapping is None:
            self.error_label.clear()
            return
        # A mapping you have only just added isn't "wrong" yet — say what it
        # still needs rather than dumping the validator's complaints.
        if not mapping.has_input_defined():
            self.error_label.setText(
                "<span style='color:#c07000'>New mapping — press <b>Record…</b> "
                "next to Input to assign a key, then set an output.</span>")
            return
        if not (mapping.output_symbol or mapping.output_type is not None):
            self.error_label.setText(
                "<span style='color:#c07000'>Now set an output — type a key name "
                "or press <b>Record…</b> next to Output.</span>")
            return

        if uses_left_click_input(mapping) and disables_left_click(self.mappings):
            self.error_label.setText(
                "<span style='color:#d13438'>⚠ This takes over <b>left click</b> "
                "and nothing gives it back — you would be left unable to click. "
                "Map something in this preset to <tt>BTN_LEFT</tt> first.</span>")
            return

        error = mapping.get_error()
        if error:
            first = str(error).strip().splitlines()
            detail = " ".join(line.strip() for line in first[1:4]) or first[0]
            self.error_label.setText(
                f"<span style='color:#d13438'>⚠ {detail}</span>")
        else:
            self.error_label.setText("<span style='color:#2e7d32'>✓ valid</span>")

    # ---------- recording ----------

    @guard
    def do_record_input(self):
        mapping = self.current()
        if mapping is None:
            return
        nodes = device_nodes(self.device)
        if not nodes:
            QMessageBox.warning(self, "Device not connected",
                                f"“{self.device}” isn't connected right now.")
            return

        # Remapping is already paused for the whole editor session; just make
        # sure nothing has re-grabbed the device since.
        stop_injections()
        try:
            recorder = Recorder(
                [path for path, _h in nodes], "Record input",
                f"Press the key (or several at once) on <b>{self.device}</b>.<br><br>"
                "Injection is paused while recording, and resumes when you're done."
                "<br><br><i>This device is captured exclusively, so if it's a mouse "
                "its pointer pauses — press Esc to cancel.</i>",
                combination=True, parent=self)
            accepted = recorder.exec()
        finally:
            pass

        if accepted and recorder.result_keys:
            configs = [self.api["InputConfig"](type=t, code=c, origin_hash=h)
                       for t, c, h in recorder.result_keys]
            try:
                mapping.input_combination = self.api["InputCombination"](configs)
            except Exception as exc:
                QMessageBox.warning(self, "Could not set input", str(exc))
            if uses_left_click_input(mapping):
                QMessageBox.warning(
                    self, "That's the left mouse button",
                    "You've bound left click as the input.\n\n"
                    "Unless something in this preset outputs BTN_LEFT, applying it "
                    "will leave you unable to click anything — including the window "
                    "you'd need to undo it.\n\n"
                    "Set this mapping's output to BTN_LEFT, or add another mapping "
                    "that produces one.")
            self.show_mapping(self.list.currentRow())
            self.list.item(self.list.currentRow()).setText(self.describe(mapping))

    @guard
    def do_record_single_key(self):
        """Capture one key or chord straight into the output."""
        mapping = self.current()
        if mapping is None:
            return
        try:
            import evdev
            sources = evdev.list_devices()
        except ImportError:
            sources = []

        stop_injections()
        time.sleep(0.2)
        recorder = MacroRecorder(sources, parent=self, single=True)
        if not recorder.exec():
            return
        text = recorder.result_text()
        if not text:
            return
        self.output_field.setText(
            build_output(text, self.hold_check.isChecked(), self.loop_delay_ms()))
        self.pull_form()

    @guard
    def do_record_output(self):
        mapping = self.current()
        if mapping is None:
            return
        # Listen to every device, including the one being remapped: its own
        # buttons are a perfectly reasonable output (a Naga side button mapped to
        # a Naga click), and excluding it made clicks unrecordable for anyone
        # whose only mouse is the device they're editing.
        try:
            import evdev
            sources = evdev.list_devices()
        except ImportError:
            sources = []

        # Remapping is paused for the session, so devices report their raw keys.
        stop_injections()
        time.sleep(0.2)   # let the daemon drop any grab before we start reading
        # A trailing wait only belongs to the loop delay when the output is a
        # hold(...) — then it is re-applied on the way back, so leaving it in
        # would return as an extra step and be appended twice. Without hold it
        # is an ordinary part of the macro and must stay visible in the editor.
        current = self.output_field.text()
        if is_held(current):
            base, _delay = split_trailing_wait(unwrap_hold(current))
        else:
            base = current
        recorder = MacroRecorder(sources, parent=self, initial=base)
        accepted = recorder.exec()
        if not accepted:
            return
        text = recorder.result_text()
        if not text:
            return
        # Keep whatever the Loop-if-held tick and its delay were set to.
        self.output_field.setText(
            build_output(text, self.hold_check.isChecked(), self.loop_delay_ms()))
        self.pull_form()

    def loop_delay_ms(self):
        text = self.loop_delay_field.text().strip()
        return max(1, min(int(text), 60_000)) if text.isdigit() else None

    @guard
    def rebuild_output(self):
        """Re-assemble the output from its base macro, the hold and its delay."""
        if self.loading:
            return
        base, _old = split_trailing_wait(unwrap_hold(self.output_field.text().strip()))
        rebuilt = build_output(base, self.hold_check.isChecked(), self.loop_delay_ms())
        if rebuilt != self.output_field.text():
            self.output_field.setText(rebuilt)
        self.pull_form()

    @guard
    def toggle_hold(self, _checked):
        self.rebuild_output()

    # ---------- mappings ----------

    @guard
    def add_mapping(self):
        # Seed the target so a new row isn't invalid on two counts at once.
        mapping = self.api["UIMapping"](target_uinput="keyboard")
        self.mappings.append(mapping)
        self.refresh_list(select=len(self.mappings) - 1)
        self.list.setCurrentRow(len(self.mappings) - 1)

    @guard
    def delete_mapping(self):
        row = self.list.currentRow()
        if not 0 <= row < len(self.mappings):
            return
        del self.mappings[row]
        self.refresh_list(select=max(0, row - 1))

    def resume_remapping(self):
        """Start the switcher again — whatever way this window was dismissed.

        Restarting it is also what re-applies the current profile, since its
        last-applied cache dies with the process.
        """
        if getattr(self, "switcher_was_up", False):
            self.switcher_was_up = False
            systemctl("start", SERVICE)

    def closeEvent(self, event):
        self.resume_remapping()
        super().closeEvent(event)

    def reject(self):
        self.resume_remapping()
        super().reject()

    def accept(self):
        self.resume_remapping()
        super().accept()

    @guard
    def save(self):
        path = PRESET_DIR / self.device / f"{self.preset_name}.json"
        invalid = [m for m in self.mappings if not m.is_valid()]
        if invalid:
            answer = QMessageBox.question(
                self, "Incomplete mappings",
                f"{len(invalid)} mapping(s) are incomplete and will not be saved "
                f"(first problem: {invalid[0].get_error()}).\n\nSave the rest?")
            if answer != QMessageBox.StandardButton.Yes:
                return

        if disables_left_click(self.mappings):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("This would disable left click")
            box.setText("This preset remaps the left mouse button and nothing "
                        "in it produces a left click.")
            box.setInformativeText(
                "While it's applied you would not be able to click anything — "
                "including this window, or Input Remapper, to undo it.\n\n"
                "Recovering usually means a keyboard-only session or editing the "
                "preset file by hand.\n\n"
                "Add a mapping whose output is BTN_LEFT, or map left click to "
                "itself, and it's safe.")
            box.setStandardButtons(QMessageBox.StandardButton.Cancel
                                   | QMessageBox.StandardButton.Save)
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            box.button(QMessageBox.StandardButton.Save).setText(
                "Save anyway — I have another way to click")
            if box.exec() != QMessageBox.StandardButton.Save:
                return

        if path.is_file():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = backup_dir() / self.device
            try:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target / f"{path.name}.{stamp}")
            except OSError:
                pass

        duplicates = []
        try:
            preset = self.api["Preset"](path, mapping_factory=self.api["UIMapping"])
            # empty(), not clear() — clear() also drops Preset.path, after which
            # save() silently does nothing.
            preset.empty()
            for mapping in self.mappings:
                if not mapping.is_valid():
                    continue
                try:
                    preset.add(mapping)
                except KeyError:
                    duplicates.append(
                        " + ".join(key_name(c.code) for c in mapping.input_combination))
            preset.save()
        except Exception as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return

        if not path.is_file():
            QMessageBox.critical(self, "Could not save",
                                 f"{path} was not written.")
            return
        if duplicates:
            QMessageBox.warning(
                self, "Duplicate inputs",
                "These inputs are mapped more than once; only the first was kept:\n\n"
                + "\n".join(f"  • {d}" for d in duplicates))

        # Bring the switcher back before reloading, so the reload sees it running
        # and restarts it — which is what re-applies the freshly saved preset.
        self.resume_remapping()
        parent = self.parent()
        note = parent.run_reload() if hasattr(parent, "run_reload") else ""
        QMessageBox.information(
            self, "Saved",
            f"“{self.preset_name}” saved.\n\n{note}" if note else "Saved.")
        self.accept()


class NoScrollComboBox(QComboBox):
    """A dropdown that ignores the wheel while closed.

    In a scrolling list it is far too easy to spin the wheel over a row and
    silently repoint a game at a different preset. Scrolling passes through to
    the list instead; the dropdown still scrolls once its popup is open.
    """

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class PresetCell(QWidget):
    """A preset dropdown with an edit button beside it.

    Presents the same currentText/currentTextChanged surface as the plain
    QComboBox it replaces, so the table code doesn't need to care.
    """

    def __init__(self, presets, value, on_edit):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.combo = NoScrollComboBox()
        self.combo.setEditable(True)
        items = list(presets)
        if value and value not in items:
            items.append(value)
        self.combo.addItems(items)
        self.combo.setCurrentText(value)
        layout.addWidget(self.combo, 1)

        self.edit_button = QToolButton()
        self.edit_button.setText("✎")
        self.edit_button.setToolTip("Edit this preset's keys")
        self.edit_button.clicked.connect(lambda: on_edit(self))
        layout.addWidget(self.edit_button)

        self.currentTextChanged = self.combo.currentTextChanged

    def currentText(self):
        return self.combo.currentText()

    def setCurrentText(self, text):
        self.combo.setCurrentText(text)


class SteamPicker(QDialog):
    """Pick games from the installed Steam libraries to add as mappings."""

    def __init__(self, existing, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add from Steam library")
        self.resize(900, 620)
        self.existing = {normalize(p) for p in existing if p}
        self.chosen = []

        layout = QVBoxLayout(self)
        self.info = QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        self.filter = QLineEdit(placeholderText="Filter games…")
        layout.addWidget(self.filter)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Game", "Process to match", "Detected as"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.create_presets = QCheckBox(
            f"Create a preset for each game by copying “{DEFAULT_PRESET}”, named after "
            "the Steam game")
        self.create_presets.setChecked(True)
        self.create_presets.setToolTip(
            "Copies <device>/Default.json to <device>/<game name>.json for any game "
            "that has no preset yet. Existing presets are never overwritten.")
        layout.addWidget(self.create_presets)

        self.reload_after = QCheckBox(
            "Save config.txt afterwards and restart Input Remapper + the auto-switch "
            "service, so the new mappings apply straight away")
        self.reload_after.setChecked(True)
        self.reload_after.setToolTip(
            "Input Remapper only sees preset files that existed when it started, and "
            "the switcher re-reads config.txt on start — so the file has to be written "
            "before either is restarted.")
        layout.addWidget(self.reload_after)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        rescan = buttons.addButton("Rescan", QDialogButtonBox.ButtonRole.ActionRole)
        check_all = buttons.addButton("Check all", QDialogButtonBox.ButtonRole.ActionRole)
        check_none = buttons.addButton("Uncheck all", QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        rescan.clicked.connect(self.scan)
        check_all.clicked.connect(lambda: self.set_all(True))
        check_none.clicked.connect(lambda: self.set_all(False))
        self.filter.textChanged.connect(self.apply_filter)

        self.scan()

    def scan(self):
        self.table.setRowCount(0)
        apps = steam_apps()
        libs = steam_libraries()
        skipped = 0

        for _appid, name, path in apps:
            candidates = executable_candidates(path)
            if not candidates:
                skipped += 1
                continue
            best = candidates[0]
            already = normalize(best[1]) in self.existing

            r = self.table.rowCount()
            self.table.insertRow(r)

            item = QTableWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if already
                               else Qt.CheckState.Checked)
            if already:
                item.setToolTip("Already in your config")
                item.setForeground(QBrush(QColor("#888888")))
            self.table.setItem(r, 0, item)

            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems([c[1] for c in candidates])
            for i, c in enumerate(candidates):
                combo.setItemData(i, c[2], Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(0)
            self.table.setCellWidget(r, 1, combo)

            note = "already configured" if already else best[3]
            self.table.setItem(r, 2, QTableWidgetItem(note))

        self.info.setText(
            f"<b>{self.table.rowCount()}</b> game(s) across <b>{len(libs)}</b> Steam "
            f"librar{'y' if len(libs) == 1 else 'ies'}"
            + (f", {skipped} with no detectable executable" if skipped else "")
            + ".<br>Unreal games are matched on the real "
            "<tt>…/Binaries/Win64/…-Win64-Shipping.exe</tt>, not the stub in the install "
            "root. The process column is a dropdown of the other binaries found, and is "
            "editable if none of them are right.")
        self.apply_filter(self.filter.text())

    def set_all(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            if not self.table.isRowHidden(r):
                self.table.item(r, 0).setCheckState(state)

    def apply_filter(self, text):
        text = text.lower()
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(r, text not in self.table.item(r, 0).text().lower())

    def accept(self):
        self.chosen = [
            (self.table.item(r, 0).text(), self.table.cellWidget(r, 1).currentText().strip())
            for r in range(self.table.rowCount())
            if self.table.item(r, 0).checkState() == Qt.CheckState.Checked
            and self.table.cellWidget(r, 1).currentText().strip()
        ]
        super().accept()


class FixAssignmentsDialog(QDialog):
    """Show presets whose mappings point at a device node that no longer exists."""

    def __init__(self, findings, problems, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fix profile assignments")
        self.resize(860, 520)
        self.findings = findings

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Input Remapper stamps every mapping with an ID for the exact input "
            "device node it was recorded from. That ID is derived from the device's "
            "reported capabilities, so a kernel, driver or firmware update can change "
            "it — and mappings recorded before that keep pointing at a node that no "
            "longer exists. The key still shows in Input Remapper, it just never "
            "fires. Re-recording it by hand is the usual workaround; this does the "
            "same thing to every affected mapping at once.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        if problems:
            note = QLabel("⚠ " + "<br>".join(problems))
            note.setWordWrap(True)
            note.setStyleSheet("color:#d13438")
            layout.addWidget(note)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Preset", "Broken mappings", "Repoint to", "Why that node"])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        for finding in findings:
            r = self.table.rowCount()
            self.table.insertRow(r)
            item = QTableWidgetItem(f"{finding['preset']}   ({finding['device']})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, item)
            self.table.setItem(r, 1, QTableWidgetItem(str(finding["count"])))
            self.table.setItem(r, 2, QTableWidgetItem(finding["node"]))
            evidence = (f"{finding['evidence']} working mapping(s) already use it"
                        if finding["evidence"] else "best capability match")
            self.table.setItem(r, 3, QTableWidgetItem(evidence))

        if not findings:
            layout.addWidget(QLabel(
                "<b>Nothing to fix</b> — every mapping points at a device that is "
                "currently connected."))

        layout.addWidget(QLabel(
            "<b>Input Remapper and the auto-switch service are restarted "
            "afterwards</b>, so the repaired presets are picked up immediately."))

        layout.addWidget(QLabel(
            f"<i>Originals are copied to {backup_dir()} first.</i>"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Fix selected")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(findings))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        return [f for r, f in enumerate(self.findings)
                if self.table.item(r, 0).checkState() == Qt.CheckState.Checked]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(DISPLAY_NAME)
        self.setToolTip(f"Editing {CONFIG_FILE}")
        # Opens at the smallest size the layout allows (log collapsed); the
        # table keeps enough height to be usable, and it's freely resizable.
        self.devices = known_devices()
        self.all_rows = []          # every mapping, all devices
        self.current_device = ""    # the one the table is showing
        self.declined_defaults = set()
        self.loading_device = False
        self.dirty = False
        self.log_proc = None

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_mappings())
        splitter.addWidget(self._build_service())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self.status = self.statusBar()
        self.load_config()
        self.refresh_fix_indicator()
        self.warn_about_other_configs()

        self.table.setMinimumHeight(240)   # ~7 rows, so the minimum stays usable
        smallest = self.minimumSizeHint()
        self.setMinimumSize(smallest)      # dragging smaller would overlap widgets
        self.resize(smallest)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_service_status)
        self.timer.start(3000)
        self.refresh_service_status()
        self.start_log()

    # ---------------- mappings ----------------

    def _build_mappings(self):
        box = QGroupBox("Profile mappings — checked top to bottom, first running "
                        "process wins; DEFAULT applies when nothing else matches")
        outer = QVBoxLayout(box)

        device_bar = QHBoxLayout()
        device_bar.addWidget(QLabel("<b>Device:</b>"))
        self.device_selector = QComboBox()
        self.device_selector.setMinimumWidth(320)
        self.device_selector.setToolTip(
            "Everything below applies to this device. Mappings for other devices "
            "are kept, just hidden.")
        self.device_selector.currentTextChanged.connect(self.device_selected)
        device_bar.addWidget(self.device_selector)
        self.device_count_label = QLabel()
        device_bar.addWidget(self.device_count_label)
        device_bar.addStretch(1)
        outer.addLayout(device_bar)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Process", "Preset"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.table.horizontalHeader()
        # Share the width between the two columns so there's no dead space,
        # and let either be dragged from the divider.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self.on_item_changed)
        outer.addWidget(self.table, 1)

        # A device with presets but no mappings would otherwise just show a
        # blank table, which reads as "my presets are missing".
        self.empty_label = QLabel()
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        outer.addWidget(self.empty_label)

        delete = QShortcut(QKeySequence.StandardKey.Delete, self.table)
        delete.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete.activated.connect(self.remove_row)

        # On hover rather than permanently on screen. Worth keeping somewhere:
        # the Device and Preset columns hold dropdowns, so clicking those does
        # not select the row — which is genuinely surprising.
        self.table.setToolTip(
            "Click the <b>Process</b> column to select a row — the Device and "
            "Preset columns are dropdowns, so clicking those won't select it.<br><br>"
            "Ctrl+click or Shift+click for several, then <b>Remove selected</b> "
            "(or press Delete).")

        row = QHBoxLayout()

        # One entry point for the three ways of adding a mapping, rather than
        # three buttons competing for space.
        self.add_button = QPushButton("Add mapping")
        add_menu = QMenu(self.add_button)

        add_menu.addAction("From Steam library…", lambda: self.add_from_steam())
        add_menu.addAction("From running process…", lambda: self.add_from_process())
        add_menu.addSeparator()
        add_menu.addAction("Manual", lambda: self.add_row())
        self.add_button.setMenu(add_menu)
        row.addWidget(self.add_button)

        # The per-row ✎ only reaches presets a mapping already uses, so a device
        # with no rows would otherwise be unreachable.
        self.presets_button = QPushButton("Presets")
        presets_menu = QMenu(self.presets_button)
        presets_menu.aboutToShow.connect(
            lambda: self.build_presets_menu(presets_menu))
        self.presets_button.setMenu(presets_menu)
        self.presets_button.setToolTip(
            "Open, create and repair presets — for any device, including ones with "
            "no mappings yet.")
        row.addWidget(self.presets_button)

        self.fix_button = QPushButton("Fix profile assignments…")
        self.fix_button_text = "Fix profile assignments…"
        self.fix_button.clicked.connect(self.fix_assignments)
        row.addWidget(self.fix_button)
        row.addStretch(1)
        outer.addLayout(row)

        # Row actions, kept compact so the window can be narrow.
        row2 = QHBoxLayout()
        for label, slot, tip in (
            ("Duplicate", self.duplicate_row, "Duplicate the selected mapping(s)"),
            ("Remove", self.remove_row, "Remove the selected mapping(s)  (Delete)"),
        ):
            btn = QPushButton(label)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            row2.addWidget(btn)
        for label, delta, tip in (("\u2191", -1, "Move up"), ("\u2193", 1, "Move down")):
            btn = QPushButton(label)
            btn.setToolTip(tip)
            btn.setFixedWidth(34)
            btn.clicked.connect(lambda _c, d=delta: self.move_row(d))
            row2.addWidget(btn)
        row2.addSpacing(12)
        self.warn_label = QLabel()
        self.warn_label.setWordWrap(True)
        row2.addWidget(self.warn_label, 1)
        for label, slot in (("Reload from disk", self.reload_from_disk),
                            ("Save config", self.save_config)):
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            row2.addWidget(btn)
        outer.addLayout(row2)
        return box

    def _preset_combo(self, device, value):
        return PresetCell(presets_for(device), value, self.edit_preset_cell)

    def edit_preset_cell(self, cell):
        """Open the editor on the preset belonging to this row."""
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 1) is cell:
                self.open_preset_editor(self.current_device, self.rows()[r][2])
                return

    def _wire_row(self, r):
        self.table.cellWidget(r, 1).currentTextChanged.connect(
            lambda _: self.mark_dirty())

    def insert_row(self, r, proc, device, preset):
        # `device` is accepted for call-site compatibility but the table is
        # always scoped to the selected device.
        self.table.blockSignals(True)
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(proc))
        self.table.setCellWidget(r, 1, self._preset_combo(
            device or self.current_device, preset))
        self.table.blockSignals(False)
        self._wire_row(r)

    def rewire_all(self):
        for r in range(self.table.rowCount()):
            try:
                self.table.cellWidget(r, 1).currentTextChanged.disconnect()
            except TypeError:
                pass
            self._wire_row(r)

    def on_item_changed(self, _item):
        self.mark_dirty()

    def mark_dirty(self):
        self.dirty = True
        self.validate()

    def rows(self):
        """Visible rows, as (process, device, preset) for the selected device."""
        out = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            proc = item.text().strip() if item else ""
            preset = self.table.cellWidget(r, 1).currentText().strip()
            out.append((proc, self.current_device, preset))
        return out

    def validate(self):
        problems = []
        seen = {}
        self.table.blockSignals(True)
        for r, (proc, device, preset) in enumerate(self.rows()):
            bad = False
            if not proc or not device or not preset:
                problems.append(f"row {r + 1}: incomplete — the script skips it")
                bad = True
            elif not preset_exists(device, preset):
                problems.append(f"row {r + 1}: preset “{preset}” not found for “{device}”")
                bad = True
            key = (proc.lower(), device)
            if proc and key in seen:
                problems.append(
                    f"row {r + 1}: duplicate of row {seen[key] + 1} — the earlier one wins")
            elif proc:
                seen[key] = r
            item = self.table.item(r, 0)
            if item:
                item.setForeground(QBrush(QColor("#d13438")) if bad
                                   else QBrush(self.palette().text().color()))
                font = QFont(item.font())
                font.setBold(proc == DEFAULT_KEY)
                item.setFont(font)
        self.table.blockSignals(False)

        if problems:
            shown = problems[:6]
            more = "" if len(problems) <= 6 else f"  (+{len(problems) - 6} more)"
            self.warn_label.setText(
                "<span style='color:#d13438'>⚠ " + "<br>".join(shown) + more + "</span>")
        else:
            self.warn_label.setText("<span style='color:#2e7d32'>✓ All mappings valid</span>")

    # ---------------- row actions ----------------

    def current_row(self):
        r = self.table.currentRow()
        return r if r >= 0 else self.table.rowCount() - 1

    def selected_rows(self):
        """Every selected row, descending, so callers can delete safely."""
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        if not rows and self.table.currentRow() >= 0:
            rows = {self.table.currentRow()}
        return sorted(rows, reverse=True)

    def add_row(self, proc=""):
        device = self.target_device() or (self.devices[0] if self.devices else "")
        # Keep DEFAULT at the bottom where it reads as the fallback.
        r = min(self.current_row() + 1, self.default_row_index()) \
            if self.table.rowCount() else 0
        preset = preset_for_process(device, proc)
        self.insert_row(r, proc or "", device, preset)
        self.table.setCurrentCell(r, 0)
        self.mark_dirty()

        # Named a preset that doesn't exist yet? Create it from Default, the same
        # as importing from Steam does — otherwise the row is added already
        # broken and the preset has to be made by hand.
        if proc and preset and not preset_exists(device, preset):
            ok, message = create_preset_from_default(device, preset)
            self.refresh_preset_lists()
            self.table.setCurrentCell(r, 0)
            self.status.showMessage(
                f"Added “{proc}” — {message}" if ok else
                f"Added “{proc}” — preset “{preset}” still needs creating ({message})",
                15000)

    def add_from_process(self):
        dialog = ProcessPicker(self)
        if dialog.exec() and dialog.choice:
            self.add_row(dialog.choice)

    # ---------- device scoping ----------

    def sync_visible_rows(self):
        """Fold the table back into the full mapping list.

        Only the selected device's mappings are on screen; the rest are held in
        self.all_rows and must survive editing and saving untouched.
        """
        if not self.current_device:
            return
        others = [r for r in self.all_rows if r[1] != self.current_device]
        self.all_rows = others + [r for r in self.rows() if r[0] and r[2]]

    def device_selected(self, device):
        """Switch the table to another device, keeping everything else intact."""
        if self.loading_device or not device:
            return
        self.sync_visible_rows()
        self.current_device = device
        self.show_device_rows()
        save_gui_state({"last_device": device})
        self.offer_default_preset(device)

    def has_default_mapping(self, device):
        """Is there a DEFAULT fallback line for this device in the config?"""
        return any(proc == DEFAULT_KEY and dev == device
                   for proc, dev, _preset in self.all_rows) or any(
            proc == DEFAULT_KEY for proc, dev, _preset in self.rows()
            if dev == device)

    def ensure_default_mapping(self, device, preset=DEFAULT_PRESET):
        """Add the DEFAULT fallback line so the preset is actually applied.

        Creating a preset file only puts it on disk; without a mapping pointing
        at it the switcher never uses it and it never appears in the list.
        """
        if self.has_default_mapping(device):
            return False
        if device == self.current_device:
            self.insert_row(self.table.rowCount(), DEFAULT_KEY, device, preset)
            self.rewire_all()
        else:
            self.all_rows.append((DEFAULT_KEY, device, preset))
        self.mark_dirty()
        return True

    def offer_default_preset(self, device):
        """Make sure a device has both a Default preset and a DEFAULT mapping.

        Asked once per device per session, so declining doesn't nag on every
        switch back and forth.
        """
        if not device or device in self.declined_defaults:
            return
        missing_preset = not preset_exists(device, DEFAULT_PRESET)
        missing_mapping = not self.has_default_mapping(device)
        if not (missing_preset or missing_mapping):
            return

        if missing_preset and missing_mapping:
            detail = (f"“{device}” has no {DEFAULT_PRESET} preset, and nothing "
                      "tells the switcher to use one.")
        elif missing_preset:
            detail = f"“{device}” has no {DEFAULT_PRESET} preset."
        else:
            detail = (f"“{device}” has a {DEFAULT_PRESET} preset, but no mapping "
                      "points at it, so the switcher never applies it.")

        answer = QMessageBox.question(
            self, "No default for this device",
            f"{detail}\n\nThe DEFAULT mapping is what gets applied when no game "
            "is running.\n\nSet it up now?")
        if answer != QMessageBox.StandardButton.Yes:
            self.declined_defaults.add(device)
            return

        done = []
        if missing_preset:
            path = PRESET_DIR / device / f"{DEFAULT_PRESET}.json"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[]")
                done.append(f"created an empty {DEFAULT_PRESET} preset")
            except OSError as exc:
                QMessageBox.critical(self, "Could not create preset", str(exc))
                return
            self.refresh_preset_lists()
        if self.ensure_default_mapping(device):
            done.append("added the DEFAULT mapping (remember to Save config)")
        self.status.showMessage(f"“{device}”: " + ", ".join(done), 20000)

    def show_device_rows(self):
        """Populate the table with just the selected device's mappings."""
        self.loading_device = True
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        for proc, device, preset in self.all_rows:
            if device == self.current_device:
                self.insert_row(self.table.rowCount(), proc, device, preset)
        self.loading_device = False

        hidden = sum(1 for r in self.all_rows if r[1] != self.current_device)
        others = len({r[1] for r in self.all_rows if r[1] != self.current_device})
        self.device_count_label.setText(
            f"<i>{self.table.rowCount()} mapping(s)</i>" + (
                f" &nbsp;·&nbsp; <i>{hidden} more on {others} other device(s)</i>"
                if hidden else ""))

        if self.table.rowCount() == 0:
            presets = presets_for(self.current_device)
            self.empty_label.setText(
                f"<b>No mappings for “{self.current_device}” yet.</b><br>"
                + (f"Its presets: <b>{', '.join(presets)}</b> — use "
                   "<b>Add mapping</b> to point a game at one, or "
                   "<b>Presets ▸</b> to edit them."
                   if presets else
                   "It has no presets either — use <b>Presets ▸ Create new device "
                   "default…</b> to make one.")
                + "<br><i>This list shows mappings (game → preset), not the presets "
                  "themselves.</i>")
            self.empty_label.setVisible(True)
        else:
            self.empty_label.setVisible(False)
        self.validate()

    def refresh_device_selector(self):
        """Devices from the config and from the system, selection preserved."""
        devices = sorted(set(all_device_names())
                         | {r[1] for r in self.all_rows if r[1]})
        self.loading_device = True
        self.device_selector.blockSignals(True)
        self.device_selector.clear()
        self.device_selector.addItems(devices)
        wanted = self.current_device if self.current_device in devices else None
        if wanted is None:
            counts = {}
            for _proc, device, _preset in self.all_rows:
                counts[device] = counts.get(device, 0) + 1
            remembered = load_gui_state().get("last_device")
            # Only reopen on the remembered device if it has mappings here —
            # otherwise the window looks empty and the config looks lost, when
            # really it's just showing a device this config says nothing about.
            if remembered in devices and (counts.get(remembered) or not counts):
                wanted = remembered
            elif counts:
                wanted = max(counts, key=counts.get)
            else:
                wanted = devices[0] if devices else ""
        self.device_selector.setCurrentText(wanted)
        self.device_selector.blockSignals(False)
        self.loading_device = False
        self.current_device = wanted

    def refresh_preset_lists(self):
        """Rebuild the preset dropdowns after presets are created on disk."""
        self.devices = known_devices()
        for r, (_proc, device, preset) in enumerate(self.rows()):
            self.table.setCellWidget(r, 1, self._preset_combo(device, preset))
        self.rewire_all()
        self.validate()
        self.refresh_fix_indicator()

    def create_presets_for_selection(self):
        """Copy Default.json to the preset name of every selected row that lacks one."""
        rows = self.selected_rows()
        if not rows:
            return
        todo = []
        for r in rows:
            _proc, device, preset = self.rows()[r]
            if device and preset and not preset_exists(device, preset):
                todo.append((device, preset))
        if not todo:
            QMessageBox.information(
                self, "Nothing to create",
                "Every selected row already points at an existing preset.")
            return

        names = "\n".join(f"  • {p}  ({d})" for d, p in todo)
        answer = QMessageBox.question(
            self, "Create presets",
            f"Copy “{DEFAULT_PRESET}” to {len(todo)} new preset(s)?\n\n{names}")
        if answer != QMessageBox.StandardButton.Yes:
            return

        created, failed = [], []
        for device, preset in todo:
            ok, message = create_preset_from_default(device, preset)
            (created if ok else failed).append(message)
        self.refresh_preset_lists()

        message = f"Created {len(created)} preset(s)"
        if failed:
            message += f" — {len(failed)} failed: " + "; ".join(failed[:3])
        if created and self.ask_reload():
            message += " — " + self.run_reload()
        self.status.showMessage(message, 20000)

    def ask_reload(self):
        answer = QMessageBox.question(
            self, "Reload Input Remapper",
            "Input Remapper only sees presets that existed when it started.\n\n"
            "Save config.txt and restart it plus the auto-switch service now?")
        return answer == QMessageBox.StandardButton.Yes

    def refresh_fix_indicator(self):
        """Flag the fix button when presets point at a device that isn't there.

        Cheap enough to run on every preset change: it reads the preset files and
        enumerates evdev, both of which are local and small.
        """
        try:
            findings, _problems = analyze_presets()
        except Exception:  # never let a broken scan stop the GUI from opening
            return
        button = getattr(self, "fix_button", None)
        if button is None:
            return

        if findings:
            broken = sum(f["count"] for f in findings)
            button.setText(f"⚠ {self.fix_button_text}  ({len(findings)})")
            button.setStyleSheet(
                "QPushButton { background-color:#d13438; color:white; font-weight:bold; }"
                "QPushButton:hover { background-color:#e14b4f; }")
            button.setToolTip(
                f"{broken} mapping(s) in {len(findings)} preset(s) point at an input "
                "device that is no longer connected, so those keys do nothing:\n\n"
                + "\n".join(f"  • {f['preset']} ({f['count']})" for f in findings[:10])
                + "\n\nClick to repair them.")
            self.status.showMessage(
                f"⚠ {broken} mapping(s) in {len(findings)} preset(s) are pointing at a "
                "device that no longer exists — use “Fix profile assignments”", 30000)
        else:
            button.setText(self.fix_button_text)
            button.setStyleSheet("")
            button.setToolTip(
                "Scan presets for mappings recorded against a device node that no "
                "longer exists.")

    def fix_assignments(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            findings, problems = analyze_presets()
        finally:
            QApplication.restoreOverrideCursor()

        dialog = FixAssignmentsDialog(findings, problems, self)
        if not dialog.exec():
            return
        chosen = dialog.selected()
        if not chosen:
            return

        fixed, failed = [], []
        for finding in chosen:
            ok, message = apply_hash_fix(finding)
            (fixed if ok else failed).append(message)

        parts = [f"Repaired {len(fixed)} preset(s)"]
        if failed:
            parts.append(f"{len(failed)} failed: " + "; ".join(failed[:3]))
        if fixed:
            # Always restart: the daemon only reads presets at injection time and
            # the switcher caches the last profile it applied, so without this the
            # repair sits on disk doing nothing.
            parts.append(self.run_reload())
        self.refresh_fix_indicator()
        self.status.showMessage(" — ".join(parts), 25000)

    def open_preset_editor(self, device, preset):
        """Open the preset editor, reached from the ✎ button on a row."""
        if not device:
            QMessageBox.information(self, "No device",
                                    "No Input Remapper devices were found.")
            return
        if not preset or not preset_exists(device, preset):
            answer = QMessageBox.question(
                self, "Preset doesn't exist yet",
                f"“{preset or '(none)'}” doesn't exist for “{device}”.\n\n"
                f"Create it from {DEFAULT_PRESET} and edit it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            ok, message = create_preset_from_default(device, preset)
            if not ok:
                QMessageBox.warning(self, "Could not create preset", message)
                return
            self.refresh_preset_lists()

        editor = PresetEditor(device, preset, self)
        editor.exec()
        self.refresh_preset_lists()

    def build_presets_menu(self, menu):
        """Preset actions, plus every device's presets — rebuilt on each open."""
        menu.clear()
        menu.addAction("Create preset from Default (selected rows)",
                       self.create_presets_for_selection)
        menu.addAction("Create new device default…", self.create_device_default)
        menu.addSeparator()

        devices = all_device_names()
        if not devices:
            menu.addAction("No devices found").setEnabled(False)
            return
        header = menu.addAction("Edit a preset:")
        header.setEnabled(False)
        for device in devices:
            submenu = menu.addMenu(device)
            presets = presets_for(device)
            if not presets:
                action = submenu.addAction("no presets yet")
                action.setEnabled(False)
                submenu.addSeparator()
                submenu.addAction(
                    f"Create empty {DEFAULT_PRESET}…",
                    lambda d=device: self.create_default_for(d))
                continue
            for preset in presets:
                submenu.addAction(
                    preset, lambda d=device, p=preset: self.open_preset_editor(d, p))

    def create_default_for(self, device):
        """Create an empty Default for one device, wire it up, then open it."""
        path = PRESET_DIR / device / f"{DEFAULT_PRESET}.json"
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[]")
            except OSError as exc:
                QMessageBox.critical(self, "Could not create preset", str(exc))
                return
            self.refresh_preset_lists()
        # A preset nothing points at is never applied, so add the fallback too.
        self.ensure_default_mapping(device)
        self.open_preset_editor(device, DEFAULT_PRESET)

    def target_device(self):
        """New mappings belong to whichever device is selected at the top."""
        return self.current_device or self.preferred_device()

    def create_device_default(self):
        """Give a device its own empty Default preset so it can be mapped."""
        devices = all_device_names()
        if not devices:
            QMessageBox.information(self, "No devices",
                                    "No input devices were found.")
            return

        labels, lookup = [], {}
        selected = 0
        for index, device in enumerate(devices):
            has = preset_exists(device, DEFAULT_PRESET)
            label = f"{device}" + ("   (already has a Default)" if has else "")
            labels.append(label)
            lookup[label] = device
            if device == self.current_device:
                selected = index      # start on whatever the window is showing

        label, ok = QInputDialog.getItem(
            self, "Create device default",
            "Create an empty Default preset for:", labels, selected, False)
        if not ok:
            return
        device = lookup[label]

        path = PRESET_DIR / device / f"{DEFAULT_PRESET}.json"
        if path.exists():
            answer = QMessageBox.question(
                self, "Already exists",
                f"“{device}” already has a {DEFAULT_PRESET} preset.\n\n"
                "Open it in the editor instead?")
            if answer == QMessageBox.StandardButton.Yes:
                self.open_preset_editor(device, DEFAULT_PRESET)
            return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[]")     # a valid preset with no mappings yet
        except OSError as exc:
            QMessageBox.critical(self, "Could not create preset", str(exc))
            return

        self.refresh_preset_lists()
        self.refresh_device_selector()
        added = self.ensure_default_mapping(device)
        self.status.showMessage(
            f"Created an empty {DEFAULT_PRESET} preset for “{device}”"
            + (" and added its DEFAULT mapping (remember to Save config)"
               if added else ""), 20000)
        if QMessageBox.question(
                self, "Preset created",
                f"Empty {DEFAULT_PRESET} preset created for “{device}”.\n\n"
                "Open the editor to record some keys for it?") \
                == QMessageBox.StandardButton.Yes:
            self.open_preset_editor(device, DEFAULT_PRESET)

    def preferred_device(self):
        """The device most of the existing mappings use, else the first known one."""
        counts = {}
        for _proc, device, _preset in self.rows():
            if device:
                counts[device] = counts.get(device, 0) + 1
        if counts:
            return max(counts, key=counts.get)
        return self.devices[0] if self.devices else ""

    def add_from_steam(self):
        dialog = SteamPicker([p for p, _d, _s in self.rows()], self)
        if not dialog.exec() or not dialog.chosen:
            return

        device = self.target_device()
        by_norm = {normalize(p): p for p in presets_for(device)}
        added, created, failed, missing = 0, [], [], []

        for game, process in dialog.chosen:
            # Reuse an existing preset whose name matches the game; otherwise use
            # the game's own name, creating it from Default when asked to.
            preset = by_norm.get(normalize(game), safe_preset_name(game))
            if not preset_exists(device, preset):
                if dialog.create_presets.isChecked():
                    ok, message = create_preset_from_default(device, preset)
                    (created if ok else failed).append(message)
                else:
                    missing.append(preset)
            self.insert_row(self.default_row_index(), process, device, preset)
            added += 1

        self.rewire_all()
        self.refresh_preset_lists()
        self.mark_dirty()

        parts = [f"Added {added} mapping(s) on “{device}”"]
        if created:
            parts.append(f"{len(created)} preset(s) copied from {DEFAULT_PRESET}")
        if failed:
            parts.append(f"{len(failed)} preset(s) failed: " + "; ".join(failed[:3]))
        if missing:
            parts.append("still need presets: " + ", ".join(missing[:5])
                         + ("…" if len(missing) > 5 else ""))
        if dialog.reload_after.isChecked():
            parts.append(self.run_reload())
        self.status.showMessage(" — ".join(parts), 20000)

    def default_row_index(self):
        """Insert above any DEFAULT fallback row so it stays the last resort."""
        for r, (proc, _device, _preset) in enumerate(self.rows()):
            if proc == DEFAULT_KEY:
                return r
        return self.table.rowCount()

    def duplicate_row(self):
        rows = self.selected_rows()
        if not rows:
            return
        data = self.rows()
        for r in rows:  # descending, so later inserts don't shift earlier ones
            self.insert_row(r + 1, *data[r])
        self.rewire_all()
        self.table.setCurrentCell(rows[0] + 1, 0)
        self.mark_dirty()

    def orphaned_presets(self, rows):
        """Presets referenced only by the rows about to go.

        A preset still used by a row that survives is left alone, and Default is
        never a candidate — it's the fallback everything else is copied from.
        """
        going = set(rows)
        all_rows = self.rows()
        kept = {(d, p) for i, (_proc, d, p) in enumerate(all_rows)
                if i not in going and d and p}
        doomed = {(d, p) for i, (_proc, d, p) in enumerate(all_rows)
                  if i in going and d and p and (d, p) not in kept
                  and p != DEFAULT_PRESET and preset_exists(d, p)}
        return sorted(doomed)

    def remove_row(self):
        rows = self.selected_rows()
        if not rows:
            return

        doomed = self.orphaned_presets(rows)
        box = QMessageBox(self)
        box.setWindowTitle("Remove mappings")
        box.setText(f"Remove {len(rows)} selected mapping(s)?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)

        checkbox = None
        if doomed:
            names = "\n".join(f"  • {p}   ({d})" for d, p in doomed)
            box.setInformativeText(
                f"These presets are used by no other mapping:\n\n{names}")
            checkbox = QCheckBox(
                "Also delete these presets from Input Remapper, then restart it")
            checkbox.setToolTip(
                f"Preset files are moved to {trash_dir()} rather than erased, so "
                "you can put them back.")
            box.setCheckBox(checkbox)

        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        for r in rows:  # descending, so indices stay valid
            self.table.removeRow(r)
        self.rewire_all()
        self.mark_dirty()

        if not (checkbox and checkbox.isChecked()):
            self.validate()
            return

        deleted, failed = [], []
        for device, preset in doomed:
            ok, message = delete_preset(device, preset)
            (deleted if ok else failed).append(message)
        self.refresh_preset_lists()

        parts = [f"Removed {len(rows)} mapping(s)", f"{len(deleted)} preset(s) moved "
                 f"to {trash_dir()}"]
        if failed:
            parts.append(f"{len(failed)} failed: " + "; ".join(failed[:3]))
        parts.append(self.run_reload())
        self.status.showMessage(" — ".join(parts), 20000)

    def move_row(self, delta):
        r = self.table.currentRow()
        target = r + delta
        if r < 0 or not 0 <= target < self.table.rowCount():
            return
        proc, device, preset = self.rows()[r]
        self.table.removeRow(r)
        self.insert_row(target, proc, device, preset)
        self.rewire_all()
        self.table.setCurrentCell(target, 0)
        self.mark_dirty()

    # ---------------- config I/O ----------------

    def warn_about_other_configs(self):
        """Say so when mappings live in a config file we're not using.

        Only the first candidate is read, so a second one holding real mappings
        is invisible otherwise — and looks exactly like "my config was lost".
        """
        others = [(path, count) for path, count in config_candidates()
                  if path != CONFIG_FILE and count > 0]
        if not others:
            return
        listing = "\n".join(f"  • {path}  ({count} line(s))" for path, count in others)
        QMessageBox.warning(
            self, "Another config file was found",
            f"This window is editing:\n  {CONFIG_FILE}\n"
            f"({len(self.all_rows)} mapping(s))\n\n"
            f"But these also contain mappings:\n{listing}\n\n"
            "Only the first file in the search order is used, so the others are "
            "ignored — including by the switcher if it resolves differently.\n\n"
            "Point both at one file: pass --config, set $AUTOSWITCH_CONFIG, or "
            "delete the one you don't want.")

    def load_config(self):
        self.devices = known_devices()
        self.all_rows = []

        if CONFIG_FILE.is_file():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                while len(parts) < 3:
                    parts.append("")
                self.all_rows.append(tuple(parts[:3]))
        else:
            self.status.showMessage(
                f"No config at {CONFIG_FILE} — starting empty. Saving creates it "
                "there; use --config to work on a different file.", 20000)

        self.refresh_device_selector()
        self.show_device_rows()
        self.dirty = False
        if self.all_rows:
            self.status.showMessage(
                f"Loaded {len(self.all_rows)} mapping(s) from {CONFIG_FILE}", 6000)

    def reload_from_disk(self):
        """Re-run the search and reload — a file restored elsewhere is found.

        The path is worked out once at startup, so without this a config put back
        in a higher-priority location would keep being ignored until a restart.
        """
        global CONFIG_FILE
        resolved = resolve_config_file(config_from_argv(sys.argv[1:]))
        if resolved != CONFIG_FILE:
            CONFIG_FILE = resolved
            self.setWindowTitle(DISPLAY_NAME)
            self.status.showMessage(f"Now using {CONFIG_FILE}", 15000)
        self.load_config()
        self.warn_about_other_configs()

    def save_config(self):
        """Write config.txt. Returns False if the user backed out.

        Writes every device's mappings, not just the visible ones — the table
        only ever shows the selected device.
        """
        self.sync_visible_rows()
        rows = self.all_rows
        empty = [r + 1 for r, (p, d, s) in enumerate(rows) if not (p and d and s)]
        if empty:
            answer = QMessageBox.question(
                self, "Incomplete rows",
                f"Row(s) {', '.join(map(str, empty))} are incomplete and will be dropped. "
                "Save anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return False

        if CONFIG_FILE.is_file():
            shutil.copy2(CONFIG_FILE, CONFIG_FILE.with_suffix(".txt.bak"))

        lines = [HEADER]
        # Grouped per device so the file stays readable with several devices.
        for device in sorted({r[1] for r in rows if r[1]}):
            entries = [r for r in rows if r[1] == device and r[0] and r[2]]
            if not entries:
                continue
            lines.append(f"\n# {device}\n")
            for proc, _device, preset in entries:
                if proc == DEFAULT_KEY:
                    lines.append("# fallback when nothing above is running:\n")
                lines.append(f"{proc}|{device}|{preset}\n")

        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text("".join(lines))
        self.dirty = False
        self.status.showMessage(
            f"Saved to {CONFIG_FILE} (backup: config.txt.bak) — "
            "the running service reloads within 60s", 10000)
        return True

    # ---------------- service ----------------

    def _build_service(self):
        box = self.service_box = QGroupBox(
            "Service — input-remapper-autoswitch.service")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        self.svc_label = QLabel("checking…")
        row.addWidget(self.svc_label)
        row.addStretch(1)
        for label, args in (
            ("Start", ("start",)),
            ("Stop", ("stop",)),
            ("Enable at login", ("enable", "--now")),
            ("Disable at login", ("disable",)),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _, a=args: self.service_action(a))
            row.addWidget(btn)
        outer.addLayout(row)

        row2 = QHBoxLayout()
        self.remapper_label = QLabel("checking…")
        row2.addWidget(self.remapper_label)
        row2.addStretch(1)
        restart = QPushButton("Restart both services")
        restart.setToolTip(
            "Restarts input-remapper.service, then the auto-switch service so it "
            "re-applies the current profile.\n\n"
            + ("Passwordless sudo is configured, so this runs silently."
               if passwordless_sudo() else
               "This needs root: your desktop will ask for a password."))
        restart.clicked.connect(self.restart_both)
        row2.addWidget(restart)

        # The log is useful when something misbehaves and just noise otherwise,
        # so it starts collapsed and remembers whichever you chose.
        self.log_toggle = QPushButton("Show log")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setToolTip("Show the auto-switch service log")
        self.log_toggle.toggled.connect(self.toggle_log)
        row2.addWidget(self.log_toggle)
        outer.addLayout(row2)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(500)
        self.log.setFont(QFont("monospace"))
        self.log.setMinimumHeight(120)
        self.log.setVisible(False)
        outer.addWidget(self.log, 1)
        return box

    def service_action(self, args):
        code, out = systemctl(*args, SERVICE)
        self.status.showMessage(
            f"systemctl --user {' '.join(args)} {SERVICE}: "
            + ("ok" if code == 0 else out), 8000)
        self.refresh_service_status()

    def run_reload(self):
        """Persist the config, then reload_remapper() behind a wait cursor.

        Saving first is essential: the switcher re-reads config.txt on start, so
        restarting it with unsaved rows would apply the old file.
        """
        if self.dirty and not self.save_config():
            return "config not saved — nothing restarted"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return reload_remapper()
        finally:
            QApplication.restoreOverrideCursor()
            self.refresh_service_status()

    def toggle_log(self, shown):
        """Collapse the service pane down to its buttons when the log is hidden."""
        self.log.setVisible(shown)
        self.log_toggle.setText("Hide log" if shown else "Show log")
        splitter = self.centralWidget()
        if isinstance(splitter, QSplitter):
            top, bottom = splitter.sizes()
            total = top + bottom
            wanted = max(240, total // 3) if shown else \
                self.service_box.sizeHint().height()
            splitter.setSizes([max(120, total - wanted), wanted])

    def restart_both(self):
        self.status.showMessage(self.run_reload(), 15000)

    def refresh_service_status(self):
        active = systemctl("is-active", SERVICE, check_output=True)
        enabled = systemctl("is-enabled", SERVICE, check_output=True)
        color = "#2e7d32" if active == "active" else "#d13438"
        self.svc_label.setText(
            f"<b>Auto-switch:</b> <span style='color:{color}'>{active}</span> "
            f"&nbsp;·&nbsp; <b>At login:</b> {enabled}")

        remapper = systemctl("is-active", REMAPPER_SERVICE, check_output=True, system=True)
        color = "#2e7d32" if remapper == "active" else "#d13438"
        self.remapper_label.setText(
            f"<b>Input Remapper:</b> <span style='color:{color}'>{remapper}</span> "
            f"&nbsp;·&nbsp; <i>{REMAPPER_SERVICE} (system unit)</i>")

    def start_log(self):
        self.log_proc = QProcess(self)
        self.log_proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.log_proc.readyReadStandardOutput.connect(self.read_log)
        self.log_proc.start("journalctl",
                            ["--user", "-u", SERVICE, "-n", "60", "-f", "--no-pager"])

    def read_log(self):
        data = bytes(self.log_proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in data.splitlines():
            self.log.appendPlainText(line)

    # ---------------- lifecycle ----------------

    def closeEvent(self, event):
        if self.dirty:
            answer = QMessageBox.question(
                self, "Unsaved changes", "Save changes to config.txt before closing?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.StandardButton.Save:
                self.save_config()
        if self.log_proc:
            self.log_proc.kill()
            self.log_proc.waitForFinished(1000)
        event.accept()


USAGE = f"""{DISPLAY_NAME} — configuration GUI

Usage: autoswitch-gui.py [--config FILE]

Without --config the file is located exactly as auto-switch.sh locates it:
  $AUTOSWITCH_CONFIG, then $AUTOSWITCH_CONFIG_DIR/config.txt, otherwise
  config.txt beside this script — so the folder is self-contained wherever you
  put it. Set $INPUT_REMAPPER_CONFIG_DIR to point at a non-standard Input
  Remapper directory.
"""


def apply_app_icon(app):
    """Give the window the same icon as the menu entry.

    On Wayland the compositor takes a window's icon from the .desktop file whose
    basename matches the app id, which Qt sets from setDesktopFileName() — without
    it KDE shows the generic placeholder. setWindowIcon covers X11 and anywhere
    the theme icon is used directly.
    """
    app.setDesktopFileName(APP_NAME)

    icon = QIcon.fromTheme(APP_ICON)
    if icon.isNull():
        # Not installed into the icon theme (running from a checkout, or the
        # cache hasn't caught up) — load the file we ship directly.
        for candidate in (SCRIPT_DIR / "icons" / f"{APP_NAME}.svg",
                          XDG_DATA_HOME / "icons/hicolor/scalable/apps"
                          / f"{APP_NAME}.svg"):
            if candidate.is_file():
                icon = QIcon(str(candidate))
                break
    if icon.isNull():
        for fallback in ("input-gaming", "input-keyboard", "input-tablet",
                         "preferences-desktop-keyboard"):
            icon = QIcon.fromTheme(fallback)
            if not icon.isNull():
                break
    if not icon.isNull():
        app.setWindowIcon(icon)
    return icon


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        return
    app = QApplication(sys.argv)
    app.setApplicationName(DISPLAY_NAME)
    # Qt appends applicationDisplayName to every window title, and it defaults to
    # applicationName — so leaving it set repeats the name in the title bar.
    app.setApplicationDisplayName("")
    apply_app_icon(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
