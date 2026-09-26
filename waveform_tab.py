"""
waveform_tab.py -- waveform + timing-marks/tracks editor plugin for trackED.

Discovered automatically because this file matches *_tab.py (see
utils.discover_tab_plugins). Claims .mp3/.mp4/.wav and draws a waveform
with named point/range timing marks and named timing tracks into the
Canvas panel, with playback controls and Export Timing (xLights/LRC/
Audacity) in a right-click track menu.

Ported from the Sequence Editor project's tabs.py (an earlier, standalone
multi-tab editor this session also worked on), adapted to trackED's
plugin/onload() contract and generic EditorTab (sash position, cursor/
selection state, and the text pane are all handled by editor_tab.py
itself -- this plugin only owns the canvas). Two things were deliberately
left out of this port:

  - The Sequence Editor's onload() *scripting* feature -- a small Python
    sandbox letting a saved script generate marks/tracks programmatically.
    That's a different, unrelated use of the name "onload" than trackED's
    own plugin-discovery onload() this file implements; to avoid any
    confusion between the two, and because it's excluded from this port,
    it now lives in repl-todo.py, disabled, for possible reactivation
    later.
  - Its own ffplay-based playback engine is not the *active* one here --
    per the task this file was written for, playback now goes through a
    port of trackED's own approach instead (see timing_helpers.py:
    SoundDevicePlaybackEngine). The ffplay engine is still fully wrapped
    and available (FfplayPlaybackEngine, same file) for a future fallback;
    flip timing_helpers.ACTIVE_ENGINE to switch.

Dependencies: this plugin's own waveform decode/rendering only needs
ffmpeg/ffprobe on PATH (same as the Sequence Editor originally required)
-- no additional pip package is needed just to see and edit marks/tracks.
Only the *active* (sounddevice) playback engine needs the numpy/
soundfile/sounddevice pip packages; if they're missing, waveform editing
still works and the Play controls show an "Install missing packages"
prompt, following the same pattern audio_tab.py uses for its own
dependencies (see _show_missing_playback_ui / _install_packages below).

Known integration note: utils.discover_tab_plugins() tries *_tab.py
plugins in alphabetical order and the first one whose onload() returns
truthy claims the file. "audio_tab.py" sorts before "waveform_tab.py",
so as shipped, audio_tab.py's own onload() will claim .mp3/.mp4/.wav
first and this plugin will never run. Resolving that (e.g. retiring or
renaming audio_tab.py, or narrowing its claimed extensions) is a
deliberate decision left to you rather than something this file changes
on its own -- audio_tab.py's stem-separation feature has no equivalent
here and you may want to keep it.
"""

from __future__ import annotations

import copy
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, colorchooser, filedialog

from logview_tab import debug
from utils import insert_styled_text

import timing_helpers as th

VERSION = "1.0.0"
AUDIO_EXTS = {".mp3", ".mp4", ".wav"}


# ---------------------------------------------------------------------------
# Small UI helpers (ported as-is from the Sequence Editor)
# ---------------------------------------------------------------------------

class _Tooltip:
    """A small delayed tooltip for a toolbar button/control."""

    def __init__(self, widget, text: str, delay_ms: int = 500):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self.text, background="#ffffe0", relief="solid", borderwidth=1,
            font=("TkDefaultFont", 8), padx=4, pady=2,
        ).pack()

    def _hide(self, event=None):
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


def _make_magnifier_icon(sign: str, size: int = 14, color: str = "#333333", bg: str = "#f0f0f0"):
    """A tiny magnifying-glass icon (circle + handle, with a +/- drawn
    inside) built from raw pixel data -- avoids relying on an emoji glyph
    that isn't available in every system's default UI font."""
    img = tk.PhotoImage(width=size, height=size)
    img.put(bg, to=(0, 0, size, size))
    cx, cy, r = size // 2 - 2, size // 2 - 2, size // 2 - 4
    for y in range(size):
        for x in range(size):
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if r - 1 <= dist <= r + 0.5:
                img.put(color, (x, y))
    for i in range(max(3, size // 4)):
        x, y = cx + r - 1 + i, cy + r - 1 + i
        if 0 <= x < size and 0 <= y < size:
            img.put(color, (x, y))
            if x + 1 < size:
                img.put(color, (x + 1, y))
    mid = cy
    half = max(1, r // 2)
    for x in range(cx - half, cx + half + 1):
        if 0 <= x < size:
            img.put(color, (x, mid))
    if sign == "+":
        for y in range(cy - half, cy + half + 1):
            if 0 <= y < size:
                img.put(color, (cx, y))
    return img


# ---------------------------------------------------------------------------
# Missing playback-dependency UI (mirrors audio_tab.py's own pattern:
# detect what's missing, offer to pip install it, report back in the
# status area) -- but scoped to *playback only*, not the whole plugin, so
# marks/tracks editing still works even before/without installing it.
# ---------------------------------------------------------------------------

def _install_packages(packages, status_callback):
    """Run `python -m pip install ...` and return (success, message).
    Same approach audio_tab.py uses for its own missing dependencies."""
    if not packages:
        return True, "Nothing to install"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
    status_callback(f"Running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0:
            return True, "Install finished successfully.\nPlease restart the application."
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"pip failed (code {proc.returncode}):\n{err[:800]}"
    except subprocess.TimeoutExpired:
        return False, "pip timed out"
    except Exception as exc:
        return False, f"Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# WaveformController -- the plugin's main object. One per claimed canvas.
# ---------------------------------------------------------------------------

class WaveformController:
    """Owns the canvas' toolbar rows and drawing/interaction for one
    media file: waveform, timing marks/tracks, and playback. Analogous
    to audio_tab.py's own WaveformPlayer, but adding the marks/tracks
    layer that project doesn't have."""

    LABEL_ZONE_HEIGHT = 24
    TRACK_HEIGHT = 30
    TRACK_PALETTE = ["#3a86ff", "#ff6b6b", "#51cf66", "#ffb703"]
    SELECTION_COLOR = "#ffe066"
    EDGE_ZONE = 10
    UNDO_LIMIT = 20  # marks/tracks undo steps kept (no per-app preference here, unlike the original)

    def __init__(self, canvas: tk.Canvas, text: Optional[tk.Text], filepath: str, tab=None):
        self.canvas = canvas
        self.text = text
        self.filepath = filepath
        self.tab = tab
        self.parent = canvas.master  # top_frame, per editor_tab.py's layout

        # -- waveform/view state --
        self.audio_duration: Optional[float] = None
        self.full_peaks = []
        self.peaks = []
        self.view_start = 0.0
        self.view_end = 0.0

        # -- marks/tracks state --
        self.marks = th.load_marks(filepath)
        self.tracks = th.load_tracks(filepath)
        self.selected = None   # ("mark", id) | ("track", id) | None
        self._hover = None
        self._mark_hit_regions = {}
        self._track_label_regions = {}
        self._track_band_regions = {}
        self._move_drag = None
        self._track_drag = None
        self._press_info = None
        self._preview_range = None
        self._mark_history = [self._mark_snapshot()]
        self._mark_history_index = 0

        # -- playback state --
        self.engine = th.make_playback_engine()
        self._play_state = "stopped"  # "stopped" | "playing" | "paused"
        self._play_seg_start = 0.0
        self._play_seg_end: Optional[float] = None
        self._play_position = 0.0
        self._play_speed = 1.0
        self._play_started_wall: Optional[float] = None

        self._build_toolbars()
        self._configure_canvas()
        self._start_load()

    # ------------------------------------------------------------------ setup
    def _build_toolbars(self):
        # Clear any toolbar rows a previous onload() call on this same
        # canvas/tab left behind (editor_tab.py clears the canvas' own
        # children on reload, but not sibling frames packed into
        # canvas.master alongside it).
        for w in getattr(self.canvas, "_waveform_toolbars", []):
            try:
                w.destroy()
            except Exception:
                pass
        toolbars = []

        zoom_bar = ttk.Frame(self.parent)
        zoom_bar.pack(side="top", fill="x", before=self.canvas)
        toolbars.append(zoom_bar)
        self.duration_label = ttk.Label(zoom_bar, text="Duration: detecting...")
        self.duration_label.pack(side="left")
        self._duration_text_base = "Duration: detecting..."

        controls = ttk.Frame(zoom_bar)
        controls.pack(side="right")
        self.jump_start_btn = ttk.Button(controls, text="\u25c0|", width=3, state="disabled",
                                          command=lambda: self.jump_waveform_edge("start"))
        self._zoom_out_icon = _make_magnifier_icon("-")
        self._zoom_in_icon = _make_magnifier_icon("+")
        self.zoom_out_btn = ttk.Button(controls, image=self._zoom_out_icon, text="\u2212", compound="left",
                                        width=3, state="disabled", command=lambda: self.zoom_waveform("out"))
        self.zoom_fit_btn = ttk.Button(controls, text="Fit", width=3, state="disabled",
                                        command=lambda: self.zoom_waveform("fit"))
        self.zoom_in_btn = ttk.Button(controls, image=self._zoom_in_icon, text="+", compound="left",
                                       width=3, state="disabled", command=lambda: self.zoom_waveform("in"))
        self.jump_end_btn = ttk.Button(controls, text="|\u25b6", width=3, state="disabled",
                                        command=lambda: self.jump_waveform_edge("end"))
        self.jump_start_btn.pack(side="left", padx=(0, 2))
        self.zoom_out_btn.pack(side="left", padx=(0, 2))
        self.zoom_fit_btn.pack(side="left", padx=(0, 2))
        self.zoom_in_btn.pack(side="left", padx=(0, 2))
        self.jump_end_btn.pack(side="left")
        _Tooltip(self.jump_start_btn, "Jump to start")
        _Tooltip(self.zoom_out_btn, "Zoom out")
        _Tooltip(self.zoom_fit_btn, "Fit whole waveform in view")
        _Tooltip(self.zoom_in_btn, "Zoom in")
        _Tooltip(self.jump_end_btn, "Jump to end")

        play_bar = ttk.Frame(self.parent)
        play_bar.pack(side="top", fill="x", before=self.canvas)
        toolbars.append(play_bar)
        self.play_btn = ttk.Button(play_bar, text="\u25b6", width=3, state="disabled", command=self.toggle_play)
        self.play_back_btn = ttk.Button(play_bar, text="\u25c0\u25c0", width=3, state="disabled",
                                         command=lambda: self.skip_play(-5.0))
        self.play_fwd_btn = ttk.Button(play_bar, text="\u25b6\u25b6", width=3, state="disabled",
                                        command=lambda: self.skip_play(5.0))
        self.play_stop_btn = ttk.Button(play_bar, text="\u25a0", width=3, state="disabled", command=self.stop_play)
        self.play_back_btn.pack(side="left", padx=(0, 2))
        self.play_btn.pack(side="left", padx=(0, 2))
        self.play_stop_btn.pack(side="left", padx=(0, 2))
        self.play_fwd_btn.pack(side="left", padx=(0, 8))
        _Tooltip(self.play_back_btn, "Skip back 5 seconds")
        _Tooltip(self.play_btn, "Play / pause (plays the selected mark or range, else the whole file)")
        _Tooltip(self.play_stop_btn, "Stop playback")
        _Tooltip(self.play_fwd_btn, "Skip forward 5 seconds")

        ttk.Label(play_bar, text="Speed:").pack(side="left")
        self.play_speed_var = tk.StringVar(value="1x")
        self.play_speed_combo = ttk.Combobox(play_bar, textvariable=self.play_speed_var, state="readonly",
                                              values=["0.5x", "1x", "2x"], width=4)
        self.play_speed_combo.pack(side="left", padx=(4, 8))
        self.play_speed_combo.bind("<<ComboboxSelected>>", self._on_play_speed_changed)
        _Tooltip(self.play_speed_combo, "Playback speed")

        self.play_status_var = tk.StringVar(value="")
        ttk.Label(play_bar, textvariable=self.play_status_var, foreground="#555555").pack(side="left", padx=(0, 8))

        # Missing-playback-dependency prompt (hidden unless actually needed;
        # see _refresh_playback_availability). Placed in the same row so it
        # doesn't block marks/tracks editing above the fold.
        self.install_btn = ttk.Button(play_bar, text="Install playback packages",
                                       command=self._on_install_clicked)
        self._refresh_playback_availability()

        self.canvas._waveform_toolbars = toolbars

    def _refresh_playback_availability(self):
        """Show/hide the "install missing playback packages" button based
        on whether the active engine's dependencies are present. Doesn't
        block the waveform/marks/tracks UI -- only affects the Play
        controls' usability."""
        if th.PLAYBACK_MISSING and isinstance(self.engine, th.SoundDevicePlaybackEngine):
            self.install_btn.pack(side="left")
            self.play_status_var.set("Playback packages missing")
        else:
            self.install_btn.pack_forget()

    def _on_install_clicked(self):
        packages = list(th.PLAYBACK_MISSING.values())
        if not packages:
            return
        self.install_btn.configure(state="disabled", text="Installing...")
        self.play_status_var.set("Installing...")

        def status_cb(msg):
            self.canvas.after(0, lambda: self.play_status_var.set(msg[:60]))

        def worker():
            ok, message = _install_packages(packages, status_cb)
            self.canvas.after(0, lambda: self._install_finished(ok, message))

        threading.Thread(target=worker, daemon=True).start()

    def _install_finished(self, success, message):
        if success:
            self.play_status_var.set("Installed -- please restart the app")
            self.install_btn.configure(text="Installed -- restart required", state="disabled")
            messagebox.showinfo("Installation complete", "Packages installed.\nPlease restart the application.")
        else:
            self.play_status_var.set("Install failed")
            self.install_btn.configure(text="Install failed -- retry?", state="normal")
            messagebox.showerror("Installation failed", message)

    def _configure_canvas(self):
        c = self.canvas
        c.configure(background="#1e1e1e", height=160)
        c.create_text(10, 80, text="Preparing waveform...", fill="#aaaaaa", anchor="w", tags="placeholder")
        c.bind("<Button-1>", self._on_waveform_press)
        c.bind("<B1-Motion>", self._on_waveform_drag)
        c.bind("<ButtonRelease-1>", self._on_waveform_release)
        c.bind("<Double-Button-1>", self._on_waveform_double_click)
        c.bind("<Motion>", self._on_waveform_motion)
        c.bind("<Button-3>", self._on_waveform_right_click)
        c.bind("<MouseWheel>", self._on_waveform_wheel)
        c.bind("<Button-4>", self._on_waveform_wheel)
        c.bind("<Button-5>", self._on_waveform_wheel)
        c.bind("<Configure>", lambda e: self.render_waveform())
        c.bind("<Enter>", lambda e: c.focus_set())
        c.bind("<Leave>", self._on_waveform_leave)
        c.bind("<Up>", self._on_key_up)
        c.bind("<Down>", self._on_key_down)
        c.bind("<Left>", self._on_key_left)
        c.bind("<Right>", self._on_key_right)
        c.bind("<Delete>", self._on_key_delete)
        c.bind("<BackSpace>", self._on_key_delete)
        c.bind("<Escape>", self._on_key_escape)
        c.bind("<Tab>", self._on_key_tab)
        c.bind("<Shift-Tab>", self._on_key_shift_tab)
        c.bind("<ISO_Left_Tab>", self._on_key_shift_tab)
        # Bound directly on the canvas (not via bind_all on the app), same
        # fix as trackED.py's own undo()/redo() -- see the module docstring
        # of the Ctrl-Z/Ctrl-Y fix in trackED.py itself. A Text widget's
        # native undo binding and an app-wide bind_all would double-fire;
        # the canvas has no native undo binding to conflict with, so this
        # is the one safe place for marks/tracks undo/redo shortcuts.
        c.bind("<Control-z>", lambda e: self.undo_marks())
        c.bind("<Control-y>", lambda e: self.redo_marks())

    def _set_zoom_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for btn in (self.jump_start_btn, self.zoom_in_btn, self.zoom_out_btn,
                    self.zoom_fit_btn, self.jump_end_btn,
                    self.play_btn, self.play_back_btn, self.play_fwd_btn, self.play_stop_btn):
            btn.configure(state=state)

    # ------------------------------------------------------------------ loading
    def _start_load(self):
        """Threaded initial decode: duration + a cached (or freshly
        decoded) full-file waveform overview, mirroring the Sequence
        Editor's original open_media_file/finish_initial_waveform split.
        Also loads the file into the active playback engine."""
        result = {}

        def worker():
            try:
                cached = th.load_waveform_cache(self.filepath)
                if cached:
                    result["duration"] = cached["duration"]
                    result["peaks"] = cached["peaks"]
                else:
                    duration = th.get_audio_duration_seconds(self.filepath)
                    if duration is None:
                        result["error"] = "Could not determine audio duration (is ffmpeg/ffprobe on PATH?)"
                        return
                    peaks = th.decode_waveform_peaks(self.filepath, 0.0, duration, 2000)
                    th.save_waveform_cache(self.filepath, duration, peaks)
                    result["duration"] = duration
                    result["peaks"] = peaks
                self.engine.load(self.filepath)
            except Exception as exc:
                result["error"] = str(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        def poll():
            if thread.is_alive():
                self.canvas.after(80, poll)
                return
            if "error" in result:
                self._show_waveform_error(result["error"])
                debug(1, f"{{red}}waveform_tab load error: {result['error']}")
                return
            self._finish_initial_waveform(result["duration"], result["peaks"])

        self.canvas.after(80, poll)

    def _finish_initial_waveform(self, duration, peaks):
        self.audio_duration = duration
        self.view_start, self.view_end = 0.0, duration
        self.full_peaks = peaks
        self.peaks = th.rebucket_peaks(peaks, 0.0, 1.0, 2000)
        self._duration_text_base = f"Duration: {th.format_time_ms(duration)}"
        self.render_waveform()
        self._set_zoom_controls_enabled(True)
        debug(2, f"{{green}}waveform_tab loaded {self.filepath}: {th.format_time_ms(duration)}")

    def _show_waveform_error(self, message):
        self._duration_text_base = "Duration: unavailable"
        self.duration_label.configure(text=self._duration_text_base)
        self.canvas.delete("all")
        w = self.canvas.winfo_width() or 600
        h = self.canvas.winfo_height() or 160
        self.canvas.create_text(w // 2, h // 2, text=message, fill="#e08080", width=w - 20, justify="center")

    # ------------------------------------------------------------------ zoom / pan
    def zoom_waveform(self, action):
        if self.audio_duration is None:
            return
        duration = self.audio_duration
        span = self.view_end - self.view_start
        mid = (self.view_start + self.view_end) / 2
        if action == "fit":
            new_start, new_end = 0.0, duration
        elif action == "in":
            new_span = max(0.05, span / 2)
            new_start = max(0.0, mid - new_span / 2)
            new_end = min(duration, new_start + new_span)
            new_start = max(0.0, new_end - new_span)
        elif action == "out":
            new_span = min(duration, span * 2)
            new_start = max(0.0, mid - new_span / 2)
            new_end = min(duration, new_start + new_span)
            new_start = max(0.0, new_end - new_span)
        else:
            return
        self.view_start, self.view_end = new_start, new_end
        self._refresh_view_from_cache()

    def jump_waveform_edge(self, edge):
        if self.audio_duration is None:
            return
        duration = self.audio_duration
        span = self.view_end - self.view_start
        if edge == "start":
            self.view_start, self.view_end = 0.0, min(duration, span)
        elif edge == "end":
            self.view_end = duration
            self.view_start = max(0.0, duration - span)
        else:
            return
        self._refresh_view_from_cache()

    def _refresh_view_from_cache(self):
        """Redraw for the current view_start/view_end from the cached
        full-file overview. Unlike the Sequence Editor's original, this
        port always serves zoom/pan from the cached overview rather than
        re-invoking ffmpeg for extreme zoom levels -- a deliberate
        simplification for this port; see the module docstring."""
        if not self.audio_duration:
            return
        start_frac = self.view_start / self.audio_duration
        end_frac = self.view_end / self.audio_duration
        self.peaks = th.rebucket_peaks(self.full_peaks, start_frac, end_frac, 2000)
        self.render_waveform()

    # ------------------------------------------------------------------ marks/tracks core
    def _mark_snapshot(self):
        return (copy.deepcopy(self.marks), copy.deepcopy(self.tracks))

    def _mark_changed(self, record_history=True):
        """Marks/tracks are saved eagerly (like the waveform cache),
        rather than tied to trackED's own text-file Save flow -- they live
        in their own "<mediafile>-marks.json" sidecar (see
        timing_helpers.save_marks), so there's nothing to lose by not
        going through File > Save."""
        th.save_marks(self.filepath, self.marks, self.tracks)
        if record_history:
            self._push_mark_history()

    def _push_mark_history(self):
        del self._mark_history[self._mark_history_index + 1:]
        self._mark_history.append(self._mark_snapshot())
        max_len = self.UNDO_LIMIT + 1
        if len(self._mark_history) > max_len:
            del self._mark_history[: len(self._mark_history) - max_len]
        self._mark_history_index = len(self._mark_history) - 1

    def can_undo_marks(self):
        return self._mark_history_index > 0

    def can_redo_marks(self):
        return self._mark_history_index < len(self._mark_history) - 1

    def undo_marks(self):
        if not self.can_undo_marks():
            return
        self._mark_history_index -= 1
        self.marks, self.tracks = copy.deepcopy(self._mark_history[self._mark_history_index])
        self.selected = None
        self._hover = None
        self._mark_changed(record_history=False)
        self.render_waveform()

    def redo_marks(self):
        if not self.can_redo_marks():
            return
        self._mark_history_index += 1
        self.marks, self.tracks = copy.deepcopy(self._mark_history[self._mark_history_index])
        self.selected = None
        self._hover = None
        self._mark_changed(record_history=False)
        self.render_waveform()

    def _add_mark(self, mark_type, start, end, label="", track_id=None, source="user", record_history=True):
        mark = {
            "id": uuid.uuid4().hex[:8], "type": mark_type, "start": start, "end": end,
            "label": label, "track_id": track_id, "source": source,
        }
        self.marks.append(mark)
        self._mark_changed(record_history=record_history)
        return mark

    def _new_track(self, name, color=None, source="user", record_history=True):
        if color is None:
            color = self.TRACK_PALETTE[len(self.tracks) % len(self.TRACK_PALETTE)]
        track = {"id": uuid.uuid4().hex[:8], "name": name, "color": color, "source": source}
        self.tracks.append(track)
        self._mark_changed(record_history=record_history)
        return track

    def delete_track(self, track, record_history=True):
        """Delete a track (a track dict, or a track id string) and every
        mark assigned to it. No confirmation prompt -- see _delete_track
        for the confirming UI wrapper."""
        track_id = track["id"] if isinstance(track, dict) else track
        self.marks = [m for m in self.marks if m.get("track_id") != track_id]
        self.tracks = [t for t in self.tracks if t["id"] != track_id]
        if self.selected:
            kind, sel_id = self.selected
            if (kind == "track" and sel_id == track_id) or (
                kind == "mark" and not any(m["id"] == sel_id for m in self.marks)
            ):
                self.selected = None
        if record_history:
            self._mark_changed()
        self.render_waveform()
        return track_id

    def _delete_track(self, track):
        n_marks = sum(1 for m in self.marks if m.get("track_id") == track["id"])
        warning = f"Delete track \"{track['name']}\"?"
        if n_marks:
            plural = "s" if n_marks != 1 else ""
            warning += f" This will also delete the {n_marks} timing mark{plural} inside it."
        if not messagebox.askyesno("Delete Track", warning):
            return
        self.delete_track(track)

    def _delete_mark(self, mark):
        self.marks = [m for m in self.marks if m["id"] != mark["id"]]
        if self.selected == ("mark", mark["id"]):
            self.selected = None
        self._mark_changed()
        self.render_waveform()

    def _delete_all_marks(self):
        self.marks = []
        if self.selected and self.selected[0] == "mark":
            self.selected = None
        self._mark_changed()
        self.render_waveform()

    def _edit_mark_label(self, mark):
        new_label = simpledialog.askstring("Mark Label", "Label text:", initialvalue=mark["label"])
        if new_label is not None:
            mark["label"] = new_label.strip()
            self._mark_changed()
            self.render_waveform()

    def _clear_mark_label(self, mark):
        mark["label"] = ""
        self._mark_changed()
        self.render_waveform()

    def _set_mark_duration(self, mark):
        current = (mark["end"] - mark["start"]) if mark["type"] == "range" and mark.get("end") is not None else 0.0
        result = simpledialog.askfloat("Set Duration", "Duration (seconds):",
                                        initialvalue=round(current, 3), minvalue=0.01)
        if result is None:
            return
        duration = self.audio_duration or 0.0
        new_end = mark["start"] + result
        if duration:
            new_end = min(duration, new_end)
        mark["type"] = "range"
        mark["end"] = new_end
        self._mark_changed()
        self.render_waveform()

    def _rename_track(self, track):
        new_name = simpledialog.askstring("Rename Track", "Track name:", initialvalue=track["name"])
        if new_name and new_name.strip():
            track["name"] = new_name.strip()
            self._mark_changed()
            self.render_waveform()

    def _change_track_color(self, track):
        result = colorchooser.askcolor(color=track["color"], title="Track Color")
        if result and result[1]:
            track["color"] = result[1]
            self._mark_changed()
            self.render_waveform()

    def _prompt_new_track_for_mark(self, mark):
        name = simpledialog.askstring("New Timing Track", "Track name:")
        if name and name.strip():
            track = self._new_track(name.strip())
            mark["track_id"] = track["id"]
            self._mark_changed()
            self.render_waveform()

    # ------------------------------------------------------------------ export
    _EXPORT_FILETYPES = [
        ("xLights Timing", "*.xtiming"),
        ("LRC Lyrics", "*.lrc"),
        ("Audacity Labels", "*.txt"),
        ("All Files", "*.*"),
    ]

    def export_timing(self):
        """Export every track -- plus, as a track named "untitled", any
        marks still in the work area -- to one timing file, in xLights,
        LRC, or Audacity label format depending on what's chosen in the
        save dialog."""
        tracks_with_marks = []
        for tr in self.tracks:
            marks = [m for m in self.marks if m.get("track_id") == tr["id"]]
            if marks:
                tracks_with_marks.append((tr["name"], marks))
        untracked = [m for m in self.marks if m.get("track_id") is None]
        if untracked:
            tracks_with_marks.append(("untitled", untracked))
        if not tracks_with_marks:
            messagebox.showinfo("Export Timing", "There are no timing marks to export.")
            return
        initial = Path(self.filepath).stem + ".xtiming"
        path = filedialog.asksaveasfilename(title="Export Timing", initialfile=initial,
                                             defaultextension=".xtiming", filetypes=self._EXPORT_FILETYPES)
        if not path:
            return
        try:
            th.export_timing_tracks(path, tracks_with_marks)
            debug(1, f"{{green}}Exported {len(tracks_with_marks)} timing track(s) to {path}")
        except Exception as e:
            messagebox.showerror("Export Timing", f"Could not export timing:\n{e}")

    def export_timing_track(self, track):
        marks = [m for m in self.marks if m.get("track_id") == track["id"]]
        if not marks:
            messagebox.showinfo("Export Timing", f"Track \"{track['name']}\" has no timing marks to export.")
            return
        path = filedialog.asksaveasfilename(title="Export Timing", initialfile=f"{track['name']}.xtiming",
                                             defaultextension=".xtiming", filetypes=self._EXPORT_FILETYPES)
        if not path:
            return
        try:
            th.export_timing_tracks(path, [(track["name"], marks)])
            debug(1, f"{{green}}Exported timing track '{track['name']}' to {path}")
        except Exception as e:
            messagebox.showerror("Export Timing", f"Could not export timing:\n{e}")

    # ------------------------------------------------------------------ playback
    def playback_segment(self):
        if self.selected and self.selected[0] == "mark":
            mark = next((m for m in self.marks if m["id"] == self.selected[1]), None)
            if mark is not None:
                if mark["type"] == "range" and mark.get("end") is not None:
                    return mark["start"], mark["end"]
                return mark["start"], None
        return 0.0, None

    def _play_speed_value(self):
        return {"0.5x": 0.5, "1x": 1.0, "2x": 2.0}.get(self.play_speed_var.get(), 1.0)

    def _elapsed_play_time(self):
        if self._play_started_wall is None:
            return 0.0
        return (time.monotonic() - self._play_started_wall) * self._play_speed

    def toggle_play(self):
        if self._play_state == "playing":
            self.pause_play()
        else:
            self.start_play()

    def start_play(self):
        if self.audio_duration is None or not self.filepath:
            return
        if self._play_state == "paused":
            start_at = self._play_position
        else:
            self._play_seg_start, self._play_seg_end = self.playback_segment()
            start_at = self._play_seg_start
        self._play_speed = self._play_speed_value()
        duration = None
        if self._play_seg_end is not None:
            duration = max(0.0, self._play_seg_end - start_at)
            if duration <= 0:
                return
        ok = self.engine.play_segment(start_at, duration, self._play_speed)
        if not ok:
            self._refresh_playback_availability()
            if not th.PLAYBACK_MISSING:
                self.play_status_var.set("Playback failed to start")
            return
        self._play_state = "playing"
        self._play_position = start_at
        self._play_started_wall = time.monotonic()
        self._update_play_controls()
        self._poll_playback()

    def pause_play(self):
        if self._play_state != "playing":
            return
        self._play_position = min(
            self._play_seg_end if self._play_seg_end is not None else self.audio_duration or 0.0,
            self._play_position + self._elapsed_play_time(),
        )
        self.engine.stop()
        self._play_started_wall = None
        self._play_state = "paused"
        self._update_play_controls()
        self.render_waveform()

    def stop_play(self):
        self.engine.stop()
        self._play_started_wall = None
        self._play_state = "stopped"
        self._play_position = self._play_seg_start
        self._update_play_controls()
        self.render_waveform()

    def skip_play(self, delta):
        if self.audio_duration is None:
            return
        was_playing = self._play_state == "playing"
        if was_playing:
            self._play_position = self._play_position + self._elapsed_play_time()
            self.engine.stop()
            self._play_started_wall = None
        seg_end = self._play_seg_end if self._play_seg_end is not None else self.audio_duration
        self._play_position = max(self._play_seg_start, min(seg_end, self._play_position + delta))
        if was_playing:
            self._play_state = "paused"
            self.start_play()
        else:
            self._play_state = "paused"
            self._update_play_controls()
            self._follow_playhead(self._play_position)
            self.render_waveform()

    def current_play_position(self):
        if self._play_state == "stopped":
            return None
        pos = self._play_position
        if self._play_state == "playing":
            pos += self._elapsed_play_time()
        seg_end = self._play_seg_end if self._play_seg_end is not None else self.audio_duration
        if seg_end is not None:
            pos = min(pos, seg_end)
        return pos

    def _follow_playhead(self, pos):
        if self.audio_duration is None or pos is None:
            return False
        span = self.view_end - self.view_start
        if span <= 0:
            return False
        margin = span * 0.1
        if self.view_start <= pos <= self.view_end - margin:
            return False
        new_start = max(0.0, min(self.audio_duration - span, pos - span * 0.1))
        if abs(new_start - self.view_start) < 1e-9:
            return False
        self.view_start, self.view_end = new_start, new_start + span
        if self.full_peaks:
            self.peaks = th.rebucket_peaks(
                self.full_peaks, self.view_start / self.audio_duration, self.view_end / self.audio_duration, 2000,
            )
        return True

    def _poll_playback(self):
        """Watch for the engine's segment finishing on its own so the
        controls reset without the user pressing Stop, and drive the
        moving playhead while playing."""
        if self._play_state != "playing":
            return
        if not self.engine.is_active():
            self._play_state = "stopped"
            self._play_position = self._play_seg_start
            self._play_started_wall = None
            self._update_play_controls()
            self.render_waveform()
            return
        self._update_play_controls()
        self._follow_playhead(self.current_play_position())
        self.render_waveform()
        self.canvas.after(100, self._poll_playback)

    def _on_play_speed_changed(self, event=None):
        if self._play_state == "playing":
            self._play_position = self._play_position + self._elapsed_play_time()
            self.engine.stop()
            self._play_started_wall = None
            self._play_state = "paused"
            self.start_play()

    def _update_play_controls(self):
        self.play_btn.configure(text="\u23f8" if self._play_state == "playing" else "\u25b6")
        if self._play_state == "stopped":
            if not th.PLAYBACK_MISSING:
                self.play_status_var.set("")
            return
        pos = self._play_position + (self._elapsed_play_time() if self._play_state == "playing" else 0.0)
        seg_end = self._play_seg_end if self._play_seg_end is not None else self.audio_duration
        label = "Playing" if self._play_state == "playing" else "Paused"
        self.play_status_var.set(f"{label} {th.format_time_ms(pos)} / {th.format_time_ms(seg_end)}")

    def stop_and_release(self):
        """Called when the tab/file is going away -- stop playback and
        release the engine's resources."""
        self.engine.stop()
        self.engine.close()

    # ------------------------------------------------------------------ hit-testing
    def _time_at_x(self, x):
        w = self.canvas.winfo_width() or 1
        frac = max(0.0, min(1.0, x / w))
        return self.view_start + frac * (self.view_end - self.view_start)

    def _track_layout(self):
        total_h = self.canvas.winfo_height() or 160
        reserved = (len(self.tracks) + 1) * self.TRACK_HEIGHT
        work_height = max(60, total_h - reserved)
        return {"total_h": total_h, "work_height": work_height, "track_top": work_height}

    def _track_zone_at_y(self, y):
        layout = self._track_layout()
        if y < layout["work_height"]:
            return "work", None
        idx = int((y - layout["track_top"]) // self.TRACK_HEIGHT)
        if 0 <= idx < len(self.tracks):
            return "track", self.tracks[idx]["id"]
        return "new_track", None

    def _is_highlighted(self, kind, item_id):
        target = (kind, item_id)
        return self.selected == target or self._hover == target

    def _track_label_hit(self, track_id, x, y):
        bbox = self._track_label_regions.get(track_id)
        if not bbox:
            return False
        x1, y1, x2, y2 = bbox
        return x1 - 4 <= x <= x2 + 4 and y1 - 4 <= y <= y2 + 4

    def _mark_and_edge_at_x(self, x, pool=None):
        if pool is None:
            pool = [m for m in self.marks if m.get("track_id") is None]
        w = self.canvas.winfo_width() or 1
        span = self.view_end - self.view_start
        if span <= 0:
            return None, None
        x_of = lambda t: (t - self.view_start) / span * w
        tolerance = self.EDGE_ZONE
        for m in pool:
            if m["type"] == "point":
                if not (self.view_start <= m["start"] <= self.view_end):
                    continue
                if abs(x - x_of(m["start"])) <= tolerance:
                    return m, None
            else:
                if m["end"] < self.view_start or m["start"] > self.view_end:
                    continue
                x1 = x_of(max(m["start"], self.view_start))
                x2 = x_of(min(m["end"], self.view_end))
                if x1 - tolerance <= x <= x2 + tolerance:
                    if abs(x - x1) <= self.EDGE_ZONE and abs(x - x1) <= abs(x - x2):
                        return m, "start"
                    if abs(x - x2) <= self.EDGE_ZONE:
                        return m, "end"
                    return m, None
        return None, None

    def _cursor_for_edge(self, edge):
        if edge == "start":
            return "left_side"
        if edge == "end":
            return "right_side"
        return "sb_h_double_arrow"

    def _selected_work_mark_at(self, x, y):
        if not self.selected or self.selected[0] != "mark":
            return None
        mark = next((m for m in self.marks if m["id"] == self.selected[1]), None)
        if mark is None or mark.get("track_id") is not None:
            return None
        if self.audio_duration is None or self.view_end <= self.view_start:
            return None
        layout = self._track_layout()
        if not (0 <= y < layout["work_height"]):
            return None
        w = self.canvas.winfo_width() or 1
        span = self.view_end - self.view_start
        x_of = lambda t: (t - self.view_start) / span * w
        if mark["type"] == "range":
            x1, x2 = x_of(mark["start"]), x_of(mark["end"])
            if min(x1, x2) - self.EDGE_ZONE <= x <= max(x1, x2) + self.EDGE_ZONE:
                return mark
        else:
            if abs(x - x_of(mark["start"])) <= self.EDGE_ZONE:
                return mark
        return None

    # ------------------------------------------------------------------ mouse/keyboard
    def _on_waveform_motion(self, event):
        if self._move_drag or self._press_info or self._track_drag:
            return
        cursor = ""
        hit = None
        if self.audio_duration is not None and self.view_end > self.view_start:
            zone, target = self._track_zone_at_y(event.y)
            if zone == "work":
                if event.y >= self.LABEL_ZONE_HEIGHT:
                    mark, edge = self._mark_and_edge_at_x(event.x)
                    if mark is not None:
                        cursor = self._cursor_for_edge(edge)
                        hit = ("mark", mark["id"])
            elif zone == "track":
                if self._track_label_hit(target, event.x, event.y):
                    cursor = "fleur"
                    hit = ("track", target)
                else:
                    pool = [m for m in self.marks if m.get("track_id") == target]
                    mark, edge = self._mark_and_edge_at_x(event.x, pool=pool)
                    if mark is not None:
                        cursor = self._cursor_for_edge(edge)
                        hit = ("mark", mark["id"])
        self.canvas.configure(cursor=cursor)
        if hit != self._hover:
            self._hover = hit
            self.render_waveform()

    def _on_waveform_leave(self, event):
        if self._hover is not None:
            self._hover = None
            self.render_waveform()

    def _deselect(self):
        if self.selected is not None:
            self.selected = None
            self.render_waveform()

    def _on_waveform_press(self, event):
        if self.audio_duration is None or self.view_end <= self.view_start:
            return
        self._move_drag = None
        self._press_info = None
        self._track_drag = None

        zone, target = self._track_zone_at_y(event.y)

        if zone == "track":
            if self._track_label_hit(target, event.x, event.y):
                self.selected = ("track", target)
                self._track_drag = {"track_id": target}
                self.canvas.configure(cursor="fleur")
                self.render_waveform()
                return
            pool = [m for m in self.marks if m.get("track_id") == target]
            hit_mark, edge = self._mark_and_edge_at_x(event.x, pool=pool)
            if hit_mark is not None:
                self.selected = ("mark", hit_mark["id"])
                self._move_drag = {
                    "mark": hit_mark, "edge": edge,
                    "start_orig": hit_mark["start"], "end_orig": hit_mark.get("end"),
                    "press_time": self._time_at_x(event.x), "orig_track_id": hit_mark.get("track_id"),
                }
                self.canvas.configure(cursor=self._cursor_for_edge(edge))
                self.render_waveform()
                return
            self._deselect()
            return

        if zone == "new_track":
            self._deselect()
            return

        if event.y >= self.LABEL_ZONE_HEIGHT:
            hit_mark, edge = self._mark_and_edge_at_x(event.x)
            if hit_mark is not None:
                self.selected = ("mark", hit_mark["id"])
                self._move_drag = {
                    "mark": hit_mark, "edge": edge,
                    "start_orig": hit_mark["start"], "end_orig": hit_mark.get("end"),
                    "press_time": self._time_at_x(event.x), "orig_track_id": hit_mark.get("track_id"),
                }
                self.canvas.configure(cursor=self._cursor_for_edge(edge))
                self.render_waveform()
                return
        self._press_info = {
            "x": event.x, "time": self._time_at_x(event.x),
            "shift": bool(event.state & 0x0001), "dragging": False,
        }

    def _on_waveform_drag(self, event):
        if self._track_drag:
            self._drag_reorder_track(event)
            return
        if self._move_drag:
            self._drag_move_mark(event)
            return
        if not self._press_info or self.audio_duration is None:
            return
        if not self._press_info["dragging"] and abs(event.x - self._press_info["x"]) < 4:
            return
        self._press_info["dragging"] = True
        current_time = self._snap_time(self._time_at_x(event.x), event)
        anchor = self._snap_time(self._press_info["time"], event)
        self._preview_range = (min(anchor, current_time), max(anchor, current_time))
        self._press_info["last_xy"] = (event.x, event.y)
        self.render_waveform()

    def _snap_time(self, t, event):
        if event.state & 0x0001:
            return t
        interval = th.time_grid_interval(self.view_end - self.view_start)
        return round(t / interval) * interval if interval > 0 else t

    def _snap_delta(self, delta, event):
        if event.state & 0x0001:
            return delta
        interval = th.time_grid_interval(self.view_end - self.view_start)
        if interval <= 0:
            return delta
        return round(delta / interval) * interval

    def _drag_reorder_track(self, event):
        track_id = self._track_drag["track_id"]
        cur_idx = next((i for i, t in enumerate(self.tracks) if t["id"] == track_id), None)
        if cur_idx is None:
            return
        layout = self._track_layout()
        idx = int((event.y - layout["track_top"]) // self.TRACK_HEIGHT)
        idx = max(0, min(len(self.tracks) - 1, idx))
        if idx != cur_idx:
            track = self.tracks.pop(cur_idx)
            self.tracks.insert(idx, track)
            self._mark_changed()
            self.render_waveform()

    def _drag_move_mark(self, event):
        duration = self.audio_duration or 0.0
        mark = self._move_drag["mark"]
        edge = self._move_drag.get("edge")
        min_span = 0.01

        orig_track_id = self._move_drag.get("orig_track_id")
        orig_zone = ("work", None) if orig_track_id is None else ("track", orig_track_id)
        current_zone = self._track_zone_at_y(event.y)
        crossing_rows = edge is None and current_zone != orig_zone

        if crossing_rows:
            mark["start"] = self._move_drag["start_orig"]
            if mark["type"] == "range":
                mark["end"] = self._move_drag["end_orig"]
            self.canvas.configure(cursor="hand2")
        else:
            current_time = self._time_at_x(event.x)
            delta = self._snap_delta(current_time - self._move_drag["press_time"], event)
            if mark["type"] == "point" or edge is None:
                if mark["type"] == "point":
                    mark["start"] = max(0.0, min(duration, self._move_drag["start_orig"] + delta))
                else:
                    span = self._move_drag["end_orig"] - self._move_drag["start_orig"]
                    new_start = self._move_drag["start_orig"] + delta
                    new_start = max(0.0, min(duration - span, new_start))
                    mark["start"] = new_start
                    mark["end"] = new_start + span
            elif edge == "start":
                new_start = self._move_drag["start_orig"] + delta
                new_start = max(0.0, min(self._move_drag["end_orig"] - min_span, new_start))
                mark["start"] = new_start
            else:
                new_end = self._move_drag["end_orig"] + delta
                new_end = min(duration, max(self._move_drag["start_orig"] + min_span, new_end))
                mark["end"] = new_end
            self.canvas.configure(cursor=self._cursor_for_edge(edge))
        if edge is None:
            self._move_drag["preview_zone"] = current_zone
        self._move_drag["last_xy"] = (event.x, event.y)
        self.render_waveform()

    def _on_waveform_release(self, event):
        if self._track_drag:
            self._track_drag = None
            self.render_waveform()
            self._on_waveform_motion(event)
            return
        if self._move_drag:
            mark = self._move_drag["mark"]
            edge = self._move_drag.get("edge")
            if edge is None:
                zone, target = self._track_zone_at_y(event.y)
                if zone == "work":
                    mark["track_id"] = None
                elif zone == "track":
                    mark["track_id"] = target
                else:
                    self._prompt_new_track_for_mark(mark)
            self._move_drag = None
            self._mark_changed()
            self.render_waveform()
            self._on_waveform_motion(event)
            return
        if not self._press_info:
            return
        new_mark = None
        prompt_for_label = True
        if self._press_info["dragging"] and self._preview_range:
            start_t, end_t = self._preview_range
            if end_t - start_t > 0.01:
                new_mark = self._add_mark("range", start_t, end_t)
                self.selected = ("mark", new_mark["id"])
            self._preview_range = None
        else:
            clicked_time = self._press_info["time"]
            work_marks = [m for m in self.marks if m.get("track_id") is None]
            if self._press_info["shift"] and work_marks:
                last = work_marks[-1]
                anchor = last["start"]
                label = last.get("label", "")
                if last["type"] == "point":
                    self.marks.remove(last)
                new_mark = self._add_mark("range", min(anchor, clicked_time), max(anchor, clicked_time), label=label)
                prompt_for_label = False
            else:
                new_mark = self._add_mark("point", clicked_time, None)
            self.selected = ("mark", new_mark["id"])
        self._press_info = None
        self.render_waveform()
        self._on_waveform_motion(event)
        if new_mark is not None and prompt_for_label:
            self._edit_mark_label(new_mark)

    def _on_key_up(self, event):
        self._move_selected_vertically(-1)
        return "break"

    def _on_key_down(self, event):
        self._move_selected_vertically(1)
        return "break"

    def _move_selected_vertically(self, direction):
        if not self.selected or self.audio_duration is None:
            return
        kind, sel_id = self.selected
        if kind == "track":
            idx = next((i for i, t in enumerate(self.tracks) if t["id"] == sel_id), None)
            if idx is None:
                return
            new_idx = idx + direction
            if 0 <= new_idx < len(self.tracks):
                track = self.tracks.pop(idx)
                self.tracks.insert(new_idx, track)
                self._mark_changed()
                self.render_waveform()
            return

        mark = next((m for m in self.marks if m["id"] == sel_id), None)
        if mark is None:
            return
        track_id = mark.get("track_id")
        if track_id is None:
            if direction > 0:
                if self.tracks:
                    mark["track_id"] = self.tracks[0]["id"]
                    self._mark_changed()
                    self.render_waveform()
                else:
                    self._prompt_new_track_for_mark(mark)
            return
        idx = next((i for i, t in enumerate(self.tracks) if t["id"] == track_id), None)
        if idx is None:
            return
        new_idx = idx + direction
        if new_idx < 0:
            mark["track_id"] = None
            self._mark_changed()
            self.render_waveform()
        elif new_idx < len(self.tracks):
            mark["track_id"] = self.tracks[new_idx]["id"]
            self._mark_changed()
            self.render_waveform()
        else:
            self._prompt_new_track_for_mark(mark)

    def _on_key_left(self, event):
        self._nudge_selected_horizontally(-1)
        return "break"

    def _on_key_right(self, event):
        self._nudge_selected_horizontally(1)
        return "break"

    def _nudge_selected_horizontally(self, direction):
        if not self.selected or self.selected[0] != "mark" or self.audio_duration is None:
            return
        mark = next((m for m in self.marks if m["id"] == self.selected[1]), None)
        if mark is None:
            return
        interval = th.time_grid_interval(self.view_end - self.view_start)
        delta = direction * interval
        duration = self.audio_duration
        if mark["type"] == "point":
            mark["start"] = max(0.0, min(duration, mark["start"] + delta))
        else:
            span = mark["end"] - mark["start"]
            new_start = max(0.0, min(duration - span, mark["start"] + delta))
            mark["start"] = new_start
            mark["end"] = new_start + span
        self._mark_changed()
        self.render_waveform()

    def _on_key_delete(self, event):
        if not self.selected:
            return "break"
        kind, sel_id = self.selected
        if kind == "mark":
            mark = next((m for m in self.marks if m["id"] == sel_id), None)
            if mark is not None:
                self._delete_mark(mark)
        else:
            track = next((t for t in self.tracks if t["id"] == sel_id), None)
            if track is not None:
                self._delete_track(track)
        return "break"

    def _on_key_escape(self, event):
        if self.selected is not None:
            self.selected = None
            self.render_waveform()
        return "break"

    def _on_key_tab(self, event):
        self._select_adjacent_mark(1)
        return "break"

    def _on_key_shift_tab(self, event):
        self._select_adjacent_mark(-1)
        return "break"

    def _select_adjacent_mark(self, direction):
        if not self.marks:
            return
        ordered = sorted(self.marks, key=lambda m: (m["start"], m["id"]))
        current_idx = None
        if self.selected and self.selected[0] == "mark":
            current_idx = next((i for i, m in enumerate(ordered) if m["id"] == self.selected[1]), None)
        if current_idx is None:
            new_idx = 0 if direction > 0 else len(ordered) - 1
        else:
            new_idx = (current_idx + direction) % len(ordered)
        mark = ordered[new_idx]
        self.selected = ("mark", mark["id"])
        self._hover = None
        self._ensure_mark_visible(mark)
        self.render_waveform()

    def _ensure_mark_visible(self, mark):
        if self.audio_duration is None:
            return
        if self.view_start <= mark["start"] <= self.view_end:
            return
        duration = self.audio_duration
        span = self.view_end - self.view_start
        if span <= 0:
            return
        new_start = max(0.0, min(duration - span, mark["start"] - span / 2))
        self.view_start, self.view_end = new_start, new_start + span
        if self.full_peaks:
            self.peaks = th.rebucket_peaks(self.full_peaks, self.view_start / duration, self.view_end / duration, 2000)

    def _on_waveform_wheel(self, event):
        if self.audio_duration is None:
            return
        if getattr(event, "num", None) == 4:
            direction = "in"
        elif getattr(event, "num", None) == 5:
            direction = "out"
        elif getattr(event, "delta", None):
            direction = "in" if event.delta > 0 else "out"
        else:
            return
        self.zoom_waveform(direction)
        return "break"

    def _on_waveform_double_click(self, event):
        for tr in self.tracks:
            if self._track_label_hit(tr["id"], event.x, event.y):
                self._rename_track(tr)
                return "break"
        mark = self._selected_work_mark_at(event.x, event.y)
        if mark is None:
            hit_id = None
            for mark_id, bbox in self._mark_hit_regions.items():
                if not bbox:
                    continue
                x1, y1, x2, y2 = bbox
                if x1 - 4 <= event.x <= x2 + 4 and y1 - 4 <= event.y <= y2 + 4:
                    hit_id = mark_id
                    break
            if hit_id is not None:
                mark = next((m for m in self.marks if m["id"] == hit_id), None)
        if mark is not None:
            self._edit_mark_label(mark)
        return "break"

    def _on_waveform_right_click(self, event):
        for tr in self.tracks:
            if self._track_label_hit(tr["id"], event.x, event.y):
                self._show_track_menu(tr, event)
                return

        mark = self._selected_work_mark_at(event.x, event.y)
        if mark is None:
            hit_id = None
            for mark_id, bbox in self._mark_hit_regions.items():
                if not bbox:
                    continue
                x1, y1, x2, y2 = bbox
                if x1 - 4 <= event.x <= x2 + 4 and y1 - 4 <= event.y <= y2 + 4:
                    hit_id = mark_id
                    break
            if hit_id is None:
                return
            mark = next((m for m in self.marks if m["id"] == hit_id), None)
            if mark is None:
                return

        menu = tk.Menu(self.canvas, tearoff=False)
        if mark["label"]:
            menu.add_command(label="Edit Label...", command=lambda: self._edit_mark_label(mark))
            menu.add_command(label="Delete Label", command=lambda: self._clear_mark_label(mark))
        else:
            menu.add_command(label="Add Label...", command=lambda: self._edit_mark_label(mark))
        if mark.get("track_id") is None:
            menu.add_command(label="Set Duration...", command=lambda: self._set_mark_duration(mark))
        menu.add_separator()
        menu.add_command(label="Delete Mark", command=lambda: self._delete_mark(mark))
        menu.add_command(label="Delete All Marks", command=self._delete_all_marks)
        menu.tk_popup(event.x_root, event.y_root)

    def _show_track_menu(self, track, event):
        menu = tk.Menu(self.canvas, tearoff=False)
        menu.add_command(label="Rename Track...", command=lambda: self._rename_track(track))
        menu.add_command(label="Change Color...", command=lambda: self._change_track_color(track))
        menu.add_separator()
        menu.add_command(label="Export Timing...", command=lambda: self.export_timing_track(track))
        menu.add_separator()
        menu.add_command(label="Delete Track", command=lambda: self._delete_track(track))
        menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------ rendering
    def _render_grid(self, x_of, w, h, mid_y, amplitude_px):
        c = self.canvas
        db_step = th.db_grid_step(amplitude_px)
        floor_db = min(60, db_step * 8)
        db = 0
        while db >= -floor_db:
            amp = 10 ** (db / 20)
            y_top = mid_y - amp * amplitude_px
            y_bot = mid_y + amp * amplitude_px
            c.create_line(0, y_top, w, y_top, fill="#7a7a7a", dash=(3, 2))
            c.create_line(0, y_bot, w, y_bot, fill="#7a7a7a", dash=(3, 2))
            c.create_text(2, y_top, text=f"{db} dB", fill="#39ff14", anchor="sw", font=("TkDefaultFont", 7, "bold"))
            db -= db_step

        span = self.view_end - self.view_start
        if span > 0:
            interval = th.time_grid_interval(span)
            t = int(self.view_start / interval) * interval
            guard = 0
            while t <= self.view_end and guard < 200:
                if t >= self.view_start:
                    x = x_of(t)
                    c.create_line(x, 0, x, h, fill="#7a7a7a", dash=(3, 2))
                    c.create_text(x + 2, h - 16, text=th.format_time_ms(t), fill="#bbbbbb", anchor="sw",
                                  font=("TkDefaultFont", 7))
                t += interval
                guard += 1

    def _lane_epsilon(self, w):
        span = self.view_end - self.view_start
        if not w or span <= 0:
            return 0.0
        return 16.0 * span / w

    def _marks_with_lanes(self, pool, w):
        visible = []
        for m in pool:
            if m["type"] == "point":
                if self.view_start <= m["start"] <= self.view_end:
                    visible.append(m)
            else:
                if m["end"] >= self.view_start and m["start"] <= self.view_end:
                    visible.append(m)
        visible.sort(key=lambda m: m["start"])
        items = [(m["start"], m["end"] if m["type"] == "range" else m["start"], m["id"]) for m in visible]
        lanes = th.assign_lanes(items, self._lane_epsilon(w))
        lane_count = (max(lanes.values()) + 1) if lanes else 1
        return visible, lanes, lane_count

    def render_waveform(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 600
        layout = self._track_layout()
        h = layout["work_height"]
        mid_y = h // 2
        amplitude_px = mid_y - 10
        span = self.view_end - self.view_start

        if self.audio_duration is not None:
            self.duration_label.configure(
                text=f"{self._duration_text_base}    View: {th.format_time_ms(self.view_start)} - {th.format_time_ms(self.view_end)}"
            )

        def x_of(t):
            if span <= 0:
                return 0
            return (t - self.view_start) / span * w

        self._render_grid(x_of, w, h, mid_y, amplitude_px)

        work_marks = [m for m in self.marks if m.get("track_id") is None]
        visible_work, work_lanes, work_lane_count = self._marks_with_lanes(work_marks, w)
        lane_h = h / work_lane_count
        for m in visible_work:
            if m["type"] != "range":
                continue
            x1 = x_of(max(m["start"], self.view_start))
            x2 = x_of(min(m["end"], self.view_end))
            lane = work_lanes[m["id"]]
            c.create_rectangle(x1, lane * lane_h, x2, (lane + 1) * lane_h, fill="#2e4a63", outline="")
        if self._preview_range:
            ps, pe = self._preview_range
            if pe >= self.view_start and ps <= self.view_end:
                x1 = x_of(max(ps, self.view_start))
                x2 = x_of(min(pe, self.view_end))
                c.create_rectangle(x1, 0, x2, h, fill="#4a4a2e", outline="")

        n = len(self.peaks)
        if n == 0:
            c.create_text(w // 2, mid_y, text="(no waveform data)", fill="#888888")
        else:
            for i, (mn, mx) in enumerate(self.peaks):
                x = int(i * w / n)
                y1 = mid_y - mx * amplitude_px
                y2 = mid_y - mn * amplitude_px
                c.create_line(x, y1, x, y2, fill="#4fc3f7")

        c.create_text(4, h - 4, text=th.format_time_ms(self.view_start), fill="#cccccc", anchor="sw")
        c.create_text(w - 4, h - 4, text=th.format_time_ms(self.view_end), fill="#cccccc", anchor="se")

        self._render_marks(x_of, w, h, visible_work, work_lanes, work_lane_count)
        self._render_tracks(x_of, w, layout)
        self._render_drag_preview(w, layout)
        self._render_playhead(x_of, layout)
        self._render_time_readout(w)

    def _render_marks(self, x_of, w, h, visible, lanes, lane_count):
        self._mark_hit_regions = {}
        lane_h = h / lane_count

        labeled = [m for m in visible if m["label"]]
        labeled_pos = {m["id"]: i for i, m in enumerate(labeled)}

        def label_width_limit(mark):
            i = labeled_pos.get(mark["id"])
            if i is None:
                return w
            if i + 1 < len(labeled):
                return x_of(labeled[i + 1]["start"])
            return w

        c = self.canvas
        for m in visible:
            is_sel = self._is_highlighted("mark", m["id"])
            color = self.SELECTION_COLOR if is_sel else ("#ff9800" if m["type"] == "range" else "#ff3b3b")
            width = 2 if is_sel else 1
            x1 = x_of(max(m["start"], self.view_start))
            tag = f"mark_{m['id']}"
            text_tag = f"marktext_{m['id']}"
            lane = lanes[m["id"]]
            y0, y1 = lane * lane_h, (lane + 1) * lane_h

            c.create_line(x1, y0, x1, y1, fill=color, width=width, tags=(tag,))
            if m["type"] == "range":
                x2 = x_of(min(m["end"], self.view_end))
                c.create_line(x2, y0, x2, y1, fill=color, width=width, tags=(tag,))
                ts_text = f"{th.format_time_ms(m['start'])} - {th.format_time_ms(m['end'])}"
            else:
                ts_text = th.format_time_ms(m["start"])

            c.create_text(x1 + 3, y0 + 2, text=ts_text, fill=color, anchor="nw",
                          font=("TkDefaultFont", 7, "bold"), tags=(tag, text_tag))
            if m["label"]:
                allotted = max(20, label_width_limit(m) - (x1 + 3))
                c.create_text(x1 + 3, y0 + 13, text=m["label"], fill="#ffffff", anchor="nw",
                              width=allotted, font=("TkDefaultFont", 7), tags=(tag, text_tag))
            self._mark_hit_regions[m["id"]] = c.bbox(text_tag)

    def _render_tracks(self, x_of, w, layout):
        c = self.canvas
        work_height = layout["work_height"]
        track_top = layout["track_top"]

        c.create_line(0, work_height, w, work_height, fill="#000000")

        self._track_label_regions = {}
        self._track_band_regions = {}

        for i, tr in enumerate(self.tracks):
            y0 = track_top + i * self.TRACK_HEIGHT
            y1 = y0 + self.TRACK_HEIGHT
            mid = (y0 + y1) // 2
            band_color = th.blend_color(tr["color"], 0.30)
            mark_color = th.blend_color(tr["color"], 0.85)
            track_selected = self._is_highlighted("track", tr["id"])
            band_outline = self.SELECTION_COLOR if track_selected else "#000000"
            c.create_rectangle(0, y0, w, y1, fill=band_color, outline=band_outline,
                               width=2 if track_selected else 1)
            self._track_band_regions[tr["id"]] = (y0, y1)

            label_tag = f"tracklabel_{tr['id']}"
            c.create_text(6, mid, text=tr["name"], fill="#ffffff", anchor="w",
                          font=("TkDefaultFont", 8, "bold"), tags=(label_tag,))
            self._track_label_regions[tr["id"]] = c.bbox(label_tag)

            track_pool = [m for m in self.marks if m.get("track_id") == tr["id"]]
            _visible_track, track_lanes, track_lane_count = self._marks_with_lanes(track_pool, w)
            lane_h = self.TRACK_HEIGHT / track_lane_count

            for m in self.marks:
                if m.get("track_id") != tr["id"]:
                    continue
                if m["type"] == "point":
                    if not (self.view_start <= m["start"] <= self.view_end):
                        continue
                elif m["end"] < self.view_start or m["start"] > self.view_end:
                    continue
                mark_selected = self._is_highlighted("mark", m["id"])
                this_mark_color = self.SELECTION_COLOR if mark_selected else mark_color
                tag = f"mark_{m['id']}"
                text_tag = f"marktext_{m['id']}"
                x1 = x_of(max(m["start"], self.view_start))
                lane = track_lanes[m["id"]]
                sub_y0, sub_y1 = y0 + lane * lane_h, y0 + (lane + 1) * lane_h
                sub_mid = (sub_y0 + sub_y1) / 2
                if m["type"] == "range":
                    x2 = x_of(min(m["end"], self.view_end))
                    outline = self.SELECTION_COLOR if mark_selected else ""
                    pad = min(4, lane_h / 4)
                    c.create_rectangle(x1, sub_y0 + pad, x2, sub_y1 - pad, fill=this_mark_color, outline=outline,
                                       width=2 if mark_selected else 1, tags=(tag,))
                    ts_text = f"{th.format_time_ms(m['start'])} - {th.format_time_ms(m['end'])}"
                else:
                    c.create_line(x1, sub_y0, x1, sub_y1, fill=this_mark_color, width=3 if mark_selected else 2,
                                  tags=(tag,))
                    ts_text = th.format_time_ms(m["start"])
                label = m["label"] or ts_text
                c.create_text(x1 + 3, sub_mid, text=label, fill="#ffffff", anchor="w",
                              font=("TkDefaultFont", 7), tags=(tag, text_tag))
                self._mark_hit_regions[m["id"]] = c.bbox(text_tag)

        blank_y0 = track_top + len(self.tracks) * self.TRACK_HEIGHT
        blank_y1 = blank_y0 + self.TRACK_HEIGHT
        c.create_rectangle(0, blank_y0, w, blank_y1, fill="", outline="#3a3a3a", dash=(3, 2))
        c.create_text(w // 2, (blank_y0 + blank_y1) // 2, text="Drop a mark here to create a new track",
                      fill="#5a5a5a", font=("TkDefaultFont", 7))

    def _render_drag_preview(self, w, layout):
        if not self._move_drag or self._move_drag.get("edge") is not None:
            return
        zone, target = self._move_drag.get("preview_zone", (None, None))
        if zone is None:
            return
        orig_track_id = self._move_drag.get("orig_track_id")
        orig_zone = ("work", None) if orig_track_id is None else ("track", orig_track_id)
        if (zone, target) == orig_zone:
            return
        c = self.canvas
        if zone == "work":
            y0, y1 = 0, layout["work_height"]
        elif zone == "track":
            idx = next((i for i, t in enumerate(self.tracks) if t["id"] == target), None)
            if idx is None:
                return
            y0 = layout["track_top"] + idx * self.TRACK_HEIGHT
            y1 = y0 + self.TRACK_HEIGHT
        else:
            y0 = layout["track_top"] + len(self.tracks) * self.TRACK_HEIGHT
            y1 = y0 + self.TRACK_HEIGHT
        c.create_rectangle(1, y0 + 1, w - 1, y1 - 1, outline=self.SELECTION_COLOR, width=2, dash=(4, 2))

    def _render_playhead(self, x_of, layout):
        pos = self.current_play_position()
        if pos is None or self.audio_duration is None:
            return
        if not (self.view_start <= pos <= self.view_end):
            return
        x = x_of(pos)
        self.canvas.create_line(x, 0, x, layout["total_h"], fill="#ff2222", width=2)

    def _render_time_readout(self, w):
        text = xy = None
        if self._move_drag and self._move_drag.get("last_xy"):
            xy = self._move_drag["last_xy"]
            mark = self._move_drag["mark"]
            edge = self._move_drag.get("edge")
            if edge == "start":
                text = f"start {th.format_time_ms(mark['start'])}"
            elif edge == "end":
                text = f"end {th.format_time_ms(mark['end'])}"
            elif mark["type"] == "point":
                text = th.format_time_ms(mark["start"])
            else:
                text = f"{th.format_time_ms(mark['start'])} - {th.format_time_ms(mark['end'])}"
        elif self._press_info and self._press_info.get("dragging") and self._press_info.get("last_xy") \
                and self._preview_range:
            xy = self._press_info["last_xy"]
            ps, pe = self._preview_range
            text = f"{th.format_time_ms(ps)} - {th.format_time_ms(pe)}"
        if text is None:
            return
        c = self.canvas
        x, y = xy
        tx, ty = min(w - 10, x + 12), max(2, y - 16)
        box_w = max(36, len(text) * 6 + 8)
        c.create_rectangle(tx - 4, ty - 2, tx + box_w, ty + 12, fill="#000000", outline=self.SELECTION_COLOR)
        c.create_text(tx, ty, text=text, fill=self.SELECTION_COLOR, anchor="nw", font=("TkDefaultFont", 7, "bold"))


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def onload(filepath: str, canvas=None, text=None, tab=None):
    p = Path(filepath)
    if p.suffix.lower() not in AUDIO_EXTS:
        return False

    descr = (
        f"{{cyan}}[waveform_tab]\n"
        f"{{blue}}Audio: {{cyan}}{p.name}\n"
        f"{{blue}}Path:  {p.resolve()}\n"
    )
    engine_name = "sounddevice (trackED's own)" if th.ACTIVE_ENGINE == "sounddevice" else "ffplay (Sequence Editor's original)"
    descr += f"{{blue}}Playback engine: {{cyan}}{engine_name}\n"
    if th.PLAYBACK_MISSING:
        descr += "\n{yellow}Missing playback packages (waveform/marks still work without them):\n"
        for pkg in th.PLAYBACK_MISSING.values():
            descr += f"{{red}}  - {pkg}  (pip install {pkg})\n"
    else:
        descr += "\n{green}All playback packages are present.\n"

    descr += (
        "\n{blue}Mouse: {cyan}click{blue}=new point mark  {cyan}drag{blue}=new range  "
        "{cyan}shift+click{blue}=extend last mark into a range\n"
        "{blue}      {cyan}drag a mark{blue}=move/resize  {cyan}drag into a track band{blue}=assign it\n"
        "{blue}      {cyan}double-click{blue}=edit label  {cyan}right-click{blue}=mark/track menu\n"
        "{blue}Keys:  {cyan}Up/Down{blue}=move selection between tracks  {cyan}Left/Right{blue}=nudge by one grid step\n"
        "{blue}       {cyan}Tab/Shift+Tab{blue}=select next/prev mark  {cyan}Delete{blue}=delete selection\n"
        "{blue}       {cyan}Ctrl+Z/Ctrl+Y{blue}=undo/redo marks (while the waveform has focus)\n"
        "{blue}       {cyan}wheel{blue}=zoom\n"
        "{blue}Export Timing (xLights/LRC/Audacity): right-click a track's label.\n"
    )

    if canvas is not None:
        canvas.delete("all")
        for seq in canvas.bind():
            canvas.unbind(seq)
        # Stop/release any controller a previous onload() call on this same
        # canvas left running, before replacing it.
        old = getattr(canvas, "_waveform_controller", None)
        if old is not None:
            try:
                old.stop_and_release()
            except Exception:
                pass
        try:
            if tab is not None and hasattr(tab, "paned"):
                tab.paned.sashpos(0, 220)
        except Exception:
            pass
        # Keep a reference so it isn't garbage-collected.
        canvas._waveform_controller = WaveformController(canvas, text, str(p.resolve()), tab=tab)

    if text is not None:
        text.delete("1.0", "end")
        insert_styled_text(text, descr)

    return True

# eof
