"""Start MonitorPad automatically when you sign in.

This deliberately registers a *Scheduled Task with a logon trigger* rather
than installing a Windows service.

A service runs in session 0, which has no display configuration of its own
and cannot draw a tray icon. The display topology belongs to the interactive
desktop session, so anything that reconfigures monitors has to live there
too. A logon-triggered task with an interactive token is the closest thing
Windows offers: it starts by itself, survives reboots, is managed from Task
Scheduler, and can be started or stopped on demand -- but it runs as you, in
your session, where the display APIs actually work.
"""

import getpass
import os
import subprocess
import sys
import tempfile

TASK_NAME = "MonitorPad"

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "monitorpad.pyw")

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AutostartError(Exception):
    """schtasks refused to do what we asked."""


def _run(args):
    result = subprocess.run(
        ["schtasks"] + args, capture_output=True, text=True,
        creationflags=_NO_WINDOW)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def pythonw():
    """The windowless interpreter, so no console flashes up at logon."""
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.isfile(candidate):
        return candidate
    return sys.executable


def current_user():
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return "{}\\{}".format(domain, user) if domain else user


def is_enabled():
    code, _ = _run(["/Query", "/TN", TASK_NAME])
    return code == 0


def status():
    """A short human-readable state for the tray menu and the CLI."""
    code, output = _run(["/Query", "/TN", TASK_NAME, "/FO", "LIST"])
    if code != 0:
        return {"enabled": False, "state": "not installed"}
    state = ""
    for line in output.splitlines():
        if line.lower().startswith("status:"):
            state = line.split(":", 1)[1].strip()
            break
    return {"enabled": True, "state": state or "installed"}


def _task_xml(port):
    """Build the task definition.

    Registered from XML rather than with schtasks' /TR switch, which caps the
    command line at 261 characters -- easy to exceed once the install path is
    a few folders deep. XML also lets us ask for the restart-on-failure
    behaviour you would get from a service.
    """
    user = current_user()
    arguments = '"{}" --port {}'.format(ENTRY, port)
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>MonitorPad display agent. Runs in your desktop session so it can reconfigure monitors.</Description>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
      <Delay>PT10S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""".format(name=TASK_NAME, user=_xml(user), command=_xml(pythonw()),
           arguments=_xml(arguments), workdir=_xml(HERE))


def _xml(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def enable(port=8777):
    """Register (or replace) the logon task."""
    if not os.path.isfile(ENTRY):
        raise AutostartError("Cannot find {}".format(ENTRY))

    handle, path = tempfile.mkstemp(suffix=".xml", prefix="monitorpad-")
    os.close(handle)
    try:
        # schtasks insists on UTF-16 for /XML.
        with open(path, "w", encoding="utf-16") as stream:
            stream.write(_task_xml(port))
        code, output = _run(["/Create", "/TN", TASK_NAME, "/XML", path,
                             "/F"])
        if code != 0:
            raise AutostartError(output.strip() or "schtasks failed")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return True


def disable():
    if not is_enabled():
        return False
    code, output = _run(["/Delete", "/TN", TASK_NAME, "/F"])
    if code != 0:
        raise AutostartError(output.strip() or "schtasks failed")
    return True


def toggle(port=8777):
    if is_enabled():
        disable()
        return False
    enable(port)
    return True


def run_now():
    """Ask the scheduler to start the task immediately."""
    code, output = _run(["/Run", "/TN", TASK_NAME])
    if code != 0:
        raise AutostartError(output.strip() or "schtasks failed")


def main(argv):
    action = argv[1] if len(argv) > 1 else "status"
    port = int(argv[2]) if len(argv) > 2 else 8777
    try:
        if action == "enable":
            enable(port)
            print("MonitorPad will now start when you sign in.")
            print("Task: \\{}   (manage it in Task Scheduler)".format(
                TASK_NAME))
        elif action == "disable":
            print("Removed." if disable() else "It was not installed.")
        elif action == "status":
            info = status()
            print("auto-start: {}   ({})".format(
                "on" if info["enabled"] else "off", info["state"]))
        else:
            print("usage: autostart.py [enable|disable|status] [port]")
            return 2
    except AutostartError as exc:
        print("Failed: {}".format(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
