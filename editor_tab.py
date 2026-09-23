"""Renders the content of one file in a notebook tab.
Top: graphical panel (Canvas) – starts small
Bottom: text editor with line numbers
Horizontal sash is user-draggable; position remembered per file.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Callable, Optional

from utils import (
    abbreviated_name, get_sash_pos, set_sash_pos,
    get_cursor_pos, set_cursor_pos, DEFAULT_SASH_POS,
    is_probably_text_file, file_meta_summary, run_onload_plugins,
)
from debug_tab import debug
#def debug(level: int, *args, **kwargs):
#    msg = ""
#    for i, arg in enumerate(args):
#        msg += str(arg) + " "
#    print(msg)
#    return args[-1]  #for inlining last arg

class EditorTab:
    """One tab containing a vertical PanedWindow:
       - upper pane  = graphical panel (Canvas)
       - lower pane  = text area with scrollbars
    """

    def __init__(
        self,
        notebook: ttk.Notebook,
        filepath: Optional[str] = None,
        on_modified: Optional[Callable[["EditorTab"], None]] = None,
        on_close_request: Optional[Callable[["EditorTab"], None]] = None,
    ):
        self.notebook = notebook
        self.filepath = filepath
        self.on_modified = on_modified
        self.on_close_request = on_close_request
        self.dirty = False
        self._loading = False  # suppress dirty flag while loading file
        self._sash_ready = False

        # outer Frame that becomes the notebook page
        self.frame = ttk.Frame(notebook)

        # Vertical paned window
        self.paned = ttk.Panedwindow(self.frame, orient=tk.VERTICAL)
        self.paned.pack(fill="both", expand=True)

        # ----- Upper graphical panel (small by default) -----
        self.top_frame = ttk.Frame(self.paned)
        self.canvas = tk.Canvas(
            self.top_frame,
            bg="#f0f0f0",
            highlightthickness=0,
            height=DEFAULT_SASH_POS,
        )
        self.canvas.pack(fill="both", expand=True)

        # Simple placeholder graphics (can be replaced later)
        self.canvas.create_text(
            8, 6,
            anchor="nw",
            text="Graphical panel",
            fill="#888888",
            font=("Segoe UI", 9),
        )
        # Draw a light grid so it is obvious this is a canvas
        def _draw_grid(event=None):
            self.canvas.delete("grid")
            w = max(self.canvas.winfo_width(), 1)
            h = max(self.canvas.winfo_height(), 1)
            for x in range(0, w, 16):
                self.canvas.create_line(x, 0, x, h, fill="#e8e8e8", tags="grid")
            for y in range(0, h, 16):
                self.canvas.create_line(0, y, w, y, fill="#e8e8e8", tags="grid")
        self.canvas.bind("<Configure>", _draw_grid)
        self.paned.add(self.top_frame, weight=0)

        # ----- Lower: line numbers + text -----
        self.bottom_frame = ttk.Frame(self.paned)

        self.linenumbers = tk.Text(
            self.bottom_frame,
            width=4,
            padx=4,
            takefocus=0,
            border=0,
            highlightthickness=0,
            state="disabled",
            wrap="none",
            font=("Consolas", 11) if tk.TkVersion >= 8.6 else ("Courier", 11),
            bg="#f5f5f5",
            fg="#666666",
        )
        self.text = tk.Text(
            self.bottom_frame,
            wrap="none",
            undo=True,
            maxundo=-1,
            autoseparators=True,
            font=("Consolas", 11) if tk.TkVersion >= 8.6 else ("Courier", 11),
        )
        self.vsb = ttk.Scrollbar(self.bottom_frame, orient="vertical", command=self._on_scroll)
        self.hsb = ttk.Scrollbar(self.bottom_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self._on_text_yscroll, xscrollcommand=self.hsb.set)
        self.linenumbers.configure(yscrollcommand=lambda *a: None)

        self.linenumbers.grid(row=0, column=0, sticky="ns")
        self.text.grid(row=0, column=1, sticky="nsew")
        self.vsb.grid(row=0, column=2, sticky="ns")
        self.hsb.grid(row=1, column=1, sticky="ew")
        self.bottom_frame.rowconfigure(0, weight=1)
        self.bottom_frame.columnconfigure(1, weight=1)

        self.paned.add(self.bottom_frame, weight=1)

        # Restore sash position after the widget is mapped
        self.frame.after(30, self._restore_sash)
        # Remember sash whenever the user finishes dragging it
        self.paned.bind("<ButtonRelease-1>", self._on_sash_released)
        # Custom tab label: [name *] [red X]
        # Custom tab label area (name + dirty marker; close via menu / Ctrl+W / middle-click)
        # Custom tab label helpers (kept for compatibility)
        self.tab_frame = ttk.Frame(notebook)
        self.label = ttk.Label(self.tab_frame, text=self._label_text())
        self.label.pack(side="left", padx=(4, 2))

#        self.close_btn = tk.Label(
#            self.tab_frame,
#            text="✕",
#            fg="#c0392b",
#            cursor="hand2",
#            font=("", 9, "bold"),
#        )
#        self.close_btn.pack(side="left", padx=(0, 4))
#        self.close_btn.bind("<Button-1>", self._request_close)
#        # Also allow middle-click on the tab label area to close
#        self.tab_frame.bind("<Button-2>", self._request_close)
#        self.label.bind("<Button-2>", self._request_close)

#        notebook.add(self.frame, text="")  # text is managed by our custom label
#        notebook.tab(self.frame, text="")  # keep empty; we draw our own
        notebook.add(self.frame, text=self._label_text())

        # After the tab is added we can attach the custom label
        # (Tk requires the tab to exist first)
        self._attach_custom_tab()

        self.text.bind("<<Modified>>", self._on_text_modified)
#        self.text.bind("<Control-s>", lambda e: None)  # handled by main
        self.text.bind("<KeyRelease>", lambda e: self._update_line_numbers())
        self.text.bind("<ButtonRelease-1>", lambda e: self._update_line_numbers())
        self.text.bind("<MouseWheel>", lambda e: self.frame.after_idle(self._update_line_numbers))

        # Register with notebook (close button comes from CustomNotebook style)
        notebook.add(self.frame, text=self._label_text())

        if filepath:
            self.load_file(filepath)
        else:
            self._update_line_numbers()
            debug(2, "Created new untitled tab")

    def _on_scroll(self, *args):
        self.text.yview(*args)
        self.linenumbers.yview(*args)

    def _on_text_yscroll(self, first, last):
        self.vsb.set(first, last)
        self.linenumbers.yview_moveto(first)

    def _update_line_numbers(self, event=None) -> None:
        self.linenumbers.configure(state="normal")
        self.linenumbers.delete("1.0", "end")
        try:
            end_line = int(self.text.index("end-1c").split(".")[0])
        except Exception:
            end_line = 1
        # width based on digits
        width = max(3, len(str(end_line)))
        self.linenumbers.configure(width=width)
        lines = "\n".join(str(i) for i in range(1, end_line + 1))
        self.linenumbers.insert("1.0", lines)
        self.linenumbers.configure(state="disabled")
        # sync scroll
        try:
            self.linenumbers.yview_moveto(self.text.yview()[0])
        except Exception:
            pass

    # ------------------------------------------------------------------ Sash / cursor (unchanged logic)
    def _restore_sash(self) -> None:
        try:
            pos = get_sash_pos(self.filepath)
            # sashpos expects an absolute pixel value from the top of the paned window
            self.paned.sashpos(0, pos)
            self._sash_ready = True
            debug(3, f"Restored sash for {self.filepath or 'Untitled'} → {pos}px")
        except tk.TclError:
            pass

    def _on_sash_released(self, event=None) -> None:
        if not self._sash_ready:
            return
        try:
            pos = self.paned.sashpos(0)
            set_sash_pos(self.filepath, pos)
            debug(3, f"Saved sash {pos}px for {self.filepath or 'Untitled'}")
        except tk.TclError:
            pass

    def save_current_sash(self) -> None:
        """Call before the tab is destroyed so the latest position is stored."""
        self._on_sash_released()

    # ------------------------------------------------------------------ Cursor / view
    def save_cursor_state(self) -> None:
        if not self.filepath:
            return
        try:
            index = self.text.index("insert")
            yview = self.text.yview()[0]
            set_cursor_pos(self.filepath, index, yview)
            debug(4, f"Saved cursor {index} yview={yview:.3f} for {self.filepath}")
        except tk.TclError:
            pass

    def restore_cursor_state(self) -> None:
        if not self.filepath:
            return
        info = get_cursor_pos(self.filepath)
        try:
            self.text.mark_set("insert", info.get("index", "1.0"))
            self.text.see("insert")
            self.text.yview_moveto(float(info.get("yview", 0.0)))
            debug(4, f"Restored cursor for {self.filepath}")
        except (tk.TclError, ValueError):
            pass

    # ------------------------------------------------------------------ Tab label
    def _attach_custom_tab(self) -> None:
        """Replace the default tab text with our Frame containing label + close button."""
        try:
#?            self.notebook.tab(self.frame, compound="left")
            # The only reliable way in pure Tk is to set the tab text to empty
            # and use a separate label that we position... but Notebook does not
            # easily support arbitrary widgets as tab labels in all versions.
            # Workaround used by many pure-Tk editors: keep a short text and
            # put the close button next to it via a style, OR simply use the
            # text and a close accelerator.  For a clean "red X on the tab"
            # we keep the custom frame approach that works on both Linux/Windows
            # with recent Tk.
            self.notebook.tab(self.frame, text=self._label_text())
            # Store reference so we can update later
        except tk.TclError:
            pass

    def _label_text(self) -> str:
        name = abbreviated_name(self.filepath)
        return f"{'*' if self.dirty else ''}{name}"

    def update_tab_label(self) -> None:
        try:
            self.notebook.tab(self.frame, text=self._label_text())
#            self.label.configure(text=self._label_text())
        except tk.TclError:
            pass

    def _on_text_modified(self, event=None) -> None:
        if self._loading:
            self.text.edit_modified(False)
            return
        if self.text.edit_modified():
            if not self.dirty:
                self.dirty = True
                self.update_tab_label()
                if self.on_modified:
                    self.on_modified(self)
            self.text.edit_modified(False)
            self._update_line_numbers()

    def _request_close(self) -> None:
        if self.on_close_request:
            self.on_close_request(self)

    # ------------------------------------------------------------------ File I/O with plugins
    def load_file(self, path: str) -> bool:
        path = str(Path(path).resolve())
        self.filepath = path
        self._loading = True
        self.text.delete("1.0", "end")

        # 1. Plugin onload() hooks from *_tab.py
        plugin_content = run_onload_plugins(path)
        if plugin_content is not None:
            self.text.insert("1.0", plugin_content)
            debug(1, f"Plugin handled {path}")
        elif is_probably_text_file(path):
            try:
                data = Path(path).read_text(encoding="utf-8", errors="replace")
                self.text.insert("1.0", data)
                debug(1, f"Opened text file {path}")
            except Exception as e:
                messagebox.showerror("Open Error", f"Could not open file:\n{e}")
                debug(1, f"Failed to open {path}: {e}")
                self._loading = False
                return False
        else:
            # Non-text: show metadata only
            summary = file_meta_summary(path)
            self.text.insert("1.0", summary)
            debug(1, f"Opened binary/non-text as summary: {path}")

        self.text.edit_modified(False)
        self.text.edit_reset()  # clear undo stack
        self._loading = False
#        self.filepath = str(Path(path).resolve())
        self.dirty = False
        self.update_tab_label()
        # Apply the sash that belongs to this file
        self._update_line_numbers()
        self.frame.after(40, self._restore_sash)
        self.frame.after(50, self.restore_cursor_state)
        return True

    def get_content(self) -> str:
        return self.text.get("1.0", "end-1c")

    def mark_clean(self) -> None:
        self.dirty = False
        self.text.edit_modified(False)
        self.update_tab_label()

    def set_filepath(self, path: str) -> None:
        old = self.filepath
        self.filepath = str(Path(path).resolve())
        # Migrate sash position if the file was previously untitled or renamed
        if old != self.filepath:
            try:
                set_sash_pos(self.filepath, self.paned.sashpos(0))
            except tk.TclError:
                pass
        self.update_tab_label()
        debug(2, f"Path set to {self.filepath}")

    def focus(self) -> None:
        self.text.focus_set()

    def destroy(self) -> None:
        self.save_current_sash()
        self.save_cursor_state()
        try:
            self.notebook.forget(self.frame)
        except tk.TclError:
            pass
        self.frame.destroy()
        debug(2, f"Closed tab {self.filepath or 'Untitled'}")

#eof
