# MonitorPad

Rearrange, enable, disable and reconfigure your Windows monitors from your
iPhone. Resolution, refresh rate, orientation, HDR and which screen is primary
— all from the couch.

It is two pieces:

- **The agent** — a small Python program that lives in your Windows
  notification area and talks to the display APIs.
- **The app** — a web app the agent serves. You add it to your iPhone Home
  Screen, where it gets its own icon and runs fullscreen with no browser
  chrome, like any other app.

Python standard library only. Nothing to `pip install`.

---

## Why a Home Screen app and not an App Store app

Building a native iOS app requires a Mac running Xcode and an Apple Developer
account to get the build onto your phone. On a Windows-only setup that route
is closed. An installable web app gets you an icon on the Home Screen, a
fullscreen app window, and it installs in about ten seconds with no developer
account.

---

## Setup

### 1. Start the agent

Double-click **`start.cmd`**. MonitorPad appears in the notification area
(the tray, next to the clock) — no console window. Click the icon for:

| Menu item | What it does |
| --- | --- |
| **Pair a phone…** | Opens a window with a fresh PIN and the address to type. |
| **Copy pairing link** | Puts the full tokenised link on the clipboard. |
| **Open MonitorPad** | Opens the app window on this PC (see below). |
| **Open in browser** | The same interface as an ordinary browser tab. |
| **Identify monitors** | Flashes a big number on each screen. |
| **Apply "…"** | One-tap for each saved profile. |
| **Start when I sign in** | Toggles auto-start (see below). |
| **Add to Start Menu** | Toggles the shortcut, so MonitorPad launches like any other app. |
| **View log** | Opens the agent's log file (see below). |
| **Open config folder** | Where the token, profiles and log live. |
| **Quit MonitorPad** | Stops the agent, rolling back anything unconfirmed. |

Double-clicking the icon opens the app window directly.

### On the PC it is a real window, not a tab

**Open MonitorPad** — from the tray, the Start Menu, or a double-click on the
tray icon — gives you a proper application window: no address bar, no tabs, no
bookmarks, its own taskbar button and its own icon. A tall, narrow box that
suits the single-column layout, which you can move, resize and snap like
anything else.

Under the hood that is Edge (or Chrome) in app mode, pointed at the same
interface your phone uses. It stays a single implementation rather than a
second, native UI that would drift out of step — but nothing about it reads as
a browser.

It runs in its own browser profile under `%LOCALAPPDATA%\MonitorPad\`, so it
never lands as a tab in the window you happen to be browsing in, and the
window is reused rather than piling up if you open it again.

The window also uses a tighter layout than the phone does — settings pair up
two to a line and the spacing comes in, so a typical set of displays fits
without scrolling. The phone keeps the roomier version, where controls have to
stay finger-sized.

Only the first launch picks a window size; after that it reopens at whatever
size you left it.

**Pin it to the taskbar** by finding MonitorPad in the Start Menu and choosing
Pin to taskbar. From then on it launches like any installed app, starting the
agent first if it is not already running.

Neither Edge nor Chrome installed? It falls back to a normal browser tab and
says so.

If Python is not installed, get it from [python.org](https://python.org) and
tick **Add python.exe to PATH** during setup. To watch the log instead of
using the tray, run `console.cmd`.

### 2. Let your phone through the firewall

The first time, Windows will probably block the incoming connection. Run this
in an **Administrator** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -Firewall
```

That adds an inbound rule for TCP 8777 on **private networks only**, so it
never opens up on café or hotel Wi-Fi. If your home network is marked Public
in Windows, the script will tell you — switch it to Private under
Settings → Network & internet.

### 3. Pair the phone

Click the tray icon → **Pair a phone…**. On your iPhone, on the same Wi-Fi,
open the address shown, type the six-digit PIN, and tap Connect. Then
**Share → Add to Home Screen**.

The PIN is single-use and expires after ten minutes; the menu hands out a
fresh one every time you open that window. Behind it sits a long random token
that your phone stores, so you only pair once.

### 4. Start it automatically

Tick **Start when I sign in** in the tray menu, or:

```powershell
powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -AutoStart
```

Undo either from the same menu item, or with `-Remove`.

> **Why this is not a Windows service**
>
> A service runs in session 0, which has no display configuration of its own
> and cannot draw a tray icon. The monitor layout belongs to your interactive
> desktop session, so anything that reconfigures it has to live there too.
>
> So auto-start registers a **scheduled task with a logon trigger** — the
> closest Windows equivalent that still runs in your session. It starts by
> itself ten seconds after you sign in, restarts up to three times a minute
> apart if it ever falls over, and is managed from Task Scheduler under the
> name `MonitorPad`. `Get-ScheduledTask MonitorPad` and
> `Start-ScheduledTask MonitorPad` work the way `sc.exe` would for a service.
>
> The one thing it cannot do is run before you sign in — nothing that touches
> your desktop can.

Because the task stores an absolute path, **move the folder before enabling
auto-start**, not after. If you do move it, re-run the tray toggle (or
`python agent\autostart.py enable`) from the new location.

---

## Using it

### Arrangement

Drag the numbered rectangles. They snap to each other's edges, and the
snapping is what you want — Windows requires the desktop to be contiguous, so
if you leave monitors floating apart, Windows will shove them together on its
own terms. The app warns you when that is about to happen.

The primary display is drawn in green and always sits at the origin;
everything else is positioned relative to it, which is exactly how Windows
stores it.

### Per-display controls

| Control | Notes |
| --- | --- |
| **On/off switch** | Attaches or detaches the monitor from the desktop. The app refuses to turn off your last remaining screen. Switching off the *primary* works too — the remaining displays slide across so one of them takes the origin, which is what Windows requires. Greyed out on a display marked **Not detected**. |
| **Resolution** | Every mode the driver reports, native one marked. |
| **Refresh rate** | Filtered to rates valid for the chosen resolution. |
| **Desktop** | Extend (its own desktop) or duplicate another display. Only appears where duplication is actually possible — see below. |
| **Orientation** | Landscape, portrait, and both flipped variants. |
| **HDR** | Only offered when the monitor reports HDR support on its current connection. Shows colour encoding, bit depth and SDR white level when on. |
| **Make primary** | The chip beside the badges. Moves the taskbar and decides where new windows open. |

**Identify** (the monitor icon, top right) flashes a big number on each screen
so you can tell which rectangle is which without getting up.

### Extending and duplicating

Switching a display on **extends** the desktop onto it — it gets its own
space, its own resolution and its own place on the map.

To mirror instead, set **Desktop** to *Duplicate …* on the display that
should show a copy. It then loses its own rectangle and disappears from the
arrangement map, because it no longer has a position of its own.

Windows can only duplicate displays driven by the same graphics adapter, so
the option only lists the ones it can actually pair with. If a display shows
no **Desktop** row at all, there is nothing on its adapter to duplicate.

Internally this is about *sources*: a source is one desktop surface, and a
display is a path from a source to a panel. Two paths on one source show the
same surface — that is duplication. Extending means every display owns a
source.

### Collapsing displays

Tap a display's name to fold it away; tap again to open it. The header keeps
showing the things worth knowing at a glance — the port, the current mode,
and the badges — so a folded card is still informative.

Displays start folded, so the list opens as a summary of the whole setup and
you unfold only the one you came to change. Each remembers what you left it
as, per device, so the phone and the PC can differ. **Collapse all** in the
Displays header folds or unfolds everything at once.

### Nothing applies until you tap Apply

Changes collect in a bar at the bottom. Tap **Apply** to send them all at once
— the desktop reflows once, not once per setting — or **Discard** to drop
them.

### The safety net

If you pick a mode your monitor cannot actually display, you would normally be
stuck looking at a black screen with no way to click "revert". So:

**Every risky change asks you to confirm, and rolls itself back after 15
seconds if you don't.**

Resolution, refresh rate, arrangement, enable/disable and primary all count as
risky. HDR toggles apply immediately, since they are trivially reversible from
the app.

The rollback happens on the PC, not the phone, so it still fires if your phone
drops off Wi-Fi, the app crashes, or you simply walk away. If you stop the
agent while a change is unconfirmed, it rolls back on the way out.

The rollback checks its own work. A monitor that has just been switched back
on takes a moment to republish its modes, so the geometry pass retries until
the desktop actually matches the snapshot. If something still will not go
back, the app says so in a banner rather than quietly leaving you somewhere in
between.

A batch is also validated as a whole before anything is applied, so a request
that would end up turning off every display is refused without a single screen
going dark.

### New monitors

Nothing needs configuring when you plug something in. Every request re-reads
the hardware from Windows, so a new display shows up on the phone within about
eight seconds — or immediately if you pull to refresh — with its own modes,
refresh rates and HDR support. If Windows has not extended the desktop onto it
yet, it appears switched off with a toggle ready.

Each display is identified by a hash of its Windows device path, which encodes
the panel's EDID and the connector it is plugged into. That survives reboots
and standby, so saved profiles keep pointing at the right screen.

Two consequences worth knowing:

- **Moving a monitor to a different port makes it a new display** as far as
  MonitorPad is concerned, because Windows gives it a different device path.
  The old entry sticks around as *Not detected* until you forget it.
- **Two identical panels are told apart by which port they are on**, so
  swapping their cables swaps their settings. Windows behaves the same way —
  there is nothing in the EDID of two identical monitors to distinguish them.

Where two displays report the same name, the "#1"/"#2" suffix is allocated
once and remembered, so it keeps meaning the same physical screen even after
one is switched off, moved to another port, or joined by a third of the same
model.

### Profiles

Save a whole setup — arrangement, resolutions, refresh rates, HDR, which
monitors are on — under a name, and restore it in one tap. Useful for
"Work" versus "Gaming" versus "Movie night, TV only". Applying a profile is
covered by the same 15-second confirm.

---

## Security

The agent can reconfigure your displays, so treat it like any other remote
control for your PC:

- **LAN only.** It binds to your local network. There is no port forwarding
  and nothing is exposed to the internet.
- **Private networks only.** The firewall rule the setup script adds is scoped
  to private profiles.
- **Token-authenticated.** Every API call needs a 32-character random token,
  compared in constant time. Unauthenticated callers get a 401.
- **PINs are short-lived.** Six digits, ten minutes, single use, five wrong
  guesses and it is dead.
- **Plain HTTP.** Fine on a home network you trust; anyone already on that
  network who can sniff your traffic could capture the token. If that matters
  to you, put the PC and phone on a Tailscale tailnet and point the app at the
  tailnet address instead — no other changes needed.

Revoke a phone's access at any time:

```
agent\> python server.py --new-token
```

That invalidates every paired device. Pair again with a fresh PIN.

---

## Troubleshooting

**The phone cannot reach the PC.** Check both are on the same Wi-Fi (not one
on a guest network, and not the phone on cellular). Then check the firewall
rule — see step 2. Some routers have "AP isolation" or "client isolation"
switched on, which blocks devices from seeing each other; turn it off.

**"Mode not supported by this monitor".** The driver advertised a mode the
panel will not actually take, usually over a cable that cannot carry the
bandwidth. Nothing changed; pick another mode.

**HDR is greyed out.** The monitor is not reporting HDR support on its current
connection. Common causes: an HDMI or DisplayPort cable below the required
spec, an input on the display that is not in its high-bandwidth mode (on LG
TVs this is "HDMI Deep Colour" / "Ultra HD Deep Colour" per input), or the
resolution and refresh rate together exceeding what the link can carry. Windows
sometimes reports HDR as force-disabled, and the app will say so.

**A disabled monitor shows no resolution options.** Windows only reports modes
for monitors attached to the desktop. Switch it on, Apply, and the modes
appear.

**A display I own is not in the list at all.** Windows has no EDID on that
output, so as far as it is concerned nothing is plugged in. This is almost
always a TV that is powered off, in standby, or showing a different input; an
HDMI link that is down looks identical to an empty port.

Displays Windows cannot see are hidden rather than shown as dead entries.
Power one on or switch it to the right input and it appears on its own within
a few seconds, ready to enable.

MonitorPad still remembers them behind the scenes — that is what keeps labels
like "(HDMI #1)" and "(HDMI #2)" from shuffling when one twin is asleep, and
what keeps saved profiles pointing at the right screen. To see them listed
again, set `"showDisconnected": true` in
`%LOCALAPPDATA%\MonitorPad\config.json` and restart the agent; they then show
as *Not detected* with a **Forget this display** action. Either way an entry
is dropped automatically once it has been gone for 60 days.

**Changes keep reverting.** That is the safety net doing its job — you are
not tapping Keep within 15 seconds. The dialog is in the app; if the app was
backgrounded when the change landed, reopen it faster, or lengthen
`CONFIRM_SECONDS` at the top of `agent/server.py`.

**The app looks stale after changing something in Windows itself.** It polls
every 8 seconds; pull the refresh button top right to force it.

**Is it still talking to the PC?** The dot beside the computer's name in the
header is green while the agent is answering and red when it is not. The app
says so once when the connection drops rather than repeating itself every
poll.

### The log

The agent writes to `%LOCALAPPDATA%\MonitorPad\monitorpad.log` — **View log**
in the tray menu opens it. It rotates at 512 KB and keeps three old files.

Every request is recorded with its result, how long it took and which device
made it, alongside each apply, rejection, rollback and pairing. That trail is
what makes an intermittent fault diagnosable after it has happened: a gap in
the polling, a burst of retries, or an apply that ran slowly all show up
plainly.

```
19:44:17  INFO    MonitorPad starting on 0.0.0.0:8777  (python 3.14.3, pid 984)
19:44:17  INFO    Reachable at: 192.168.68.50
19:44:20  DEBUG   GET /api/state -> 200 in 51ms  [192.168.68.61]
19:44:20  INFO    Rejected /api/apply: Unknown display nope.
19:44:20  WARNING client [192.168.68.61]: retried GET /api/state after: Load failed
```

That last kind of line is the phone reporting a request that failed before it
reached the PC. The agent cannot see those itself — they never arrive — so
the app tells it after the retry succeeds.

**The tray icon vanished.** If Explorer restarted, MonitorPad re-adds itself
automatically. If it is genuinely gone, check whether the process is still
alive — `Get-Process pythonw` — and look in the tray overflow menu, since
Windows hides new icons there by default. Drag it onto the taskbar to pin it.

**Auto-start is on but nothing runs after a reboot.** The task waits ten
seconds after sign-in. If it still does not appear, check it in Task
Scheduler: `Get-ScheduledTaskInfo MonitorPad` shows the last result. `0x0`
means it ran fine. Most failures are a moved folder — re-register from the
new location.

**"Could not listen on port 8777."** MonitorPad is already running; look for
it in the tray overflow. Or something else has the port, in which case start
it with `--port 8800` and re-pair.

---

## Layout

```
monitorpad/
  start.cmd              Launch into the tray (double-click this)
  console.cmd            Same thing with a visible log, for troubleshooting
  setup-windows.ps1      Firewall rule and auto-start
  agent/
    monitorpad.pyw       Tray entry point: server thread + message loop
    open_app.pyw         What the shortcuts run: start agent, show window
    tray.py              Shell_NotifyIcon tray icon and menu, via ctypes
    appwindow.py         Opens the interface as a real desktop window
    shortcut.py          Start Menu / desktop shortcuts
    autostart.py         Registers/removes the logon scheduled task
    pair_window.py       The "pair your phone" dialog
    server.py            HTTP API, auth, pairing, revert watchdog
    display.py           Monitor enumeration and control
    ccd.py               ctypes bindings for the Win32 display APIs
    identify.py          The big-number overlays
    monitorpad.ico       Tray icon, 16px through 256px
    web/                 The phone app
```

The interface is one stylesheet driven by tokens on `:root` — a 4px spacing
scale, a five-step type scale, and a set of colour roles that swap for light
mode. The tighter desktop layout is mostly a matter of redefining a handful
of those tokens inside one media query rather than overriding rules
individually. Every interactive element takes a visible focus ring, and
animation respects `prefers-reduced-motion`.

The tray app runs the Windows message loop on the main thread — Windows
requires the thread that created a window to pump its messages — with the
HTTP server on a background thread beside it. Anything that needs its own
event loop (the pairing dialog, the identify overlays) is a short-lived
subprocess, so Tk never has to share a thread with either.

`ccd.py` wraps two Windows APIs. The modern **CCD** API
(`QueryDisplayConfig` / `SetDisplayConfig`) is what Settings itself uses, and
is the only way to read real monitor names, enable or disable an output, and
control HDR. The older `ChangeDisplaySettingsEx` is still the most reliable
way to enumerate supported modes and to commit geometry changes for several
monitors atomically, so both are used where each is strongest.

Configuration and saved profiles live in
`%LOCALAPPDATA%\MonitorPad\config.json`.
