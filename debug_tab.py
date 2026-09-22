"""
Dedicated debug-log tab and logging helpers.
Kept separate so future changes to editor_tab.py cannot break the debug view.
"""

from __future__ import annotations

import datetime
import inspect
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import List, Optional, Callable

from utils import CONFIG_DIR, ensure_config_dir, available_memory_str

# Global debug level (0 = off). Set from main via set_debug_level().
_debug_level: int = 0
_debug_tab: Optional["DebugTab"] = None
_log_lines: List[str] = []
MAX_LOG_LINES = 5000          # rolling window
LOG_FILE_NAME = "debug.log"


def set_debug_level(level: int) -> None:
    global _debug_level
    _debug_level = max(0, min(99, int(level)))


def get_debug_level() -> int:
    return _debug_level


def set_debug_tab(tab: Optional["DebugTab"]) -> None:
    global _debug_tab
    _debug_tab = tab


def debug(level: int, msg: str) -> None:
    """
    Log if level <= current debug level. Includes caller file:line.
    Add a timestamped entry to the debug log if level <= current debug level.
    level 1 = high-level, 99 = very low-level detail.
    """
    if _debug_level <= 0 or level > _debug_level:
        return

    # Caller: skip this frame (debug itself)
    try:
        frame = inspect.currentframe()
        outer = frame.f_back if frame else None
        if outer:
            fname = Path(outer.f_code.co_filename).name
            lineno = outer.f_lineno
            caller = f"{fname}:{lineno}"
        else:
            caller = "?:?"
    except Exception:
        caller = "?:?"
    finally:
        del frame  # avoid reference cycles

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts} L{level:02d}] {msg}  @{caller}"
    _log_lines.append(line)
    if len(_log_lines) > MAX_LOG_LINES:
        del _log_lines[: len(_log_lines) - MAX_LOG_LINES]

    # Append to rolling file
    try:
        ensure_config_dir()
        log_path = CONFIG_DIR / LOG_FILE_NAME
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        # simple size limit (~2 MB)
        if log_path.stat().st_size > 2_000_000:
            # keep last half
            data = log_path.read_text(encoding="utf-8").splitlines()
            log_path.write_text("\n".join(data[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass

    if _debug_tab is not None:
        _debug_tab.append_line(line)


class DebugTab:
    """Read-only-ish text tab that shows the rolling debug log."""

    def __init__(
        self,
        notebook: ttk.Notebook,
        on_close_request: Optional[Callable[["DebugTab"], None]] = None,
    ):
        self.notebook = notebook
        self.on_close_request = on_close_request
        self.filepath = None          # special tab – never saved as a file
        self.dirty = False

        self.frame = ttk.Frame(notebook)
        self.text = tk.Text(
            self.frame,
            wrap="none",
            state="normal",           # we need selection, so keep normal
            font=("Consolas", 10) if tk.TkVersion >= 8.6 else ("Courier", 10),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#ffffff",
        )
        self.vsb = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.hsb = ttk.Scrollbar(self.frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)

        # Make it mostly read-only: block ordinary key presses
        self.text.bind("<Key>", self._block_keys)
        self.text.bind("<<Selection>>", self._on_selection_change)

        self._has_selection = False
        self._saved_sel: Optional[tuple] = None   # (start, end) when leaving tab

        # Add as the LAST tab
        notebook.add(self.frame, text="Debug Log")
#        self.notebook.tab(self.frame, text="Debug Log")
        try:
            notebook.insert("end", self.frame)
        except tk.TclError:
            pass

        # Populate with anything already logged
        if _log_lines:
            self.text.insert("1.0", "\n".join(_log_lines) + "\n")
            self._scroll_to_end_if_allowed()

        set_debug_tab(self)
        debug(1, "Debug tab created")
        debug(1, f"Available memory: {available_memory_str()}")

    def _block_keys(self, event) -> Optional[str]:
        # Allow Ctrl-C / Ctrl-A / navigation, block everything else
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
            return None  # type: ignore
        if event.keysym in (
            "Left", "Right", "Up", "Down", "Home", "End",
            "Prior", "Next", "Shift_L", "Shift_R", "Control_L", "Control_R",
        ):
            return None  # type: ignore
        return "break"

    def _on_selection_change(self, event=None) -> None:
        try:
            self.text.index("sel.first")
            self._has_selection = True
        except tk.TclError:
            self._has_selection = False

    def append_line(self, line: str) -> None:
        self.text.insert("end", line + "\n")
        # Trim if the widget itself grows too large
        line_count = int(self.text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES + 100:
            self.text.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self._scroll_to_end_if_allowed()

    def _scroll_to_end_if_allowed(self) -> None:
        if not self._has_selection:
            self.text.see("end")

    def save_selection_state(self) -> None:
        """Called when the user leaves this tab."""
        try:
            start = self.text.index("sel.first")
            end = self.text.index("sel.last")
            self._saved_sel = (start, end)
            self._has_selection = True
        except tk.TclError:
            self._saved_sel = None
            self._has_selection = False

    def restore_selection_state(self) -> None:
        """Called when the user returns to this tab."""
        if self._saved_sel:
            try:
                self.text.tag_remove("sel", "1.0", "end")
                self.text.tag_add("sel", self._saved_sel[0], self._saved_sel[1])
                self.text.mark_set("insert", self._saved_sel[1])
                self.text.see(self._saved_sel[0])
                self._has_selection = True
            except tk.TclError:
                self._saved_sel = None
                self._has_selection = False
        else:
            self._scroll_to_end_if_allowed()

    def focus(self) -> None:
        self.text.focus_set()

    def destroy(self) -> None:
        set_debug_tab(None)
        try:
            self.notebook.forget(self.frame)
        except tk.TclError:
            pass
        self.frame.destroy()

#eof
