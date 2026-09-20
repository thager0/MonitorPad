"""Open MonitorPad in its own desktop window rather than a browser tab.

Edge and Chrome both support an "app window": a normal OS window with no
address bar, tabs or bookmarks, its own taskbar button, and the page's icon.
That gets a real app on the desktop while still running the same interface
the phone uses, instead of maintaining a second, native UI that would drift
out of step with it.

Falls back to an ordinary browser tab if neither is installed.
"""

import ctypes
import os
import subprocess
import sys
import time
import webbrowser
import winreg
from ctypes import WINFUNCTYPE, byref, create_unicode_buffer
from ctypes.wintypes import BOOL, HWND, LPARAM

user32 = ctypes.WinDLL("user32", use_last_error=True)

# A tall, narrow box: the interface is laid out as a single column, so this
# matches its natural shape instead of stranding it in a wide empty window.
WINDOW_WIDTH = 520
# Tall enough that a typical set of displays needs no scrolling at all; it is
# clamped to what the screen can actually show, just below.
WINDOW_HEIGHT = 1240
MIN_HEIGHT = 620
SCREEN_MARGIN = 90

# How long to wait for the app window to appear before deciding the browser
# is not going to show one.
LAUNCH_TIMEOUT = 12

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
BROWSERS = ["msedge.exe", "chrome.exe"]

# Keeping the app window in its own browser profile stops it from being
# absorbed into an ordinary browsing window, and keeps its stored token out
# of the profile used for everyday browsing.
PROFILE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "MonitorPad", "app-window")


def _from_registry(executable):
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root,
                                APP_PATHS + "\\" + executable) as key:
                path = winreg.QueryValueEx(key, None)[0]
        except OSError:
            continue
        if path and os.path.isfile(path):
            return path
    return None


def _from_disk(executable):
    roots = [
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA"),
    ]
    folders = {
        "msedge.exe": [r"Microsoft\Edge\Application"],
        "chrome.exe": [r"Google\Chrome\Application"],
    }
    for root in filter(None, roots):
        for folder in folders.get(executable, []):
            candidate = os.path.join(root, folder, executable)
            if os.path.isfile(candidate):
                return candidate
    return None


def find_browser():
    """Path to a Chromium browser that understands --app, or None."""
    for executable in BROWSERS:
        path = _from_registry(executable) or _from_disk(executable)
        if path:
            return path
    return None


# ------------------------------------------------------------------ focusing

ENUM_PROC = WINFUNCTYPE(BOOL, HWND, LPARAM)

SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# The page title, which an app window shows on its own. A tabbed browser
# window appends the browser name ("MonitorPad - Brave"), so an exact match
# is what separates our window from someone just having the page open.
APP_TITLE = "MonitorPad"


def _process_image(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                  pid)
    if not handle:
        return ""
    try:
        buffer = create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                   byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _find_existing(browser=None):
    """An app window we opened earlier, if one is still around.

    Matching has to be strict. Simply looking for "MonitorPad" in a window
    title also finds an ordinary browser tab that happens to have the page
    open -- which would focus that tab instead of opening the app window,
    the exact behaviour this module exists to avoid.
    """
    browser = browser or find_browser()
    if not browser:
        return None
    wanted = os.path.normcase(browser)
    found = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        # Chromium top-level windows only; this also skips our own hidden
        # tray window, which shares the title but not the class.
        if not cls.value.startswith("Chrome_WidgetWin"):
            return True
        text = create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, text, 512)
        if text.value.strip() != APP_TITLE:
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if os.path.normcase(_process_image(pid.value)) != wanted:
            return True
        found.append(hwnd)
        return False

    user32.EnumWindows(ENUM_PROC(callback), 0)
    return found[0] if found else None


def focus_existing(browser=None):
    hwnd = _find_existing(browser)
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


# ------------------------------------------------------------------- opening


def _window_size():
    height = user32.GetSystemMetrics(1)  # SM_CYSCREEN, in logical pixels
    if height:
        height = max(MIN_HEIGHT, min(WINDOW_HEIGHT, height - SCREEN_MARGIN))
    else:
        height = WINDOW_HEIGHT
    return WINDOW_WIDTH, height


def open_app_window(url, reuse=True):
    """Show the interface in its own window. Returns how it was opened."""
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return "browser"

    if reuse and focus_existing(browser):
        return "focused"

    width, height = _window_size()
    os.makedirs(PROFILE_DIR, exist_ok=True)
    try:
        process = subprocess.Popen(
            [
                browser,
                "--app=" + url,
                "--user-data-dir=" + PROFILE_DIR,
                "--window-size={},{}".format(width, height),
                "--no-first-run",
                "--no-default-browser-check",
            ],
            creationflags=_NO_WINDOW,
            close_fds=True,
        )
    except OSError:
        webbrowser.open(url)
        return "browser"

    # Make sure a window really showed up. A browser launched against a cold
    # profile can decide to hand the URL to the system default browser and
    # exit, which would silently leave the user with the tab they asked not
    # to have. Waiting for our own window catches that.
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        if _find_existing(browser):
            return "app"
        if process.poll() is not None:
            # It gave up. Anything is better than no window at all.
            webbrowser.open(url)
            return "browser"
        time.sleep(0.25)
    # Still starting: slow machines and first runs can take a while, and the
    # process is alive, so let it finish rather than opening a second window.
    return "app"


if __name__ == "__main__":
    print(find_browser() or "no Chromium browser found")
    if len(sys.argv) > 1:
        print(open_app_window(sys.argv[1]))
