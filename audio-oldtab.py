"""
audio_tab.py – waveform viewer + simplified media-player controls.

Claims .mp3 / .mp4 / .wav (and a few others when the backend supports them).
Displays metadata in the Text pane and a full waveform player in the Canvas.

Dependencies (all permissive licenses – MIT/BSD/ISC):
    pip install tinytag numpy soundfile sounddevice
    # optional broader format support:
    pip install miniaudio
    # optional stem separation (MIT):
    pip install demucs torch

setup:
pip install tinytag numpy soundfile sounddevice
# optional for broader format support (especially MP3):
pip install miniaudio
# optional vocal / non-vocal stem partitioning:
pip install demucs torch

NOTE:
This plug-in tab conflicts with waveform_tab (both claim audio files).
This one is renamed (and discontinued) to let waveform_tab handle audio files.

TODO:
tab.paned.sashpos(0, 220) !worky
double free or corruption (out), Aborted (core dumped)  in skip_back / skip_fwd
Snap-to-zero-crossing seek (optional)
Simple peak / RMS meter
Export visible selection as WAV
True streaming playback with a sounddevice OutputStream callback for sample-accurate position and low latency.
Zero-crossing snap.
Selection → “Export selection as WAV”.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
import queue
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

import tkinter as tk
from tkinter import ttk, messagebox

from utils import debug, insert_styled_text

VERSION = "1.0.8"
AUDIO_EXTS = {".mp3", ".mp4", ".wav"}  #, ".m4a", ".flac", ".ogg", ".aiff", ".aif"}  #don't need these

# Waveform region colors (after demucs)
COLOR_GRAY   = "#888888"   # initial / unknown
COLOR_VOCAL  = "#2ecc71"   # green  – vocal-only
COLOR_NOVOC  = "#3498db"   # blue   – non-vocal-only
COLOR_MIXED  = "#1abc9c"   # teal   – mixed
COLOR_SEL    = "#f39c12"
COLOR_SEL_EDGE = "#e67e22"

# ---------------------------------------------------------------------------
# Optional backends (graceful degradation)
# Dependency detection (evaluated once at import) – demucs/torch are LAZY
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

#    try:
#        import demucs  # noqa: F401
#        import torch   # noqa: F401
#    except ImportError:
    # demucs/torch are heavy – do NOT import here (blocks UI on startup).
    # Presence is checked lazily when separation is requested.
    _OPTIONAL_NOTES["demucs"] = (
        "demucs + torch (optional – vocal / non-vocal stem partitioning)"
    )

    debug(1, f"{{blue}}probe:", _MISSING, _OPTIONAL_NOTES)

_probe()

# Re-export flags for remainder of module
HAS_NUMPY       = "numpy" not in _MISSING
HAS_SOUNDFILE   = "soundfile" not in _MISSING
HAS_SOUNDDEVICE = "sounddevice" not in _MISSING
HAS_TINYTAG     = "tinytag" not in _MISSING
HAS_MINIAUDIO   = "miniaudio" not in _OPTIONAL_NOTES
#HAS_DEMUCS      = "demucs" not in _OPTIONAL_NOTES
# tri-state: None = not yet checked, True/False after first lazy probe
_HAS_DEMUCS: Optional[bool] = None

def _demucs_available() -> bool:
    """Lazy check for demucs + torch (avoid blocking startup)."""
    global _HAS_DEMUCS
    if _HAS_DEMUCS is not None:
        return _HAS_DEMUCS
    try:
        import demucs  # noqa: F401
        import torch   # noqa: F401
        _HAS_DEMUCS = True
    except ImportError:
        _HAS_DEMUCS = False
    return _HAS_DEMUCS

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
# Tooltip (explicit colors so text is visible on Linux)
# ---------------------------------------------------------------------------
class ToolTip:
    def __init__(self, widget, text: str, delay_ms: int = 500):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._tip: Optional[tk.Toplevel] = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._hide()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        if self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        tw.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#ffffe0",
            foreground="#000000",
            relief="solid",
            borderwidth=1,
            font=("TkDefaultFont", 9),
            padx=6,
            pady=3,
        )
        lbl.pack()
        tw.update_idletasks()

    def _hide(self, _event=None):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

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
#            channels = info.channels

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
# Cache + stem paths
# ---------------------------------------------------------------------------
def _app_version() -> str:
    """Best-effort main application version (from utils / tracked)."""
    try:
        import utils
        return str(getattr(utils, "VERSION", "?"))
    except Exception:
        return "?"

def _cache_path(filepath: str) -> Path:
    p = Path(filepath)
    return p.with_name(p.stem + "-tracked.json")

def _stem_paths(filepath: str) -> Tuple[Path, Path]:
    p = Path(filepath)
    return (
        p.with_name(p.stem + "-vocals.wav"),
        p.with_name(p.stem + "-non_vocals.wav"),
    )

def _stems_are_fresh(filepath: str) -> bool:
    """True if both stem WAVs exist and are newer than the audio file."""
    audio = Path(filepath)
    if not audio.is_file():
        return False
    try:
        audio_mtime = audio.stat().st_mtime
    except OSError:
        return False
    voc, nov = _stem_paths(filepath)
    for sp in (voc, nov):
        if not sp.is_file():
            return False
        try:
            if sp.stat().st_mtime < audio_mtime:
                return False
        except OSError:
            return False
    return True

def _load_cache(filepath: str) -> Optional[Dict[str, Any]]:
    """Return cached dict if it exists and is newer than the audio file."""
    cp = _cache_path(filepath)
    if not cp.is_file():
        return None
    try:
        audio_mtime = Path(filepath).stat().st_mtime
        cache_mtime = cp.stat().st_mtime
        if cache_mtime < audio_mtime:
            debug(1, f"{{yellow}}cache older than audio, ignoring {cp}")
            return None
        data = json.loads(cp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # TODO: reject cache written by a different audio_tab major version + force recompute after format changes.
        return data
    except Exception as exc:
        debug(1, f"{{red}}cache load error: {exc}")
        return None

def _save_cache(filepath: str, data: Dict[str, Any]) -> None:
    cp = _cache_path(filepath)
    try:
        data.setdefault("audio_tab_version", VERSION)
        data.setdefault("app_version", _app_version())
        cp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        debug(1, f"{{green}}wrote cache {cp}")
    except Exception as exc:
        debug(1, f"{{red}}cache save error: {exc}")

def _append_text(text_widget: Optional[tk.Text], msg: str) -> None:
    """Insert styled text and scroll so the new lines are visible."""
    if text_widget is None:
        return
    try:
        insert_styled_text(text_widget, msg)
        text_widget.see("end")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Demucs 2-stem separation + region partitioning (background)
# ---------------------------------------------------------------------------
def _run_demucs_and_partition(
    filepath: str,
    n_peaks: int,
    duration: float,
    progress_cb=None,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Returns (regions, error_msg).
    Saves stem WAVs when separation runs (or reuses fresh ones).
    progress_cb(msg: str) may be called from the worker thread.

    Run demucs 2-stem (vocals / no_vocals) and return a list of regions:
        [{"start": sec, "end": sec, "kind": "vocal"|"novocal"|"mixed"}, ...]
    Also returns an error string (empty on success).
    Imports demucs/torch only here so the main UI stays responsive.
    """
    if not HAS_NUMPY:
        return [], "numpy not available"
    if not _demucs_available():
        return [], "demucs/torch not available"

    t0 = time.time()
    voc_path, nov_path = _stem_paths(filepath)

    try:
        import torch
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
        from demucs.audio import AudioFile

        reuse = _stems_are_fresh(filepath)
        voc_mono = None
        nov_mono = None

        if reuse and HAS_SOUNDFILE:
            if progress_cb:
                progress_cb("Loading cached stem files… (0%)")
            try:
                voc_audio, _sr_v = sf.read(str(voc_path), dtype="float32", always_2d=True)
                nov_audio, _sr_n = sf.read(str(nov_path), dtype="float32", always_2d=True)
                voc_mono = voc_audio.mean(axis=1)
                nov_mono = nov_audio.mean(axis=1)
                if progress_cb:
                    progress_cb("Stems loaded from cache (40%)")
            except Exception as exc:
                debug(1, f"{{yellow}}stem reload failed, re-separating: {exc}")
                reuse = False

        if not reuse:
            if progress_cb:
                progress_cb("Loading demucs model… (5%)")

        # 2-stem model (vocals + accompaniment)
            model = get_model("htdemucs")
            model.eval()
            device = "cuda" if torch.cuda.is_available() else "cpu"
            if device == "cuda":
                model.cuda()

            if progress_cb:
                progress_cb("Decoding audio for demucs… (15%)")

            # Load full mix (demucs expects (channels, samples))
            wav = AudioFile(filepath).read(
                streams=0, samplerate=model.samplerate, channels=model.audio_channels
            )
            # wav shape: (channels, samples)
            ref = wav.mean(0)
            wav = (wav - ref.mean()) / (ref.std() + 1e-8)

            audio_len = int(wav.shape[-1])

            def demucs_callback(info: dict):
                if progress_cb is None:
                    return
                try:
                    offset = float(info.get("segment_offset", 0) or 0)
                    total = float(info.get("audio_length", audio_len) or audio_len)
                    state = info.get("state", "")
                    if total > 0 and state == "end":
                        frac = min(1.0, offset / total)
                        pct = 20 + int(frac * 65)
                        progress_cb(f"Separating stems… ({pct}%)")
                except Exception:
                    pass

            if progress_cb:
                progress_cb("Running demucs separation… (20%)")

            with torch.no_grad():
                sources = apply_model(
                    model,
                    wav[None],
                    device=device,
                    shifts=1,
                    split=True,
                    overlap=0.25,
                    progress=False,
                    callback=demucs_callback,
                )[0]  # (sources, channels, samples)

            sources = sources * (ref.std() + 1e-8) + ref.mean()

            # sources order for htdemucs: drums, bass, other, vocals
            # Build vocals vs no_vocals
            src_names = list(model.sources)
            voc_idx = src_names.index("vocals")
            vocals = sources[voc_idx]
            novoc = sources.sum(0) - vocals

            if progress_cb:
                progress_cb("Saving stem files… (90%)")
            if HAS_SOUNDFILE:
                try:
                    sf.write(str(voc_path), vocals.cpu().numpy().T, model.samplerate)
                    sf.write(str(nov_path), novoc.cpu().numpy().T, model.samplerate)
                    debug(1, f"{{green}}saved stems {voc_path.name}, {nov_path.name}")
                except Exception as exc:
                    debug(1, f"{{red}}stem save error: {exc}")

            # Mix to mono energy envelopes
            voc_mono = vocals.mean(0).cpu().numpy()
            nov_mono = novoc.mean(0).cpu().numpy()

        if progress_cb:
            progress_cb("Partitioning regions… (95%)")

        # Align to the same number of visual bins as the waveform peaks
        n_src = len(voc_mono)
        if n_src == 0 or n_peaks < 2:
            return [], "empty demucs output"

        # RMS energy per peak-bin
        samples_per_bin = n_src / n_peaks
        voc_e = np.empty(n_peaks, dtype=np.float32)
        nov_e = np.empty(n_peaks, dtype=np.float32)
        for i in range(n_peaks):
            a = int(i * samples_per_bin)
            b = int((i + 1) * samples_per_bin)
            if b <= a:
                voc_e[i] = nov_e[i] = 0.0
            else:
                voc_e[i] = np.sqrt(np.mean(voc_mono[a:b] ** 2))
                nov_e[i] = np.sqrt(np.mean(nov_mono[a:b] ** 2))

        # Adaptive thresholds
        v_th = max(float(np.percentile(voc_e, 60)) * 0.4, 1e-5)
        n_th = max(float(np.percentile(nov_e, 60)) * 0.4, 1e-5)

        kinds = []
        for i in range(n_peaks):
            v = voc_e[i] > v_th
            n = nov_e[i] > n_th
            if v and n:
                kinds.append("mixed")
            elif v:
                kinds.append("vocal")
            elif n:
                kinds.append("novocal")
            else:
                kinds.append("mixed")  # silence → treat as mixed/gray later if wanted

        # Collapse contiguous identical kinds into regions (seconds)
        regions: List[Dict[str, Any]] = []
#        if not kinds:
#            return [], ""
        cur = kinds[0]
        start_i = 0
        for i in range(1, n_peaks):
            if kinds[i] != cur:
                regions.append({
                    "start": start_i / n_peaks * duration,
                    "end": i / n_peaks * duration,
                    "kind": cur,
                })
                cur = kinds[i]
                start_i = i
        regions.append({
            "start": start_i / n_peaks * duration,
            "end": duration,
            "kind": cur,
        })

        if progress_cb:
            progress_cb("Done (100%)")

        elapsed = time.time() - t0
        debug(1, f"{{green}}demucs finished in {elapsed:.1f}s, {len(regions)} regions")
        return regions, ""
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return [], f"demucs: {exc}"

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
            timeout=600,          # 10 min safety
        )
        if proc.returncode == 0:
            return True, "Install finished successfully.\nPlease restart the application."
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
    EDGE_GRAB_PX = 8
    SEEK_STEP = 1.0
    BOUNDARY_STEP = 0.05

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
        self._pcm_cache: Optional[Tuple[Any, int]] = None  # (data, sr) for faster seeks
        self._play_lock = threading.Lock()
        self._play_gen = 0          # bumped on every stop/seek to invalidate old ticks
        self.volume = 1.0
        self.speed = 1.0
        self.h_zoom = 1.0                   # 1.0 = fit whole file
        self.v_zoom = 1.0                   # amplitude scale
        self.view_start = 0.0               # seconds of left edge
#        self._drag_start_x = None
        self._active_play_start: Optional[float] = None
        self._active_play_end: Optional[float] = None

        self.sel_start: Optional[float] = None
        self.sel_end: Optional[float] = None
        self._drag_edge: Optional[str] = None
        self._shift_down = False
        self._loop_mode = False
        self._sel_anchor: Optional[float] = None  # for shift-drag
        self._shift_click_guard = 0.0   # time.time() of last Shift-click

        # stem regions: list of {"start", "end", "kind"}
        self.regions: List[Dict[str, Any]] = []
        self._using_cache = False
        self._last_progress_msg = ""

        # colors
        self.bg = "#1e1e1e"
        self.wave_color = COLOR_GRAY          # initial gray
        self.center_color = "#555555"
        self.guide_color = "#333333"
        self.playhead_color = "#e74c3c"
        self.ruler_color = "#aaaaaa"
        self.text_color = "#cccccc"

        self._build_controls()
        self._configure_canvas()
        self._bind_keys()
        self.canvas.bind("<Destroy>", self._on_destroy, add="+")

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
        self.btn_start = ttk.Button(self.ctrl, text="|◀", width=4, command=self._seek_start)
        self.btn_end   = ttk.Button(self.ctrl, text="▶|", width=4, command=self._seek_end)
        self.btn_start.pack(side="left", padx=2)
        self.btn_end.pack(side="left", padx=2)

        ToolTip(self.btn_start, "Seek to start and clear selection  (Home)")
        ToolTip(self.btn_end, "Seek to end and clear selection  (End)")
        ToolTip(self.btn_back, "Skip back 5 seconds  (←)")  #(Left)")
        ToolTip(self.btn_play, "Play / Pause  (Space)\nShift+Play = loop selection")
        ToolTip(self.btn_fwd,  "Skip forward 5 seconds  (→)")  #(Right)")

        ttk.Separator(self.ctrl, orient="vertical").pack(side="left", fill="y", padx=6)

        # Volume
        ttk.Label(self.ctrl, text="Vol").pack(side="left")
        self.vol_var = tk.DoubleVar(value=1.0)
        self.vol_scale = ttk.Scale(
            self.ctrl, from_=0.0, to=1.0, variable=self.vol_var,
            orient="horizontal", length=80, command=self._on_vol,
        )
        self.vol_scale.pack(side="left", padx=2)
        self._vol_tip = ToolTip(self.vol_scale, "Volume (disabled during playback)")

        # Speed
        ttk.Label(self.ctrl, text="Speed").pack(side="left", padx=(8, 0))
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_scale = ttk.Scale(
            self.ctrl, from_=0.25, to=2.0, variable=self.speed_var,
            orient="horizontal", length=80, command=self._on_speed,
        )
        self.speed_scale.pack(side="left", padx=2)
        ToolTip(self.speed_scale, "Playback speed")

        ttk.Separator(self.ctrl, orient="vertical").pack(side="left", fill="y", padx=6)

        # Zoom
        btn_hm = ttk.Button(self.ctrl, text="H−", width=3, command=lambda: self._zoom_h(0.7))  #.pack(side="left")
        btn_hp = ttk.Button(self.ctrl, text="H+", width=3, command=lambda: self._zoom_h(1.4))  #.pack(side="left")
        btn_vm = ttk.Button(self.ctrl, text="V−", width=3, command=lambda: self._zoom_v(0.7))  #.pack(side="left", padx=(6, 0))
        btn_vp = ttk.Button(self.ctrl, text="V+", width=3, command=lambda: self._zoom_v(1.4))  #.pack(side="left")
        btn_fit = ttk.Button(self.ctrl, text="Fit", width=4, command=self._zoom_fit)  #.pack(side="left", padx=4)
        btn_hm.pack(side="left")
        btn_hp.pack(side="left")
        btn_vm.pack(side="left", padx=(6, 0))
        btn_vp.pack(side="left")
        btn_fit.pack(side="left", padx=4)

        ToolTip(btn_hm, "Zoom out horizontally  (−)")
        ToolTip(btn_hp, "Zoom in horizontally  (+)")
        ToolTip(btn_vm, "Zoom out vertically  (Ctrl+−)")
        ToolTip(btn_vp, "Zoom in vertically  (Ctrl++)")
        ToolTip(btn_fit, "Fit whole file  (0)")

        # Position readout
        self.pos_label = ttk.Label(self.ctrl, text="00:00.000 / 00:00.000", width=36)
        self.pos_label.pack(side="right", padx=6)

        # Status / progress while loading
        self.status_var = tk.StringVar(value="Loading…")
        self.status_lbl = ttk.Label(self.ctrl, textvariable=self.status_var)
        self.status_lbl.pack(side="right", padx=8)

    def _set_vol_enabled(self, enabled: bool):
        try:
            if enabled:
                self.vol_scale.state(["!disabled"])
                self.vol_scale.configure(cursor="")
            else:
                self.vol_scale.state(["disabled"])
                self.vol_scale.configure(cursor="X_cursor")
        except tk.TclError:
            pass

    def _configure_canvas(self):
        self.canvas.configure(bg=self.bg, highlightthickness=1, takefocus=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        # Explicit Shift-Button-1 for reliable Linux selection
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Shift-Button-1>", self._on_shift_click)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Shift-B1-Motion>", self._on_shift_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        # Shift held while pointer is over canvas (works even without prior key focus)
        self.canvas.bind("<Shift-Motion>", self._on_shift_motion)
        # Windows / macOS
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        # Linux: Button-4 = up, Button-5 = down – honour Control for vertical zoom
        self.canvas.bind("<Button-4>", self._on_linux_wheel_up)
        self.canvas.bind("<Button-5>", self._on_linux_wheel_down)
        # Also bind Control-Button so the modifier is reliably seen
        self.canvas.bind("<Control-Button-4>", lambda e: self._zoom_v(1.2))
        self.canvas.bind("<Control-Button-5>", lambda e: self._zoom_v(0.8))
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set(), add="+")
#        self.canvas.configure(takefocus=True)

    def _bind_keys(self):
        c = self.canvas
        c.bind("<space>", lambda e: (self._toggle_play(), "break")[1])
        c.bind("<Left>", lambda e: (self._on_left_key(e), "break")[1])
        c.bind("<Right>", lambda e: (self._on_right_key(e), "break")[1])
        c.bind("<Shift-Left>", lambda e: (self._nudge_boundary(-self.BOUNDARY_STEP), "break")[1])
        c.bind("<Shift-Right>", lambda e: (self._nudge_boundary(+self.BOUNDARY_STEP), "break")[1])
        c.bind("<plus>", lambda e: self._zoom_h(1.4))
        c.bind("<equal>", lambda e: self._zoom_h(1.4))
        c.bind("<minus>", lambda e: self._zoom_h(0.7))
        c.bind("<KP_Add>", lambda e: self._zoom_h(1.4))
        c.bind("<KP_Subtract>", lambda e: self._zoom_h(0.7))
        c.bind("<Control-plus>", lambda e: self._zoom_v(1.4))
        c.bind("<Control-equal>", lambda e: self._zoom_v(1.4))
        c.bind("<Control-minus>", lambda e: self._zoom_v(0.7))
        c.bind("<Key-0>", lambda e: self._zoom_fit())
#        c.bind("<Home>", lambda e: self._seek_to(0.0))
#        c.bind("<End>", lambda e: self._seek_to(self.duration))
        c.bind("<Home>", lambda e: (self._seek_start(), "break")[1])
        c.bind("<End>",  lambda e: (self._seek_end(), "break")[1])
        c.bind("<Escape>", lambda e: (self._clear_selection(), "break")[1])
        c.bind("<Escape>", lambda e: self._clear_selection())
        c.bind("<KeyPress-Shift_L>", lambda e: self._set_shift(True))
        c.bind("<KeyPress-Shift_R>", lambda e: self._set_shift(True))
        c.bind("<KeyRelease-Shift_L>", lambda e: self._set_shift(False))
        c.bind("<KeyRelease-Shift_R>", lambda e: self._set_shift(False))

    def _set_shift(self, down: bool):
        self._shift_down = down
        # Re-evaluate cursor using last known edge (no event); motion will refine
        self._update_cursor(shift=down)

    def _update_cursor(self, edge: Optional[str] = None, shift: bool = False):
        try:
            if shift and not self.playing:
                # Loop / selection affordance whenever Shift is held over the wave
                self.canvas.configure(cursor="exchange")
            elif edge in ("start", "end") and not self.playing:
                self.canvas.configure(cursor="sb_h_double_arrow")
            else:
                self.canvas.configure(cursor="")
        except tk.TclError:
            pass

    def _on_destroy(self, _event=None):
        self.playing = False
        self.stop_event.set()
        self._play_gen += 1
        try:
            if HAS_SOUNDDEVICE:
                sd.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------ Missing deps
    def _show_missing_ui(self):
        """Draw a clear message + Install button on the canvas."""
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

        # Place a real ttk.Button on top of the canvas
        self.install_btn = ttk.Button(
            self.canvas,
            text="Install missing packages",
            command=self._on_install_clicked,
        )
#        self.install_btn_window = 
        self.canvas.create_window(
            w // 2, h // 2 + 50,
            window=self.install_btn,
            tags="install_btn",
        )

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
            # demucs is heavy; only install if user is already installing something
            # and explicitly listed; keep it optional
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
            _append_text(self.text, (
                "\n{green}────────────────────────────────────\n"
                "{green}Packages installed successfully.\n"
                "{yellow}Please close and restart the application\n"
                "{yellow}so the new modules can be imported.\n"
                "{green}────────────────────────────────────\n"
            ))
            messagebox.showinfo(
                "Installation complete",
                "Packages installed.\nPlease restart the application.",
            )
        else:
            self.status_var.set("Install failed")
            self.install_btn.configure(text="Install failed – retry?", state="normal")
            _append_text(self.text, f"\n{{red}}Install error:\n{message}\n")
            messagebox.showerror("Installation failed", message)

    # ------------------------------------------------------------------ Loading (only when deps present)
    def _start_load(self):
        self.status_var.set("Decoding…")
        q: queue.Queue = queue.Queue()

        def worker():
            # Try cache first
            cache = _load_cache(self.filepath)
            if cache is not None:
                try:
                    peaks = np.array(cache["peaks"], dtype=np.float32)
                    sr = int(cache["sample_rate"])
                    dur = float(cache["duration"])
                    regions = cache.get("regions", [])
                    prefs = {
                        "h_zoom": float(cache.get("h_zoom", 1.0)),
                        "v_zoom": float(cache.get("v_zoom", 1.0)),
                        "volume": float(cache.get("volume", 1.0)),
                        "speed": float(cache.get("speed", 1.0)),
                        "view_start": float(cache.get("view_start", 0.0)),
                        "sel_start": cache.get("sel_start"),
                        "sel_end": cache.get("sel_end"),
                    }
                    q.put(("cache", peaks, sr, dur, regions, prefs, ""))
                    return
                except Exception as exc:
                    debug(1, f"{{red}}cache parse error: {exc}")

            peaks, sr, dur, err = _extract_peaks(self.filepath)
            q.put(("fresh", peaks, sr, dur, [], {}, err))

        def poll():
            try:
                kind, peaks, sr, dur, regions, prefs, err = q.get_nowait()
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
            self.regions = regions or []
            self._using_cache = (kind == "cache")

            self.h_zoom = max(1.0, min(50.0, prefs.get("h_zoom", 1.0)))
            self.v_zoom = max(0.2, min(10.0, prefs.get("v_zoom", 1.0)))
            self.volume = max(0.0, min(1.0, prefs.get("volume", 1.0)))
            self.speed = max(0.25, min(2.0, prefs.get("speed", 1.0)))
            self.view_start = max(0.0, prefs.get("view_start", 0.0))
            self.vol_var.set(self.volume)
            self.speed_var.set(self.speed)
            ss, se = prefs.get("sel_start"), prefs.get("sel_end")
            if ss is not None and se is not None:
                self.sel_start, self.sel_end = float(ss), float(se)
            if self._using_cache:
                self.status_var.set("Ready (cached)")
                _append_text(self.text, "\n{green}using cached data\n")
            else:
                self.status_var.set("Ready")

            self.wave_color = COLOR_GRAY
            self._update_pos_label()
            self._redraw()
            # Let the event loop paint the gray waveform before starting demucs
            self.canvas.update_idletasks()

            debug(
                1, f"{{green}}waveform ready", f"{dur:.2f}s",
                f"{len(peaks)} peaks", "cache" if self._using_cache else "fresh",
            )

            if not self.regions and _demucs_available():
                self.canvas.after(50, self._start_demucs)
            elif self.regions:
                # already from cache – just count & report
                self._report_partition(0.0)

        threading.Thread(target=worker, daemon=True).start()
        self.canvas.after(50, poll)

    def _start_demucs(self):
        self.status_var.set("Separating stems…")
        _append_text(self.text, "\n{yellow}starting background demucs stem separation…\n")
        # force a paint before the worker thread does real work
        self.canvas.update_idletasks()

        q: queue.Queue = queue.Queue()
        t0 = time.time()

        def progress(msg: str):
            if msg == self._last_progress_msg:
                return
            self._last_progress_msg = msg

            def ui():
                self.status_var.set(msg[:60])
                if "%" in msg:
                    _append_text(self.text, f"{{cyan}}  {msg}\n")
            # Always hop to the main thread for UI
            try:
                self.canvas.after(0, ui)
            except Exception:
                pass

        def worker():
            regions, err = _run_demucs_and_partition(
                self.filepath,
                len(self.peaks) if self.peaks is not None else 0,
                self.duration,
                progress_cb=progress,
            )
            q.put((regions, err, time.time() - t0))

        def poll():
            try:
                regions, err, elapsed = q.get_nowait()
            except queue.Empty:
                self.canvas.after(100, poll)
                return

            if err:
                self.status_var.set("Stems failed")
                debug(1, f"{{red}}demucs failed: {err}")
                _append_text(self.text, f"\n{{red}}demucs error: {err}\n")
                return

            self.regions = regions
            self.status_var.set("Ready (stems)")
            self._redraw()
            self._report_partition(elapsed)
            self._write_cache()

        threading.Thread(target=worker, daemon=True).start()
        self.canvas.after(100, poll)

    def _report_partition(self, elapsed: float):
        if not self.regions:
            return
        counts = {"vocal": 0, "novocal": 0, "mixed": 0}
        for r in self.regions:
            k = r.get("kind", "mixed")
            counts[k] = counts.get(k, 0) + 1
        _append_text(self.text, (
            f"\n{{green}}stems partitioned\n"
            f"{{blue}}  vocal-only regions: {{cyan}}{counts.get('vocal', 0)}\n"
            f"{{blue}}  non-vocal-only regions: {{cyan}}{counts.get('novocal', 0)}\n"
            f"{{blue}}  mixed regions: {{cyan}}{counts.get('mixed', 0)}\n"
            f"{{blue}}  elapsed: {{cyan}}{elapsed:.1f}s\n"
        ))

    def _write_cache(self):
        try:
            _save_cache(self.filepath, {
                "peaks": self.peaks.tolist() if self.peaks is not None else [],
                "sample_rate": self.sample_rate,
                "duration": self.duration,
                "regions": self.regions,
                "h_zoom": self.h_zoom,
                "v_zoom": self.v_zoom,
                "volume": self.volume,
                "speed": self.speed,
                "view_start": self.view_start,
                "sel_start": self.sel_start,
                "sel_end": self.sel_end,
                "audio_tab_version": VERSION,
                "app_version": _app_version(),
            })
        except Exception as exc:
            debug(1, f"{{red}}cache write: {exc}")

    # ------------------------------------------------------------------ Drawing
    def _safe_line(self, coords, fill, width=1, tags="wave"):
        """create_line needs ≥ 2 points (4 numbers); skip otherwise."""
        if not coords or len(coords) < 4:
            return
        try:
            self.canvas.create_line(*coords, fill=fill, width=width, tags=tags)
        except tk.TclError as exc:
            debug(2, f"{{yellow}}create_line skipped: {exc}")

    def _redraw(self):
        if _MISSING:          # still showing the install UI
            return
        try:
            self.canvas.delete("all")
        except tk.TclError:
            return
        w = max(self.canvas.winfo_width(), 10)
        h = max(self.canvas.winfo_height(), 10)
        mid = h // 2

        if self.peaks is None or len(self.peaks) == 0:
            self.canvas.create_text(w // 2, h // 2, text="No waveform data", fill=self.text_color)
            return

        # Visible time window
        view_dur = self.duration / self.h_zoom if self.h_zoom else self.duration
        view_end = min(self.view_start + view_dur, self.duration)
        view_start = max(0.0, view_end - view_dur)
        self.view_start = view_start  # keep consistent

        if self.sel_start is not None and self.sel_end is not None:
            a = min(self.sel_start, self.sel_end)
            b = max(self.sel_start, self.sel_end)
            if b > view_start and a < view_end:
                span = max(view_end - view_start, 1e-9)
                x0 = (max(a, view_start) - view_start) / span * w
                x1 = (min(b, view_end) - view_start) / span * w
                self.canvas.create_rectangle(x0, 0, x1, h, fill="#3d2e0a", outline="", tags="sel_bg")
                if view_start <= a <= view_end:
                    xa = (a - view_start) / span * w
                    self.canvas.create_line(xa, 0, xa, h, fill=COLOR_SEL_EDGE, width=2, tags="sel_edge")
                if view_start <= b <= view_end:
                    xb = (b - view_start) / span * w
                    self.canvas.create_line(xb, 0, xb, h, fill=COLOR_SEL_EDGE, width=2, tags="sel_edge")

        # Horizontal center line
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
        if self.duration <= 0 or n_peaks < 2:
            return
        start_idx = max(0, min(int(view_start / self.duration * n_peaks), n_peaks - 1))
        end_idx = max(start_idx + 1, min(int(view_end / self.duration * n_peaks), n_peaks))
        visible = self.peaks[start_idx:end_idx]
        n_vis = len(visible)
        if n_vis < 2:
            return
        xs = np.linspace(0, w, n_vis) if np is not None else [i * w / (n_vis - 1) for i in range(n_vis)]
        scale = (h * 0.45) * self.v_zoom

        # Draw as a filled outline (min/max)
        # If we have regions, draw each visible segment in its color;
        # otherwise draw the whole visible range in the current wave_color (gray).
        if self.regions:
            self._draw_colored_waveform(xs, visible, start_idx, n_peaks, mid, scale)
        else:
            coords_top, coords_bot = [], []
            for i, (mn, mx) in enumerate(visible):
                x = xs[i]
                coords_top.extend((x, mid - mx * scale))
                coords_bot.extend((x, mid - mn * scale))
            # simple polyline for speed
            self._safe_line(coords_top, self.wave_color)
            self._safe_line(coords_bot, self.wave_color)

        # Playhead
        if view_start <= self.position <= view_end:
            frac = (self.position - view_start) / max(view_end - view_start, 1e-9)
            x = frac * w
            self.canvas.create_line(x, 0, x, h, fill=self.playhead_color, width=2, tags="playhead")

    def _draw_colored_waveform(self, xs, visible, start_idx, n_peaks, mid, scale):
        """Draw waveform segments colored by region kind."""
        # Build a per-peak kind array for the visible range
        kind_of = ["mixed"] * n_peaks
        for r in self.regions:
            a = max(0, min(int(r["start"] / self.duration * n_peaks), n_peaks))
            b = max(a, min(int(r["end"] / self.duration * n_peaks), n_peaks))
            for i in range(a, b):
                kind_of[i] = r["kind"]

        color_map = {
            "vocal":   COLOR_VOCAL,
            "novocal": COLOR_NOVOC,
            "mixed":   COLOR_MIXED,
        }

        # Draw contiguous same-color runs inside the visible window
        i = 0
        while i < len(visible):
            global_i = start_idx + i
            cur_kind = kind_of[global_i] if global_i < n_peaks else "mixed"
            j = i + 1
            while j < len(visible):
                gi = start_idx + j
                if gi >= n_peaks or kind_of[gi] != cur_kind:
                    break
                j += 1
            if j - i < 2: #need >= 2 samples < draw
                i = j
                continue
            # draw [i, j)
            coords_top, coords_bot = [], []
            for k in range(i, j):
                mn, mx = visible[k]
                x = xs[k]
                coords_top.extend((x, mid - mx * scale))
                coords_bot.extend((x, mid - mn * scale))
            col = color_map.get(cur_kind, COLOR_GRAY)
            self._safe_line(coords_top, col)
            self._safe_line(coords_bot, col)
            i = j

    def _draw_time_ruler(self, w: int, h: int, t0: float, t1: float):
        """Simple adaptive time ticks."""
        span = t1 - t0
        if span <= 0:
            return
        # Choose a pleasant tick interval
        candidates = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60]
        interval = min(candidates, key=lambda c: abs(c - span / 8))
        t = math.ceil(t0 / interval) * interval
        while t <= t1:
            x = (t - t0) / span * w
            self.canvas.create_line(x, h - 12, x, h, fill=self.ruler_color)
            label = self._fmt_time(t, short=True)
            self.canvas.create_text(x + 2, h - 14, anchor="sw",
                                   text=label, fill=self.ruler_color, font=("", 7))
            t += interval

    # ------------------------------------------------------------------ Interaction
    def _time_at_x(self, x: float) -> float:
        w = max(self.canvas.winfo_width(), 1)
        view_dur = self.duration / self.h_zoom if self.h_zoom else self.duration
        return self.view_start + (x / w) * view_dur

    def _x_at_time(self, t: float) -> float:
        w = max(self.canvas.winfo_width(), 1)
        view_dur = self.duration / self.h_zoom if self.h_zoom else self.duration
        if view_dur <= 0:
            return 0.0
        return (t - self.view_start) / view_dur * w

    def _edge_near(self, x: float) -> Optional[str]:
        if self.sel_start is None or self.sel_end is None or self.playing:
            return None
        a, b = min(self.sel_start, self.sel_end), max(self.sel_start, self.sel_end)
        if abs(x - self._x_at_time(a)) <= self.EDGE_GRAB_PX:
            return "start"
        if abs(x - self._x_at_time(b)) <= self.EDGE_GRAB_PX:
            return "end"
        return None

    def _is_shift(self, event) -> bool:
        # Bit 0x0001 = Shift on X11/Windows/macOS Tk
        # Prefer live modifier mask; fall back to key-tracking flag
#        return bool(event.state & 0x0001) or self._shift_down
        return bool(getattr(event, "state", 0) & 0x0001) or self._shift_down

    # ------------------------------------------------------------------ Mouse
    def _on_click(self, event):
        """Unshifted click: set selection *start* (and playhead). Shift → end only."""
        if _MISSING:
            return
        self.canvas.focus_set()

        if (time.time() - getattr(self, "_shift_click_guard", 0.0)) < 0.15:
            return "break"

        if self._is_shift(event):
            return self._on_shift_click(event)

        t = max(0.0, min(self.duration, self._time_at_x(event.x)))

        # Dragging an existing boundary still allowed when not playing
        if not self.playing:
            edge = self._edge_near(event.x)
            if edge:
                self._drag_edge = edge
                return "break"

        self.position = t
        # Unshifted click defines the selection start (end waits for Shift+click)
        self.sel_start = t
        self.sel_end = t
        self._sel_anchor = t

        self._update_pos_label()
        self._redraw()
        self._write_cache()

        if self.playing:
            self._restart_playback_at(t)
        return "break"

    def _on_shift_click(self, event):
        """Shift+click: change only the selection *end* (start stays fixed)."""
        if _MISSING:
            return "break"
        self.canvas.focus_set()
        self._shift_down = True
        self._shift_click_guard = time.time()

        t = max(0.0, min(self.duration, self._time_at_x(event.x)))

        if self.sel_start is None:
            # No start yet — use current playhead as start
            self.sel_start = self.position
            self._sel_anchor = self.position

        self.sel_end = t
        # Do NOT swap start/end: start is always the unshifted anchor
        self.position = t

        self._update_pos_label()
        self._redraw()
        self._write_cache()

        if self.playing:
            self._on_selection_changed_during_play()
        return "break"

    def _on_shift_drag(self, event):
        """Shift+drag: move end only; start stays at anchor."""
        if _MISSING:
            return "break"
        self._shift_down = True
        self._shift_click_guard = time.time()
        t = max(0.0, min(self.duration, self._time_at_x(event.x)))

        if self.sel_start is None:
            self.sel_start = self.position if self._sel_anchor is None else self._sel_anchor
            self._sel_anchor = self.sel_start
        if self._sel_anchor is None:
            self._sel_anchor = self.sel_start

        self.sel_start = self._sel_anchor
        self.sel_end = t
        self.position = t
        self._update_pos_label()
        self._redraw()
        return "break"

    def _on_release(self, event):
        if self._drag_edge:
            # Keep semantic start = original unshifted anchor when possible
            self._drag_edge = None
            self._write_cache()
            self._update_pos_label()
            self._redraw()
            if self.playing:
                self._on_selection_changed_during_play()
        self._sel_anchor = self.sel_start
        self._sync_shift_from_event(event) if hasattr(self, "_sync_shift_from_event") else None

    def OLD_on_shift_click(self, event):
        """Shift+click: create or extend selection (never a bare playhead-only marker)."""
        if _MISSING:
            return "break"
        self.canvas.focus_set()
        self._shift_down = True
        self._shift_click_guard = time.time()

        t = max(0.0, min(self.duration, self._time_at_x(event.x)))

        if self.sel_start is None or self.sel_end is None:
            # No selection yet: range from current playhead → click
            # (if playhead ≈ click, start a point anchor; next Shift+click extends it)
            if abs(self.position - t) > 0.02:
                self.sel_start = min(self.position, t)
                self.sel_end = max(self.position, t)
                self._sel_anchor = self.position
            else:
                self.sel_start = self.sel_end = t
                self._sel_anchor = t
        else:
            a = min(self.sel_start, self.sel_end)
            b = max(self.sel_start, self.sel_end)
            if abs(b - a) < 0.02:
                # Existing point marker → expand to [marker, click]
                self.sel_start = min(a, t)
                self.sel_end = max(a, t)
                self._sel_anchor = a
            else:
                # Existing range → move the nearer edge to the click (extend/shrink)
                if abs(t - a) <= abs(t - b):
                    self.sel_start, self.sel_end = t, b
                else:
                    self.sel_start, self.sel_end = a, t

        if self.sel_start is not None and self.sel_end is not None:
            if self.sel_start > self.sel_end:
                self.sel_start, self.sel_end = self.sel_end, self.sel_start

        self.position = t
        self._update_pos_label()
        self._redraw()
        self._write_cache()
        return "break"

    def OLD_on_click(self, event):
        if _MISSING:
            return
        self.canvas.focus_set()

        # Linux often delivers Button-1 after Shift-Button-1 for the same press.
        # Do not let the plain handler undo the selection we just set.
        if (time.time() - getattr(self, "_shift_click_guard", 0.0)) < 0.15:
            return "break"

        if self._is_shift(event):
            return self._on_shift_click(event)

        t = max(0.0, min(self.duration, self._time_at_x(event.x)))

        if not self.playing:
            edge = self._edge_near(event.x)
            if edge:
                self._drag_edge = edge
                return "break"

        self.position = t
        # Plain click outside an existing selection clears it
        if self.sel_start is not None and self.sel_end is not None:
            a, b = min(self.sel_start, self.sel_end), max(self.sel_start, self.sel_end)
            if not (a - 0.01 <= t <= b + 0.01):
                self.sel_start = self.sel_end = None

        self._update_pos_label()
        self._redraw()
        if self.playing:
            self._restart_playback_at(t)
        return "break"

    def OLD_on_shift_click(self, event):
        """Dedicated Linux-safe Shift+click → set/extend selection."""
        if _MISSING:
            return
        self.canvas.focus_set()
        t = max(0.0, min(self.duration, self._time_at_x(event.x)))
        if self.sel_start is None or self.sel_end is None:
            self.sel_start = t
            self.sel_end = t
            self._sel_anchor = t
        else:
            # extend: move nearer endpoint
            if abs(t - self.sel_start) <= abs(t - self.sel_end):
                self.sel_start = t
            else:
                self.sel_end = t
            self._sel_anchor = None
        self.position = t
        self._update_pos_label()
        self._redraw()
        return "break"

    def OLD_on_shift_drag(self, event):
        if _MISSING:
            return "break"
        self._shift_down = True
        self._shift_click_guard = time.time()
        t = max(0.0, min(self.duration, self._time_at_x(event.x)))
        if self._sel_anchor is None:
            self._sel_anchor = self.sel_start if self.sel_start is not None else self.position
        self.sel_start = self._sel_anchor
        self.sel_end = t
        self.position = t
        self._update_pos_label()
        self._redraw()
        return "break"

    def OLD_on_shift_drag(self, event):
        if _MISSING:
            return
        t = max(0.0, min(self.duration, self._time_at_x(event.x)))
        if self._sel_anchor is None:
            self._sel_anchor = self.sel_start if self.sel_start is not None else t
        self.sel_start = self._sel_anchor
        self.sel_end = t
        self.position = t
        self._update_pos_label()
        self._redraw()
        return "break"

    def OLD_on_click(self, event):
        if _MISSING:
            return
        self.canvas.focus_set()
        # If shift somehow arrives here without Shift-Button-1, still handle it
        if self._is_shift(event):
            return self._on_shift_click(event)

        t = max(0.0, min(self.duration, self._time_at_x(event.x)))
#        shift = bool(event.state & 0x1) or self._shift_down

        if not self.playing:
            edge = self._edge_near(event.x)
            if edge:  # and not shift:
                self._drag_edge = edge
                return

        self.position = t
        if self.sel_start is not None and self.sel_end is not None:
            a, b = min(self.sel_start, self.sel_end), max(self.sel_start, self.sel_end)
            if not (a <= t <= b):
                self.sel_start = self.sel_end = None

        self._update_pos_label()
        self._redraw()

        if self.playing:
            # Seek during play: stop cleanly, then restart from new position
            self._restart_playback_at(t)
        return "break"

    def _on_drag(self, event):
        if _MISSING:
            return
        if self._is_shift(event):
            return self._on_shift_drag(event)
        t = max(0.0, min(self.duration, self._time_at_x(event.x)))
        if self._drag_edge and not self.playing:
            if self._drag_edge == "start":
                self.sel_start = t
                self._sel_anchor = t
            else:
                self.sel_end = t
            self._update_pos_label()
            self._redraw()
            return
        if not self.playing:
            # Scrub playhead only; selection start is set on click, not on drag
            self.position = t
            self._update_pos_label()
            self._redraw()
#        to allow edge drag during play:
#        self._on_selection_changed_during_play()

    def OLD_on_release(self, event):
        if self._drag_edge:
            if self.sel_start is not None and self.sel_end is not None:
                if self.sel_start > self.sel_end:
                    self.sel_start, self.sel_end = self.sel_end, self.sel_start
            self._drag_edge = None
            self._write_cache()
            self._update_pos_label()
            self._redraw()
        self._sel_anchor = None

    def _on_motion(self, event):
        if _MISSING:
            return
        shift = self._is_shift(event)
        self._shift_down = shift  # keep flag in sync even if KeyPress was missed
        if self.playing:
            self._update_cursor(shift=False)
            return
        edge = self._edge_near(event.x)
        self._update_cursor(edge=edge, shift=shift)

    def _on_shift_motion(self, event):
        """Linux: Motion with Shift held always has Shift in the binding."""
        if _MISSING or self.playing:
            return
        self._shift_down = True
        self._update_cursor(edge=self._edge_near(event.x), shift=True)

    def _on_wheel(self, event):
        """Windows / macOS: Ctrl = vertical zoom, plain = horizontal zoom."""
        if _MISSING:
            return
        factor = 1.2 if event.delta > 0 else 0.8
        if event.state & 0x4:  # Control → vertical
            self._zoom_v(factor)
        else:                          # plain wheel = horizontal zoom
            self._zoom_h(factor)

    def _on_linux_wheel_up(self, event):
        """Linux Button-4 (scroll up)."""
        if _MISSING:
            return
        if event.state & 0x4:          # Control → vertical
            self._zoom_v(1.2)
        else:
            self._zoom_h(1.2)

    def _on_linux_wheel_down(self, event):
        """Linux Button-5 (scroll down)."""
        if _MISSING:
            return
        (self._zoom_v if event.state & 0x4 else self._zoom_h)(0.8)

    # ------------------------------------------------------------------ Keys
    def _on_left_key(self, event):
        if self.sel_start is not None and self.sel_end is not None and not self.playing:
            self._nudge_boundary(-self.BOUNDARY_STEP)
        else:
            self._seek_to(max(0.0, self.position - self.SEEK_STEP))

    def _on_right_key(self, event):
        if self.sel_start is not None and self.sel_end is not None and not self.playing:
            self._nudge_boundary(+self.BOUNDARY_STEP)
        else:
            self._seek_to(min(self.duration, self.position + self.SEEK_STEP))

    def _nudge_boundary(self, delta: float):
        if self.sel_start is None or self.sel_end is None or self.playing:
            return
        a, b = min(self.sel_start, self.sel_end), max(self.sel_start, self.sel_end)
        if abs(self.position - a) <= abs(self.position - b):
            a = max(0.0, min(b - 0.01, a + delta))
            self.sel_start, self.sel_end = a, b
            self.position = a
        else:
            b = min(self.duration, max(a + 0.01, b + delta))
            self.sel_start, self.sel_end = a, b
            self.position = b
        self._update_pos_label()
        self._ensure_playhead_visible()
        self._redraw()
        self._write_cache()

    def _clear_selection(self):
        self.sel_start = self.sel_end = None
        self._loop_mode = False
        self._update_pos_label()
        self._redraw()
        self._write_cache()

    def _seek_to(self, t: float):
        t = max(0.0, min(self.duration, t))
        self.position = t
        self._update_pos_label()
        self._ensure_playhead_visible()
        self._redraw()
        if self.playing:
            self._restart_playback_at(t)

    def _zoom_h(self, factor: float):
#        old_zoom = self.h_zoom
        self.h_zoom = max(1.0, min(50.0, self.h_zoom * factor))
        # keep playhead roughly centred
        mid_time = self.position
        view_dur = self.duration / self.h_zoom
        self.view_start = max(0.0, mid_time - view_dur / 2)
        self._redraw()
        self._write_cache()

    def _zoom_v(self, factor: float):
        self.v_zoom = max(0.2, min(10.0, self.v_zoom * factor))
        self._redraw()
        self._write_cache()

    def _zoom_fit(self):
        self.h_zoom = 1.0
        self.v_zoom = 1.0
        self.view_start = 0.0
        self._redraw()
        self._write_cache()

    def _skip_back(self):
        self._seek_to(max(0.0, self.position - 5.0))

    def _skip_fwd(self):
        self._seek_to(min(self.duration, self.position + 5.0))

    def _on_vol(self, _=None):
        if self.playing:
            return
        self.volume = float(self.vol_var.get())
        self._write_cache()

    def _on_speed(self, _=None):
        self.speed = float(self.speed_var.get())
        self._write_cache()

    def _seek_start(self):
        """Jump to 0:00 and clear any selection point/range."""
        self.sel_start = self.sel_end = None
        self._loop_mode = False
        self._seek_to(0.0)
        self._write_cache()

    def _seek_end(self):
        """Jump to end of file and clear any selection point/range."""
        self.sel_start = self.sel_end = None
        self._loop_mode = False
        self._seek_to(self.duration)
        self._write_cache()

    # ------------------------------------------------------------------ Playback (non-blocking, seek-safe)
    def _load_pcm(self) -> Optional[Tuple[Any, int]]:
        if not hasattr(self, "_pcm_cache"):
            self._pcm_cache = None
        if self._pcm_cache is not None:
            return self._pcm_cache
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
                return None
            self._pcm_cache = (data, sr)
            return self._pcm_cache
        except Exception as exc:
            debug(1, "{red}pcm load error:", exc)
            return None

    def _toggle_play(self):
        if _MISSING:
            return
        if self.playing:
            self._stop_playback()
        else:
            self._loop_mode = bool(self._shift_down) and self._has_selection()
            self._start_playback()

    def _has_selection(self) -> bool:
        return (
            self.sel_start is not None
            and self.sel_end is not None
            and abs(self.sel_end - self.sel_start) > 0.01
        )

    def _on_selection_changed_during_play(self):
        """Re-slice audio when the selection changes while playing."""
        if not self.playing:
            return
        start, end = self._play_range()
        # Keep playhead inside the new window
        if self.position < start or self.position > end:
            self.position = start
        self._restart_playback_at(self.position)

    def _play_range(self) -> Tuple[float, float]:
        if self._has_selection():
            a = float(self.sel_start)
            b = float(self.sel_end)
            return (a, b) if a <= b else (b, a)
        return self.position, self.duration

    def OLD_play_range(self) -> Tuple[float, float]:
        if self._has_selection():
            a = min(self.sel_start, self.sel_end)  # type: ignore
            b = max(self.sel_start, self.sel_end)  # type: ignore
            return a, b
        return self.position, self.duration

    def DROP_start_playback(self):
        if not HAS_SOUNDDEVICE or self.peaks is None or self.duration <= 0:
            self.status_var.set("Playback unavailable (install sounddevice)")
            return

        start, end = self._play_range()
        if self._has_selection() and self.position >= end - 0.01:
            self.position = start
        elif self._has_selection() and self.position < start:
            self.position = start

        self.playing = True
        self.btn_play.configure(text="⏸")
        self.stop_event.clear()
        self.play_thread = threading.Thread(
            target=self._playback_loop, args=(start, end), daemon=True,
        )
        self.play_thread.start()
        self._tick()

    def _stop_playback(self):
        self.playing = False
        self.stop_event.set()
        self._play_gen += 1
        self._loop_mode = False
        self._active_play_start = None
        self._active_play_end = None
        try:
            if HAS_SOUNDDEVICE:
                sd.stop()
        except Exception:
            pass
        try:
            self.btn_play.configure(text="▶")
        except tk.TclError:
            pass
        self._set_vol_enabled(True)

    def _restart_playback_at(self, t: float):
        """Stop current audio and resume from t without losing 'playing' intent."""
        self.position = t
        was_loop = self._loop_mode
        self._stop_playback()
        self._loop_mode = was_loop
        # brief delay so PortAudio fully releases the device on Linux
        self.canvas.after(30, self._start_playback)

    def _start_playback(self):
        if not HAS_SOUNDDEVICE or self.peaks is None or self.duration <= 0:
            self.status_var.set("Playback unavailable (install sounddevice)")
            return

        if not hasattr(self, "_pcm_cache"):
            self._pcm_cache = None

        pcm = self._load_pcm()
        if pcm is None:
            self.status_var.set("Could not load audio for playback")
            return
        data, sr = pcm

        start, end = self._play_range()
        self._active_play_start = start
        self._active_play_end = end

        if self._has_selection():
            if self.position >= end - 0.01 or self.position < start:
                self.position = start

        start_frame = int(self.position * sr)
        end_frame = min(int(end * sr), len(data))
        if start_frame >= end_frame:
            self._stop_playback()
            return

        chunk = data[start_frame:end_frame]
        if self.volume != 1.0:
            chunk = chunk * self.volume
        if abs(self.speed - 1.0) > 0.01 and HAS_NUMPY:
            new_len = int(len(chunk) / self.speed)
            if new_len > 0:
                idx = np.linspace(0, len(chunk) - 1, new_len).astype(int)
                chunk = chunk[idx]

        with self._play_lock:
            try:
                sd.stop()
            except Exception:
                pass
            try:
                sd.play(chunk, sr, blocking=False)
            except Exception as exc:
                debug(1, "{red}play error:", exc)
                self._stop_playback()
                return

        self.playing = True
        self.stop_event.clear()
        self._play_gen += 1
        gen = self._play_gen
        self.btn_play.configure(text="🔁" if self._loop_mode else "⏸")
        self._set_vol_enabled(False)
        self._tick(gen)

    def _stream_active(self) -> bool:
        try:
            stream = sd.get_stream()
            return stream is not None and getattr(stream, "active", False)
        except Exception:
            return False

    def _tick(self, gen: int):
        if gen != self._play_gen or not self.playing:
            return

        # If selection was edited mid-play, restart with the new window
        if self._has_selection() and self._active_play_start is not None:
            cur_s, cur_e = self._play_range()
            if (
                abs(cur_s - (self._active_play_start or 0)) > 0.01
                or abs(cur_e - (self._active_play_end or 0)) > 0.01
            ):
                self._on_selection_changed_during_play()
                return

        start, end = self._play_range()
        self.position = min(end, self.position + 0.05 * self.speed)
        finished = self.position >= end - 0.001 or not self._stream_active()
        if finished:
            if self._loop_mode and self.playing:
                self.position = start
                self._update_pos_label()
                self._redraw()
                self.canvas.after(
                    10,
                    lambda: self._restart_playback_at(start) if self.playing else None,
                )
                return
            self._update_pos_label()
            self._redraw()
            self._stop_playback()
            return
        self._update_pos_label()
        self._ensure_playhead_visible()
        self._redraw()
        try:
            self.canvas.after(50, lambda: self._tick(gen))
        except tk.TclError:
            pass

    def _ensure_playhead_visible(self):
        """Scroll the view so the current position stays inside the window."""
        if self.duration <= 0 or self.h_zoom <= 1.0:
            return
        view_dur = self.duration / self.h_zoom
        view_end = self.view_start + view_dur
        # Keep a small margin so the playhead isn't glued to the edge
        margin = view_dur * 0.05
        if self.position < self.view_start + margin:
            self.view_start = max(0.0, self.position - margin)
        elif self.position > view_end - margin:
            self.view_start = min(
                max(0.0, self.duration - view_dur),
                self.position - view_dur + margin,
            )

    def DROP_tick(self):
        """Update playhead while playing (approximate) and keep it in view."""
        if not self.playing:
            return
        # We don't get precise position from sd.play(blocking=True) easily,
        # so we advance by wall-clock * speed.
        # A streaming callback would be more accurate.
        start, end = self._play_range()
        self.position = min(end, self.position + 0.05 * self.speed)
        if self.position >= end - 0.001:
            if self._loop_mode:
                self.position = start
            else:
                self._update_pos_label()
                self._redraw()
                self._stop_playback()
                return
        self._update_pos_label()
        self._ensure_playhead_visible()
        self._redraw()
        if self.playing:
            self.canvas.after(50, self._tick)
#        else:
#            self._stop_playback()

    # ------------------------------------------------------------------ Helpers
    def _update_pos_label(self):
        base = f"{self._fmt_time(self.position)} / {self._fmt_time(self.duration)}"
        if self.sel_start is not None and self.sel_end is not None:
            a = min(self.sel_start, self.sel_end)
            b = max(self.sel_start, self.sel_end)
            if abs(b - a) < 0.01:
                base += f"  |  sel {self._fmt_time(a)}"
            else:
                base += f"  |  sel {self._fmt_time(a)}–{self._fmt_time(b)}"
        try:
            self.pos_label.configure(text=base)
        except tk.TclError:
            pass

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
                f"{{blue}}Sample rate: {{cyan}}{num_fmt(getattr(tags, 'samplerate', '—'), '.0f')}\n"
                f"{{blue}}Channels: {{cyan}}{num_fmt(getattr(tags, 'channels', '—'), '.0f')}\n"
                f"{{blue}}Bitrate: {{cyan}}{num_fmt(getattr(tags, 'bitrate', '—'), '.1f')}\n"
            )
        except Exception as exc:
            descr += f"{{red}}tinytag error: {exc}\n"
            debug(1, f"{{red}}tinytag error: {exc}")
    else:
        descr += "{yellow}tinytag not installed – limited metadata\n"

    # Explicit missing-dependency section
    if _MISSING or _OPTIONAL_NOTES:
        descr += "\n{yellow}────────────────────────────────────\n{yellow}Missing / optional dependencies:\n"
        for pkg in _MISSING:
            descr += f"{{red}}  • {pkg}  (required)\n"
        for note in _OPTIONAL_NOTES.values():
            descr += f"{{yellow}}  • {note}\n"
        descr += "{yellow}────────────────────────────────────\n"
    else:
        descr += "\n{green}All recommended packages are present.\n"

    descr += (
        "\n{blue}Shortcuts: {cyan}Space{blue}=play/pause  "
        "{cyan}Left/Right{blue}=seek or nudge edge  "
        "{cyan}+/-{blue}=H-zoom  {cyan}Ctrl+/-{blue}=V-zoom  "
        "{cyan}0{blue}=fit  {cyan}Esc{blue}=clear selection\n"
        "{blue}Mouse: {cyan}click{blue}=seek  {cyan}Shift+click/drag{blue}=select region  "
        "{cyan}drag edges{blue}=resize  {cyan}Shift+Play{blue}=loop selection\n"
    )

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
        except Exception:
            pass

        # Keep a reference so it is not garbage-collected
        canvas._waveform_player = WaveformPlayer(canvas, text, str(p.resolve()))

    descr += (
        "\n{yellow}Usage:\n"
        "{yellow}drag the separator to resize the panels\n"
        "{yellow}waveform: Ctrl+wheel = horizontal zoom, plain wheel = vertical\n"
    )

    if text is not None:
        text.delete("1.0", "end")
        insert_styled_text(text, descr)

    return True

def num_fmt(val, fmt) -> str:
    # Check if the value is either an int or a float
    if isinstance(val, (int, float)):
        return f"{val:{fmt}}"
    return str(val)

#eof
