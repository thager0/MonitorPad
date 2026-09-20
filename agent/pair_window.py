"""The 'pair your phone' window the tray icon opens.

A separate short-lived process, like identify.py, so Tk never has to share a
thread with the Win32 message loop or the HTTP server.

argv[1] is JSON: {"pin", "url", "host", "seconds", "link"}
"""

import json
import sys
import tkinter as tk

BG = "#0b1020"
CARD = "#141b31"
TEXT = "#e8edfb"
DIM = "#94a3c8"
ACCENT = "#5b8cff"
GOOD = "#31d0aa"


class PairWindow:
    def __init__(self, root, info):
        self.root = root
        self.info = info
        self.remaining = int(info.get("seconds", 600))

        root.title("Pair your phone with MonitorPad")
        root.configure(bg=BG)
        root.resizable(False, False)

        outer = tk.Frame(root, bg=BG, padx=28, pady=24)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Pair your phone", bg=BG, fg=TEXT,
                 font=("Segoe UI", 17, "bold")).pack(anchor="w")
        tk.Label(outer, text="Both devices need to be on the same Wi-Fi.",
                 bg=BG, fg=DIM, font=("Segoe UI", 10)).pack(anchor="w",
                                                            pady=(2, 18))

        self._step(outer, "1", "Open this address in Safari on your iPhone")
        address = tk.Label(outer, text=info["url"], bg=CARD, fg=ACCENT,
                           font=("Consolas", 15, "bold"), padx=16, pady=11)
        address.pack(fill="x", pady=(6, 16))

        self._step(outer, "2", "Enter this pairing PIN")
        pin = info["pin"]
        self.pin_label = tk.Label(
            outer, text="{}  {}  {}".format(pin[0:2], pin[2:4], pin[4:6]),
            bg=CARD, fg=GOOD, font=("Consolas", 30, "bold"), pady=14)
        self.pin_label.pack(fill="x", pady=(6, 4))
        self.countdown = tk.Label(outer, text="", bg=BG, fg=DIM,
                                  font=("Segoe UI", 9))
        self.countdown.pack(anchor="w", pady=(0, 16))

        self._step(outer, "3", "Tap Share, then Add to Home Screen")
        tk.Label(outer,
                 text="It then behaves like any other app on your phone.",
                 bg=BG, fg=DIM, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", pady=(4, 20))

        buttons = tk.Frame(outer, bg=BG)
        buttons.pack(fill="x")
        self.copy_button = tk.Button(
            buttons, text="Copy link", command=self.copy_link,
            bg=CARD, fg=TEXT, activebackground=ACCENT, activeforeground=TEXT,
            relief="flat", padx=16, pady=7, font=("Segoe UI", 10),
            cursor="hand2", borderwidth=0)
        self.copy_button.pack(side="left")
        tk.Button(buttons, text="Close", command=root.destroy,
                  bg=ACCENT, fg="#ffffff", activebackground=ACCENT,
                  activeforeground="#ffffff", relief="flat", padx=20, pady=7,
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  borderwidth=0).pack(side="right")

        root.bind("<Escape>", lambda _event: root.destroy())
        self.tick()
        self.centre()

    def _step(self, parent, number, text):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", anchor="w")
        tk.Label(row, text=number, bg=ACCENT, fg="#ffffff",
                 font=("Segoe UI", 9, "bold"), width=2).pack(side="left")
        tk.Label(row, text=text, bg=BG, fg=TEXT,
                 font=("Segoe UI", 11)).pack(side="left", padx=(9, 0))

    def copy_link(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.info.get("link", self.info["url"]))
        self.copy_button.configure(text="Copied")
        self.root.after(1500,
                        lambda: self.copy_button.configure(text="Copy link"))

    def tick(self):
        if self.remaining <= 0:
            self.pin_label.configure(fg="#ff5d6c", text="expired")
            self.countdown.configure(
                text="Reopen this window from the tray for a new PIN.")
            return
        minutes, seconds = divmod(self.remaining, 60)
        self.countdown.configure(
            text="Valid for {}:{:02d}, for one device.".format(minutes,
                                                               seconds))
        self.remaining -= 1
        self.root.after(1000, self.tick)

    def centre(self):
        # -topmost has to go on first: applying it to a window that has not
        # been mapped yet makes Tk rebuild the toplevel, which throws away
        # any position set beforehand and drops it at the default cascade
        # spot instead.
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry("{}x{}+{}+{}".format(width, height,
                                                max(0, x), max(0, y)))
        self.root.after(400, lambda: self.root.attributes("-topmost", False))


def main():
    if len(sys.argv) < 2:
        return 2
    info = json.loads(sys.argv[1])
    root = tk.Tk()
    PairWindow(root, info)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
