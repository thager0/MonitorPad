"""Create the Start Menu / desktop shortcuts that launch MonitorPad.

Written through WScript.Shell, which is present on every Windows install, so
this still needs nothing outside the standard library.
"""

import os
import subprocess
import sys
import tempfile
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "open_app.pyw")
ICON = os.path.join(HERE, "monitorpad.ico")

NAME = "MonitorPad.lnk"

SHELL_FOLDERS = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                 r"\User Shell Folders")


def _shell_folder(value, fallback):
    """Where Windows actually keeps a well-known folder.

    Never assume %USERPROFILE%\\Desktop: OneDrive's folder backup moves
    Desktop, Documents and Pictures under the OneDrive root, and plenty of
    machines have it switched on.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SHELL_FOLDERS) as key:
            raw = winreg.QueryValueEx(key, value)[0]
        path = os.path.expandvars(raw)
        if path and os.path.isdir(path):
            return path
    except OSError:
        pass
    return fallback


def start_menu():
    return _shell_folder("Programs", os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu",
        "Programs"))


def desktop():
    return _shell_folder("Desktop", os.path.join(
        os.environ.get("USERPROFILE", ""), "Desktop"))

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SCRIPT = """
Set shell = CreateObject("WScript.Shell")
Set link = shell.CreateShortcut("{path}")
link.TargetPath = "{target}"
link.Arguments = "{arguments}"
link.WorkingDirectory = "{workdir}"
link.IconLocation = "{icon}"
link.Description = "Arrange and configure your monitors"
link.WindowStyle = 1
link.Save
"""


class ShortcutError(Exception):
    pass


def pythonw():
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def _write(path, port):
    script = SCRIPT.format(
        path=path.replace('"', ''),
        target=pythonw(),
        arguments='""{}"" --port {}'.format(ENTRY, port),
        workdir=HERE,
        icon=ICON,
    )
    handle, vbs = tempfile.mkstemp(suffix=".vbs", prefix="monitorpad-")
    os.close(handle)
    try:
        # cscript reads .vbs as ANSI unless told otherwise; keep it simple by
        # writing the system encoding and letting paths stay ASCII-ish.
        with open(vbs, "w", encoding="mbcs", errors="replace") as stream:
            stream.write(script)
        result = subprocess.run(["cscript", "//nologo", vbs],
                                capture_output=True, text=True,
                                creationflags=_NO_WINDOW)
        if result.returncode != 0:
            raise ShortcutError((result.stdout + result.stderr).strip()
                                or "cscript failed")
    finally:
        try:
            os.unlink(vbs)
        except OSError:
            pass
    if not os.path.isfile(path):
        raise ShortcutError("Shortcut was not created at {}".format(path))


def locations(on_desktop=False):
    out = []
    programs = start_menu()
    if programs and os.path.isdir(programs):
        out.append(os.path.join(programs, NAME))
    if on_desktop:
        folder = desktop()
        if folder and os.path.isdir(folder):
            out.append(os.path.join(folder, NAME))
    return out


def exists():
    return any(os.path.isfile(p) for p in locations(on_desktop=True))


def create(port=8777, on_desktop=False):
    targets = locations(on_desktop=on_desktop)
    if not targets:
        raise ShortcutError("Could not find the Start Menu folder.")
    for path in targets:
        _write(path, port)
    return targets


def remove():
    removed = []
    for path in locations(on_desktop=True):
        if os.path.isfile(path):
            os.unlink(path)
            removed.append(path)
    return removed


def main(argv):
    argv = list(argv)
    on_desktop = "--desktop" in argv
    if on_desktop:
        argv.remove("--desktop")
    action = argv[1] if len(argv) > 1 else "create"
    port = int(argv[2]) if len(argv) > 2 else 8777
    try:
        if action == "create":
            for path in create(port, on_desktop=on_desktop):
                print("Created {}".format(path))
        elif action == "remove":
            paths = remove()
            print("\n".join("Removed {}".format(p) for p in paths)
                  or "Nothing to remove.")
        else:
            print("usage: shortcut.py [create|remove] [port]")
            return 2
    except ShortcutError as exc:
        print("Failed: {}".format(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
