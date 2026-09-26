"""
timing_helpers.py -- generic (non-Tk) logic for waveform_tab.py.

Ported from the Sequence Editor project's media_utils.py, trimmed to what
waveform_tab.py actually needs and adapted to live inside timED as a
*_tab.py plugin's helper module rather than a standalone app's data layer.
Nothing here imports tkinter or touches a GUI widget, so it can be used
(and unit-tested) independent of Tk, exactly like media_utils.py was.

Covers:
  - time formatting (format_time, format_time_ms)
  - waveform peak decoding via ffmpeg, plus a per-file JSON cache
  - marks/tracks persistence (a "<mediafile>-marks.json" sidecar, matching
    the sash/cursor sidecar-free convention timED's own utils.py uses for
    its *centralized* session.json -- marks/tracks use a per-file sidecar
    instead, same as the waveform cache below, so they travel with the
    media file rather than living in one growing session file)
  - overlap-lane assignment for drawing overlapping marks side by side
  - external timing-format exporters: xLights (.xtiming), LRC (.lrc),
    Audacity labels (.txt)
  - two playback engine wrappers behind one common interface: a
    SoundDevicePlaybackEngine (timED's own approach -- numpy/soundfile/
    sounddevice, real audio output, no external binary) and an
    FfplayPlaybackEngine (the Sequence Editor's original approach --
    shells out to ffplay). SoundDevicePlaybackEngine is active by
    default; see ACTIVE_ENGINE below to switch, or fall back automatically
    if its packages aren't installed.

Optional third-party imports (numpy, soundfile, sounddevice) are probed
at import time and degrade gracefully -- see HAS_NUMPY / HAS_SOUNDFILE /
HAS_SOUNDDEVICE and PLAYBACK_MISSING below. waveform_tab.py uses those to
decide whether to show timED's "missing packages -> offer to pip install"
UI (see its _show_missing_playback_ui), following the same pattern
audio_tab.py already uses for its own dependencies.
"""

from __future__ import annotations

import array
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Optional third-party playback dependencies (probed once, degrade gracefully)
# ---------------------------------------------------------------------------
PLAYBACK_MISSING: Dict[str, str] = {}  # import_name -> pip package name, for whatever's absent

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore
    PLAYBACK_MISSING["numpy"] = "numpy"

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore
    PLAYBACK_MISSING["soundfile"] = "soundfile"

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore
    PLAYBACK_MISSING["sounddevice"] = "sounddevice"
except OSError:
    # sounddevice is installed but its native PortAudio library isn't found
    # on this system -- pip alone won't fix that, but we still want to
    # degrade gracefully rather than crash the whole plugin on import.
    sd = None  # type: ignore
    PLAYBACK_MISSING["sounddevice"] = "sounddevice"

HAS_NUMPY = np is not None
HAS_SOUNDFILE = sf is not None
HAS_SOUNDDEVICE = sd is not None


# ---------------------------------------------------------------------------
# Fallback cache/sidecar directory (used only when a media file's own
# directory isn't writable -- the normal case is a sidecar file right next
# to the media file, same convention as the Sequence Editor used).
# ---------------------------------------------------------------------------
_FALLBACK_DIR = os.path.join(os.path.expanduser("~"), ".waveform_tab_cache")


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------

def format_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_time_ms(seconds: Optional[float]) -> str:
    """Like format_time, but with millisecond precision -- used for the
    waveform panel's timestamps, where sub-second precision is genuinely
    useful for identifying a specific point in the audio."""
    if seconds is None:
        return "--:--.---"
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000))
    h, rem_ms = divmod(total_ms, 3600000)
    m, rem_ms = divmod(rem_ms, 60000)
    s, ms = divmod(rem_ms, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m}:{s:02d}.{ms:03d}"


def blend_color(hex_color: str, alpha: float, bg: str = "#1e1e1e") -> str:
    """Blend hex_color toward bg by alpha (0..1) and return a solid hex
    color. Tk canvas fills don't support real alpha, so translucency on
    the dark waveform background is faked this way: a low alpha reads as
    a dim, "translucent" band color, a high alpha reads as bright/opaque
    -- which is how track bands (dim) and the marks within them
    (brighter) are told apart."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    bg = bg.lstrip("#")
    br, bgc, bb = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)
    nr = int(r * alpha + br * (1 - alpha))
    ng = int(g * alpha + bgc * (1 - alpha))
    nb = int(b * alpha + bb * (1 - alpha))
    return f"#{nr:02x}{ng:02x}{nb:02x}"


# A 1-2-5 progression (like a ruler or graph-paper axis) so whatever interval
# gets picked reads as a "round" step at a glance, from milliseconds up to
# hours -- covers everything from fully zoomed in to a very long recording.
_TIME_GRID_CANDIDATES = (
    0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
    1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
)


def time_grid_interval(view_span: float, target_lines: int = 8) -> float:
    """Pick a "nice" time interval (seconds) for vertical grid lines in
    the waveform so roughly `target_lines` lines fit across the current
    zoom level."""
    if view_span <= 0:
        return 1.0
    for candidate in _TIME_GRID_CANDIDATES:
        if view_span / candidate <= target_lines:
            return candidate
    return _TIME_GRID_CANDIDATES[-1]


def db_grid_step(amplitude_px: float) -> int:
    """Pick a "nice" dB step for horizontal amplitude grid lines based on
    how many vertical pixels are available."""
    if amplitude_px >= 300:
        return 1
    if amplitude_px >= 150:
        return 2
    if amplitude_px >= 80:
        return 3
    if amplitude_px >= 40:
        return 6
    return 10


# ---------------------------------------------------------------------------
# ffmpeg/ffprobe/ffplay discovery
# ---------------------------------------------------------------------------

def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def find_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


def find_ffplay() -> Optional[str]:
    return shutil.which("ffplay")


def get_audio_duration_seconds(path: str) -> Optional[float]:
    """Best-effort duration lookup via ffprobe (preferred, exact) or by
    parsing ffmpeg's own stderr banner as a fallback. Returns None if
    neither tool is available or duration can't be determined."""
    ffprobe = find_ffprobe()
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=15,
            )
            value = proc.stdout.strip()
            if value:
                return float(value)
        except Exception:
            pass
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        try:
            proc = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True, timeout=15)
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr)
            if match:
                h, m, s = match.groups()
                return int(h) * 3600 + int(m) * 60 + float(s)
        except Exception:
            pass
    return None


def decode_waveform_peaks(path: str, start_sec: float, duration_sec: float,
                           target_buckets: int, sample_rate: int = 22050) -> List[Tuple[float, float]]:
    """Decode a time range [start_sec, start_sec+duration_sec) of the given
    media file into a coarse min/max waveform with `target_buckets` points.

    Streams ffmpeg's raw PCM output in fixed-size chunks and only retains
    the small downsampled peaks list -- never the full decoded audio -- so
    memory use stays bounded regardless of file length or zoom range. This
    is the Sequence Editor's original waveform *visualization* decode path,
    kept as-is: it only needs ffmpeg on PATH, independent of whichever
    playback engine is active (see ACTIVE_ENGINE below) -- so the waveform
    itself still renders even if numpy/soundfile/sounddevice aren't
    installed; only playback would be unavailable in that case.
    Returns a list of (min, max) float tuples in range [-1.0, 1.0], or an
    empty list if ffmpeg isn't available or the range is invalid.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not duration_sec or duration_sec <= 0:
        return []

    target_buckets = max(20, min(4000, int(target_buckets)))
    total_samples = max(1, int(duration_sec * sample_rate))
    samples_per_bucket = max(1, total_samples // target_buckets)

    cmd = [
        ffmpeg, "-v", "error",
        "-ss", f"{max(0.0, start_sec):.3f}",
        "-t", f"{max(0.01, duration_sec):.3f}",
        "-i", path,
        "-f", "s16le", "-ac", "1", "-ar", str(sample_rate),
        "-",
    ]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    peaks: List[Tuple[float, float]] = []
    cur_min, cur_max, count = 1.0, -1.0, 0
    leftover = b""
    CHUNK_BYTES = 65536  # ~32K samples per read -- keeps memory flat regardless of file size

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kwargs)
    try:
        while len(peaks) < target_buckets:
            chunk = proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            data = leftover + chunk
            usable_len = len(data) - (len(data) % 2)
            leftover = data[usable_len:]
            samples = array.array("h")
            samples.frombytes(data[:usable_len])
            for s in samples:
                v = s / 32768.0
                if v < cur_min:
                    cur_min = v
                if v > cur_max:
                    cur_max = v
                count += 1
                if count >= samples_per_bucket:
                    peaks.append((cur_min, cur_max))
                    cur_min, cur_max, count = 1.0, -1.0, 0
                    if len(peaks) >= target_buckets:
                        break
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    if count > 0 and len(peaks) < target_buckets:
        peaks.append((cur_min, cur_max))
    return peaks


def rebucket_peaks(full_peaks: List[Tuple[float, float]], start_frac: float,
                    end_frac: float, target_buckets: int) -> List[Tuple[float, float]]:
    """Derive a (possibly zoomed) peaks array covering the fractional time
    range [start_frac, end_frac) of `full_peaks`, resampled to roughly
    `target_buckets` points -- pure Python, no decoding involved. Used to
    serve zoom/pan from the cached overview instead of re-invoking ffmpeg."""
    n = len(full_peaks)
    if n == 0:
        return []
    start_idx = max(0, min(n, int(start_frac * n)))
    end_idx = max(start_idx + 1, min(n, int(end_frac * n)))
    sub = full_peaks[start_idx:end_idx]
    if not sub:
        return []
    if len(sub) <= target_buckets:
        return sub  # already coarser than requested; no more detail to derive
    out = []
    per = len(sub) / target_buckets
    for i in range(target_buckets):
        a = int(i * per)
        b = max(a + 1, int((i + 1) * per))
        b = min(len(sub), b)
        group = sub[a:b]
        if not group:
            continue
        out.append((min(p[0] for p in group), max(p[1] for p in group)))
    return out


# ---------------------------------------------------------------------------
# Per-file sidecar helpers (waveform cache, marks/tracks)
# ---------------------------------------------------------------------------

def _sidecar_path(media_path: str, suffix: str) -> str:
    """Where a "<mediafile><suffix>" sidecar should live: next to the
    media file if that directory is writable, else a fallback dir under
    the home directory (using a flattened/escaped absolute path so files
    from different directories can't collide)."""
    directory = os.path.dirname(media_path) or "."
    base = os.path.basename(media_path)
    if os.access(directory, os.W_OK):
        return os.path.join(directory, f"{base}{suffix}")
    os.makedirs(_FALLBACK_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.abspath(media_path))
    return os.path.join(_FALLBACK_DIR, f"{safe}{suffix}")


def load_waveform_cache(media_path: str) -> Optional[Dict[str, Any]]:
    """Return {"duration":..., "peaks":[...]} if a valid, up-to-date cache
    exists for this media file, else None. Invalidated if the media
    file's mtime/size no longer match what was cached."""
    cache_path = _sidecar_path(media_path, "-waveform-cache.json")
    try:
        if not os.path.exists(cache_path):
            return None
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("source_mtime") != os.path.getmtime(media_path):
            return None
        if data.get("source_size") != os.path.getsize(media_path):
            return None
        peaks = [tuple(p) for p in data.get("peaks", [])]
        duration = data.get("duration")
        if not peaks or not duration:
            return None
        return {"duration": duration, "peaks": peaks}
    except Exception:
        return None


def save_waveform_cache(media_path: str, duration: float, peaks: List[Tuple[float, float]]) -> None:
    cache_path = _sidecar_path(media_path, "-waveform-cache.json")
    try:
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        data = {
            "source_mtime": os.path.getmtime(media_path),
            "source_size": os.path.getsize(media_path),
            "duration": duration,
            "peaks": peaks,
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass  # caching is best-effort


def load_marks(media_path: str) -> List[Dict[str, Any]]:
    """Return the persisted marks list for this media file, or [] if none
    exist or the file has changed since they were saved."""
    data = _load_marks_file(media_path)
    return data.get("marks", []) if data else []


def load_tracks(media_path: str) -> List[Dict[str, Any]]:
    data = _load_marks_file(media_path)
    return data.get("tracks", []) if data else []


def _load_marks_file(media_path: str) -> Optional[Dict[str, Any]]:
    path = _sidecar_path(media_path, "-marks.json")
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("source_mtime") != os.path.getmtime(media_path):
            return None
        if data.get("source_size") != os.path.getsize(media_path):
            return None
        return data
    except Exception:
        return None


def save_marks(media_path: str, marks: List[Dict[str, Any]],
                tracks: Optional[List[Dict[str, Any]]] = None) -> None:
    path = _sidecar_path(media_path, "-marks.json")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {
            "source_mtime": os.path.getmtime(media_path),
            "source_size": os.path.getsize(media_path),
            "marks": marks,
            "tracks": tracks or [],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass  # best-effort, same as the waveform cache


# ---------------------------------------------------------------------------
# Overlap-lane assignment (for drawing overlapping marks side by side)
# ---------------------------------------------------------------------------

def assign_lanes(items: List[Tuple[float, float, str]], epsilon: float = 0.0) -> Dict[str, int]:
    """Greedy interval-graph lane assignment (like overlapping clips in a
    video timeline): items is a list of (start, end, id) -- end may equal
    start for a point mark. Returns {id: lane_index}, with overlapping
    items never sharing a lane and non-overlapping items packed into as
    few lanes as possible. `epsilon` pads the required gap between items
    before they're allowed to share a lane, so near-touching points/edges
    still separate visually (otherwise their labels would collide even
    though the raw intervals don't technically overlap)."""
    order = sorted(items, key=lambda it: it[0])
    lane_end: List[float] = []  # lane_end[i] = the (unpadded) end time currently occupying lane i
    lanes: Dict[str, int] = {}
    for start, end, item_id in order:
        for i, occupied_until in enumerate(lane_end):
            if start >= occupied_until + epsilon:
                lane_end[i] = end
                lanes[item_id] = i
                break
        else:
            lane_end.append(end)
            lanes[item_id] = len(lane_end) - 1
    return lanes


# ---------------------------------------------------------------------------
# xLights timing export (.xtiming)
#
# Format confirmed against a real file exported directly from xLights
# 2025.13.1 -- always a <timings> root (even for one track), each
# <timing> carrying subType/SourceVersion attributes, and each <Effect>
# using lowercase starttime/endtime (no id/ref/protectionKeyType).
# ---------------------------------------------------------------------------

XLIGHTS_SOURCE_VERSION = "2025.13.1"


def _xlights_effect_element(mark: Dict[str, Any]) -> ET.Element:
    """One <Effect> entry for a single mark. xLights timing marks are
    always intervals (there's no "instant" marker concept), so a point
    mark gets a minimal 1ms width rather than being dropped or
    misrepresented as a longer range."""
    start_ms = int(round(mark["start"] * 1000))
    end = mark.get("end")
    end_ms = start_ms + 1 if end is None else max(start_ms + 1, int(round(end * 1000)))
    label = mark.get("label") or format_time_ms(mark["start"])
    return ET.Element("Effect", {
        "label": label,
        "starttime": str(start_ms),
        "endtime": str(end_ms),
    })


def build_xlights_timing_element(track_name: str, marks: List[Dict[str, Any]]) -> ET.Element:
    """Build one <timing name="..."> xLights timing-track element from a
    list of mark dicts (start/end in seconds -- end=None for a point
    mark -- and label). Marks are written in start-time order."""
    timing_el = ET.Element("timing", {
        "name": track_name,
        "subType": "",
        "SourceVersion": XLIGHTS_SOURCE_VERSION,
    })
    layer_el = ET.SubElement(timing_el, "EffectLayer")
    for m in sorted(marks, key=lambda m: m["start"]):
        layer_el.append(_xlights_effect_element(m))
    return timing_el


def export_xlights_timing_file(path: str, tracks_with_marks: List[Tuple[str, List[Dict[str, Any]]]]) -> None:
    """Write one .xtiming file. tracks_with_marks is a list of
    (track_name, marks) pairs, each becoming one <timing> element,
    always wrapped in a <timings> root -- xLights uses that wrapper even
    for a single-track export."""
    root = ET.Element("timings")
    for name, marks in tracks_with_marks:
        root.append(build_xlights_timing_element(name, marks))
    tree = ET.ElementTree(root)
    try:
        ET.indent(tree, space="  ")  # pretty-print -- Python 3.9+; purely cosmetic, safe to skip
    except AttributeError:
        pass
    tree.write(path, encoding="UTF-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# LRC (.lrc lyrics-timing) and Audacity label-track export
#
# Neither format has a native concept of multiple simultaneous timing
# tracks (LRC is one lyric timeline; an Audacity label file maps to one
# label track), so exporting more than one track at once merges them into
# a single chronological list, with each label prefixed by its track name
# to keep them distinguishable. A single-track export needs no prefix.
# ---------------------------------------------------------------------------

def _lrc_timestamp(seconds: float) -> str:
    total_cs = int(round(seconds * 100))
    m, rem_cs = divmod(total_cs, 6000)
    s, cs = divmod(rem_cs, 100)
    return f"{m:02d}:{s:02d}.{cs:02d}"


def _merge_tracks_chronologically(
    tracks_with_marks: List[Tuple[str, List[Dict[str, Any]]]]
) -> List[Tuple[str, Dict[str, Any]]]:
    """Flatten [(track_name, marks), ...] into a single list of
    (track_name, mark) pairs sorted by start time."""
    combined = [(name, m) for name, marks in tracks_with_marks for m in marks]
    combined.sort(key=lambda nm: nm[1]["start"])
    return combined


def export_lrc_file(path: str, tracks_with_marks: List[Tuple[str, List[Dict[str, Any]]]]) -> None:
    """Write one .lrc file. LRC only really supports point-in-time
    markers -- the next line's timestamp implicitly ends the previous
    one -- so a range mark's end time isn't represented, only its
    start."""
    multi = len(tracks_with_marks) > 1
    combined = _merge_tracks_chronologically(tracks_with_marks)
    header_names = ", ".join(name for name, _marks in tracks_with_marks)
    lines = [f"[ti:{header_names}]"]
    for name, m in combined:
        ts = _lrc_timestamp(m["start"])
        label = m.get("label") or format_time_ms(m["start"])
        if multi:
            label = f"[{name}] {label}"
        lines.append(f"[{ts}]{label}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def export_audacity_labels_file(path: str, tracks_with_marks: List[Tuple[str, List[Dict[str, Any]]]]) -> None:
    """Write one Audacity label-track file: tab-separated start/end/
    label, one per line, no header. A point mark is written with equal
    start/end times, which Audacity displays as a single point label
    rather than a region."""
    multi = len(tracks_with_marks) > 1
    combined = _merge_tracks_chronologically(tracks_with_marks)
    lines = []
    for name, m in combined:
        start = m["start"]
        end = m["end"] if m.get("end") is not None else start
        label = m.get("label") or format_time_ms(start)
        if multi:
            label = f"[{name}] {label}"
        lines.append(f"{start:.6f}\t{end:.6f}\t{label}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


EXPORT_FORMATS = {
    ".xtiming": ("xLights Timing", export_xlights_timing_file),
    ".lrc": ("LRC Lyrics", export_lrc_file),
    ".txt": ("Audacity Labels", export_audacity_labels_file),
}


def export_timing_tracks(path: str, tracks_with_marks: List[Tuple[str, List[Dict[str, Any]]]]) -> None:
    """Dispatch to the right exporter based on the chosen path's
    extension. Defaults to xLights format for an unrecognized/missing
    extension."""
    ext = os.path.splitext(path)[1].lower()
    _label, exporter = EXPORT_FORMATS.get(ext, EXPORT_FORMATS[".xtiming"])
    exporter(path, tracks_with_marks)


# ---------------------------------------------------------------------------
# Playback engines
#
# waveform_tab.py's playback controls (Play/Pause/Stop/skip/speed) talk to
# whichever engine make_playback_engine() returns through this one
# interface; it never touches sounddevice or subprocess directly. All
# *position/elapsed-time tracking* is the caller's job (waveform_tab.py's
# WaveformController, ported from the Sequence Editor's own play-state
# machine) -- an engine's only job is "start/stop playing this segment of
# this file", which keeps both engines simple and interchangeable.
# ---------------------------------------------------------------------------

class PlaybackEngine:
    """Common interface both playback backends implement."""

    def load(self, media_path: str) -> bool:
        """Called once when a new file is opened/selected. Returns True
        if this engine is able to play the file at all."""
        raise NotImplementedError

    def play_segment(self, start: float, duration: Optional[float], speed: float) -> bool:
        """Start playing from `start` seconds for `duration` seconds
        (None = to the end of the loaded file) at `speed`x. Non-blocking.
        Returns True if playback actually started."""
        raise NotImplementedError

    def stop(self) -> None:
        """Stop playback immediately (used for Pause/Stop/seek -- both
        engines implement "pause" as stop-and-remember-position, handled
        by the caller; see waveform_tab.py)."""
        raise NotImplementedError

    def is_active(self) -> bool:
        """True while the current segment is still playing; False once
        it finishes on its own or after stop()."""
        raise NotImplementedError

    def close(self) -> None:
        """Release any resources (decoded audio, subprocess) when the
        tab closes or a different file is loaded."""
        pass

class SoundDevicePlaybackEngine(PlaybackEngine):
    """timED's own approach (see audio_tab.py): soundfile decodes the
    whole file into memory once, sounddevice.play() outputs a sliced
    chunk. Speed is applied by simple nearest-neighbor resampling, which
    changes pitch along with tempo -- a real trade-off against
    FfplayPlaybackEngine's ffmpeg atempo filter, which time-stretches
    without changing pitch. In exchange, this engine needs no external
    ffplay/ffmpeg binary for playback (only the numpy/soundfile/
    sounddevice pip packages), and avoids the process-spawn latency of
    relaunching ffplay on every seek/pause/resume."""

    def __init__(self):
        self._data = None
        self._sr = None

    def load(self, media_path: str) -> bool:
        self._data = None
        self._sr = None
        if not (HAS_NUMPY and HAS_SOUNDFILE and HAS_SOUNDDEVICE):
            return False
        try:
            data, sr = sf.read(media_path, dtype="float32", always_2d=True)
        except Exception:
            return False
        self._data = data
        self._sr = sr
        return True

    def play_segment(self, start: float, duration: Optional[float], speed: float) -> bool:
        if not HAS_SOUNDDEVICE or self._data is None:
            return False
        sr = self._sr
        start_frame = max(0, int(start * sr))
        if duration is None:
            end_frame = len(self._data)
        else:
            end_frame = min(len(self._data), int((start + duration) * sr))
        if start_frame >= end_frame:
            return False
        chunk = self._data[start_frame:end_frame]
        if abs(speed - 1.0) > 1e-3 and HAS_NUMPY:
            new_len = max(1, int(len(chunk) / speed))
            idx = np.linspace(0, len(chunk) - 1, new_len).astype(int)
            chunk = chunk[idx]
        try:
            sd.stop()
            sd.play(chunk, sr, blocking=False)
        except Exception:
            return False
        return True

    def stop(self) -> None:
        if HAS_SOUNDDEVICE:
            try:
                sd.stop()
            except Exception:
                pass

    def is_active(self) -> bool:
        if not HAS_SOUNDDEVICE:
            return False
        try:
            stream = sd.get_stream()
            return stream is not None and getattr(stream, "active", False)
        except Exception:
            return False

    def close(self) -> None:
        self.stop()
        self._data = None
        self._sr = None


class FfplayPlaybackEngine(PlaybackEngine):
    """The Sequence Editor's original playback approach: shells out to
    ffplay (part of the ffmpeg toolchain already used for waveform
    decoding) for each segment, using its -af atempo filter for a true
    pitch-preserving time-stretch. Fully working and kept here for a
    future fallback -- e.g. a machine with ffmpeg on PATH but without
    the numpy/soundfile/sounddevice pip stack -- but it is NOT the
    active engine by default (see ACTIVE_ENGINE / make_playback_engine
    below): every seek, pause, or speed change kills and relaunches the
    ffplay process, which SoundDevicePlaybackEngine avoids."""

    def __init__(self):
        self._media_path: Optional[str] = None
        self._process = None

    def load(self, media_path: str) -> bool:
        self.close()
        self._media_path = media_path
        return find_ffplay() is not None

    def play_segment(self, start: float, duration: Optional[float], speed: float) -> bool:
        self.stop()
        if not self._media_path:
            return False
        ffplay_path = find_ffplay()
        if not ffplay_path:
            return False
        cmd = [ffplay_path, "-nodisp", "-autoexit", "-loglevel", "quiet"]
        if start:
            cmd += ["-ss", f"{start:.3f}"]
        if duration is not None:
            cmd += ["-t", f"{duration:.3f}"]
        if abs(speed - 1.0) > 1e-6:
            cmd += ["-af", f"atempo={speed:.4f}"]
        cmd.append(self._media_path)
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            )
        except Exception:
            self._process = None
            return False
        return True

    def stop(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                pass
            self._process = None

    def is_active(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def close(self) -> None:
        self.stop()
        self._media_path = None


# above are 2 alternate implementations for PlaybackEngine
# SoundDevicePlaybackEngine is currently the preferred engine (more direct control)

# Which engine waveform_tab.py should use by default. "sounddevice" is
# timED's own approach (per the task this file was written for); "ffplay"
# is the Sequence Editor's original approach, wrapped above and fully
# working, but disabled here -- flip this (or pass engine="ffplay" to
# make_playback_engine) to reactivate it.
ACTIVE_ENGINE = "sounddevice"


def make_playback_engine(engine: Optional[str] = None) -> PlaybackEngine:
    """Construct the requested playback engine ("sounddevice" or
    "ffplay"), defaulting to ACTIVE_ENGINE. Falls back to
    FfplayPlaybackEngine automatically if "sounddevice" is requested but
    its packages aren't installed and ffplay is available -- so playback
    still works out of the box on a machine that has ffmpeg but not the
    pip stack, without the caller needing to know why."""
    choice = engine or ACTIVE_ENGINE
    if choice == "sounddevice" and not (HAS_NUMPY and HAS_SOUNDFILE and HAS_SOUNDDEVICE) and find_ffplay():
        choice = "ffplay"
    if choice == "ffplay":
        return FfplayPlaybackEngine()
    return SoundDevicePlaybackEngine()
