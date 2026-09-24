"""
audio_tab.py – waveform viewer + simplified media-player controls.

Claims .mp3 / .mp4 / .wav (and a few others when the backend supports them).
Displays metadata in the Text pane and a full waveform player in the Canvas.

Dependencies (all permissive licenses – MIT/BSD/ISC):
    pip install tinytag numpy soundfile sounddevice
    # optional broader format support:
    pip install miniaudio

setup:
pip install tinytag numpy soundfile sounddevice
# optional for broader format support (especially MP3):
pip install miniaudio

TODO:
tool tips
tab.paned.sashpos(0, 220) !worky
double free or corruption (out), Aborted (core dumped)  in skip_back / skip_fwd
Loop region (start/end markers)
Snap-to-zero-crossing seek (optional)
Simple peak / RMS meter
Export visible selection as WAV
Keyboard shortcuts (Space = play/pause, arrows = seek, +/- = zoom)
Persist zoom / volume / speed per file in the session JSON (via utils)

True streaming playback with a sounddevice OutputStream callback for sample-accurate position and low latency.
Loop region with draggable markers.
Zero-crossing snap.
Selection → “Export selection as WAV”.
Persist per-file zoom/volume in the session JSON.
Keyboard bindings (Space, arrows, +/-).
"""

from __future__ import annotations

import math
import subprocess
import sys
import threading
import time
import queue
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import tkinter as tk
from tkinter import ttk, messagebox

from utils import debug, insert_styled_text

VERSION = 1.0.5
AUDIO_EXTS = {".mp3", ".mp4", ".wav"} #, ".m4a", ".flac", ".ogg", ".aiff", ".aif"} #don't need these

# ---------------------------------------------------------------------------
# Optional backends (graceful degradation)
# Dependency detection (evaluated once at import)
# ---------------------------------------------------------------------------
_MISSING: Dict[str, str] = {}          # package_name -> pip install name
_OPTIONAL_NOTES: Dict[str, str] = {}

def _probe():
    global _MISSING, _OPTIONAL_NOTES
    _MISSING = {}
    _OPTIONAL_NOTES = {}

    try:
        import numpy  # noqa: F401
    except ImportError:
        _MISSING["numpy"] = "numpy"

    try:
        import soundfile  # noqa: F401
    except ImportError:
        _MISSING["soundfile"] = "soundfile"

    try:
        import sounddevice  # noqa: F401
    except ImportError:
        _MISSING["sounddevice"] = "sounddevice"

    try:
        from tinytag import TinyTag  # noqa: F401
    except ImportError:
        _MISSING["tinytag"] = "tinytag"

    # miniaudio is optional – only needed for MP3/MP4 when soundfile cannot handle them
    try:
        import miniaudio  # noqa: F401
    except ImportError:
#        _MISSING["miniaudio"] = "miniaudio" #audio back end seems to want this
        _OPTIONAL_NOTES["miniaudio"] = (
            "miniaudio (optional – improves MP3/MP4 support)"
        )
    debug(1, f"{{blue}}probe:", _MISSING, _OPTIONAL_NOTES)

_probe()

# Re-export the flags for the rest of the module
HAS_NUMPY       = "numpy" not in _MISSING
HAS_SOUNDFILE   = "soundfile" not in _MISSING
HAS_SOUNDDEVICE = "sounddevice" not in _MISSING
HAS_TINYTAG     = "tinytag" not in _MISSING
HAS_MINIAUDIO   = "miniaudio" not in _OPTIONAL_NOTES

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore

try:
    import miniaudio
except ImportError:
    miniaudio = None  # type: ignore

try:
    from tinytag import TinyTag
except ImportError:
    TinyTag = None  # type: ignore

#debug(1,
#    f"{{pink}}deps:", 
#    HAS_NUMPY, np is not None,
#    HAS_SOUNDFILE, sf is not None,
#    HAS_SOUNDDEVICE, sd is not None,
#    HAS_TINYTAG, TinyTag is not None,
#    HAS_MINIAUDIO,
#)


# ---------------------------------------------------------------------------
# Peak extraction (runs in background thread)
# ---------------------------------------------------------------------------
def _extract_peaks(filepath: str, max_peaks: int = 4000) -> Tuple[Optional[object], int, float, str]:
    """
    Return (peaks_array, sample_rate, duration_sec, error_msg).
    peaks_array shape = (n_peaks, 2)  →  [min, max] per visual bin (float32, -1..1).
    """
    if not HAS_NUMPY:
        return None, 0, 0.0, "numpy not installed"

    path = Path(filepath)
    suffix = path.suffix.lower()

    # --- preferred: soundfile (WAV/FLAC/OGG/AIFF) ---
    if HAS_SOUNDFILE and suffix in {".wav", ".flac", ".ogg", ".aiff", ".aif"}:
        try:
            info = sf.info(filepath)
            sr = info.samplerate
            n_frames = info.frames
            duration = n_frames / sr if sr else 0.0
            channels = info.channels

            # Read in blocks to keep memory modest
            block = 65536
            chunks = []
            with sf.SoundFile(filepath) as f:
                while True:
                    data = f.read(block, dtype="float32", always_2d=True)
                    if len(data) == 0:
                        break
                    # mix to mono for display
                    mono = data.mean(axis=1)
                    chunks.append(mono)

            if not chunks:
                return None, sr, duration, "empty file"
            audio = np.concatenate(chunks)
            return _downsample_peaks(audio, max_peaks), sr, duration, ""
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return None, 0, 0.0, f"soundfile: {exc}"

    # --- miniaudio fallback (good for MP3/MP4) ---
    if HAS_MINIAUDIO:
        try:
            info = miniaudio.get_file_info(filepath) #need to get sample rate
            decoded = miniaudio.decode_file(filepath, nchannels=1, sample_rate=info.sample_rate) #sr must be int; 0 !worky; None)
            audio = np.frombuffer(decoded.samples, dtype=np.int16).astype(np.float32) / 32768.0
            sr = decoded.sample_rate
            duration = len(audio) / sr if sr else 0.0
            return _downsample_peaks(audio, max_peaks), sr, duration, ""
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return None, 0, 0.0, f"miniaudio: {exc}"

    return None, 0, 0.0, "no suitable audio backend (install soundfile and/or miniaudio)"


def _downsample_peaks(audio: "np.ndarray", max_peaks: int) -> "np.ndarray":
    n = len(audio)
    if n == 0:
        return np.zeros((0, 2), dtype=np.float32)
    if n <= max_peaks:
        # expand to min/max pairs
        return np.column_stack((audio, audio)).astype(np.float32)

    samples_per_bin = n / max_peaks
    peaks = np.empty((max_peaks, 2), dtype=np.float32)
    for i in range(max_peaks):
        start = int(i * samples_per_bin)
        end = int((i + 1) * samples_per_bin)
        chunk = audio[start:end]
        if len(chunk) == 0:
            peaks[i] = 0.0, 0.0
        else:
            peaks[i, 0] = chunk.min()
            peaks[i, 1] = chunk.max()
    return peaks


# ---------------------------------------------------------------------------
# Install helper
# ---------------------------------------------------------------------------
def _install_packages(packages: List[str], status_callback) -> Tuple[bool, str]:
    """
    Run `python -m pip install …` and return (success, message).
    status_callback(msg) is called from the worker thread (must be thread-safe
    or schedule UI updates via after()).
    """
    if not packages:
        return True, "Nothing to install"

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
    status_callback(f"Running: {' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,          # 5 min safety
        )
        if proc.returncode == 0:
            return True, "Install finished successfully.\nPlease restart the application."
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"pip failed (code {proc.returncode}):\n{err[:800]}"
    except subprocess.TimeoutExpired:
        return False, "pip timed out"
    except Exception as exc:
        return False, f"Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# Main UI class that lives inside the supplied Canvas
# ---------------------------------------------------------------------------
class WaveformPlayer:
    """
    Self-contained waveform display + transport controls drawn into the
    EditorTab's Canvas (and a few controls packed above/below it).
    """

    def __init__(self, canvas: tk.Canvas, text: Optional[tk.Text], filepath: str):
        self.canvas = canvas
        self.text = text
        self.filepath = filepath
        self.parent = canvas.master          # top_frame

        # audio state
        self.peaks: Optional[object] = None
        self.sample_rate = 0
        self.duration = 0.0
        self.position = 0.0                 # seconds
        self.playing = False
        self.play_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.volume = 1.0
        self.speed = 1.0
        self.h_zoom = 1.0                   # 1.0 = fit whole file
        self.v_zoom = 1.0                   # amplitude scale
        self.view_start = 0.0               # seconds of left edge
        self._drag_start_x = None

        # colours
        self.bg = "#1e1e1e"
        self.wave_color = "#3daee9"
        self.center_color = "#555555"
        self.guide_color = "#333333"
        self.playhead_color = "#e74c3c"
        self.ruler_color = "#aaaaaa"
        self.text_color = "#cccccc"

        self._build_controls()
        self._configure_canvas()

        if _MISSING:
            self._show_missing_ui()
        else:
            self._start_load()

    # ------------------------------------------------------------------ UI construction
    def _build_controls(self):
        # Control bar sits above the canvas inside the same top_frame
        self.ctrl = ttk.Frame(self.parent)
        self.ctrl.pack(side="top", fill="x", before=self.canvas)

        # Transport
        self.btn_back = ttk.Button(self.ctrl, text="⏪ 5s", width=6, command=self._skip_back)
        self.btn_play = ttk.Button(self.ctrl, text="▶", width=4, command=self._toggle_play)
        self.btn_fwd  = ttk.Button(self.ctrl, text="5s ⏩", width=6, command=self._skip_fwd)
        self.btn_back.pack(side="left", padx=2)
        self.btn_play.pack(side="left", padx=2)
        self.btn_fwd.pack(side="left", padx=2)

        ttk.Separator(self.ctrl, orient="vertical").pack(side="left", fill="y", padx=6)

        # Volume
        ttk.Label(self.ctrl, text="Vol").pack(side="left")
        self.vol_var = tk.DoubleVar(value=1.0)
        self.vol_scale = ttk.Scale(
            self.ctrl, from_=0.0, to=1.0, variable=self.vol_var,
            orient="horizontal", length=80, command=self._on_vol
        )
        self.vol_scale.pack(side="left", padx=2)

        # Speed
        ttk.Label(self.ctrl, text="Speed").pack(side="left", padx=(8, 0))
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_scale = ttk.Scale(
            self.ctrl, from_=0.25, to=2.0, variable=self.speed_var,
            orient="horizontal", length=80, command=self._on_speed
        )
        self.speed_scale.pack(side="left", padx=2)

        ttk.Separator(self.ctrl, orient="vertical").pack(side="left", fill="y", padx=6)

        # Zoom
        ttk.Button(self.ctrl, text="H−", width=3, command=lambda: self._zoom_h(0.7)).pack(side="left")
        ttk.Button(self.ctrl, text="H+", width=3, command=lambda: self._zoom_h(1.4)).pack(side="left")
        ttk.Button(self.ctrl, text="V−", width=3, command=lambda: self._zoom_v(0.7)).pack(side="left", padx=(6, 0))
        ttk.Button(self.ctrl, text="V+", width=3, command=lambda: self._zoom_v(1.4)).pack(side="left")
        ttk.Button(self.ctrl, text="Fit", width=4, command=self._zoom_fit).pack(side="left", padx=4)

        # Position readout
        self.pos_label = ttk.Label(self.ctrl, text="00:00.000 / 00:00.000", width=22)
        self.pos_label.pack(side="right", padx=6)

        # Status / progress while loading
        self.status_var = tk.StringVar(value="Loading…")
        self.status_lbl = ttk.Label(self.ctrl, textvariable=self.status_var)
        self.status_lbl.pack(side="right", padx=8)

    def _configure_canvas(self):
        self.canvas.configure(bg=self.bg, highlightthickness=0)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)          # Windows / macOS
        self.canvas.bind("<Button-4>", lambda e: self._zoom_h(1.2))  # Linux
        self.canvas.bind("<Button-5>", lambda e: self._zoom_h(0.8))

    # ------------------------------------------------------------------ Missing-dependency UI
    def _show_missing_ui(self):
        """Draw a clear message + Install button on the canvas."""
        debug(1, "here0")
        self.canvas.delete("all")
        w = max(self.canvas.winfo_width(), 200)
        h = max(self.canvas.winfo_height(), 120)

        msg = "Missing required packages for waveform / playback:\n\n"
        for pkg in _MISSING:
            msg += f"  • {pkg}\n"
        if _OPTIONAL_NOTES:
            msg += "\nOptional:\n"
            for note in _OPTIONAL_NOTES.values():
                msg += f"  • {note}\n"
        msg += "\nClick the button below to install them."

        self.canvas.create_text(
            w // 2, h // 2 - 30,
            text=msg,
            fill=self.text_color,
            font=("", 10),
            justify="left",
            tags="missing_msg",
        )

        debug(1, "here1")
        # Place a real ttk.Button on top of the canvas
        self.install_btn = ttk.Button(
            self.canvas,
            text="Install missing packages",
            command=self._on_install_clicked,
        )
        self.install_btn_window = self.canvas.create_window(
            w // 2, h // 2 + 50,
            window=self.install_btn,
            tags="install_btn",
        )
        debug(1, "here2")

        self.status_var.set("Dependencies missing")

        # Keep the button centred on resize
        def _reposition(event=None):
            cw = max(self.canvas.winfo_width(), 200)
            ch = max(self.canvas.winfo_height(), 120)
            self.canvas.coords("missing_msg", cw // 2, ch // 2 - 30)
            self.canvas.coords("install_btn", cw // 2, ch // 2 + 50)
        self.canvas.bind("<Configure>", _reposition, add="+")

    def _on_install_clicked(self):
        packages = list(_MISSING.values())
        # Also offer the optional one if the user is installing anything
        if _OPTIONAL_NOTES:
            packages.append("miniaudio")

        if not packages:
            return

        self.install_btn.configure(state="disabled", text="Installing…")
        self.status_var.set("Installing…")

        def status_cb(msg: str):
            # schedule UI update on main thread
            self.canvas.after(0, lambda: self.status_var.set(msg[:60]))

        def worker():
            ok, message = _install_packages(packages, status_cb)
            self.canvas.after(0, lambda: self._install_finished(ok, message))

        threading.Thread(target=worker, daemon=True).start()

    def _install_finished(self, success: bool, message: str):
        if success:
            self.status_var.set("Installed – please restart the app")
            self.install_btn.configure(text="Installed – restart required", state="disabled")
            # Also append a clear note to the text pane
            if self.text is not None:
                note = (
                    "\n{green}────────────────────────────────────\n"
                    "{green}Packages installed successfully.\n"
                    "{yellow}Please close and restart the application\n"
                    "{yellow}so the new modules can be imported.\n"
                    "{green}────────────────────────────────────\n"
                )
                insert_styled_text(self.text, note)
            messagebox.showinfo(
                "Installation complete",
                "The required packages were installed.\n\n"
                "Please close and restart the application\n"
                "for the changes to take effect."
            )
        else:
            self.status_var.set("Install failed")
            self.install_btn.configure(text="Install failed – retry?", state="normal")
            if self.text is not None:
                insert_styled_text(self.text, f"\n{{red}}Install error:\n{message}\n")
            messagebox.showerror("Installation failed", message)

    # ------------------------------------------------------------------ Loading (only when deps present)
    def _start_load(self):
        self.status_var.set("Decoding…")
        q: queue.Queue = queue.Queue()

        def worker():
            peaks, sr, dur, err = _extract_peaks(self.filepath)
            q.put((peaks, sr, dur, err))

        def poll():
            try:
                peaks, sr, dur, err = q.get_nowait()
            except queue.Empty:
                self.canvas.after(50, poll)
                return

            if err:
                self.status_var.set(f"Error: {err}")
                debug(1, f"{{red}}audio_tab load failed: {err}")
                import traceback
                traceback.print_exc()
                return

            self.peaks = peaks
            self.sample_rate = sr
            self.duration = dur
            self.view_start = 0.0
            self.h_zoom = 1.0
            self.status_var.set("Ready")
            self._update_pos_label()
            self._redraw()
            debug(1, f"{{green}}waveform ready", f"{dur:.2f}s", f"{len(peaks)} peaks")

        threading.Thread(target=worker, daemon=True).start()
        self.canvas.after(50, poll)

    # ------------------------------------------------------------------ Drawing
    def _redraw(self):
        if _MISSING:          # still showing the install UI
            return
        self.canvas.delete("all")
        w = max(self.canvas.winfo_width(), 10)
        h = max(self.canvas.winfo_height(), 10)
        mid = h // 2

        if self.peaks is None or len(self.peaks) == 0:
            self.canvas.create_text(w // 2, h // 2, text="No waveform data", fill=self.text_color)
            return

        # Visible time window
        view_dur = self.duration / self.h_zoom
        view_end = min(self.view_start + view_dur, self.duration)
        view_start = max(0.0, view_end - view_dur)

        # Horizontal centre line
        self.canvas.create_line(0, mid, w, mid, fill=self.center_color, width=1)

        # dB guides (approximate, assuming full-scale = 0 dB)
        for db in (-3, -6, -12, -24):
            amp = 10 ** (db / 20.0) * self.v_zoom
            y1 = mid - amp * (h * 0.45)
            y2 = mid + amp * (h * 0.45)
            self.canvas.create_line(0, y1, w, y1, fill=self.guide_color, dash=(2, 4))
            self.canvas.create_line(0, y2, w, y2, fill=self.guide_color, dash=(2, 4))
            self.canvas.create_text(4, y1, anchor="nw", text=f"{db} dB",
                                   fill=self.guide_color, font=("", 7))

        # Time ruler (bottom)
        self._draw_time_ruler(w, h, view_start, view_end)

        # Waveform
        n_peaks = len(self.peaks)
        # Map visible time → peak indices
        start_idx = int(view_start / self.duration * n_peaks)
        end_idx   = int(view_end   / self.duration * n_peaks)
        start_idx = max(0, min(start_idx, n_peaks - 1))
        end_idx   = max(start_idx + 1, min(end_idx, n_peaks))

        visible = self.peaks[start_idx:end_idx]
        n_vis = len(visible)
        if n_vis < 2:
            return

        xs = np.linspace(0, w, n_vis) if np else [i * w / (n_vis - 1) for i in range(n_vis)]
        scale = (h * 0.45) * self.v_zoom

        # Draw as a filled outline (min/max)
        coords_top = []
        coords_bot = []
        for i, (mn, mx) in enumerate(visible):
            x = xs[i]
            y_top = mid - mx * scale
            y_bot = mid - mn * scale
            coords_top.extend((x, y_top))
            coords_bot.extend((x, y_bot))

        # simple polyline for speed
        if coords_top:
            self.canvas.create_line(*coords_top, fill=self.wave_color, width=1, tags="wave")
            self.canvas.create_line(*coords_bot, fill=self.wave_color, width=1, tags="wave")

        # Playhead
        if view_start <= self.position <= view_end:
            frac = (self.position - view_start) / (view_end - view_start)
            x = frac * w
            self.canvas.create_line(x, 0, x, h, fill=self.playhead_color, width=2, tags="playhead")

    def _draw_time_ruler(self, w: int, h: int, t0: float, t1: float):
        """Simple adaptive time ticks."""
        span = t1 - t0
        if span <= 0:
            return
        # Choose a pleasant tick interval
        candidates = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05,
                      0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60]
        target = span / 8
        interval = min(candidates, key=lambda c: abs(c - target))

        t = math.ceil(t0 / interval) * interval
        while t <= t1:
            frac = (t - t0) / span
            x = frac * w
            self.canvas.create_line(x, h - 12, x, h, fill=self.ruler_color)
            label = self._fmt_time(t, short=True)
            self.canvas.create_text(x + 2, h - 14, anchor="sw",
                                   text=label, fill=self.ruler_color, font=("", 7))
            t += interval

    # ------------------------------------------------------------------ Interaction
    def _time_at_x(self, x: float) -> float:
        w = max(self.canvas.winfo_width(), 1)
        view_dur = self.duration / self.h_zoom
        return self.view_start + (x / w) * view_dur

    def _on_click(self, event):
        if _MISSING:
            return
        self.position = max(0.0, min(self.duration, self._time_at_x(event.x)))
        self._update_pos_label()
        self._redraw()
        if self.playing:
            # restart playback from new position
            self._stop_playback()
            self._start_playback()

    def _on_drag(self, event):
        # simple scrub
        self._on_click(event)

    def _on_release(self, event):
        pass

    def _on_wheel(self, event):
        if _MISSING:
            return
        # Ctrl+wheel = horizontal zoom, plain wheel = vertical
        if event.state & 0x4:          # Control
            factor = 1.2 if event.delta > 0 else 0.8
            self._zoom_h(factor)
        else:
            factor = 1.2 if event.delta > 0 else 0.8
            self._zoom_v(factor)

    def _zoom_h(self, factor: float):
        old_zoom = self.h_zoom
        self.h_zoom = max(1.0, min(50.0, self.h_zoom * factor))
        # keep playhead roughly centred
        mid_time = self.position
        view_dur = self.duration / self.h_zoom
        self.view_start = max(0.0, mid_time - view_dur / 2)
        self._redraw()

    def _zoom_v(self, factor: float):
        self.v_zoom = max(0.2, min(10.0, self.v_zoom * factor))
        self._redraw()

    def _zoom_fit(self):
        self.h_zoom = 1.0
        self.v_zoom = 1.0
        self.view_start = 0.0
        self._redraw()

    def _skip_back(self):
        self.position = max(0.0, self.position - 5.0)
        self._update_pos_label()
        self._redraw()
        if self.playing:
            self._stop_playback()
            self._start_playback()

    def _skip_fwd(self):
        debug(1, "here1")
        self.position = min(self.duration, self.position + 5.0)
        self._update_pos_label()
        self._redraw()
        debug(1, "here2")
        if self.playing:
            self._stop_playback()
            debug(1, "here3")
            self._start_playback()
            debug(1, "here4")

    def _on_vol(self, _=None):
        self.volume = float(self.vol_var.get())

    def _on_speed(self, _=None):
        self.speed = float(self.speed_var.get())

    # ------------------------------------------------------------------ Playback (best-effort)
    def _toggle_play(self):
        if _MISSING:
            return
        if self.playing:
            self._stop_playback()
        else:
            self._start_playback()

    def _start_playback(self):
        if not HAS_SOUNDDEVICE or self.peaks is None or self.duration <= 0:
            self.status_var.set("Playback unavailable (install sounddevice)")
            return
        self.playing = True
        self.btn_play.configure(text="⏸")
        self.stop_event.clear()
        self.play_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self.play_thread.start()
        self._tick()

    def _stop_playback(self):
        self.playing = False
        self.stop_event.set()
        self.btn_play.configure(text="▶")
        try:
            if HAS_SOUNDDEVICE:
                sd.stop()
        except Exception:
            pass

    def _playback_loop(self):
        """
        Very simple non-streaming playback: we re-decode a short chunk
        around the current position on each start.  Good enough for short
        files and keeps the implementation small.
        """
        # For a production player one would stream with a callback;
        # this keeps the dependency surface tiny.
        try:
            if HAS_SOUNDFILE:
                data, sr = sf.read(self.filepath, dtype="float32", always_2d=True)
            elif HAS_MINIAUDIO:
                decoded = miniaudio.decode_file(self.filepath)
                data = np.frombuffer(decoded.samples, dtype=np.int16).astype(np.float32) / 32768.0
                if data.ndim == 1:
                    data = data.reshape(-1, 1)
                sr = decoded.sample_rate
            else:
                return

            start_frame = int(self.position * sr)
            chunk = data[start_frame:]
            if self.volume != 1.0:
                chunk = chunk * self.volume
            # speed change via crude resampling (sounddevice does not do rate conversion)
            if abs(self.speed - 1.0) > 0.01 and HAS_NUMPY:
                new_len = int(len(chunk) / self.speed)
                if new_len > 0:
                    idx = np.linspace(0, len(chunk) - 1, new_len).astype(int)
                    chunk = chunk[idx]

            sd.play(chunk, sr, blocking=True)
        except Exception as exc:
            debug(1, "{red}playback error:", exc)
        finally:
            self.playing = False
            # UI update must be on main thread
            try:
                self.canvas.after(0, lambda: self.btn_play.configure(text="▶"))
            except Exception:
                pass

    def _tick(self):
        """Update playhead while playing (approximate)."""
        if not self.playing:
            return
        # We don't get precise position from sd.play(blocking=True) easily,
        # so we advance by wall-clock * speed.
        # A streaming callback would be more accurate.
        self.position = min(self.duration, self.position + 0.05 * self.speed)
        self._update_pos_label()
        self._redraw()
        if self.position < self.duration and self.playing:
            self.canvas.after(50, self._tick)
        else:
            self._stop_playback()

    # ------------------------------------------------------------------ Helpers
    def _update_pos_label(self):
        self.pos_label.configure(
            text=f"{self._fmt_time(self.position)} / {self._fmt_time(self.duration)}"
        )

    @staticmethod
    def _fmt_time(sec: float, short: bool = False) -> str:
        if sec < 0:
            sec = 0.0
        m, s = divmod(sec, 60)
        if short and m == 0:
            return f"{s:.2f}"
        return f"{int(m):02d}:{s:06.3f}"


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------
def onload(filepath: str, canvas=None, text=None, tab=None):
    p = Path(filepath)
    if p.suffix.lower() not in AUDIO_EXTS:
        debug(1, f"{{red}}{p.resolve()} '{p.suffix.lower()}' not audio")
        return False

    # ----- metadata + missing-dependency report into the text pane -----
    descr = (
        f"{{cyan}}[audio_tab]\n"
        f"{{blue}}Audio: {{cyan}}{p.name}\n"
        f"{{blue}}Path:  {p.resolve()}\n"
    )

    if HAS_TINYTAG:
        try:
            tags = TinyTag.get(str(p.resolve()))
            minutes, seconds = divmod(tags.duration or 0, 60)
            msec = int((seconds - int(seconds)) * 1000)
            descr += (
                f"{{blue}}Title:  {{cyan}}{tags.title or '—'}\n"
                f"{{blue}}Artist: {{cyan}}{tags.artist or '—'}\n"
                f"{{blue}}Album:  {{cyan}}{tags.album or '—'}\n"
                f"{{blue}}Duration: {{cyan}}{int(minutes)}:{int(seconds):02d}.{msec:03d}\n"
                f"{{blue}}Sample rate: {{cyan}}{num_fmt(getattr(tags, 'samplerate', '—'), '2f')}\n"
                f"{{blue}}Channels: {{cyan}}{num_fmt(getattr(tags, 'channels', '—'), '2f')}\n"
                f"{{blue}}Bitrate: {{cyan}}{num_fmt(getattr(tags, 'bitrate', '—'), '2f')}\n"
            )
        except Exception as exc:
            descr += f"{{red}}tinytag error: {exc}\n"
            debug(1, f"{{red}}tinytag error: {exc}")
    else:
        descr += "{yellow}tinytag not installed – limited metadata\n"

    # Explicit missing-dependency section
    if _MISSING or _OPTIONAL_NOTES:
        descr += "\n{yellow}────────────────────────────────────\n"
        descr += "{yellow}Missing dependencies:\n"
        if _MISSING:
            for pkg in _MISSING:
                descr += f"{{red}}  • {pkg}  (required)\n"
        if _OPTIONAL_NOTES:
            for note in _OPTIONAL_NOTES.values():
                descr += f"{{yellow}}  • {note}\n"
        descr += (
            "{yellow}A button to install them is shown in the\n"
            "{yellow}waveform area above. After installation\n"
            "{yellow}you will need to restart the application.\n"
            "{yellow}────────────────────────────────────\n"
        )
    else:
        descr += "\n{green}All recommended packages are present.\n"

    if text is not None:
        text.delete("1.0", "end")
        insert_styled_text(text, descr)

    # ----- waveform / install UI into the canvas -----
    if canvas is not None:
        # Clear any previous content / bindings the editor may have put there
        canvas.delete("all")
        for seq in canvas.bind():
            canvas.unbind(seq)
        # Give the upper pane a bit more room by default
        try:
            if tab is not None and hasattr(tab, "paned"):
                tab.paned.sashpos(0, 220)
        except Exception as exc:
            pass

        # Keep a reference so it is not garbage-collected
        canvas._waveform_player = WaveformPlayer(canvas, text, str(p.resolve()))

    return True

def num_fmt(val, fmt) -> str:
    # Check if the value is either an int or a float
    if isinstance(val, (int, float)):
        return f"{val:.2f}"
    return str(val)

#eof