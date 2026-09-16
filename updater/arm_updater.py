#!/usr/bin/env python3
"""ARM Firmware Updater.

Fetches the firmware releases published in the public GitHub repo, finds the
ARM device on a serial port, and flashes the chosen release - no separate
.bin download, no command line.

    python arm_updater.py                # GUI; auto-detects the device
    python arm_updater.py --port COM7    # skip detection, use this port
    python arm_updater.py --repo owner/name   # point at a different repo

What "flash" does: connects to the ESP32 ROM bootloader through esptool
(auto-reset via DTR/RTS, no button pressing), verifies the chip is an
ESP32, and writes the release's *merged* image at offset 0x0. The merged
image carries the bootloader, partition table and app together, so it is
the right thing to write regardless of what was on the device before
(including the pre-2.1.0 partition layout). Afterwards it reconnects at the
application baud rate and asks the new firmware for its version.

Device detection: every serial port is asked <SYSTEM:PING> at 921600 baud;
the one that answers with the ARM JSON protocol is the device. If none
answers (e.g. the device is bricked or running something else), the first
port with a known USB-serial bridge is offered as an unverified guess.

Requires: pyserial, esptool >= 5 (tkinter ships with Python).
"""

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import serial
from serial.tools import list_ports

UPDATER_VERSION = "1.0.0"
DEFAULT_REPO = "TactorumInc/ARM-Firmware-Public"
RELEASES_URL = "https://api.github.com/repos/{repo}/releases?per_page=50"
ASSET_RE = re.compile(r"^firmware-merged_v(.+)\.bin$")

DEVICE_BAUD = 921600          # the ARM firmware's UART0 rate
FLASH_BAUD = 921600           # esptool transfer rate once the stub is running
PING_TIMEOUT = 1.2            # seconds to wait for a PING reply per attempt
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "ARMUpdater", "cache")

# USB-serial bridges seen on ESP32 boards: vid -> human name. Used only to
# rank ports and to offer a guess when nothing answers the protocol.
USB_SERIAL_VIDS = {
    0x0403: "FTDI",
    0x10C4: "Silicon Labs CP210x",
    0x1A86: "WCH CH340",
    0x303A: "Espressif USB",
}


# ---------------------------------------------------------------- GitHub ---

def fetch_releases(repo):
    """Return releases that carry a merged firmware image, newest first.

    Each entry: tag, name, version, prerelease, published, body, asset_name,
    asset_url, asset_size. Releases without a merged image are skipped (they
    are not flashable by this tool).
    """
    req = urllib.request.Request(
        RELEASES_URL.format(repo=repo),
        headers={"User-Agent": f"ARMUpdater/{UPDATER_VERSION}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    out = []
    for rel in data:
        if rel.get("draft"):
            continue
        for a in rel.get("assets", []):
            m = ASSET_RE.match(a.get("name", ""))
            if m:
                out.append({
                    "tag": rel["tag_name"],
                    "name": rel.get("name") or rel["tag_name"],
                    "version": m.group(1),
                    "prerelease": bool(rel.get("prerelease")),
                    "published": (rel.get("published_at") or "")[:10],
                    "body": rel.get("body") or "",
                    "asset_name": a["name"],
                    "asset_url": a["browser_download_url"],
                    "asset_size": int(a.get("size") or 0),
                })
                break
    return out


def download_asset(rel, progress):
    """Download rel's merged image into the cache (skipped if already there).
    progress(done_bytes, total_bytes) is called as data arrives. Returns the
    local path. The write goes to a temp file and is renamed on completion,
    so a half-finished download is never mistaken for a good image."""
    folder = os.path.join(CACHE_DIR, rel["tag"])
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, rel["asset_name"])
    if os.path.isfile(dest) and (rel["asset_size"] == 0 or
                                 os.path.getsize(dest) == rel["asset_size"]):
        progress(rel["asset_size"], rel["asset_size"])
        return dest
    req = urllib.request.Request(
        rel["asset_url"],
        headers={"User-Agent": f"ARMUpdater/{UPDATER_VERSION}"})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or rel["asset_size"] or 0)
        done = 0
        while True:
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            progress(done, total)
    if rel["asset_size"] and os.path.getsize(tmp) != rel["asset_size"]:
        os.remove(tmp)
        raise IOError(f"download truncated ({os.path.getsize(tmp)} of "
                      f"{rel['asset_size']} bytes)")
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------- device ---

def _read_json_lines(ser, seconds):
    """Yield parsed JSON objects arriving on ser for up to `seconds`. Binary
    stream frames and anything else that is not a JSON line are ignored."""
    end = time.monotonic() + seconds
    buf = b""
    while time.monotonic() < end:
        data = ser.read(4096)
        if not data:
            time.sleep(0.02)
            continue
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"{"):
                continue
            try:
                yield json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue


def _ask(ser, cmd, seconds):
    """Send <SYSTEM:cmd> and return the response's data field, or None."""
    ser.reset_input_buffer()
    ser.write(f"<SYSTEM:{cmd}>\n".encode())
    for obj in _read_json_lines(ser, seconds):
        if obj.get("module") == "SYSTEM" and obj.get("cmd") == cmd:
            return obj.get("data")
    return None


def query_device(port, attempts=2):
    """Return the firmware version string if an ARM device answers on port,
    else None. Two attempts, because opening the port may have just reset
    the ESP32 (auto-reset wiring) and it needs a moment to boot."""
    try:
        ser = serial.Serial(port, DEVICE_BAUD, timeout=0)
    except (serial.SerialException, OSError):
        return None
    try:
        for i in range(attempts):
            if i:
                time.sleep(1.5)
            if _ask(ser, "PING", PING_TIMEOUT) is not None:
                ver = _ask(ser, "VERSION", PING_TIMEOUT)
                return str(ver) if ver is not None else "?"
    finally:
        ser.close()
    return None


def list_candidate_ports():
    """All serial ports, known USB-serial bridges first. Each entry:
    (device, description, bridge_name_or_None)."""
    found = []
    for p in list_ports.comports():
        bridge = USB_SERIAL_VIDS.get(p.vid) if p.vid is not None else None
        found.append((p.device, p.description or "", bridge))
    found.sort(key=lambda t: (t[2] is None, t[0]))
    return found


def autodetect(log):
    """Find the ARM device. Returns (port, version, verified) or None."""
    ports = list_candidate_ports()
    if not ports:
        log("No serial ports found. Is the device plugged in and its USB "
            "driver installed?")
        return None
    for dev, desc, bridge in ports:
        log(f"Probing {dev} ({desc})...")
        ver = query_device(dev)
        if ver is not None:
            log(f"  ARM firmware v{ver} answered on {dev}")
            return dev, ver, True
    for dev, desc, bridge in ports:
        if bridge:
            log(f"No device answered the ARM protocol. {dev} has a {bridge} "
                f"USB-serial bridge - offering it as a guess (it may be a "
                f"device that is not running ARM firmware).")
            return dev, None, False
    log("No device answered and no USB-serial bridge was recognised.")
    return None


# ----------------------------------------------------------------- flash ---

class GuiLogger:
    """esptool TemplateLogger implementation that forwards to the GUI."""

    def __init__(self, log, progress):
        self._log = log
        self._progress = progress

    def print(self, *args, **kwargs):
        text = " ".join(str(a) for a in args)
        if text.strip():
            self._log(text.rstrip())

    def note(self, message):
        self.print(message)

    # esptool >= 5.3 routes warning()/error() through warn()/err() with an
    # optional suggestion; older versions call warning()/error() directly.
    def warn(self, message, suggestion=None):
        self.print("WARNING: " + str(message)
                   + (f" ({suggestion})" if suggestion else ""))

    def err(self, message, suggestion=None):
        self.print("ERROR: " + str(message)
                   + (f" ({suggestion})" if suggestion else ""))

    def warning(self, message):
        self.warn(message)

    def error(self, message):
        self.err(message)

    def debug(self, *args):
        pass

    def hint(self, message):
        self.print("Hint: " + str(message))

    def stage(self, finish=False):
        pass

    def set_verbosity(self, verbosity):
        pass

    def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30):
        self._progress(cur_iter, total_iters, prefix.strip() or "Writing")


def flash_image(port, path, log, progress):
    """Write the merged image at 0x0 through esptool. Raises on failure."""
    import esptool.logger as esplog
    from esptool import cmds
    from esptool.logger import TemplateLogger

    # TemplateLogger is abstract; register our duck-typed logger through the
    # class so isinstance() in set_logger accepts it.
    TemplateLogger.register(GuiLogger)
    esplog.log.set_logger(GuiLogger(log, progress))

    log(f"Connecting to {port}...")
    esp = cmds.detect_chip(port, baud=115200, connect_mode="default-reset",
                           connect_attempts=7)
    try:
        chip = esp.CHIP_NAME
        log(f"Found {chip}" + (f" ({esp.get_chip_description()})"
                               if hasattr(esp, "get_chip_description") else ""))
        if chip != "ESP32":
            raise RuntimeError(f"This is an {chip}, but ARM firmware targets the "
                               f"original ESP32 - refusing to flash it.")
        esp = cmds.run_stub(esp)
        try:
            esp.change_baud(FLASH_BAUD)
        except Exception as e:                       # noqa: BLE001
            log(f"Could not raise baud rate ({e}); continuing at 115200")
        cmds.attach_flash(esp)
        size = os.path.getsize(path)
        log(f"Writing {os.path.basename(path)} ({size:,} bytes) at 0x0...")
        cmds.write_flash(esp, [(0x0, path)], flash_mode="keep",
                         flash_freq="keep", flash_size="keep")
        log("Write complete, resetting device...")
        cmds.reset_chip(esp, "hard-reset")
    finally:
        try:
            esp._port.close()
        except Exception:                            # noqa: BLE001
            pass


# ------------------------------------------------------------------- GUI ---

class UpdaterGUI:
    def __init__(self, root, repo, preselect_port):
        self.root = root
        self.repo = repo
        self.q = queue.Queue()
        self.releases = []
        self.tasks = set()                   # running task keys
        self.detected = None                 # (port, version, verified)
        root.title(f"ARM Firmware Updater  v{UPDATER_VERSION}")
        root.geometry("760x640")
        root.minsize(640, 520)

        pad = {"padx": 8, "pady": 4}

        # --- firmware row ---
        fw = ttk.LabelFrame(root, text="Firmware release", padding=8)
        fw.pack(fill="x", **pad)
        self.rel_var = tk.StringVar()
        self.rel_box = ttk.Combobox(fw, textvariable=self.rel_var, state="readonly",
                                    width=60)
        self.rel_box.pack(side="left", fill="x", expand=True)
        self.rel_box.bind("<<ComboboxSelected>>", lambda e: self._show_notes())
        self.rel_refresh = ttk.Button(fw, text="Refresh", command=self.load_releases)
        self.rel_refresh.pack(side="left", padx=(8, 0))

        self.notes = scrolledtext.ScrolledText(root, height=7, state="disabled",
                                               font=("Consolas", 9), wrap="word")
        self.notes.pack(fill="x", padx=8)

        # --- device row ---
        dv = ttk.LabelFrame(root, text="Device", padding=8)
        dv.pack(fill="x", **pad)
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(dv, textvariable=self.port_var, width=40)
        self.port_box.pack(side="left")
        self.port_detect = ttk.Button(dv, text="Detect device", command=self.detect)
        self.port_detect.pack(side="left", padx=(8, 0))
        self.dev_lbl = ttk.Label(dv, text="", foreground="#666")
        self.dev_lbl.pack(side="left", padx=(12, 0))

        # --- action row ---
        act = ttk.Frame(root, padding=(8, 4))
        act.pack(fill="x")
        self.flash_btn = ttk.Button(act, text="Flash selected firmware",
                                    command=self.flash)
        self.flash_btn.pack(side="left")
        self.status_lbl = ttk.Label(act, text="", font=("", 10, "bold"))
        self.status_lbl.pack(side="left", padx=(12, 0))
        self.bar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=8, pady=(0, 4))

        # --- log ---
        lf = ttk.LabelFrame(root, text="Log", padding=4)
        lf.pack(fill="both", expand=True, **pad)
        self.log_box = scrolledtext.ScrolledText(lf, state="disabled",
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_config("err", foreground="#b00")
        self.log_box.tag_config("ok", foreground="#080")

        self.log(f"Releases from github.com/{repo}")
        self.root.after(50, self._pump)
        self.load_releases()
        if preselect_port:
            self.port_var.set(preselect_port)
            self.log(f"Using {preselect_port} (--port); skipping detection")
        else:
            self.detect()

    # --- plumbing ---
    def log(self, text, tag=None):
        self.q.put(("log", (text, tag)))

    def _log_now(self, text, tag=None):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n", tag or ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # Fetching releases (network) and finding the device (serial) are
    # independent and may overlap - both start at launch. Flashing owns the
    # serial port and must run alone, so it locks every button.
    BUTTONS = {"rel": "rel_refresh", "port": "port_detect", "flash": "flash_btn"}

    def _task_start(self, key, status):
        self.tasks.add(key)
        self.status_lbl.configure(text=status, foreground="#000")
        self._apply_button_states()

    def _task_end(self, key, ok, msg):
        self.tasks.discard(key)
        if msg:
            self.status_lbl.configure(text=msg, foreground="#080" if ok else "#b00")
        self._apply_button_states()

    def _apply_button_states(self):
        flashing = "flash" in self.tasks
        for key, attr in self.BUTTONS.items():
            busy = flashing or key in self.tasks
            getattr(self, attr).configure(state="disabled" if busy else "normal")
        if self.tasks:
            self.flash_btn.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log_now(*payload)
                elif kind == "progress":
                    cur, total, label = payload
                    self.bar.configure(maximum=max(total, 1), value=cur)
                    self.status_lbl.configure(text=label)
                elif kind == "releases":
                    self._fill_releases(payload)
                elif kind == "detected":
                    self._fill_detect(payload)
                elif kind == "done":
                    key, ok, msg = payload
                    self._task_end(key, ok, msg)
                    if not ok:
                        self.bar.configure(value=0)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _run(self, key, fn, status):
        if key in self.tasks or "flash" in self.tasks:
            return
        self._task_start(key, status)
        threading.Thread(target=fn, daemon=True).start()

    # --- releases ---
    def load_releases(self):
        def work():
            try:
                rels = fetch_releases(self.repo)
            except urllib.error.HTTPError as e:
                self.log(f"GitHub API error: HTTP {e.code} {e.reason}", "err")
                rels = []
            except Exception as e:                   # noqa: BLE001
                self.log(f"Could not fetch releases: {e}", "err")
                rels = []
            self.q.put(("releases", rels))
            self.q.put(("done", ("rel", True, "")))
        self._run("rel", work, "Fetching releases...")

    def _fill_releases(self, rels):
        self.releases = rels
        labels = []
        for i, r in enumerate(rels):
            tag = f"{r['name']}  ({r['published']})"
            if i == 0:
                tag += "   <- latest"
            if r["prerelease"]:
                tag += "   [pre-release]"
            labels.append(tag)
        self.rel_box["values"] = labels
        if rels:
            self.rel_box.current(0)
            self.log(f"{len(rels)} flashable release(s) found; latest is "
                     f"{rels[0]['name']}")
        else:
            self.rel_var.set("")
            self.log("No flashable releases found in this repo.", "err")
        self._show_notes()

    def _selected_release(self):
        i = self.rel_box.current()
        return self.releases[i] if 0 <= i < len(self.releases) else None

    def _show_notes(self):
        r = self._selected_release()
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        if r:
            head = f"{r['name']}  -  {r['asset_name']}  ({r['asset_size']:,} bytes)\n\n"
            self.notes.insert("end", head + (r["body"].strip() or "(no release notes)"))
        self.notes.configure(state="disabled")

    # --- device ---
    def detect(self):
        def work():
            ports = [p[0] for p in list_candidate_ports()]
            self.q.put(("detected", (ports, autodetect(self.log))))
            self.q.put(("done", ("port", True, "")))
        self._run("port", work, "Looking for the device...")

    def _fill_detect(self, payload):
        ports, found = payload
        self.port_box["values"] = ports
        self.detected = found
        if found:
            port, ver, verified = found
            self.port_var.set(port)
            if verified:
                self.dev_lbl.configure(text=f"ARM firmware v{ver}", foreground="#080")
            else:
                self.dev_lbl.configure(text="unverified guess - check the port",
                                       foreground="#b60")
        else:
            self.dev_lbl.configure(text="not found", foreground="#b00")
            if ports and not self.port_var.get():
                self.port_var.set(ports[0])

    # --- flash ---
    def flash(self):
        rel = self._selected_release()
        port = self.port_var.get().strip()
        if not rel:
            messagebox.showerror("No firmware", "Select a firmware release first.")
            return
        if not port:
            messagebox.showerror("No device", "Select or detect the device's COM port.")
            return
        installed = (self.detected[1] if self.detected and self.detected[0] == port
                     else None)
        msg = (f"Flash {rel['name']} to the device on {port}?\n\n"
               + (f"Currently installed: v{installed}\n" if installed else "")
               + f"Image: {rel['asset_name']}\n\n"
               "Do not unplug the device until the flash completes.")
        if not messagebox.askokcancel("Confirm flash", msg):
            return

        def progress(cur, total, label):
            self.q.put(("progress", (cur, total, label)))

        def work():
            try:
                self.log(f"Downloading {rel['asset_name']}...")
                path = download_asset(
                    rel, lambda d, t: progress(d, t, f"Downloading {d // 1024} KB"))
                self.log(f"Image ready: {path}")
                flash_image(port, path, self.log, progress)
                self.log("Waiting for the new firmware to boot...")
                time.sleep(2.0)
                ver = query_device(port, attempts=3)
                if ver is None:
                    self.log("Flash finished but the device did not answer "
                             "<SYSTEM:PING> afterwards. Power-cycle it and "
                             "press 'Detect device'.", "err")
                    self.q.put(("done", ("flash", False, "Flashed - device not answering")))
                    return
                self.detected = (port, ver, True)
                self.q.put(("detected", ([p[0] for p in list_candidate_ports()],
                                         self.detected)))
                if ver == rel["version"]:
                    self.log(f"Success: device reports firmware v{ver}", "ok")
                    self.q.put(("done", ("flash", True, f"Done - running v{ver}")))
                else:
                    self.log(f"Device reports v{ver} but the release is "
                             f"v{rel['version']} - the flash may not have taken.", "err")
                    self.q.put(("done", ("flash", False, f"Version mismatch: v{ver}")))
            except Exception as e:                   # noqa: BLE001
                self.log(f"FAILED: {e}", "err")
                self.q.put(("done", ("flash", False, "Flash failed - see log")))

        self._run("flash", work, "Starting...")


def main():
    ap = argparse.ArgumentParser(description="ARM firmware updater")
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help="GitHub owner/name to read releases from")
    ap.add_argument("--port", default=None,
                    help="serial port to use (skips auto-detection)")
    ap.add_argument("--list", action="store_true",
                    help="print the flashable releases and exit (no GUI)")
    args = ap.parse_args()

    if args.list:
        for r in fetch_releases(args.repo):
            flag = " [pre-release]" if r["prerelease"] else ""
            print(f"{r['tag']:16s} {r['published']}  {r['asset_name']}  "
                  f"{r['asset_size']:,} bytes{flag}")
        return

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    UpdaterGUI(root, args.repo, args.port)
    root.mainloop()


if __name__ == "__main__":
    main()
