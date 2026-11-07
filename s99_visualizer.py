#!/usr/bin/env python3
"""
Aula S99 music visualizer driver (unofficial).

Protocol (decoded from a USBPcap capture of the official app):
  - Device: VID 0x1A2C, PID 0x4B64, HID collection usage page 0xFF01 / usage 1
  - Handshake (feature report, ID 7):  07 FB 01 00 00 00 00 00
  - Music frames (feature report, ID 7): 07 23 45 67 89 01 <mode> <level>
        mode  = 0..6  (in practice this changes the color style; shape is fixed)
        level = 0..~77 (current loudness; the keyboard animates by itself)

Install:   pip install hidapi pyaudiowpatch numpy
Run:       python s99_visualizer.py --test        (no audio, sweeps the level)
           python s99_visualizer.py --mode 0      (follow system audio)
           python s99_visualizer.py --dry-run     (audio only, no keyboard writes)
Close the official Aula app first, otherwise it fights over the same channel.
"""
import argparse
import colorsys
import json
import os
import datetime
import threading
import time

import numpy as np

VID, PID = 0x1A2C, 0x4B64
VENDOR_PAGE = 0xFF01
MAGIC = (0x23, 0x45, 0x67, 0x89, 0x01)


# ---------------------------------------------------------------- keyboard
def list_devices():
    import hid
    for d in hid.enumerate(VID, PID):
        print("usage_page", hex(d["usage_page"]), "usage", hex(d["usage"]),
              "iface", d["interface_number"], d["path"])


def open_keyboard():
    import hid
    for d in hid.enumerate(VID, PID):
        if d["usage_page"] == VENDOR_PAGE and d["usage"] == 1:
            dev = hid.device()
            dev.open_path(d["path"])
            return dev
    raise RuntimeError("S99 vendor channel (0xFF01) not found. Is it plugged in by "
                       "cable/dongle, and is the official app closed?")


def handshake(dev):
    dev.send_feature_report(bytes([7, 0xFB, 1, 0, 0, 0, 0, 0]))
    for _ in range(4):  # the official app polls the status report a few times
        time.sleep(0.017)
        try:
            dev.get_feature_report(7, 8)
        except OSError:
            pass


def send_level(dev, mode, level):
    level = max(0, min(255, int(level)))
    dev.send_feature_report(bytes([7, *MAGIC, mode, level]))


# ------------------------------------------------------------------- audio
def band_energy(x, rate, weights):
    """Weighted RMS energy in bass / mid / high bands of one audio window."""
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) / len(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
    total = 0.0
    for (lo, hi), w in zip(((20, 250), (250, 2000), (2000, 8000)), weights):
        m = (freqs >= lo) & (freqs < hi)
        if m.any():
            total += w * float(np.sqrt(np.mean(spec[m] ** 2)))
    return total


class Loopback:
    """Captures whatever Windows is currently playing (WASAPI loopback)."""

    def __init__(self, window=2048):
        import pyaudiowpatch as pa
        self._pa_mod = pa
        self.pa = pa.PyAudio()
        dev = self.pa.get_default_wasapi_loopback()
        self.rate = int(dev["defaultSampleRate"])
        self.ch = dev["maxInputChannels"]
        self.buf = np.zeros(window, dtype=np.float32)
        self.lock = threading.Lock()
        self.last = 0.0
        self.stream = self.pa.open(
            format=pa.paInt16, channels=self.ch, rate=self.rate, input=True,
            input_device_index=dev["index"], frames_per_buffer=512,
            stream_callback=self._cb)
        print("Capturing:", dev["name"], f"({self.rate} Hz, {self.ch} ch)")

    def _cb(self, data, frames, t, status):
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        x = x.reshape(-1, self.ch).mean(axis=1)
        with self.lock:
            self.buf = np.concatenate((self.buf, x))[-len(self.buf):]
            self.last = time.time()
        return (None, self._pa_mod.paContinue)

    def energy(self, weights):
        with self.lock:
            x = self.buf.copy()
            stale = time.time() - self.last > 0.25  # loopback is silent when idle
        return 0.0 if stale else band_energy(x, self.rate, weights)

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()


class Leveler:
    """Auto-gain + smoothing: raw energy -> 0..max_level."""

    def __init__(self, max_level, attack=0.6, release=0.15, decay=0.995,
                 gamma=0.8, floor=2e-4, gate=2e-5):
        self.max_level, self.attack, self.release = max_level, attack, release
        self.decay, self.gamma, self.floor, self.gate = decay, gamma, floor, gate
        self.peak = floor
        self.cur = 0.0

    def __call__(self, raw):
        self.peak = max(raw, self.peak * self.decay, self.floor)
        norm = 0.0 if raw < self.gate else min(1.0, raw / self.peak) ** self.gamma
        k = self.attack if norm > self.cur else self.release
        self.cur += (norm - self.cur) * k
        return int(round(self.cur * self.max_level))



# ------------------------------------------------------- screen color follow
PALETTE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s99_palette.json")


def load_palette():
    """7 entries: [r, g, b] the style looks like, or None if unset/skipped."""
    try:
        with open(PALETTE_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and len(data) == 7:
            return [tuple(c) if c else None for c in data]
    except (OSError, ValueError):
        pass
    return [None] * 7


def save_palette(palette):
    try:
        with open(PALETTE_FILE, "w") as f:
            json.dump([list(c) if c else None for c in palette], f)
    except OSError:
        pass


def _hs(rgb):
    h, s, _ = colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    return h * 360.0, s


def _circ(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def dominant_color(rgb):
    """Most common *colorful* hue in an HxWx3 uint8 image -> (r, g, b) or None.

    Counts hues (weighted by saturation*brightness) instead of averaging, so a
    screen that is half red / half green gives red or green, not muddy brown.
    Mostly gray / black / white screens return None.
    """
    px = rgb.reshape(-1, 3).astype(np.float32) / 255.0
    mx, mn = px.max(1), px.min(1)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    ok = (mx > 0.25) & (sat > 0.35)
    if ok.sum() < max(10, 0.02 * len(px)):
        return None
    pk, mx, sat = px[ok], mx[ok], sat[ok]
    r, g, b = pk[:, 0], pk[:, 1], pk[:, 2]
    d = np.maximum(pk.max(1) - pk.min(1), 1e-6)
    hue = np.where(mx == r, ((g - b) / d) % 6,
                   np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60.0
    bins = (hue / 15.0).astype(int) % 24
    score = np.bincount(bins, weights=sat * mx, minlength=24)
    score = score + 0.5 * (np.roll(score, 1) + np.roll(score, -1))
    best = int(score.argmax())
    sel = np.isin(bins, [(best - 1) % 24, best, (best + 1) % 24])
    col = pk[sel].mean(0) * 255.0
    return tuple(int(round(c)) for c in col)


def pick_style(color, palette, current=None, margin=20.0):
    """Closest calibrated style by hue; sticks with `current` unless clearly worse."""
    if color is None:
        return current
    h = _hs(color)[0]
    cands = {i: _circ(h, _hs(c)[0]) for i, c in enumerate(palette)
             if c is not None and _hs(c)[1] >= 0.25}
    if not cands:
        return current
    best = min(cands, key=cands.get)
    dcur = cands.get(current)
    if dcur is not None and current != best and dcur - cands[best] < margin:
        return current
    return best


class ScreenColor(threading.Thread):
    """Samples the primary monitor a few times a second (needs: pip install mss)."""

    def __init__(self, hz=3.0):
        super().__init__(daemon=True)
        import mss  # noqa: F401  (fail early if missing)
        self.hz = hz
        self.color = None      # latest dominant color, or None if screen is gray/dark
        self.frames = 0        # successful captures so far
        self.last_error = None
        self.stop_event = threading.Event()

    def run(self):
        import mss
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                print(f"[screen] capturing monitor 1: {mon['width']}x{mon['height']}", flush=True)
                while not self.stop_event.is_set():
                    try:
                        img = np.asarray(sct.grab(mon))[::24, ::24, :3][:, :, ::-1]
                        self.color = dominant_color(np.ascontiguousarray(img))
                        self.frames += 1
                        self.last_error = None
                    except Exception as e:
                        msg = f"{type(e).__name__}: {e}"
                        if msg != self.last_error:  # print each new error once
                            print(f"[screen] capture error: {msg}", flush=True)
                        self.last_error = msg
                    self.stop_event.wait(1.0 / self.hz)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"[screen] could not start screen capture: {self.last_error}", flush=True)


# --------------------------------------------- exact color (saved by keyboard)
WRITES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s99_writes.json")


def send_color(dev, r, g, b):
    """Static color. The keyboard SAVES this, so call it sparingly."""
    dev.send_feature_report(bytes([7, 0xF7, 3, 0, int(r), int(g), 3, int(b)]))


def normalize_color(c):
    """Scale so the brightest channel is 255 (like the official color picker);
    the keyboard's own brightness setting then controls how bright it looks."""
    m = max(c)
    if m <= 0:
        return None
    return tuple(int(round(v * 255.0 / m)) for v in c)


def ease_color(current, target, alpha):
    """Move `current` a fraction `alpha` of the way to `target` along the color
    wheel (hue/saturation), at full brightness. alpha=1 jumps straight there."""
    if current is None or alpha >= 1.0:
        return target
    hc, sc = _hs(current)
    ht, st = _hs(target)
    dh = ((ht - hc + 180.0) % 360.0) - 180.0  # shortest way around the wheel
    h = (hc + dh * alpha) % 360.0
    sat = max(0.0, min(1.0, sc + (st - sc) * alpha))
    r, g, b = colorsys.hsv_to_rgb(h / 360.0, sat, 1.0)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def color_distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def writes_today():
    today = datetime.date.today().isoformat()
    try:
        with open(WRITES_FILE) as f:
            d = json.load(f)
        if d.get("date") == today:
            return int(d.get("count", 0))
    except (OSError, ValueError):
        pass
    return 0


def count_write():
    n = writes_today() + 1
    try:
        with open(WRITES_FILE, "w") as f:
            json.dump({"date": datetime.date.today().isoformat(), "count": n}, f)
    except OSError:
        pass
    return n

# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Aula S99 music visualizer")
    ap.add_argument("--mode", type=int, default=0, choices=range(7),
                    help="color style 0-6 (try each one)")
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--max-level", type=int, default=77)
    ap.add_argument("--bass", type=float, default=1.0)
    ap.add_argument("--mid", type=float, default=0.5)
    ap.add_argument("--high", type=float, default=0.25)
    ap.add_argument("--test", action="store_true", help="sweep level, no audio")
    ap.add_argument("--dry-run", action="store_true", help="audio only, no keyboard")
    ap.add_argument("--list", action="store_true", help="list S99 HID interfaces")
    a = ap.parse_args()

    if a.list:
        return list_devices()

    dev = None if a.dry_run else open_keyboard()
    if dev:
        handshake(dev)
    audio = None if a.test else Loopback()
    lev = Leveler(a.max_level)
    weights = (a.bass, a.mid, a.high)
    dt = 1.0 / a.fps
    nxt = time.perf_counter()
    t0 = nxt
    print(f"Running, mode {a.mode}. Ctrl+C to stop.")
    try:
        while True:
            if a.test:  # triangle wave 0 -> max -> 0 over 4 seconds
                ph = ((time.perf_counter() - t0) % 4.0) / 4.0
                level = int((1 - abs(2 * ph - 1)) * a.max_level)
            else:
                level = lev(audio.energy(weights))
            if dev:
                send_level(dev, a.mode, level)
            else:
                print("#" * level)
            nxt += dt
            time.sleep(max(0.0, nxt - time.perf_counter()))
    except KeyboardInterrupt:
        pass
    finally:
        if dev:
            for _ in range(3):
                send_level(dev, a.mode, 0)
                time.sleep(dt)
            dev.close()
        if audio:
            audio.close()
        print("Stopped.")


if __name__ == "__main__":
    main()