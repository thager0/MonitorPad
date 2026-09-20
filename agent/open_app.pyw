"""Open the MonitorPad window, starting the agent first if it is not up.

This is what the Start Menu and desktop shortcuts point at, so clicking
MonitorPad works whether or not the tray app happens to be running.
"""

import argparse
import ctypes
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import appwindow  # noqa: E402
import server  # noqa: E402

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
START_TIMEOUT = 20


def agent_running(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def start_agent(port):
    """Launch the tray app and wait for it to answer."""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    subprocess.Popen([pythonw, os.path.join(HERE, "monitorpad.pyw"),
                      "--port", str(port)],
                     creationflags=_NO_WINDOW, cwd=HERE, close_fds=True)

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if agent_running(port):
            return True
        time.sleep(0.4)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    args = parser.parse_args()

    if not agent_running(args.port) and not start_agent(args.port):
        ctypes.windll.user32.MessageBoxW(
            None,
            "MonitorPad could not start on port {}.\n\nRun console.cmd to "
            "see what went wrong.".format(args.port),
            "MonitorPad", 0x10)
        return 1

    appwindow.open_app_window(server.pairing_link(args.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
