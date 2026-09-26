"""
repl-todo.py -- the Sequence Editor's onload() *scripting* feature, excluded
from this port, kept here in case it's reactivated later.

IMPORTANT NAMING NOTE: this is a different, unrelated thing from trackED's own
plugin-discovery onload(filepath, canvas, text, tab) that waveform_tab.py
implements. That one is a *developer* extension point -- a *_tab.py module
beside the app, tried in turn until one claims a file. THIS onload() is a
*user*-authored Python script, saved per audio file, that runs automatically
whenever the file loads and can programmatically create timing marks/tracks
-- more like a very small, purpose-built REPL/macro layer on top of the
marks/tracks data model waveform_tab.py already provides. The two happen to
share a name by coincidence of two projects independently choosing it; they
are not related, and this module does not use trackED's plugin discovery at
all (it doesn't match *_tab.py, on purpose, so it's never auto-discovered).

Not wired into anything: nothing in waveform_tab.py imports this module.
Reactivating it is intended to look roughly like:

    from repl_todo import OnloadReplMixin   # after renaming this file
                                             # (see note at the bottom)

    class WaveformController(OnloadReplMixin, ...):
        ...

then wiring a Run button/Ctrl+Enter (see _on_ctrl_enter/_run_onload_clicked
below, both still present but unused) and calling run_onload_code() off the
main thread the same way seqed.py's App.run_onload originally did -- see the
"App-level orchestration" note near the bottom of this file for what that
threading/polling glue looked like, since it lived one level up (in the
Sequence Editor's App class, not the tab) and has no equivalent here yet.

What onload() scripts could do: read file_path/audio.duration/audio.peaks
and the existing marks/tracks (read-only), then call add_mark(start,
end=None, label="", track=None), add_track(name, color=None), and
delete_track(name) to build up new ones -- or return a
{"marks": [...], "tracks": [...]} dict instead/as well. Runs off the main
thread; only main-thread code (apply_onload_result) touches live tab state
or Tk widgets afterward.

NOTE;
This file is not wired up anywhere yet.  It's currently just holding deactivated code.
"""

from __future__ import annotations

import hashlib
import io
import json
import keyword
import os
import re
import textwrap
import tokenize
import traceback
import types

import tkinter as tk


# ---------------------------------------------------------------------------
# Per-file sidecar persistence for the onload script + its run-cache marker.
# Self-contained here (not in timing_helpers.py) since nothing else needs
# these while this feature is inactive.
# ---------------------------------------------------------------------------

def _onload_code_path(media_path):
    """A plain .py file (not JSON) so it can be opened and edited directly
    with any external text/code editor, not just this app."""
    directory = os.path.dirname(media_path) or "."
    base = os.path.basename(media_path)
    if os.access(directory, os.W_OK):
        return os.path.join(directory, f"{base}-onload.py")
    fallback = os.path.join(os.path.expanduser("~"), ".waveform_tab_cache")
    os.makedirs(fallback, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.abspath(media_path))
    return os.path.join(fallback, f"{safe}-onload.py")


def _onload_cache_path(media_path):
    directory = os.path.dirname(media_path) or "."
    base = os.path.basename(media_path)
    if os.access(directory, os.W_OK):
        return os.path.join(directory, f"{base}-onload-cache.json")
    fallback = os.path.join(os.path.expanduser("~"), ".waveform_tab_cache")
    os.makedirs(fallback, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.abspath(media_path))
    return os.path.join(fallback, f"{safe}-onload-cache.json")


def load_onload_code(media_path):
    path = _onload_code_path(media_path)
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def save_onload_code(media_path, code_text):
    path = _onload_code_path(media_path)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(code_text)
    except Exception:
        pass


def load_onload_cache(media_path):
    path = _onload_cache_path(media_path)
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_onload_cache(media_path, cache):
    path = _onload_cache_path(media_path)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


def generate_onload_starter(path, tags_description):
    """Build the initial onload() code for a media file that has no saved
    script yet: `tags_description` (a multi-line string, e.g. metadata
    already formatted for display) becomes comments, followed by a worked
    example that marks the first 10 seconds if nothing already covers it."""
    comment_lines = [("# " + line).rstrip() for line in tags_description.splitlines()]
    name = os.path.basename(path)
    example = (
        "\n"
        "# This code runs automatically whenever the file is loaded or reloaded,\n"
        "# and can be re-run anytime with the Run button (or Ctrl+Enter) without\n"
        "# reloading the file. It has access to:\n"
        "#   file_path       -- this file's path\n"
        "#   audio.duration  -- length in seconds\n"
        "#   audio.peaks     -- decoded (min, max) waveform overview, one pair per bucket\n"
        "#   marks, tracks   -- existing timing marks/tracks (read-only lists of dicts)\n"
        "# ...and new marks/tracks can be added with:\n"
        "#   add_mark(start, end=None, label=\"\", track=None)  -- end=None makes a point\n"
        "#   add_track(name, color=None)                        -- then add_mark(..., track=name)\n"
        "#   delete_track(name)                                 -- deletes it and its marks, no prompt\n"
        "# A dict of {\"marks\": [...], \"tracks\": [...]} can be returned instead/as well.\n"
        "\n"
        "# Example: mark the first 10 seconds, if nothing already covers it.\n"
        "already_marked = any(m[\"start\"] < 10.0 for m in marks)\n"
        "if not already_marked and audio.duration:\n"
        f"    add_mark(0.0, min(10.0, audio.duration), label=\"{name} - intro\")\n"
    )
    return "\n".join(comment_lines) + "\n" + example


# ---------------------------------------------------------------------------
# The sandboxed execution itself
# ---------------------------------------------------------------------------

class OnloadError(Exception):
    """Raised by run_onload_code when the user's onload() code fails to
    compile or raises -- carries the 1-based line number within the
    user's own code (not the internal wrapper) where it happened, if it
    could be determined, so the caller can highlight it."""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


def run_onload_code(code_text, file_path, audio_ns, marks_snapshot, tracks_snapshot):
    """Runs entirely off the main thread -- must not touch any Tk widget
    or live tab state. Wraps `code_text` as the body of onload(file_path,
    audio, marks, tracks, add_mark, add_track, delete_track, log) and
    executes it in a fresh namespace. New marks/tracks can be queued
    either by calling add_mark()/add_track() during execution, or by
    returning a {"marks": [...], "tracks": [...]} dict (or both -- they're
    merged). delete_track(name) removes a track (and its marks)
    programmatically, without any confirmation prompt (there's no UI to
    prompt in a background thread). Returns {"marks", "tracks",
    "tracks_to_delete", "log"}; raises OnloadError (wrapping the original
    exception, with a line number if one could be determined) on any
    compile/runtime error, for the caller to catch and report."""
    pending_tracks = []  # [{"name", "color"}]
    pending_marks = []   # [{"start", "end", "label", "track"}]
    tracks_to_delete = set()
    known_track_names = {t["name"] for t in tracks_snapshot}
    log_lines = []

    def add_track(name, color=None):
        entry = {"name": name, "color": color}
        pending_tracks.append(entry)
        known_track_names.add(name)
        return dict(entry)

    def add_mark(start, end=None, label="", track=None):
        if track is not None and track not in known_track_names:
            raise ValueError(f"add_mark: no track named {track!r} -- call add_track({track!r}) first")
        entry = {
            "start": float(start), "end": (float(end) if end is not None else None),
            "label": label, "track": track,
        }
        pending_marks.append(entry)
        return dict(entry)

    def delete_track(name):
        nonlocal pending_tracks, pending_marks
        pending_tracks = [t for t in pending_tracks if t["name"] != name]
        pending_marks = [m for m in pending_marks if m.get("track") != name]
        known_track_names.discard(name)
        tracks_to_delete.add(name)

    def log(*args):
        for a in args:
            log_lines.append(str(a))

    namespace = {
        "file_path": file_path, "audio": audio_ns,
        "marks": marks_snapshot, "tracks": tracks_snapshot,
        "add_mark": add_mark, "add_track": add_track, "delete_track": delete_track, "log": log,
    }
    wrapped = (
        "def onload(file_path, audio, marks, tracks, add_mark, add_track, delete_track, log):\n"
        + textwrap.indent(code_text, "    ")
        + "\n\n__onload_result__ = onload("
        + "file_path, audio, marks, tracks, add_mark, add_track, delete_track, log)\n"
    )
    try:
        code_obj = compile(wrapped, "<onload>", "exec")
    except SyntaxError as e:
        line = max(1, (e.lineno or 2) - 1)
        raise OnloadError(f"SyntaxError: {e.msg}", line=line) from e
    try:
        exec(code_obj, namespace)
    except Exception as e:
        line = None
        for frame in traceback.extract_tb(e.__traceback__):
            if frame.filename == "<onload>":
                line = frame.lineno
        if line is not None:
            line = max(1, line - 1)
        raise OnloadError(f"{type(e).__name__}: {e}", line=line) from e

    returned = namespace.get("__onload_result__")
    if isinstance(returned, dict):
        for t in returned.get("tracks", []) or []:
            pending_tracks.append({"name": t.get("name"), "color": t.get("color")})
            known_track_names.add(t.get("name"))
        for m in returned.get("marks", []) or []:
            pending_marks.append({
                "start": float(m["start"]), "end": (float(m["end"]) if m.get("end") is not None else None),
                "label": m.get("label", ""), "track": m.get("track"),
            })
    return {
        "marks": pending_marks, "tracks": pending_tracks,
        "tracks_to_delete": sorted(tracks_to_delete), "log": log_lines,
    }


# ---------------------------------------------------------------------------
# OnloadReplMixin -- ready to mix into WaveformController if reactivated.
# Expects the host class to provide: self.filepath, self.text, self.marks,
# self.tracks, self.audio_duration, self.full_peaks, self._new_track(),
# self._add_mark(), self._mark_changed(), self.render_waveform(),
# self.ONLOAD_API_NAMES, self.PYTHON_COMMON_BUILTINS (the latter two are
# just tuples of names -- see the bottom of this file for their values).
# ---------------------------------------------------------------------------

class OnloadReplMixin:
    def onload_cache_is_valid(self, code_text):
        """True if the audio file's mtime/size AND the onload script
        file's own mtime/size/hash all match what was recorded after
        onload() last successfully ran."""
        if not self.filepath or not os.path.exists(self.filepath):
            return False
        try:
            audio_mtime = os.path.getmtime(self.filepath)
            audio_size = os.path.getsize(self.filepath)
        except OSError:
            return False
        code_path = _onload_code_path(self.filepath)
        try:
            code_mtime = os.path.getmtime(code_path)
            code_size = os.path.getsize(code_path)
        except OSError:
            code_mtime = code_size = None
        cache = getattr(self, "_onload_cache", {})
        return (
            cache.get("audio_mtime") == audio_mtime
            and cache.get("audio_size") == audio_size
            and cache.get("code_mtime") == code_mtime
            and cache.get("code_size") == code_size
            and cache.get("code_hash") == self._code_hash(code_text)
        )

    def _record_onload_cache_marker(self, code_text):
        try:
            audio_mtime = os.path.getmtime(self.filepath)
            audio_size = os.path.getsize(self.filepath)
        except OSError:
            audio_mtime = audio_size = None
        code_path = _onload_code_path(self.filepath)
        try:
            code_mtime = os.path.getmtime(code_path)
            code_size = os.path.getsize(code_path)
        except OSError:
            code_mtime = code_size = None
        self._onload_cache = {
            "audio_mtime": audio_mtime, "audio_size": audio_size,
            "code_mtime": code_mtime, "code_size": code_size,
            "code_hash": self._code_hash(code_text),
        }

    @staticmethod
    def _code_hash(code_text):
        return hashlib.sha256(code_text.encode("utf-8")).hexdigest()

    def build_onload_namespace(self):
        """Snapshot everything a background execution needs, up front, on
        the main thread -- so run_onload_code never has to touch self.*
        tab state or any Tk widget. Only "user"-sourced marks/tracks are
        exposed (stale output from a previous onload() run is about to be
        replaced, not something a fresh run should see as "existing")."""
        marks_snapshot = [dict(m) for m in self.marks if m.get("source", "user") != "onload"]
        tracks_snapshot = [dict(t) for t in self.tracks if t.get("source", "user") != "onload"]
        audio_ns = types.SimpleNamespace(duration=self.audio_duration, peaks=list(self.full_peaks or []))
        return self.filepath, audio_ns, marks_snapshot, tracks_snapshot

    def apply_onload_result(self, outcome, error, code_text, error_line=None):
        """Main-thread only. Clears out the previous run's onload-sourced
        marks/tracks first, so re-running replaces rather than
        accumulates them; anything the user added by hand is untouched."""
        self._clear_error_highlight()
        if error is not None:
            self.run_status_text = f"onload() error: {error}"
            if error_line:
                self.run_status_text += f" (line {error_line})"
                self._highlight_error_line(error_line)
            return
        self.marks = [m for m in self.marks if m.get("source", "user") != "onload"]
        self.tracks = [t for t in self.tracks if t.get("source", "user") != "onload"]
        for name in outcome.get("tracks_to_delete", []):
            existing = next((t for t in self.tracks if t["name"] == name), None)
            if existing is not None:
                self.delete_track(existing, record_history=False)
        name_to_id = {t["name"]: t["id"] for t in self.tracks}
        for t in outcome["tracks"]:
            track = self._new_track(t["name"], t.get("color"), source="onload", record_history=False)
            name_to_id[t["name"]] = track["id"]
        for m in outcome["marks"]:
            track_id = name_to_id.get(m["track"]) if m.get("track") else None
            mark_type = "range" if m.get("end") is not None else "point"
            self._add_mark(
                mark_type, m["start"], m.get("end"), label=m.get("label", ""),
                track_id=track_id, source="onload", record_history=False,
            )
        self._record_onload_cache_marker(code_text)
        if self.filepath:
            save_onload_cache(self.filepath, self._onload_cache)
        self._onload_stale = False
        self._update_run_button_style()
        self._mark_changed()
        self.render_waveform()
        n_marks, n_tracks = len(outcome["marks"]), len(outcome["tracks"])
        n_deleted = len(outcome.get("tracks_to_delete", []))
        status = f"onload(): added {n_marks} mark(s), {n_tracks} track(s)"
        if n_deleted:
            status += f", deleted {n_deleted} track(s)"
        if outcome["log"]:
            status += "  |  " + "  ".join(outcome["log"])
        self.run_status_text = status

    def _highlight_error_line(self, line):
        self._clear_error_highlight()
        if not line:
            return
        try:
            self.text.tag_add("onload_error", f"{line}.0", f"{line + 1}.0")
            self.text.see(f"{line}.0")
        except Exception:
            pass

    def _clear_error_highlight(self):
        self.text.tag_remove("onload_error", "1.0", "end")

    # -- syntax highlighting + autocomplete for the onload() code editor --

    def _on_text_key_release(self, event=None):
        self._highlight_python()
        self._update_autocomplete()

    def _highlight_python(self):
        """Re-apply Python syntax-highlight tags to the whole buffer,
        using the standard tokenizer (rather than ad-hoc regex) so
        multi-line strings, escaped quotes, etc. highlight correctly."""
        for tag in ("py_keyword", "py_string", "py_comment", "py_number", "py_api"):
            self.text.tag_remove(tag, "1.0", "end")
        source = self.text.get("1.0", "end-1c")
        try:
            for tok_type, tok_str, start, end, _line in tokenize.generate_tokens(io.StringIO(source).readline):
                if tok_type == tokenize.COMMENT:
                    tag = "py_comment"
                elif tok_type == tokenize.STRING:
                    tag = "py_string"
                elif tok_type == tokenize.NUMBER:
                    tag = "py_number"
                elif tok_type == tokenize.NAME and keyword.iskeyword(tok_str):
                    tag = "py_keyword"
                elif tok_type == tokenize.NAME and tok_str in self.ONLOAD_API_NAMES:
                    tag = "py_api"
                else:
                    continue
                self.text.tag_add(tag, f"{start[0]}.{start[1]}", f"{end[0]}.{end[1]}")
        except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
            pass
        self.text.tag_raise("sel")

    def _autocomplete_candidates(self, prefix):
        if not prefix:
            return []
        pool = set(keyword.kwlist) | set(self.PYTHON_COMMON_BUILTINS) | set(self.ONLOAD_API_NAMES)
        pool |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", self.text.get("1.0", "end-1c")))
        return sorted(w for w in pool if w.startswith(prefix) and w != prefix)

    def _current_word_prefix(self):
        line_text = self.text.get("insert linestart", "insert")
        m = re.search(r"[A-Za-z_][A-Za-z0-9_]*$", line_text)
        return m.group(0) if m else ""

    def _update_autocomplete(self):
        prefix = self._current_word_prefix()
        if len(prefix) < 2:
            self._hide_autocomplete_popup()
            return
        candidates = self._autocomplete_candidates(prefix)
        if not candidates:
            self._hide_autocomplete_popup()
            return
        self._show_autocomplete_popup(candidates, prefix)

    def _on_autocomplete_key(self, event):
        prefix = self._current_word_prefix()
        candidates = self._autocomplete_candidates(prefix)
        if candidates:
            self._show_autocomplete_popup(candidates, prefix)
        return "break"

    def _on_text_navkey(self, event):
        if getattr(self, "_autocomplete_popup", None) is None:
            return None
        if event.keysym == "Down":
            self._move_autocomplete_selection(1)
            return "break"
        if event.keysym == "Up":
            self._move_autocomplete_selection(-1)
            return "break"
        if event.keysym in ("Return", "Tab"):
            self._accept_autocomplete_selection()
            return "break"
        if event.keysym == "Escape":
            self._hide_autocomplete_popup()
            return "break"
        return None

    def _move_autocomplete_selection(self, direction):
        listbox = self._autocomplete_listbox
        size = listbox.size()
        if size == 0:
            return
        current = listbox.curselection()
        idx = (current[0] if current else -1) + direction
        idx %= size
        listbox.selection_clear(0, "end")
        listbox.selection_set(idx)
        listbox.see(idx)

    def _accept_autocomplete_selection(self, index=None):
        listbox = self._autocomplete_listbox
        if index is None:
            current = listbox.curselection()
            index = current[0] if current else 0
        if listbox.size() == 0:
            self._hide_autocomplete_popup()
            return
        word = listbox.get(index)
        prefix = self._autocomplete_prefix
        if prefix:
            self.text.delete(f"insert -{len(prefix)}c", "insert")
        self.text.insert("insert", word)
        self._hide_autocomplete_popup()
        self._highlight_python()

    def _show_autocomplete_popup(self, candidates, prefix):
        self._hide_autocomplete_popup()
        try:
            bbox = self.text.bbox("insert")
        except Exception:
            bbox = None
        x, y, _bw, bh = bbox if bbox else (0, 0, 0, 16)
        popup = tk.Toplevel(self.text)
        popup.wm_overrideredirect(True)
        popup.wm_geometry(f"+{self.text.winfo_rootx() + x}+{self.text.winfo_rooty() + y + bh}")
        listbox = tk.Listbox(popup, height=min(8, len(candidates)))
        for word in candidates:
            listbox.insert("end", word)
        listbox.selection_set(0)
        listbox.pack()
        listbox.bind("<Button-1>", lambda e: self._accept_autocomplete_selection(listbox.nearest(e.y)))
        self._autocomplete_popup = popup
        self._autocomplete_listbox = listbox
        self._autocomplete_prefix = prefix

    def _hide_autocomplete_popup(self):
        popup = getattr(self, "_autocomplete_popup", None)
        if popup is not None:
            popup.destroy()
            self._autocomplete_popup = None
            self._autocomplete_listbox = None

    def _on_ctrl_enter(self, event):
        self._run_onload_clicked()
        return "break"

    def _run_onload_clicked(self):
        """Reactivating this needs an App-level (or WaveformController-
        level) run_onload(tab, reason) orchestrating a background thread
        + polling loop, roughly:

            def run_onload(self, reason):
                if self.onload_cache_is_valid(self.text.get("1.0", "end-1c")) and reason == "initial":
                    return
                code_text = self.text.get("1.0", "end-1c")
                file_path, audio_ns, marks_snapshot, tracks_snapshot = self.build_onload_namespace()
                result = {}

                def worker():
                    try:
                        result["outcome"] = run_onload_code(code_text, file_path, audio_ns,
                                                              marks_snapshot, tracks_snapshot)
                    except OnloadError as e:
                        result["error"] = str(e)
                        result["error_line"] = e.line

                thread = threading.Thread(target=worker, daemon=True)
                thread.start()

                def poll():
                    if thread.is_alive():
                        self.canvas.after(80, poll)
                        return
                    self.apply_onload_result(result.get("outcome"), result.get("error"),
                                              code_text, error_line=result.get("error_line"))

                self.canvas.after(80, poll)

        This lived in the Sequence Editor's App class (seqed.py), not the
        tab, since it also handled the "run automatically on file open"
        and "run again on File > Reload" cases -- neither of which have
        an equivalent in trackED yet either."""
        pass  # not wired -- see the docstring above


ONLOAD_API_NAMES = ("file_path", "audio", "marks", "tracks", "add_mark", "add_track", "delete_track", "log")
PYTHON_COMMON_BUILTINS = (
    "len", "range", "min", "max", "sum", "float", "int", "str", "list", "dict", "set", "tuple",
    "enumerate", "sorted", "reversed", "zip", "print", "abs", "round", "any", "all", "isinstance",
)

# Note on the filename: this is deliberately named repl-todo.py (with a
# hyphen) rather than repl_todo.py so it can NOT be imported as a normal
# Python module by that name (hyphens aren't valid in identifiers) and
# does NOT match utils.py's *_tab.py plugin-discovery glob -- both on
# purpose, so it stays completely inert until someone deliberately
# rename/imports it (e.g. via importlib, or a plain `mv` to
# onload_repl.py) as part of reactivating this feature.

# eof