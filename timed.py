#!/usr/bin/env python3
"""
Simple Multi-Tab Editor – main application
Handles menus, keyboard shortcuts, notebook, session restore, drag-and-drop, and lifecycle.
drag-and-drop, tab reordering, and lifecycle.

installation:
pip install tkinterdnd2

structure:
editor/
├── main.py          # Menus, window, notebook, event handling
├── editor_tab.py    # Tab content (Text widget + dirty tracking + close button)
├── logview_tab.py   # Generic log + debug log content
├── image_tab.py     # image viewer/editor
├── audio_tab.py     # audio viewer/editor
└── utils.py         # Helpers (recent files, dialogs, path utilities, etc.)

usage:
# Linux / Windows (any Python 3.8+)
python main.py
# Normal start – restores everything from the single session.json
python main.py

# Start completely clean (ignore previous session)
python main.py -fresh

# Enable debug log (level 1) in its own tab
python main.py -debug
python main.py -debug 5
python main.py -debug=20

# Open specific files (and still restore session unless -fresh is also given)
python main.py notes.txt todo.md

# Clean start + open files
python main.py -fresh report.txt

# Combine
python main.py -fresh -debug 3 notes.txt

initial prompt:
Create a  multi-tab file editor that will run on Linux and Windows.  The editor has typical file editor menus and keyboard shortcuts.  The File menu has the typical New, Open/Recent, Save/As, Close menu items.  The Edit menu has the typical Undo/Redo, Cut/Copy/Paste, and Find/Replace menu items.   The Help menu has About, Check for Updates, and a link to documentation/tutorials.  Each open file is  displayed in a separate tab, with the abbreviated base filename as the tab label and an indicator if the contents have been changed but not yet saved.  Also put a little red X on the tab label to allow the tab to be closed.  Structure the program so there is one main source file that handles the menus, a separate source file that renders file contents in the tab, and another source file for utility functions and generic helpers.   When using or suggesting third-party software or code, use only software whose license permits redistribution and modification and allows a larger work to restrict commercial usage. Prefer MIT, BSD, Apache-2.0, or public-domain/CC0 material. Do not copy GPL, AGPL, LGPL, or other copyleft code into the project without first identifying it and obtaining approval.

"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import webbrowser
from pathlib import Path
from typing import List, Optional, Union

# ---------------------------------------------------------------------------
# Application constants
# ---------------------------------------------------------------------------
APP_NAME = "timED"
VERSION = "1.0.0"

# Make the constants available to utils
import utils
utils.APP_NAME = APP_NAME
utils.VERSION = VERSION

from editor_tab import EditorTab
from logview_tab import (
    debug, set_debug_level, get_debug_level, debug_log_path,
#    create_tab as create_debug_tab,
#    LogViewerTab, 
)
from utils import (
    load_recent, add_recent, load_session_data, save_session_data,
    about_text, documentation_url, abbreviated_name,
    get_max_recent, set_preference, get_preference,
#    find_create_tab_plugin,
)
#TODO: remove find_create_tab_plugin if main no longer uses create_tab for logs.


# Optional drag-and-drop support (MIT license)
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False
    TkinterDnD = None  # type: ignore


# ---------------------------------------------------------------------------
# Custom notebook with a red close (✕) element on every tab
# ---------------------------------------------------------------------------

class CustomNotebook(ttk.Notebook):
    """ttk.Notebook with a red close element on every tab."""

    _style_initialized = False

    def __init__(self, *args, **kwargs):
        if not CustomNotebook._style_initialized:
            self._init_style()
            CustomNotebook._style_initialized = True
        kwargs["style"] = "CustomNotebook"
        super().__init__(*args, **kwargs)
        self._active_close = None
        self._close_callback = None          # set by EditorApp after construction
        self.bind("<ButtonPress-1>", self._on_close_press, True)
        self.bind("<ButtonRelease-1>", self._on_close_release)

    def _init_style(self) -> None:
        style = ttk.Style()
        # Build a simple red "X" image via PhotoImage
        # 10x10 pixels, transparent background, red X
        #data = """
        #    R0lGODlhDAAMAIABAMISEv///yH5BAEAAAEALAAAAAAMAAwAAAIdjI+py+0Po5y02ospcFtr
        #    3X1hNJbmKW7pirm2lZ5OASQFADs=
        #"""
        # Fallback: use text-based element if GIF fails; create via tk
        #try:
        #    self._img_close = tk.PhotoImage(data=data)
        #except Exception:
            # Programmatic 10x10 red X
        #    self._img_close = tk.PhotoImage(width=12, height=12)
        #    for x, y in (
        #        (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 8),
        #        (8, 2), (7, 3), (6, 4), (5, 5), (4, 6), (3, 7), (2, 8),
        #    ):
        #        self._img_close.put("#c0392b", (x, y))

        # Programmatic 12×12 red X (no external image file needed)
        self._img_close = tk.PhotoImage(width=12, height=12)
        red = "#c0392b"
        for x, y in (
            (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 8), (9, 9),
            (9, 2), (8, 3), (7, 4), (6, 5), (5, 6), (4, 7), (3, 8), (2, 9),
        ):
            self._img_close.put(red, (x, y))
        self._img_closepressed = self._img_close
        self._img_closeactive = self._img_close

        try:
            style.element_create(
                "close", "image", self._img_close,
                ("active", "pressed", "!disabled", self._img_closepressed),
                ("active", "!disabled", self._img_closeactive),
                border=6, sticky="",
            )
        except tk.TclError:
            pass  # element already exists from a previous instance

        style.layout("CustomNotebook", [("CustomNotebook.client", {"sticky": "nswe"})])
        style.layout("CustomNotebook.Tab", [
            ("CustomNotebook.tab", {
                "sticky": "nswe",
                "children": [
                    ("CustomNotebook.padding", {
                        "side": "top",
                        "sticky": "nswe",
                        "children": [
                            ("CustomNotebook.focus", {
                                "side": "top",
                                "sticky": "nswe",
                                "children": [
                                    ("CustomNotebook.label", {"side": "left", "sticky": ""}),
                                    ("CustomNotebook.close", {"side": "left", "sticky": ""}),
                                ],
                            }),
                        ],
                    }),
                ],
            }),
        ])

    def _on_close_press(self, event):
        element = self.identify(event.x, event.y)
        if "close" in element:
            index = self.index(f"@{event.x},{event.y}")
            self.state(["pressed"])
            self._active_close = index
            return "break"

    def _on_close_release(self, event):
        if not self.instate(["pressed"]):
            return
        element = self.identify(event.x, event.y)
        try:
            index = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            index = None
        if (
            "close" in element
            and self._active_close is not None
            and self._active_close == index
        ):
            if self._close_callback is not None:
                self._close_callback(index)
        self.state(["!pressed"])
        self._active_close = None

    def DROP_close_tab_by_index(self, index: int) -> None:
        try:
            frames = self.notebook.tabs()
            frame_id = frames[index]
            for tab in list(self.tabs):
                if str(tab.frame) == frame_id:
                    self.close_tab(tab)
                    return
        except Exception as e:
            debug(1, f"Close by index failed: {e}")

    def DROP_open_debug_tab(self) -> None:
        if self._debug_tab is not None:
            return
        tab = DebugTab(self.notebook, on_close_request=self.close_tab)
        self.tabs.append(tab)          # last in our list
        self._debug_tab = tab
        # Ensure it is the last notebook tab
        try:
            self.notebook.insert("end", tab.frame)
        except tk.TclError:
            pass
        # Do NOT select it by default – keep focus on content tabs


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class EditorApp(TkinterDnD.Tk if HAS_DND else tk.Tk):  # type: ignore
    def __init__(
        self,
        files_to_open: Optional[List[str]] = None,
        fresh: bool = False,
        debug_level: int = 0,
    ):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1000x700")
        self.minsize(400, 300)

#        self.tabs: List[Union[EditorTab, DebugTab]] = []
        self.tabs: List[EditorTab] = []
        self.recent_menu: Optional[tk.Menu] = None
        self._files_to_open = files_to_open or []
        self._fresh = fresh
        self._debug_tab: Optional[EditorTab] = None
        self._last_tab = None

        set_debug_level(debug_level)   # truncates/creates debug.log, stores level
        if debug_level > 0:
            debug(1, f"{{pink}}Starting {APP_NAME} v{VERSION}  debug_level={debug_level}")

        self._build_ui()
#        self._set_window_icon()
#        self.after(100, self._set_window_icon)   # after first map
        self.after(500, self._set_window_icon)   # again once WM is ready
        self._build_menus()
        self._bind_shortcuts()
        self._setup_drag_drop()
        self._setup_tab_reordering()

        # Wire the close-button callback now that the notebook exists
        self.notebook._close_callback = self._close_tab_by_index

        # Restore previous session (unless -fresh) then open any CLI files
        self.after(50, self._startup_open)
        self.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.notebook = CustomNotebook(self)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
#        self.notebook.bind("<<NotebookTabClosed>>", self._on_notebook_tab_closed)

        self.status = ttk.Label(self, text="Ready", relief="sunken", anchor="w")
        self.status.pack(side="bottom", fill="x")

    def _set_window_icon(self) -> None:
        """Set the window decoration icon (title bar / taskbar)."""
        import os
        debug(2, f"{{blue}}desktop", os.environ.get('XDG_CURRENT_DESKTOP'))
        debug(2, f"{{blue}}shell", os.environ.get('SHELL', '/bin/bash')) 
#Wayland vs X11
#Some Wayland sessions never take iconphoto from Tk
#Running under python main.py vs .desktop: Desktop entry Icon= is what the taskbar uses
#wmctrl -l / taskbar: Title bar vs dock can use different icons
        path = Path(__file__).resolve().parent / "app_icon.png"
        try:
            if path.is_file():
                self._app_icon = tk.PhotoImage(file=str(path))
                self.iconphoto(True, self._app_icon)
                debug(1, f"{{blue}}icon from file {path}")
                debug(1, f"{{blue}}Tk {tk.TkVersion}  windowing={self.tk.call('tk', 'windowingsystem')}")
                debug(1, f"{{blue}}iconphoto exists={hasattr(self, 'iconphoto')}")
                debug(1, f"{{blue}}_app_icon ref={getattr(self, '_app_icon', None)}")
                return
        except Exception as exc:
            debug(1, f"{{red}}PNG icon failed: {exc!r}")

        try:
            icon = self._make_app_icon()
            # Keep a reference so Tk does not garbage-collect it
            self._app_icon = icon
            self.iconphoto(True, icon)
            debug(1, f"{{green}}iconphoto OK  size={icon.width()}x{icon.height()}")
        except Exception as exc:
            debug(1, f"{{red}}iconphoto FAILED: {exc!r}")

    def _make_app_icon(self) -> tk.PhotoImage:
        """
        Small programmatic icon (no external file).
        32x32 teal page with a fold — distinctive enough for the title bar.
        """
        size = 32
        img = tk.PhotoImage(width=size, height=size)
        # Background
        bg, accent, line = "#2c3e50", "#1abc9c", "#ecf0f1"
        for y in range(size):
            for x in range(size):
                img.put(bg, (x, y))
        # Page rectangle
        for y in range(4, 28):
            for x in range(6, 26):
                img.put(line, (x, y))
        # Fold corner
        for i in range(8):
            for x in range(18 + i, 26):
                img.put(accent, (x, 4 + i))
        # Text lines
        for y in (12, 16, 20, 24):
            for x in range(9, 23):
                img.put(accent if y != 24 else "#3498db", (x, y))
        return img

    def DROP_on_notebook_tab_closed(self, event=None) -> None:
        # event.x / identify already handled; find tab by current close target
        try:
            # After release the selection may still be the closed tab's neighbor;
            # use the index stored during press via a short lookup
            idx = self.notebook._active_close
            if idx is None:
                # fallback: close current
                self.close_current()
                return
            # Map index → our tab object
            frames = list(self.notebook.tabs())
            if 0 <= idx < len(frames):
                frame_id = frames[idx]
                for tab in list(self.tabs):
                    if str(tab.frame) == frame_id:
                        self.close_tab(tab)
                        return
        except Exception:
            self.close_current()

    def _build_menus(self) -> None:
        menubar = tk.Menu(self)

        # ----- File -----
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New", accelerator="Ctrl+N", command=self.new_file)
        file_menu.add_command(label="Open…", accelerator="Ctrl+O", command=self.open_file)
        self.recent_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Open Recent", menu=self.recent_menu)
        self._rebuild_recent_menu()
        file_menu.add_separator()
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_file)
        file_menu.add_command(label="Save As…", accelerator="Ctrl+Shift+S", command=self.save_file_as)
        file_menu.add_separator()
        file_menu.add_command(label="Close", accelerator="Ctrl+W", command=self.close_current)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", accelerator="Alt+F4", command=self.on_quit)
        menubar.add_cascade(label="File", menu=file_menu)

        # ----- Edit -----
        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", accelerator="Ctrl+X", command=lambda: self._text_event("<<Cut>>"))
        edit_menu.add_command(label="Copy", accelerator="Ctrl+C", command=lambda: self._text_event("<<Copy>>"))
        edit_menu.add_command(label="Paste", accelerator="Ctrl+V", command=lambda: self._text_event("<<Paste>>"))
        edit_menu.add_separator()
        edit_menu.add_command(label="Find…", accelerator="Ctrl+F", command=self.find)
        edit_menu.add_command(label="Replace…", accelerator="Ctrl+H", command=self.replace)
        edit_menu.add_separator()
        edit_menu.add_command(label="Preferences…", command=self.show_preferences)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        # ----- Help -----
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Documentation / Tutorials", command=self.open_docs)
        help_menu.add_command(label="Check for Updates", command=self.check_updates)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    def _bind_shortcuts(self) -> None:
        self.bind_all("<Control-n>", lambda e: self.new_file())
        self.bind_all("<Control-o>", lambda e: self.open_file())
        self.bind_all("<Control-s>", lambda e: self.save_file())
        self.bind_all("<Control-S>", lambda e: self.save_file_as())  # Shift
        self.bind_all("<Control-w>", lambda e: self.close_current())
        self.bind_all("<Control-z>", lambda e: self.undo())
        self.bind_all("<Control-y>", lambda e: self.redo())
        self.bind_all("<Control-f>", lambda e: self.find())
        self.bind_all("<Control-h>", lambda e: self.replace())
        # Platform-specific quit
        self.bind_all("<Alt-F4>", lambda e: self.on_quit())

    def _setup_drag_drop(self) -> None:
        if not HAS_DND:
            return
        # Accept file drops on the whole window / notebook
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_drop)
        self.notebook.drop_target_register(DND_FILES)
        self.notebook.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event) -> None:
        """Open every dropped file in its own tab."""
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        for p in paths:
            p = p.strip("{}")  # Windows sometimes wraps paths in braces
            if p and Path(p).is_file():
                self.open_file(p)
                debug(2, f"Dropped file {p}")

    # ------------------------------------------------------------------ Tab reordering
    def _setup_tab_reordering(self) -> None:
        """Allow the user to drag tabs left/right to change their order."""
        self.notebook.bind("<B1-Motion>", self._on_tab_drag)

    def _on_tab_drag(self, event) -> None:
        try:
            index = self.notebook.index(f"@{event.x},{event.y}")
            self.notebook.insert(index, child=self.notebook.select())
        except tk.TclError:
            pass  # mouse is not over a tab

    # ------------------------------------------------------------------ Close button → tab object
    def _close_tab_by_index(self, index: int) -> None:
        """Called by CustomNotebook when the user clicks the red ✕ on a tab."""
        try:
            frames = self.notebook.tabs()
            if not (0 <= index < len(frames)):
                return
            frame_id = frames[index]
            for tab in list(self.tabs):
                if str(tab.frame) == frame_id:
                    self.close_tab(tab)
                    return
        except Exception as e:
            debug(1, f"Close by index failed: {e}")

    # ------------------------------------------------------------------ Startup
    def _startup_open(self) -> None:
        opened_any = False
        session = load_session_data()
        active_index = session.get("active_index", 0)

        # 1. Restore previous session (unless -fresh)
        if not self._fresh:
            paths = session.get("open_files", [])
            for path in paths:
                if Path(path).is_file():
                    self.open_file(path)
                    opened_any = True
            debug(1, f"{{blue}}Restored session with {len(paths)} file(s)")

        # 2. Files named on the command line
        for path in self._files_to_open:
            if Path(path).is_file():
                self.open_file(path)
                opened_any = True
                debug(1, f"CLI open {path}")

        # 3. At least one empty tab if nothing else
        if not opened_any:
            self.new_file()

        # 4. Debug tab is created LAST so it appears as the rightmost tab
        if get_debug_level() > 0:
            self._open_debug_tab()
#            opened_any = True

        # Restore which tab had focus (EditorTabs only)
        try:
            # skip debug tab if it is first
            real_tabs = [t for t in self.tabs if isinstance(t, EditorTab)]
            if real_tabs and 0 <= active_index < len(real_tabs):
                self.notebook.select(real_tabs[active_index].frame)
                real_tabs[active_index].focus()
                real_tabs[active_index].restore_cursor_state()
                debug(2, f"{{blue}}Restored focus to tab index {active_index}")
        except Exception as e:
            debug(1, f"Focus restore failed: {e}")

        self._update_title()

    def _open_debug_tab(self) -> None:
        """Open the app debug.log through the normal EditorTab + onload plugin path."""
        if self._debug_tab is not None:
            return
        from logview_tab import debug_log_path
        path = str(debug_log_path())
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(path).exists():
            Path(path).write_text("", encoding="utf-8")
        # Reuse open_file so EditorTab + debug_tab.onload run
        before = list(self.tabs)
        self.open_file(path)
        # Remember the tab that was just opened (for “last tab” / close tracking)
        for t in self.tabs:
            if t not in before and isinstance(t, EditorTab) and t.filepath == path:
                self._debug_tab = t
                try:
                    self.notebook.insert("end", t.frame)
                except tk.TclError:
                    pass
                break

    def OLD_open_debug_tab(self) -> None:
        if self._debug_tab is not None:
            return
        tab = DebugTab(self.notebook, on_close_request=self.close_tab)
        self.tabs.append(tab)               # keep it last in our list
        self._debug_tab = tab
        # Force it to the end of the notebook
        try:
            self.notebook.insert("end", tab.frame)
        except tk.TclError:
            pass
        # Do not auto-select the debug tab

    # ------------------------------------------------------------------ Tab helpers
    def current_tab(self) -> Optional[EditorTab]: #Union[EditorTab, DebugTab]]:
        try:
            current = self.notebook.select()
            for tab in self.tabs:
                if str(tab.frame) == current:
                    return tab
        except tk.TclError:
            pass
        return None

    def new_file(self) -> None:
        tab = EditorTab(
            self.notebook,
            on_modified=self._on_tab_modified,
            on_close_request=self.close_tab,
        )
        # Insert before the debug tab if it exists
        if self._debug_tab is not None:
            try:
                dbg_index = self.notebook.index(self._debug_tab.frame)
                self.notebook.insert(dbg_index, tab.frame)
                # Keep self.tabs order roughly matching notebook order
                if self._debug_tab in self.tabs:
                    pos = self.tabs.index(self._debug_tab)
                    self.tabs.insert(pos, tab)
                else:
                    self.tabs.append(tab)
            except tk.TclError:
                self.tabs.append(tab)
        else:
            self.tabs.append(tab)
        self.notebook.select(tab.frame)
        tab.focus()
        self._update_title()
        debug(2, "New untitled tab")

    def open_file(self, path: Optional[str] = None) -> None:
        if path is None:
            path = filedialog.askopenfilename(
                title="Open File",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
        if not path:
            return
        path = str(Path(path).resolve())

        # Reuse existing tab if already open
        for tab in self.tabs:
            if isinstance(tab, EditorTab) and tab.filepath and Path(tab.filepath) == Path(path):
                self.notebook.select(tab.frame)
                return

        # Custom tab plugins (e.g. debug_tab for *.log)
#needed?        found = find_create_tab_plugin(path)
        found = None
        if found is not None:
            _mod, create_fn = found
            tab = create_fn(
                self.notebook,
                filepath=path,
                on_close_request=self.close_tab,
                live_debug=False,
            )
            if tab is not None:
                # insert before debug tab if present (same as EditorTab)
                if self._debug_tab is not None:
                    try:
                        dbg_index = self.notebook.index(self._debug_tab.frame)
                        self.notebook.insert(dbg_index, tab.frame)
                        pos = self.tabs.index(self._debug_tab)
                        self.tabs.insert(pos, tab)
                    except (tk.TclError, ValueError):
                        self.tabs.append(tab)
                else:
                    self.tabs.append(tab)
                self.notebook.select(tab.frame)
                add_recent(path)
                self._rebuild_recent_menu()
                if hasattr(tab, "focus"):
                    tab.focus()
                self._update_title()
                debug(1, f"Opened via plugin {getattr(_mod, '__name__', '?')}: {path}")
                return

        tab = EditorTab(
            self.notebook,
            filepath=path,
            on_modified=self._on_tab_modified,
            on_close_request=self.close_tab,
        )
        if tab.filepath:  # successfully loaded
            if self._debug_tab is not None:
                try:
                    dbg_index = self.notebook.index(self._debug_tab.frame)
                    self.notebook.insert(dbg_index, tab.frame)
                    if self._debug_tab in self.tabs:
                        pos = self.tabs.index(self._debug_tab)
                        self.tabs.insert(pos, tab)
                    else:
                        self.tabs.append(tab)
                except tk.TclError:
                    self.tabs.append(tab)
            else:
                self.tabs.append(tab)
            self.notebook.select(tab.frame)
            add_recent(path)
            self._rebuild_recent_menu()
            tab.focus()
            self._update_title()
        else:
            tab.destroy()

    def save_file(self) -> bool:
        """
        Saving:
        For files on disk without visible tag markers left as-is, use get_content() to save
        (tags are only stripped in the display path if strip_style_tags was used on save)
        Tags should normally be left in the file so reload still colors).
        Default:
        save raw buffer text including {red} markers (what the user typed).
        Display applies styles on insert/load.  If the buffer holds already-expanded text without
        markers (because insert stripped them), saving won’t preserve tags. Currently markers are
        stripped on display only while inserting styled runs (so Text widget does not contain {red}).
        For log files that is correct (file on disk still has tags; viewer re-parses each reload).
        For editable tabs, either:
        A. Store tags in the widget as real characters and only apply tags via a highlighter pass, or  
        B. Accept that styled insert is for read-only/plugin/log content.

        For log viewer (file-based) path B is used.
        For editable tab load, path B will strip file’s {red} markers after load.
        To keep them editable, run a highlighter over existing text instead of stripping on insert.
        """
        tab = self.current_tab()
        if not isinstance(tab, EditorTab):
            return False
        if not tab.filepath:
            return self.save_file_as()
        try:
            Path(tab.filepath).write_text(tab.get_content(), encoding="utf-8")
            tab.mark_clean()
            add_recent(tab.filepath)
            self._rebuild_recent_menu()
            self.status.configure(text=f"Saved {tab.filepath}")
            self._update_title()
            debug(1, f"Saved {tab.filepath}")
            return True
        except Exception as e:
            messagebox.showerror("Save Error", str(e))
            debug(1, f"Save failed: {e}")
            return False

    def save_file_as(self) -> bool:
        tab = self.current_tab()
        if not isinstance(tab, EditorTab):
            return False
        path = filedialog.asksaveasfilename(
            title="Save As",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return False
        tab.set_filepath(path)
        return self.save_file()

    def close_tab(self, tab: Optional[EditorTab] = None) -> None: #Union[EditorTab, DebugTab]] = None) -> None:
        if tab is None:
            tab = self.current_tab()
        if not tab:
            return

        if isinstance(tab, EditorTab) and tab.dirty:
            name = abbreviated_name(tab.filepath)
            answer = messagebox.askyesnocancel(
                "Unsaved Changes",
                f'"{name}" has unsaved changes.\nDo you want to save them?',
            )
            if answer is None:  # Cancel
                return
            if answer:  # Yes
                # Temporarily select the tab so save_file works on it
                self.notebook.select(tab.frame)
                if not self.save_file():
                    return

        if tab is self._debug_tab:
            self._debug_tab = None

        tab.destroy()
        if tab in self.tabs:
            self.tabs.remove(tab)

        # Never leave the UI empty
        if not self.tabs:
            self.new_file()
        self._update_title()

    def close_current(self) -> None:
        self.close_tab()

    def _on_tab_modified(self, tab: EditorTab) -> None:
        self._update_title()

    def _on_tab_changed(self, event=None) -> None:
        # Preserve tab selection when leaving / returning
        prev = self._last_tab  #getattr(self, "_last_tab", None)
        cur = self.current_tab()
        if isinstance(prev, EditorTab):
            prev.save_selection_state()
        if isinstance(cur, EditorTab):
            cur.restore_selection_state()
        self._last_tab = cur
        self._update_title()

    def _update_title(self) -> None:
        tab = self.current_tab()
        if isinstance(tab, EditorTab) and tab.filepath:
            name = abbreviated_name(tab.filepath, 40)
            dirty = " *" if tab.dirty else ""
            self.title(f"{name}{dirty} – {APP_NAME}")
#        elif isinstance(tab, DebugTab):
#            self.title(f"Debug Log – {APP_NAME}")
        else:
            dirty = " *" if (isinstance(tab, EditorTab) and tab.dirty) else ""
            self.title(f"Untitled{dirty} – {APP_NAME}")

    def _rebuild_recent_menu(self) -> None:
        if not self.recent_menu:
            return
        self.recent_menu.delete(0, "end")
        recent = load_recent()
        if not recent:
            self.recent_menu.add_command(label="(empty)", state="disabled")
            return
        for p in recent:
            self.recent_menu.add_command(
                label=p,
                command=lambda path=p: self.open_file(path),
            )
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="Clear Recent", command=self._clear_recent)

    def _clear_recent(self) -> None:
        from utils import save_recent
        save_recent([])
        self._rebuild_recent_menu()
        debug(2, "Recent list cleared")

    # ------------------------------------------------------------------ Preferences
    def show_preferences(self) -> None:
        win = tk.Toplevel(self)
        win.title("Preferences")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        frm = ttk.Frame(win, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frm, text="Maximum recent files:").grid(row=0, column=0, sticky="w", pady=4)
        max_var = tk.StringVar(value=str(get_max_recent()))
        spin = ttk.Spinbox(frm, from_=1, to=100, width=6, textvariable=max_var)
        spin.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)

        def on_ok():
            try:
                val = int(max_var.get())
                val = max(1, min(val, 100))
            except ValueError:
                val = 10
            set_preference("max_recent", val)
            from utils import save_recent
            save_recent(load_recent())
            self._rebuild_recent_menu()
            debug(2, f"Preference max_recent set to {val}")
            win.destroy()

        def on_cancel():
            win.destroy()

        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=1, column=0, columnspan=2, pady=(12, 0), sticky="e")
        ttk.Button(btn_frm, text="OK", command=on_ok).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frm, text="Cancel", command=on_cancel).pack(side="right")

        win.bind("<Return>", lambda e: on_ok())
        win.bind("<Escape>", lambda e: on_cancel())
        spin.focus_set()

    # ------------------------------------------------------------------ Edit helpers
    def _text_event(self, sequence: str) -> None:
        tab = self.current_tab()
        if isinstance(tab, EditorTab):
            tab.text.event_generate(sequence)

    def undo(self) -> None:
        tab = self.current_tab()
        if isinstance(tab, EditorTab):
            try:
                tab.text.edit_undo()
            except tk.TclError:
                pass

    def redo(self) -> None:
        tab = self.current_tab()
        if isinstance(tab, EditorTab):
            try:
                tab.text.edit_redo()
            except tk.TclError:
                pass

    def find(self) -> None:
        tab = self.current_tab()
        if not isinstance(tab, EditorTab):
            return
        needle = simpledialog.askstring("Find", "Find:")
        if not needle:
            return
        start = tab.text.search(needle, "1.0", stopindex="end", nocase=True)
        if start:
            end = f"{start}+{len(needle)}c"
            tab.text.tag_remove("sel", "1.0", "end")
            tab.text.tag_add("sel", start, end)
            tab.text.mark_set("insert", end)
            tab.text.see(start)
        else:
            messagebox.showinfo("Find", "Text not found.")

    def replace(self) -> None:
        # Simple sequential replace dialog
        tab = self.current_tab()
        if not isinstance(tab, EditorTab):
            return
        find_str = simpledialog.askstring("Replace", "Find:")
        if find_str is None:
            return
        repl_str = simpledialog.askstring("Replace", "Replace with:")
        if repl_str is None:
            return
        content = tab.get_content()
        new_content = content.replace(find_str, repl_str)
        if new_content != content:
            tab.text.delete("1.0", "end")
            tab.text.insert("1.0", new_content)
            tab.dirty = True
            tab.update_tab_label()
            self._update_title()
            messagebox.showinfo("Replace", "Replacement done.")
        else:
            messagebox.showinfo("Replace", "No occurrences found.")

    # ------------------------------------------------------------------ Help
    def show_about(self) -> None:
        extra = ""
        if not HAS_DND:
            extra = "\n\n(Drag-and-drop support: install tkinterdnd2)"
        if get_debug_level() > 0:
            extra += f"\n\nDebug level: {get_debug_level()}"
        messagebox.showinfo("About", about_text() + extra)

    def check_updates(self) -> None:
        messagebox.showinfo(
            "Check for Updates",
            f"You are running {APP_NAME} version {VERSION}.\n\n"
            "No automatic update server is configured yet.\n"
            "Check the project page for newer releases.",
        )

    def open_docs(self) -> None:
        webbrowser.open(documentation_url())

    # ------------------------------------------------------------------ Quit / session
    def _collect_session(self) -> dict:
        open_files = []
        active_index = 0
        cur = self.current_tab()
        idx = 0
        for t in self.tabs:
            if isinstance(t, EditorTab) and t.filepath:
                if t is cur:
                    active_index = idx
                open_files.append(t.filepath)
                t.save_current_sash()
                t.save_cursor_state()
                idx += 1
        data = load_session_data()
        data["open_files"] = open_files
        data["active_index"] = active_index
        data["recent"] = load_recent()
        return data

    def on_quit(self) -> None:
        # Ask about every dirty tab
        for tab in list(self.tabs):
            if isinstance(tab, EditorTab) and tab.dirty:
                self.notebook.select(tab.frame)
                name = abbreviated_name(tab.filepath)
                answer = messagebox.askyesnocancel(
                    "Unsaved Changes",
                    f'"{name}" has unsaved changes.\nSave before quitting?',
                )
                if answer is None:
                    return
                if answer:
                    if not self.save_file():
                        return
            # Persist sash even for clean tabs
            if isinstance(tab, EditorTab):
                tab.save_current_sash()
                tab.save_cursor_state()

        # Remember open files for next launch
        save_session_data(self._collect_session())
        debug(1, "Session saved, exiting")
        self.destroy()


def parse_args(argv: List[str]):
    """Return (files, fresh, debug_level)."""
    fresh = False
    debug_level = 0
    files = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-fresh":
            fresh = True
        elif arg == "-debug":
            # next token may be the level
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                debug_level = int(argv[i + 1])
                i += 1
            else:
                debug_level = 1
        elif arg.startswith("-debug="):
            try:
                debug_level = int(arg.split("=", 1)[1])
            except ValueError:
                debug_level = 1
        elif not arg.startswith("-"):
            files.append(arg)
        i += 1
    return files, fresh, debug_level


if __name__ == "__main__":
    files, fresh, dbg = parse_args(sys.argv)
    app = EditorApp(files_to_open=files, fresh=fresh, debug_level=dbg)
    app.mainloop()

#eof
