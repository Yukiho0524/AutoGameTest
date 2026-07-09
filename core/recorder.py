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
import os
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
            self._cleanup_tmp()
            return {"ok": True, "video": out, "n_parts": 1, "elapsed": elapsed}

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
        self._cleanup_tmp()
        return {"ok": True, "dir": sess, "n_parts": n, "elapsed": elapsed}

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
