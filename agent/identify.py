"""Flash a large number on every active monitor.

Launched as a short-lived subprocess by the agent so its Tk event loop never
has to share a thread with the HTTP server. Takes one argv: a JSON list of
{"index", "label", "primary", "rect": {"x","y","width","height"}}.
"""

import json
import sys
import tkinter as tk

DURATION_MS = 3500
BACKDROP = "#0b1020"
ACCENT = "#5b8cff"
PRIMARY_ACCENT = "#31d0aa"


def build(root, spec):
    rect = spec["rect"]
    width = max(260, min(560, rect["width"] // 3))
    height = max(180, min(360, rect["height"] // 4))
    x = rect["x"] + (rect["width"] - width) // 2
    y = rect["y"] + (rect["height"] - height) // 2

    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.geometry("{}x{}+{}+{}".format(width, height, x, y))
    win.configure(bg=BACKDROP)
    win.attributes("-topmost", True)
    try:
        win.attributes("-alpha", 0.92)
    except tk.TclError:
        pass

    accent = PRIMARY_ACCENT if spec.get("primary") else ACCENT
    frame = tk.Frame(win, bg=BACKDROP, highlightthickness=4,
                     highlightbackground=accent, highlightcolor=accent)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text=str(spec["index"]), fg=accent, bg=BACKDROP,
             font=("Segoe UI", max(48, height // 3), "bold")).pack(
                 pady=(height // 12, 0))
    tk.Label(frame, text=spec["label"], fg="#e6ecff", bg=BACKDROP,
             font=("Segoe UI", 13)).pack()
    detail = "{}x{}".format(rect["width"], rect["height"])
    if spec.get("primary"):
        detail += "   .   primary"
    tk.Label(frame, text=detail, fg="#8ea0c8", bg=BACKDROP,
             font=("Segoe UI", 10)).pack(pady=(2, 0))
    return win


def main():
    if len(sys.argv) < 2:
        return 2
    specs = json.loads(sys.argv[1])

    root = tk.Tk()
    root.withdraw()
    for spec in specs:
        build(root, spec)
    root.after(DURATION_MS, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
