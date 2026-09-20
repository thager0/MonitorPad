"""System tray icon for MonitorPad.

Plain Win32 through ctypes -- Shell_NotifyIcon plus a hidden message window
-- so the app still needs nothing outside the standard library.

This runs the Windows message loop on the main thread while the HTTP server
works in a background thread, which is the arrangement Windows wants: the
message loop must own the thread that created the window.
"""

import ctypes
import os
from ctypes import POINTER, WINFUNCTYPE, byref, sizeof
from ctypes.wintypes import (
    BOOL, DWORD, HANDLE, HICON, HINSTANCE, HMENU, HWND, LPARAM, LPCWSTR,
    POINT, UINT, WPARAM,
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(HERE, "monitorpad.ico")

LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t

# ------------------------------------------------------------------ constants

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
# Broadcast to every top-level window whenever a monitor is attached,
# detached, or changes mode.
WM_DISPLAYCHANGE = 0x007E
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_NULL = 0x0000

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4

NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NIF_SHOWTIP = 0x80

NIIF_NONE = 0x00
NIIF_INFO = 0x01
NIIF_WARNING = 0x02
NIIF_ERROR = 0x03

NOTIFYICON_VERSION_4 = 4

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
LR_SHARED = 0x8000

SM_CXSMICON = 49
SM_CYSMICON = 50

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
MF_CHECKED = 0x0008
MF_POPUP = 0x0010

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_BOTTOMALIGN = 0x0020

CW_USEDEFAULT = -0x80000000
WS_OVERLAPPED = 0x00000000

# Menu command ids.
ID_HEADER = 1
ID_OPEN = 2
ID_PAIR = 3
ID_IDENTIFY = 4
ID_AUTOSTART = 5
ID_CONFIG = 6
ID_QUIT = 7
ID_COPY_LINK = 8
ID_OPEN_BROWSER = 9
ID_SHORTCUT = 10
ID_PROFILE_BASE = 100   # profiles occupy ID_PROFILE_BASE .. +49

# ------------------------------------------------------------------ structures


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HANDLE),
        ("hbrBackground", HANDLE),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", HWND),
        ("message", UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("hWnd", HWND),
        ("uID", UINT),
        ("uFlags", UINT),
        ("uCallbackMessage", UINT),
        ("hIcon", HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", DWORD),
        ("dwStateMask", DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", HICON),
    ]


WNDPROC = WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [
    DWORD, LPCWSTR, LPCWSTR, DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, HWND, HMENU, HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = HWND
user32.LoadImageW.argtypes = [HINSTANCE, LPCWSTR, UINT, ctypes.c_int,
                              ctypes.c_int, UINT]
user32.LoadImageW.restype = HANDLE
user32.TrackPopupMenu.argtypes = [HMENU, UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.AppendMenuW.argtypes = [HMENU, UINT, ctypes.c_size_t, LPCWSTR]
user32.AppendMenuW.restype = BOOL
user32.RegisterWindowMessageW.argtypes = [LPCWSTR]
user32.RegisterWindowMessageW.restype = UINT
shell32.Shell_NotifyIconW.argtypes = [DWORD, POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = BOOL


# ---------------------------------------------------------------------- tray


class Tray:
    def __init__(self, app):
        self.app = app
        self.hwnd = None
        self.hicon = None
        self.menu_actions = {}
        self._wndproc = WNDPROC(self._on_message)
        # Explorer broadcasts this when it restarts; the icon has to be
        # re-added or it silently disappears until the next reboot.
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    # -- lifecycle ------------------------------------------------------

    def create(self):
        instance = kernel32.GetModuleHandleW(None)
        class_name = "MonitorPadTray"

        wndclass = WNDCLASSW()
        wndclass.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wndclass.hInstance = instance
        wndclass.lpszClassName = class_name
        if not user32.RegisterClassW(byref(wndclass)):
            error = ctypes.get_last_error()
            if error != 1410:  # already registered
                raise ctypes.WinError(error)

        self.hwnd = user32.CreateWindowExW(
            0, class_name, "MonitorPad", WS_OVERLAPPED,
            CW_USEDEFAULT, CW_USEDEFAULT, 0, 0, None, None, instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self.hicon = self._load_icon()
        self._notify(NIM_ADD)

        data = self._icon_data(NIF_MESSAGE)
        data.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, byref(data))

    def _load_icon(self):
        cx = user32.GetSystemMetrics(SM_CXSMICON)
        cy = user32.GetSystemMetrics(SM_CYSMICON)
        handle = user32.LoadImageW(None, ICON_PATH, IMAGE_ICON, cx, cy,
                                   LR_LOADFROMFILE)
        if not handle:
            # Fall back to a stock icon rather than showing nothing at all.
            handle = user32.LoadImageW(None, "#32512", IMAGE_ICON, cx, cy,
                                       LR_SHARED | LR_DEFAULTSIZE)
        return handle

    def _icon_data(self, flags):
        data = NOTIFYICONDATAW()
        data.cbSize = sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self.hicon
        return data

    def _notify(self, action):
        data = self._icon_data(NIF_MESSAGE | NIF_ICON | NIF_TIP |
                               NIF_SHOWTIP)
        data.szTip = self.app.tooltip()
        return bool(shell32.Shell_NotifyIconW(action, byref(data)))

    def update_tooltip(self):
        self._notify(NIM_MODIFY)

    def balloon(self, title, message, level=NIIF_INFO):
        data = self._icon_data(NIF_INFO | NIF_ICON)
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        data.dwInfoFlags = level
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(data))

    def destroy(self):
        if self.hwnd:
            data = self._icon_data(0)
            shell32.Shell_NotifyIconW(NIM_DELETE, byref(data))
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    # -- menu -----------------------------------------------------------

    def _show_menu(self):
        menu = user32.CreatePopupMenu()
        self.menu_actions = {}

        for item in self.app.menu_items():
            if item is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            flags = MF_STRING
            if item.get("disabled"):
                flags |= MF_GRAYED
            if item.get("checked"):
                flags |= MF_CHECKED
            user32.AppendMenuW(menu, flags, item["id"], item["label"])
            if item.get("action"):
                self.menu_actions[item["id"]] = item["action"]

        point = POINT()
        user32.GetCursorPos(byref(point))
        # Without this the menu refuses to close when you click elsewhere --
        # a documented quirk of tray menus.
        user32.SetForegroundWindow(self.hwnd)
        chosen = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_BOTTOMALIGN,
            point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)

        action = self.menu_actions.get(chosen)
        if action:
            self._safely(action)

    def _safely(self, action):
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - never kill the tray
            self.balloon("MonitorPad", str(exc)[:250], NIIF_ERROR)

    # -- message pump ---------------------------------------------------

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == self.taskbar_created:
            self._notify(NIM_ADD)
            return 0
        if message == WM_DISPLAYCHANGE:
            # A monitor came or went. Nothing is cached -- the HTTP API
            # re-reads the hardware on every request -- but the tooltip is
            # only as fresh as the last time we wrote it.
            self._safely(self.app.on_displays_changed)
            return 0
        if message == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_RBUTTONUP, WM_LBUTTONUP):
                self._show_menu()
            elif event == WM_LBUTTONDBLCLK:
                self._safely(self.app.open_ui)
            return 0
        if message == WM_COMMAND:
            action = self.menu_actions.get(wparam & 0xFFFF)
            if action:
                self._safely(action)
            return 0
        if message in (WM_CLOSE, WM_DESTROY):
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self):
        message = MSG()
        while True:
            result = user32.GetMessageW(byref(message), None, 0, 0)
            if result == 0 or result == -1:
                break
            user32.TranslateMessage(byref(message))
            user32.DispatchMessageW(byref(message))

    def quit(self):
        user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
