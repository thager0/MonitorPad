"""MonitorPad, as a tray app.

Run this (or start.cmd) and MonitorPad sits in the notification area: the
HTTP server runs on a background thread, the Windows message loop owns the
main thread, and the tray menu drives both.

The .pyw extension means Windows launches it with pythonw.exe, so no console
window appears.
"""

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import appwindow  # noqa: E402
import autostart  # noqa: E402
import display  # noqa: E402
import server  # noqa: E402
import shortcut  # noqa: E402
import tray as tray_module  # noqa: E402

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class App:
    def __init__(self, port):
        self.port = port
        self.httpd = None
        self.tray = None
        self._autostart_cache = None

    # -- wiring ---------------------------------------------------------

    def start(self):
        try:
            self.httpd = server.build_server("0.0.0.0", self.port)
        except OSError as exc:
            self._fatal(
                "Could not listen on port {}.\n\n{}\n\nMonitorPad may already "
                "be running -- look for its icon in the notification area."
                .format(self.port, exc))
            return False
        server.serve_in_background(self.httpd)
        server.log("Tray app listening on port {}.".format(self.port))
        return True

    def run(self):
        if not self.start():
            return 1
        self.tray = tray_module.Tray(self)
        self.tray.create()
        self.tray.balloon(
            "MonitorPad is running",
            "Click the tray icon to pair your phone or change displays.")
        try:
            self.tray.run()
        finally:
            self.tray.destroy()
            if self.httpd:
                server.shut_down(self.httpd)
        return 0

    def _fatal(self, message):
        ctypes.windll.user32.MessageBoxW(None, message, "MonitorPad", 0x10)

    # -- tray content ---------------------------------------------------

    def tooltip(self):
        try:
            monitors = display.list_monitors()
            active = sum(1 for m in monitors if m["active"])
            return "MonitorPad - {} of {} displays on\n{}".format(
                active, len(monitors), server.pairing_url(self.port))
        except Exception:  # noqa: BLE001 - tooltip must never throw
            return "MonitorPad"

    def menu_items(self):
        items = [
            {"id": tray_module.ID_HEADER,
             "label": "MonitorPad  -  {}".format(socket.gethostname()),
             "disabled": True},
            None,
            {"id": tray_module.ID_PAIR, "label": "Pair a phone...",
             "action": self.show_pairing},
            {"id": tray_module.ID_COPY_LINK, "label": "Copy pairing link",
             "action": self.copy_link},
            {"id": tray_module.ID_OPEN, "label": "Open MonitorPad",
             "action": self.open_ui},
            {"id": tray_module.ID_OPEN_BROWSER, "label": "Open in browser",
             "action": self.open_in_browser},
            None,
            {"id": tray_module.ID_IDENTIFY, "label": "Identify monitors",
             "action": self.identify},
        ]

        profiles = sorted(server.CONFIG.data.get("profiles", {}).keys())
        if profiles:
            items.append(None)
            for index, name in enumerate(profiles[:40]):
                items.append({
                    "id": tray_module.ID_PROFILE_BASE + index,
                    "label": "Apply  \"{}\"".format(name),
                    "action": (lambda captured=name:
                               self.apply_profile(captured)),
                })

        items += [
            None,
            {"id": tray_module.ID_AUTOSTART, "label": "Start when I sign in",
             "checked": self.autostart_enabled(),
             "action": self.toggle_autostart},
            {"id": tray_module.ID_SHORTCUT,
             "label": "Add to Start Menu", "checked": self.shortcut_exists(),
             "action": self.toggle_shortcut},
            {"id": tray_module.ID_CONFIG, "label": "Open config folder",
             "action": self.open_config},
            None,
            {"id": tray_module.ID_QUIT, "label": "Quit MonitorPad",
             "action": self.quit},
        ]
        return items

    # -- actions --------------------------------------------------------

    def show_pairing(self):
        # Always hand out a fresh PIN: the one printed at startup has very
        # likely expired by the time anyone opens this.
        pin = server.PIN.issue()
        info = {
            "pin": pin,
            "url": server.pairing_url(self.port),
            "link": server.pairing_link(self.port),
            "host": socket.gethostname(),
            "seconds": server.PairingPin.LIFETIME,
        }
        subprocess.Popen(
            [sys.executable, os.path.join(HERE, "pair_window.py"),
             json.dumps(info)],
            creationflags=_NO_WINDOW, cwd=HERE)

    def copy_link(self):
        link = server.pairing_link(self.port)
        # clip.exe is the dependency-free way to reach the clipboard, but it
        # writes in the OEM codepage; the link is ASCII so that is fine.
        process = subprocess.Popen(["clip"], stdin=subprocess.PIPE,
                                   creationflags=_NO_WINDOW)
        process.communicate(link.encode("utf-8"))
        self.tray.balloon("Pairing link copied",
                          "Anyone with this link can control your displays, "
                          "so share it carefully.")

    def open_ui(self):
        how = appwindow.open_app_window(server.pairing_link(self.port))
        if how == "browser":
            self.tray.balloon(
                "Opened in your browser",
                "Install Microsoft Edge or Chrome to get MonitorPad in its "
                "own window instead of a tab.")

    def open_in_browser(self):
        webbrowser.open(server.pairing_link(self.port))

    def identify(self):
        server.identify()

    def apply_profile(self, name):
        server.apply_profile(name)
        self.tray.update_tooltip()
        self.tray.balloon(
            "Applied \"{}\"".format(name),
            "Confirm it in the app within {} seconds or it rolls back."
            .format(server.CONFIRM_SECONDS), tray_module.NIIF_WARNING)

    def on_displays_changed(self):
        """Windows told us the display set changed."""
        self.tray.update_tooltip()
        # Recording it now means a monitor that is plugged in and then goes
        # to sleep still ends up in the remembered list, even if the phone
        # never polled while it was awake.
        server.remember(display.list_monitors())

    def autostart_enabled(self):
        if self._autostart_cache is None:
            try:
                self._autostart_cache = autostart.is_enabled()
            except Exception:  # noqa: BLE001
                self._autostart_cache = False
        return self._autostart_cache

    def toggle_autostart(self):
        try:
            now_on = autostart.toggle(self.port)
        except autostart.AutostartError as exc:
            self._autostart_cache = None
            self.tray.balloon("Could not change auto-start", str(exc),
                              tray_module.NIIF_ERROR)
            return
        self._autostart_cache = now_on
        self.tray.balloon(
            "MonitorPad", "Will start when you sign in." if now_on
            else "No longer starts automatically.")

    def shortcut_exists(self):
        try:
            return shortcut.exists()
        except Exception:  # noqa: BLE001 - menu must always render
            return False

    def toggle_shortcut(self):
        try:
            if shortcut.exists():
                shortcut.remove()
                self.tray.balloon("MonitorPad",
                                  "Removed from the Start Menu.")
            else:
                shortcut.create(self.port)
                self.tray.balloon(
                    "Added to the Start Menu",
                    "Search for MonitorPad, or right-click it there to pin "
                    "it to the taskbar.")
        except shortcut.ShortcutError as exc:
            self.tray.balloon("Could not change the shortcut", str(exc),
                              tray_module.NIIF_ERROR)

    def open_config(self):
        os.makedirs(server.CONFIG_DIR, exist_ok=True)
        subprocess.Popen(["explorer", server.CONFIG_DIR])

    def quit(self):
        self.tray.quit()


def main():
    parser = argparse.ArgumentParser(description="MonitorPad tray app")
    parser.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    args = parser.parse_args()
    return App(args.port).run()


if __name__ == "__main__":
    sys.exit(main())
