"""Segmented emulator screen recording (ported from GameTestAi).

`adb shell screenrecord` caps a single clip at 180 seconds. A RecordingSession
hides that: it chains segments seamlessly (start the next one the moment the
previous ends) so the user experiences a single long recording. On stop:

- 1 segment  -> <save_dir>/rec_<timestamp>.mp4
- N segments -> <save_dir>/rec_<timestamp>/part01.mp4... + session.json

No merging is attempted: AutoGameTest is stdlib-only (no ffmpeg/cv2), so
multi-part sessions are kept as ordered parts with a manifest, same format as
GameTestAi so its frame-extraction tooling stays compatible.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime

from . import adb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SAVE_DIR = os.path.join(ROOT, "data", "recordings")

_CREATE_NO_WINDOW = 0x08000000
_DEV_PREFIX = "/sdcard/_agt_rec_seg"

_sessions: dict[str, "RecordingSession"] = {}
_lock = threading.Lock()

_GETEVENT_LINE = re.compile(r"\[\s*([\d.]+)\]\s+(\w+)\s+(\w+)\s+(\S+)")


class _TapCapture:
    """getevent touch capture running alongside the recording (from GameTestAi).

    Reads raw touch events from the emulator's input device so the generated
    taps.json has the *actual* tapped coordinates — far more reliable than
    inferring taps from video frames. Uses `adb shell -tt` to force a PTY so
    getevent is line-buffered (block buffering loses the tail on pkill).
    """

    def __init__(self, adb_path: str, serial: str):
        self.adb_path = adb_path
        self.serial = serial
        self.dev = ""
        self.max_x, self.max_y = 1279, 719
        self.screen_w, self.screen_h = 1280, 720
        self.t0 = 0.0
        self._popen: subprocess.Popen | None = None
        self._lines: list[str] = []
        self._thread: threading.Thread | None = None
        self._seq = 0

    def _shell_out(self, *args: str, timeout: int = 10) -> str:
        try:
            proc = subprocess.run(
                [self.adb_path, "-s", self.serial, "shell", *args],
                capture_output=True, timeout=timeout,
                creationflags=_CREATE_NO_WINDOW)
            return proc.stdout.decode("utf-8", "ignore")
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _detect_touch_device(self) -> str:
        """Scan /dev/input/event* for the one with ABS_MT_POSITION_X.
        The node number varies between boots; hardcoding event2 silently
        captures nothing when it happens to be the keyboard."""
        listing = self._shell_out("ls", "/dev/input")
        devs = sorted(d.strip() for d in listing.split()
                      if d.strip().startswith("event"))
        for d in devs:
            path = f"/dev/input/{d}"
            cap = self._shell_out("getevent", "-lp", path)
            if "ABS_MT_POSITION_X" in cap:
                return path
        return "/dev/input/event2"

    def _touch_range(self) -> tuple[int, int]:
        out = self._shell_out("getevent", "-lp", self.dev)
        mx = my = None
        for line in out.splitlines():
            if "ABS_MT_POSITION_X" in line:
                m = re.search(r"max\s+(\d+)", line)
                if m:
                    mx = int(m.group(1))
            elif "ABS_MT_POSITION_Y" in line:
                m = re.search(r"max\s+(\d+)", line)
                if m:
                    my = int(m.group(1))
        return (mx, my) if mx and my else (1279, 719)

    def _screen_size(self) -> tuple[int, int]:
        out = self._shell_out("wm", "size")
        m = re.search(r"(\d+)x(\d+)", out or "")
        if m:
            return int(m.group(1)), int(m.group(2))
        return self.max_x + 1, self.max_y + 1

    def start(self) -> None:
        self.dev = self._detect_touch_device()
        self.max_x, self.max_y = self._touch_range()
        self.screen_w, self.screen_h = self._screen_size()
        up = self._shell_out("cat", "/proc/uptime").strip()
        try:
            self.t0 = float(up.split()[0])
        except (ValueError, IndexError):
            self.t0 = 0.0
        self._popen = subprocess.Popen(
            [self.adb_path, "-s", self.serial, "shell", "-tt",
             "timeout", "10800", "getevent", "-lt", self.dev],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", creationflags=_CREATE_NO_WINDOW)

        def _reader(p, out):
            try:
                for line in p.stdout:
                    out.append(line)
            except Exception:
                pass

        self._thread = threading.Thread(
            target=_reader, args=(self._popen, self._lines), daemon=True)
        self._thread.start()

    def stop(self) -> list[dict]:
        self._shell_out("pkill", "getevent")
        if self._popen:
            try:
                self._popen.wait(timeout=10)
            except Exception:
                try:
                    self._popen.kill()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=5)
        return self._parse("".join(self._lines))

    def _parse(self, text: str) -> list[dict]:
        """Parse `getevent -lt` output into GameTestAi-compatible tap records.

        evdev only reports axes that CHANGED: when two consecutive touches
        share an X or Y value, the second touch gets no event for that axis.
        cur_x/cur_y persist across touches as the fallback, otherwise the
        missing axis would silently become 0.
        """
        taps: list[dict] = []
        down_t = None
        sx = sy = None           # first reported position of current touch
        cur_x = cur_y = None     # last known absolute position (persistent)
        for line in text.splitlines():
            m = _GETEVENT_LINE.search(line)
            if not m:
                continue
            ts, code, val = float(m.group(1)), m.group(3), m.group(4)
            if code == "BTN_TOUCH":
                if val == "DOWN":
                    down_t = ts
                    sx = sy = None
                elif val == "UP" and down_t is not None:
                    x = sx if sx is not None else cur_x
                    y = sy if sy is not None else cur_y
                    if x is not None and y is not None:
                        taps.append(self._touch_record(
                            down_t, ts, x, y,
                            cur_x if cur_x is not None else x,
                            cur_y if cur_y is not None else y))
                    down_t = None
            elif code == "ABS_MT_POSITION_X":
                cur_x = int(val, 16)
                if down_t is not None and sx is None:
                    sx = cur_x
            elif code == "ABS_MT_POSITION_Y":
                cur_y = int(val, 16)
                if down_t is not None and sy is None:
                    sy = cur_y
        return taps

    def inject_tap(self, x: int, y: int) -> bool:
        """Send a kernel-level tap via sendevent (screen pixel coordinates).

        Unlike `adb input tap`, kernel events flow through evdev, so the tap is
        BOTH delivered to the app and captured by this getevent session --
        letting the control panel's click-through drive a recorded script.
        """
        tx = int(x * self.max_x / max(1, self.screen_w - 1))
        ty = int(y * self.max_y / max(1, self.screen_h - 1))
        tx = max(0, min(self.max_x, tx))
        ty = max(0, min(self.max_y, ty))
        self._seq += 1
        d = self.dev
        down = (f"sendevent {d} 3 57 {1000 + self._seq} ; "
                f"sendevent {d} 1 330 1 ; "
                f"sendevent {d} 3 53 {tx} ; sendevent {d} 3 54 {ty} ; "
                f"sendevent {d} 0 0 0")
        up = (f"sendevent {d} 3 57 4294967295 ; "
              f"sendevent {d} 1 330 0 ; sendevent {d} 0 0 0")
        try:
            subprocess.run([self.adb_path, "-s", self.serial, "shell", down],
                           capture_output=True, timeout=10,
                           creationflags=_CREATE_NO_WINDOW)
            time.sleep(0.08)
            subprocess.run([self.adb_path, "-s", self.serial, "shell", up],
                           capture_output=True, timeout=10,
                           creationflags=_CREATE_NO_WINDOW)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _touch_record(self, t_down, t_up, x, y, ex, ey) -> dict:
        duration_ms = int((t_up - t_down) * 1000)
        displacement = math.hypot(ex - x, ey - y) / self.max_x
        if displacement > 0.04:
            kind = "swipe"
        elif duration_ms >= 400:
            kind = "long_press"
        else:
            kind = "tap"
        return {
            "t": round(t_down - self.t0, 3),
            "duration_ms": duration_ms,
            "x": x, "y": y,
            "nx": round(x / self.max_x, 4), "ny": round(y / self.max_y, 4),
            "end_nx": round(ex / self.max_x, 4),
            "end_ny": round(ey / self.max_y, 4),
            "kind": kind,
        }


def resolve_save_dir(save_dir: str | None) -> str:
    """Normalize the requested folder; fall back to data/recordings."""
    text = str(save_dir or "").strip().strip('"')
    if not text:
        return DEFAULT_SAVE_DIR
    return os.path.normpath(os.path.expandvars(os.path.expanduser(text)))


def _ensure_writable(path: str) -> str:
    """Create the folder and prove we can write into it. Returns '' or error."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".agt_write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return ""
    except OSError as e:
        return f"存檔位置無法寫入：{e}"


class RecordingSession:
    def __init__(self, serial: str, emulator: str | None, save_dir: str,
                 seg_seconds: int = 175, bitrate: int = 8_000_000,
                 show_touches: bool = True):
        self.serial = serial
        self.emulator = adb.normalize_emulator(emulator or adb.emulator_for_serial(serial))
        self.adb_path = adb.adb_path_for(self.emulator)
        self.save_dir = save_dir
        self.seg_seconds = max(10, min(seg_seconds, 179))
        self.bitrate = bitrate
        self.show_touches = show_touches
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.tmp_dir = os.path.join(save_dir, f".rec_tmp_{self.stamp}")
        self.started_at = 0.0
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cur_popen: subprocess.Popen | None = None
        self._parts: list[str] = []
        self._taps: list[dict] = []
        self._tap_capture: _TapCapture | None = None

    # ---- lifecycle ----
    def start(self) -> str:
        """Begin recording. Returns '' on success or an error message."""
        err = _ensure_writable(self.save_dir)
        if err:
            return err
        os.makedirs(self.tmp_dir, exist_ok=True)
        if not adb.adb_ready(self.serial, self.emulator):
            return f"裝置 {self.serial} 尚未就緒（模擬器開機中？）"
        # clear leftovers from a previous crashed session
        self._shell("rm", "-f", f"{_DEV_PREFIX}*.mp4")
        if self.show_touches:
            self._shell("settings", "put", "system", "show_touches", "1")
        # capture real touch coordinates alongside the video (for script generation)
        self._tap_capture = _TapCapture(self.adb_path, self.serial)
        try:
            self._tap_capture.start()
        except Exception:
            self._tap_capture = None   # recording still works without taps
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return ""

    def _loop(self):
        idx = 0
        quick_fails = 0
        while not self._stop.is_set():
            idx += 1
            dev = f"{_DEV_PREFIX}{idx:02d}.mp4"
            seg_started = time.time()
            self._cur_popen = subprocess.Popen(
                [self.adb_path, "-s", self.serial, "shell", "screenrecord",
                 "--time-limit", str(self.seg_seconds),
                 "--bit-rate", str(self.bitrate), dev],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW)
            self._cur_popen.wait()
            local = os.path.join(self.tmp_dir, f"part{idx:02d}.mp4")
            self._pull(dev, local)
            if os.path.isfile(local) and os.path.getsize(local) > 0:
                self._parts.append(local)
                quick_fails = 0
            else:
                # segment produced nothing; if it also died instantly the
                # device likely can't record -- abort instead of spinning
                if time.time() - seg_started < 3:
                    quick_fails += 1
                    if quick_fails >= 2:
                        self.error = "screenrecord 無法在此裝置錄影（連續兩段皆失敗）"
                        self._stop.set()
                        break
            if self._stop.is_set():
                break

    def stop(self) -> dict:
        """Stop recording and assemble output files."""
        self._stop.set()
        self._shell("pkill", "-INT", "screenrecord")   # let current segment finalize
        if self._thread:
            self._thread.join(timeout=90)
        if self._tap_capture:
            try:
                self._taps = self._tap_capture.stop()
            except Exception:
                self._taps = []
        if self.show_touches:
            self._shell("settings", "put", "system", "show_touches", "0")
        return self._assemble()

    def status(self) -> dict:
        return {
            "recording": self._thread is not None and self._thread.is_alive(),
            "serial": self.serial,
            "emulator": self.emulator,
            "elapsed": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "parts": len(self._parts) + 1,   # +1 = the segment currently rolling
            "save_dir": self.save_dir,
            "error": self.error,
        }

    # ---- assembly ----
    def _assemble(self) -> dict:
        elapsed = round(time.time() - self.started_at, 1) if self.started_at else 0
        n = len(self._parts)
        if n == 0:
            self._cleanup_tmp()
            return {"ok": False, "error": self.error or "沒有錄到任何片段",
                    "n_parts": 0, "elapsed": elapsed}

        if n == 1:
            out = os.path.join(self.save_dir, f"rec_{self.stamp}.mp4")
            os.replace(self._parts[0], out)
            taps_path = self._save_taps(out + ".taps.json")
            self._cleanup_tmp()
            return {"ok": True, "video": out, "n_parts": 1, "elapsed": elapsed,
                    "taps_json": taps_path, "n_taps": len(self._taps)}

        sess = os.path.join(self.save_dir, f"rec_{self.stamp}")
        os.makedirs(sess, exist_ok=True)
        names = []
        for i, p in enumerate(self._parts, 1):
            dst = os.path.join(sess, f"part{i:02d}.mp4")
            os.replace(p, dst)
            names.append(os.path.basename(dst))
        manifest = os.path.join(sess, "session.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"stamp": self.stamp, "parts": names},
                      f, ensure_ascii=False, indent=2)
        taps_path = self._save_taps(os.path.join(sess, "taps.json"))
        self._cleanup_tmp()
        return {"ok": True, "dir": sess, "n_parts": n, "elapsed": elapsed,
                "taps_json": taps_path, "n_taps": len(self._taps)}

    def _save_taps(self, path: str) -> str:
        """Write captured taps (GameTestAi-compatible format). '' if none."""
        if not self._taps:
            return ""
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._taps, f, ensure_ascii=False, indent=2)
            return path
        except OSError:
            return ""

    def _cleanup_tmp(self):
        try:
            for fn in os.listdir(self.tmp_dir):
                try:
                    os.remove(os.path.join(self.tmp_dir, fn))
                except OSError:
                    pass
            os.rmdir(self.tmp_dir)
        except OSError:
            pass

    # ---- adb helpers ----
    def _shell(self, *args: str) -> None:
        try:
            subprocess.run([self.adb_path, "-s", self.serial, "shell", *args],
                           capture_output=True, timeout=15,
                           creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _pull(self, dev: str, local: str) -> None:
        try:
            subprocess.run([self.adb_path, "-s", self.serial, "pull", dev, local],
                           capture_output=True, timeout=180,
                           creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._shell("rm", "-f", dev)


# ---------------- module-level manager ----------------

def start_recording(serial: str, emulator: str | None = None,
                    save_dir: str | None = None,
                    show_touches: bool = True) -> dict:
    resolved = resolve_save_dir(save_dir)
    with _lock:
        existing = _sessions.get(serial)
        if existing and existing.status()["recording"]:
            return {**existing.status(), "ok": False,
                    "error": f"{serial} 已在錄影中"}
        session = RecordingSession(serial, emulator, resolved,
                                   show_touches=show_touches)
        err = session.start()
        if err:
            return {"ok": False, "error": err, "save_dir": resolved}
        _sessions[serial] = session
    return {"ok": True, "save_dir": resolved, **session.status()}


def stop_recording(serial: str) -> dict:
    with _lock:
        session = _sessions.pop(serial, None)
    if not session:
        return {"ok": False, "error": f"{serial} 目前沒有錄影"}
    return session.stop()


def recording_tap(serial: str, x: int, y: int) -> bool:
    """If a recording is active on this serial, tap through the kernel input
    layer so the tap lands in taps.json. Returns False when not recording
    (caller should fall back to the normal `adb input tap`)."""
    with _lock:
        session = _sessions.get(serial)
    if not session:
        return False
    st = session.status()
    if not st["recording"] or not session._tap_capture:
        return False
    return session._tap_capture.inject_tap(x, y)


def status(serial: str | None = None) -> dict:
    """Status for one serial (or the first active session if omitted)."""
    with _lock:
        session = _sessions.get(serial) if serial else None
        if session is None and not serial:
            for s in _sessions.values():
                if s.status()["recording"]:
                    session = s
                    break
    if session:
        st = session.status()
        # a finished/crashed session that was never stopped: surface the error
        return {"active": st["recording"], **st}
    return {"active": False, "recording": False,
            "save_dir": "", "elapsed": 0, "parts": 0, "error": ""}
