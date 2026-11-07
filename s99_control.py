#!/usr/bin/env python3
"""
Aula S99 control panel (unofficial). Needs s99_visualizer.py in the same folder.

Two engines:
  Music pulse        - live music packets, 7 preset color styles (safe to stream)
  Exact screen color - sets the keyboard to the dominant screen color. The
                       keyboard SAVES these colors, so updates are slow and capped.

Install:  pip install hidapi pyaudiowpatch numpy mss pynput
          (pynput is optional: it makes the shortcuts work in any window)
Run:      python s99_control.py

Shortcuts (Music pulse engine):
  Ctrl+Alt+1 ... Ctrl+Alt+7     jump to color style 0-6
  Ctrl+Alt+PageUp / PageDown    next / previous color style
Close the official Aula app first.
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import colorchooser, ttk

import s99_visualizer as core

DEFAULTS = dict(mode=0, bass=1.0, mid=0.5, high=0.25, max_level=77,
                attack=0.6, release=0.15, source="music",
                interval=30, threshold=60, daily_cap=400, smooth=0.0)
SWITCH_DWELL = 1.5  # seconds a music style must stay before the next switch


def start_global_hotkeys(q):
    """System-wide shortcuts via pynput (optional). Returns the listener or None.
    Callbacks only put events on a queue; the window applies them on its own thread."""
    try:
        from pynput import keyboard
        keys = {f"<ctrl>+<alt>+{n + 1}": (lambda n=n: q.put(("set", n))) for n in range(7)}
        keys["<ctrl>+<alt>+<page_up>"] = lambda: q.put(("step", 1))
        keys["<ctrl>+<alt>+<page_down>"] = lambda: q.put(("step", -1))
        listener = keyboard.GlobalHotKeys(keys)
        listener.daemon = True
        listener.start()
        return listener
    except Exception:
        return None


class Worker(threading.Thread):
    """MUSIC engine: streams frames to the keyboard; reads live settings."""

    def __init__(self, params):
        super().__init__(daemon=True)
        self.params = params
        self.stop_event = threading.Event()
        self.level = 0
        self.status = "starting"
        self.note = ""
        self.disable_follow = False
        self.error = None

    @staticmethod
    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def run(self):
        dev = audio = screen = None
        current = None
        last_switch = 0.0
        flast_kind, flast_log = None, 0.0
        try:
            dev = core.open_keyboard()
            core.handshake(dev)
            self.log("music engine started")
            lev = core.Leveler(self.params["max_level"])
            dt = 1.0 / 30
            nxt = t0 = time.perf_counter()
            self.status = "running"
            while not self.stop_event.is_set():
                p = dict(self.params)  # snapshot of the current settings
                lev.max_level, lev.attack, lev.release = p["max_level"], p["attack"], p["release"]

                if p["hold_level"] is not None:  # calibration: steady light
                    self.level = p["hold_level"]
                elif p["source"] == "music":
                    if audio is None:
                        audio = core.Loopback()
                    self.level = lev(audio.energy((p["bass"], p["mid"], p["high"])))
                else:  # test sweep: triangle wave over 4 seconds
                    ph = ((time.perf_counter() - t0) % 4.0) / 4.0
                    self.level = int((1 - abs(2 * ph - 1)) * p["max_level"])

                style = p["mode"]
                self.note = ""
                if p["follow_screen"]:
                    if screen is None:
                        try:
                            screen = core.ScreenColor()
                            screen.start()
                            self.log("follow screen: capture thread started")
                        except ImportError:
                            self.params["follow_screen"] = False
                            self.disable_follow = True
                            self.note = "Install mss first:  pip install mss"
                            self.log("follow screen needs the mss library:  pip install mss")
                    if screen is not None:
                        usable = sum(c is not None for c in p["palette"])
                        raw = screen.color
                        extra = f" (audio level {self.level})"
                        detail = None
                        if screen.last_error:
                            kind, detail = "error", f"follow screen: capture failing: {screen.last_error}"
                        elif screen.frames == 0:
                            kind, detail = "wait", "follow screen: waiting for the first screen capture"
                        elif usable < 2:
                            kind = "calib"
                            detail = (f"follow screen: only {usable} style(s) calibrated, need at least 2 "
                                      "- click Calibrate... and set the colors")
                            self.note = "Follow screen: calibrate at least 2 styles first"
                        else:
                            cand = core.pick_style(raw, p["palette"], current)
                            now = time.time()
                            if raw is None:
                                kind = "gray"
                                detail = f"follow screen: no dominant color on screen, staying on style {current}" + extra
                            elif cand != current and (current is None or now - last_switch >= SWITCH_DWELL):
                                self.log(f"follow screen: screen {raw} -> style {cand} (was {current})" + extra)
                                current, last_switch = cand, now
                                kind = "switched"
                            elif cand != current:
                                kind = "dwell"
                                detail = f"follow screen: screen {raw} prefers style {cand}, waiting before switching" + extra
                            else:
                                kind = "hold"
                                detail = f"follow screen: screen {raw} -> style {current} (unchanged)" + extra
                            if current is not None:
                                style = current
                                self.note = f"Following screen -> style {current}"
                        if detail and (kind != flast_kind or time.time() - flast_log >= 2.0):
                            self.log(detail)
                            flast_log = time.time()
                        flast_kind = kind
                elif screen is not None:
                    screen.stop_event.set()
                    screen = None
                    current = None

                core.send_level(dev, style, self.level)
                nxt += dt
                time.sleep(max(0.0, nxt - time.perf_counter()))
        except Exception as e:  # report in the UI instead of crashing
            self.error = str(e)
        finally:
            try:
                if dev:
                    for _ in range(3):
                        core.send_level(dev, self.params["mode"], 0)
                        time.sleep(0.03)
                    dev.close()
                if audio:
                    audio.close()
                if screen:
                    screen.stop_event.set()
            except Exception:
                pass
            self.level = 0
            self.status = "stopped"


class ScreenWorker(threading.Thread):
    """EXACT-COLOR engine: slow, change-gated, capped writes of static colors.
    Prints what it sees and why it did or didn't send, to the terminal."""

    def __init__(self, params):
        super().__init__(daemon=True)
        self.params = params
        self.stop_event = threading.Event()
        self.level = 0
        self.status = "starting"
        self.note = ""
        self.disable_follow = False
        self.error = None

    @staticmethod
    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def run(self):
        dev = screen = None
        last_sent = None
        shown = None  # smoothed color the worker is easing toward the screen color
        last_time = 0.0
        last_kind, last_log = None, 0.0
        try:
            dev = core.open_keyboard()
            self.log("keyboard channel opened")
            screen = core.ScreenColor()  # raises ImportError if mss is missing
            screen.start()
            self.log("screen capture thread started")
            self.status = "running"
            while not self.stop_event.wait(0.25):
                p = dict(self.params)
                raw = screen.color
                done = core.writes_today()
                dist = None
                if screen.last_error:
                    kind, detail = "error", f"screen capture failing: {screen.last_error}"
                elif screen.frames == 0:
                    kind, detail = "wait", "waiting for the first screen capture"
                elif done >= p["daily_cap"]:
                    kind, detail = "cap", f"daily limit reached ({done}/{p['daily_cap']}) - paused"
                    self.note = f"Daily limit reached ({done}). Paused until tomorrow or raise the limit."
                elif raw is None:
                    kind, detail = "gray", "no dominant color (screen is mostly gray/dark/white) - keeping last color"
                    self.note = "No dominant color on screen - keeping the last color"
                else:
                    target = core.normalize_color(raw)
                    shown = core.ease_color(shown, target, 1.0 - p["smooth"])
                    col = shown
                    now = time.time()
                    dist = None if last_sent is None else core.color_distance(col, last_sent)
                    changed = dist is None or dist >= p["threshold"]
                    due = last_sent is None or now - last_time >= p["interval"]
                    if changed and due:
                        core.send_color(dev, *col)
                        n = core.count_write()
                        self.log(f"SENT color {col} (screen {raw}) - update #{n} today")
                        last_sent, last_time = col, now
                        kind, detail = "sent", None
                    elif not changed:
                        kind = "same"
                        detail = (f"screen {raw} -> {col}: change {dist:.0f} is below threshold "
                                  f"{p['threshold']}, not sending")
                    else:
                        kind = "wait_interval"
                        detail = (f"screen {raw} -> {col}: changed by {dist:.0f} but waiting "
                                  f"{p['interval'] - (now - last_time):.1f}s more (interval)")
                    self.note = f"Current color {last_sent}" if last_sent else ""

                # print on a change of situation, or a heartbeat every 2 s
                if detail and (kind != last_kind or time.time() - last_log >= 2.0):
                    self.log(detail)
                    last_log = time.time()
                last_kind = kind
        except ImportError:
            self.error = "Install mss first:  pip install mss"
            self.log(self.error)
        except Exception as e:
            import traceback
            self.error = str(e)
            self.log(f"ERROR: {e}")
            traceback.print_exc()
        finally:
            try:
                if screen:
                    screen.stop_event.set()
                if dev:
                    dev.close()
            except Exception:
                pass
            self.log("exact screen color stopped")
            self.status = "stopped"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Aula S99 Visualizer")
        self.resizable(False, False)
        self.params = dict(DEFAULTS, follow_screen=False, hold_level=None,
                           palette=core.load_palette(), engine="music")
        self.worker = None
        self.cal = None
        self.prev_follow = False
        self.engine_var = tk.StringVar(value="music")
        self.mode_var = tk.IntVar(value=DEFAULTS["mode"])
        self.source_var = tk.StringVar(value=DEFAULTS["source"])
        self.follow_var = tk.BooleanVar(value=False)
        self.value_labels = {}
        self.hotkeys = queue.Queue()
        self.hk_listener = start_global_hotkeys(self.hotkeys)
        if self.hk_listener is None:  # fallback: only while this window has focus
            for n in range(7):
                self.bind_all(f"<Control-Alt-Key-{n + 1}>",
                              lambda e, n=n: self.hotkeys.put(("set", n)))
            self.bind_all("<Control-Alt-Prior>", lambda e: self.hotkeys.put(("step", 1)))
            self.bind_all("<Control-Alt-Next>", lambda e: self.hotkeys.put(("step", -1)))

        pad = dict(padx=10, pady=4)
        frm = ttk.Frame(self, padding=10)
        frm.grid()
        row = 0

        ttk.Label(frm, text="Lighting").grid(row=row, column=0, sticky="w", **pad)
        eng = ttk.Frame(frm)
        eng.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        for text, val in (("Music pulse", "music"), ("Exact screen color", "screen")):
            ttk.Radiobutton(eng, text=text, value=val, variable=self.engine_var,
                            command=self.on_engine).pack(side="left", padx=3)
        row += 1

        # ---- music engine controls
        mus = ttk.LabelFrame(frm, text="Music pulse settings", padding=6)
        mus.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        r = 0
        ttk.Label(mus, text="Color style (0-6)").grid(row=r, column=0, sticky="w", padx=6, pady=3)
        modes = ttk.Frame(mus)
        modes.grid(row=r, column=1, columnspan=2, sticky="w")
        for m in range(7):
            ttk.Radiobutton(modes, text=str(m), value=m, variable=self.mode_var,
                            command=self.on_mode).pack(side="left", padx=3)
        r += 1
        ttk.Label(mus, text="Screen color").grid(row=r, column=0, sticky="w", padx=6, pady=3)
        scr = ttk.Frame(mus)
        scr.grid(row=r, column=1, columnspan=2, sticky="w")
        ttk.Checkbutton(scr, text="Follow screen (nearest preset)", variable=self.follow_var,
                        command=self.on_follow).pack(side="left", padx=3)
        ttk.Button(scr, text="Calibrate...", command=self.open_calibration).pack(side="left", padx=6)
        r += 1
        ttk.Label(mus, text="Source").grid(row=r, column=0, sticky="w", padx=6, pady=3)
        src = ttk.Frame(mus)
        src.grid(row=r, column=1, columnspan=2, sticky="w")
        for text, val in (("System audio", "music"), ("Test sweep", "test")):
            ttk.Radiobutton(src, text=text, value=val, variable=self.source_var,
                            command=self.on_source).pack(side="left", padx=3)
        r += 1
        rows = [("Bass", "bass", 0, 2, 2), ("Mids", "mid", 0, 2, 2),
                ("Highs", "high", 0, 2, 2), ("Max brightness", "max_level", 10, 77, 0),
                ("Attack (how fast it rises)", "attack", 0.05, 1.0, 2),
                ("Release (how fast it falls)", "release", 0.02, 1.0, 2)]
        for label, key, lo, hi, nd in rows:
            self.add_slider(mus, r, label, key, lo, hi, nd)
            r += 1
        ttk.Label(mus, text="Level").grid(row=r, column=0, sticky="w", padx=6, pady=3)
        self.meter = ttk.Progressbar(mus, maximum=77, length=260)
        self.meter.grid(row=r, column=1, columnspan=2, sticky="w", padx=10)

        # ---- exact screen color controls
        ex = ttk.LabelFrame(frm, text="Exact screen color settings (the keyboard saves "
                                      "these, so updates are slow and capped)", padding=6)
        ex.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        self.add_slider(ex, 0, "Update at most every (sec)", "interval", 0.5, 120, 1)
        self.add_slider(ex, 1, "Only if color changed by", "threshold", 5, 150, 0)
        self.add_slider(ex, 2, "Max updates per day", "daily_cap", 50, 20000, 0)
        self.add_slider(ex, 3, "Smoothness (fade between colors)", "smooth", 0.0, 0.95, 2)
        presets = ttk.Frame(ex)
        presets.grid(row=4, column=0, columnspan=3, sticky="w", padx=6, pady=4)
        ttk.Button(presets, text="Live preset (smooth)",
                   command=lambda: self.apply_preset(0.5, 10, 5000, 0.6)).pack(side="left", padx=3)
        ttk.Button(presets, text="Gentle preset (default)",
                   command=lambda: self.apply_preset(30, 60, 400, 0.0)).pack(side="left", padx=3)

        self.btn = ttk.Button(frm, text="Start", command=self.toggle)
        self.btn.grid(row=row, column=0, **pad)
        ttk.Button(frm, text="Reset sliders", command=self.reset).grid(row=row, column=1, **pad)
        row += 1
        self.status = ttk.Label(frm, text="Stopped", wraplength=380)
        self.status.grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        where = ("work in any window" if self.hk_listener else
                 "work only while this window is focused (pip install pynput for any window)")
        ttk.Label(frm, wraplength=380, foreground="#666666", text=(
            "Color shortcuts (Music pulse) " + where + ": Ctrl+Alt+1-7 picks style 0-6, "
            "Ctrl+Alt+PageUp / PageDown cycles.")).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self.tick)

    # ------------------------------------------------------------ sliders
    def add_slider(self, parent, row, label, key, lo, hi, nd):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
        var = tk.DoubleVar(value=self.params[key])
        setattr(self, f"var_{key}", var)

        def cb(_=None, key=key, var=var, nd=nd):
            v = round(var.get(), nd)
            self.params[key] = int(v) if nd == 0 else v
            self.value_labels[key].config(text=str(self.params[key]))

        ttk.Scale(parent, from_=lo, to=hi, variable=var, length=260,
                  command=cb).grid(row=row, column=1, padx=10, pady=3)
        lbl = ttk.Label(parent, text=str(self.params[key]), width=5)
        lbl.grid(row=row, column=2, sticky="w")
        self.value_labels[key] = lbl

    def reset(self):
        for key in ("bass", "mid", "high", "max_level", "attack", "release",
                    "interval", "threshold", "daily_cap", "smooth"):
            getattr(self, f"var_{key}").set(DEFAULTS[key])
            self.params[key] = DEFAULTS[key]
            self.value_labels[key].config(text=str(DEFAULTS[key]))

    # ----------------------------------------------------------- settings
    def apply_preset(self, interval, threshold, cap, smooth):
        for key, val in (("interval", interval), ("threshold", threshold),
                         ("daily_cap", cap), ("smooth", smooth)):
            getattr(self, f"var_{key}").set(val)
            self.params[key] = val
            self.value_labels[key].config(text=str(val))

    def on_engine(self):
        self.params["engine"] = self.engine_var.get()
        if self.worker and self.worker.is_alive():
            self.stop()
            self.start()  # joins the old worker, then starts the new engine

    def on_mode(self):
        self.params["mode"] = self.mode_var.get()

    def apply_hotkey(self, kind, val):
        """Runs on the window thread. Picking a style by hand turns off screen-follow."""
        if self.params["engine"] != "music" or self.params["hold_level"] is not None:
            return
        new = val if kind == "set" else (self.params["mode"] + val) % 7
        self.params["mode"] = new
        self.mode_var.set(new)
        if self.params["follow_screen"]:
            self.params["follow_screen"] = False
            self.follow_var.set(False)

    def on_source(self):
        self.params["source"] = self.source_var.get()

    def on_follow(self):
        self.params["follow_screen"] = self.follow_var.get()

    # -------------------------------------------------------- calibration
    def open_calibration(self):
        if self.cal is not None and self.cal.winfo_exists():
            self.cal.lift()
            return
        if self.params["engine"] != "music":  # calibration needs the music engine
            self.engine_var.set("music")
            self.on_engine()
        self.prev_follow = self.params["follow_screen"]
        self.params["follow_screen"] = False
        self.params["hold_level"] = 60  # steady light so colors are visible
        if not (self.worker and self.worker.is_alive()):
            self.start()
        win = self.cal = tk.Toplevel(self)
        win.title("Calibrate color styles")
        win.resizable(False, False)
        ttk.Label(win, wraplength=380, text=(
            "For each style: click Show, look at your keyboard, then click the "
            "colored box and pick the color you see. Use Skip for styles that "
            "cycle through rainbow colors. You only need to do this once.")
        ).grid(row=0, column=0, columnspan=4, padx=10, pady=8)
        self.swatches = {}
        for i in range(7):
            ttk.Label(win, text=f"Style {i}").grid(row=i + 1, column=0, padx=10, pady=3)
            ttk.Button(win, text="Show", command=lambda i=i: self.show_style(i)
                       ).grid(row=i + 1, column=1, padx=4)
            sw = tk.Button(win, width=10, command=lambda i=i: self.pick_color(i))
            sw.grid(row=i + 1, column=2, padx=4)
            ttk.Button(win, text="Skip", command=lambda i=i: self.skip_style(i)
                       ).grid(row=i + 1, column=3, padx=4)
            self.swatches[i] = sw
            self.paint_swatch(i)
        ttk.Button(win, text="Done", command=self.close_calibration
                   ).grid(row=8, column=0, columnspan=4, pady=10)
        win.protocol("WM_DELETE_WINDOW", self.close_calibration)

    def paint_swatch(self, i):
        c = self.params["palette"][i]
        if c is None:
            self.swatches[i].config(bg="#d9d9d9", text="not set")
        else:
            self.swatches[i].config(bg="#%02x%02x%02x" % tuple(c), text="")

    def show_style(self, i):
        self.params["mode"] = i
        self.mode_var.set(i)

    def pick_color(self, i):
        self.show_style(i)
        res = colorchooser.askcolor(parent=self.cal, title=f"Color of style {i}")
        if res and res[0]:
            self.params["palette"][i] = tuple(int(x) for x in res[0])
            self.paint_swatch(i)
            core.save_palette(self.params["palette"])

    def skip_style(self, i):
        self.params["palette"][i] = None
        self.paint_swatch(i)
        core.save_palette(self.params["palette"])

    def close_calibration(self):
        core.save_palette(self.params["palette"])
        self.params["hold_level"] = None
        self.params["follow_screen"] = self.prev_follow
        if self.cal is not None:
            self.cal.destroy()
        self.cal = None

    # ------------------------------------------------------------ control
    def start(self):
        old = self.worker
        if old and old.is_alive():
            if not old.stop_event.is_set():
                return  # already running
            old.join(timeout=1.5)
            if old.is_alive():
                return  # still shutting down
        cls = ScreenWorker if self.params["engine"] == "screen" else Worker
        self.worker = cls(self.params)
        self.worker.start()
        self.btn.config(text="Stop")

    def stop(self):
        if self.worker:
            self.worker.stop_event.set()
        self.btn.config(text="Start")

    def toggle(self):
        if self.worker and self.worker.is_alive() and not self.worker.stop_event.is_set():
            self.stop()
        else:
            self.start()

    def tick(self):
        while True:
            try:
                kind, val = self.hotkeys.get_nowait()
            except queue.Empty:
                break
            self.apply_hotkey(kind, val)
        w = self.worker
        if w:
            self.meter["value"] = w.level
            if w.disable_follow:
                self.follow_var.set(False)
                w.disable_follow = False
            if w.error:
                self.status.config(text="Error: " + w.error)
                self.btn.config(text="Start")
            else:
                text = w.status.capitalize()
                if w.note:
                    text += " - " + w.note
                if isinstance(w, ScreenWorker):
                    text += f"  (updates today: {core.writes_today()})"
                self.status.config(text=text)
        self.after(50, self.tick)

    def on_close(self):
        if self.hk_listener:
            self.hk_listener.stop()
        if self.worker:
            self.worker.stop_event.set()
            self.worker.join(timeout=1.5)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()