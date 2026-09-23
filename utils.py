"""Utility functions and generic helpers."""

from __future__ import annotations

import json
import os
import re
import platform
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
#RECURSION: from debug_tab import debug
#def debug(level: int, *args, **kwargs):
#    msg = ""
#    for i, arg in enumerate(args):
#        msg += str(arg) + " "
#    print(msg)
#    return args[-1]  #for inlining last arg


# These will be set / overridden by main.py constants when imported
APP_NAME = "timED"
VERSION = "1.0.0"
clean_name = re.sub(r'[^\x20-\x7E]', '_', APP_NAME).lower()

# Config lives in the user's home directory
CONFIG_DIR = Path.home() / f".{clean_name}"
SESSION_FILE = CONFIG_DIR / "session.json"
MAX_RECENT = 10

# Default sash position (pixels from top) when no saved value exists
DEFAULT_SASH_POS = 60  #small default height


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _default_session() -> Dict[str, Any]:
    return {
        "recent": [],
        "open_files": [],
        "active_index": 0,
        "sash_positions": {},
        "cursor_positions": {},   # path -> {"index": "line.col", "yview": float}
        "preferences": {
            "max_recent": 10,
        },
    }


def get_preference(key: str, default: Any = None) -> Any:
    data = load_session_data()
    prefs = data.get("preferences", {})
    return prefs.get(key, default)


def set_preference(key: str, value: Any) -> None:
    data = load_session_data()
    data.setdefault("preferences", {})[key] = value
    save_session_data(data)


def get_max_recent() -> int:
    """Read MAX_RECENT from Preferences; default 10."""
    try:
        val = int(get_preference("max_recent", 10))
        return max(1, min(val, 100))   # sane bounds
    except (TypeError, ValueError):
        return 10


# ---------- recent files (now use get_max_recent) ----------

def load_session_data() -> Dict[str, Any]:
    ensure_config_dir()
    if not SESSION_FILE.exists():
        return _default_session()
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_session()
        # ensure required keys exist
        base = _default_session()
        base.update(data)
        return base
    except Exception:
        return _default_session()


def save_session_data(data: Dict[str, Any]) -> None:
    ensure_config_dir()
    SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------- convenience wrappers ----------

def load_recent() -> List[str]:
    data = load_session_data()
    max_r = get_max_recent()
    return [p for p in data.get("recent", []) if isinstance(p, str) and Path(p).exists()][:max_r]


def save_recent(paths: List[str]) -> None:
    data = load_session_data()
    # Keep unique, most-recent first
    max_r = get_max_recent()
    seen = set()
    unique = []
    for p in paths:
        if p not in seen and Path(p).exists():
            seen.add(p)
            unique.append(p)
    data["recent"] = unique[:max_r]
    save_session_data(data)


def add_recent(path: str) -> List[str]:
    recent = load_recent()
    if path in recent:
        recent.remove(path)
    recent.insert(0, path)
    save_recent(recent)
    return recent


def get_sash_pos(filepath: Optional[str]) -> int:
    if not filepath:
        return DEFAULT_SASH_POS
    key = str(Path(filepath).resolve())
    return int(load_session_data().get("sash_positions", {}).get(key, DEFAULT_SASH_POS))


def set_sash_pos(filepath: Optional[str], pos: int) -> None:
    if not filepath:
        return
    key = str(Path(filepath).resolve())
    data = load_session_data()
    data.setdefault("sash_positions", {})[key] = max(30, int(pos))
    save_session_data(data)


def get_cursor_pos(filepath: Optional[str]) -> Dict[str, Any]:
    if not filepath:
        return {"index": "1.0", "yview": 0.0}
    key = str(Path(filepath).resolve())
    return load_session_data().get("cursor_positions", {}).get(
        key, {"index": "1.0", "yview": 0.0}
    )


def set_cursor_pos(filepath: Optional[str], index: str, yview: float) -> None:
    if not filepath:
        return
    key = str(Path(filepath).resolve())
    data = load_session_data()
    data.setdefault("cursor_positions", {})[key] = {
        "index": index,
        "yview": float(yview),
    }
    save_session_data(data)


def abbreviated_name(path: Optional[str], max_len: int = 24) -> str:
    """Return a short display name for a tab label."""
    if not path:
        return "Untitled"
    name = Path(path).name
    if len(name) <= max_len:
        return name
    # keep extension if possible
    stem, suffix = Path(name).stem, Path(name).suffix
    keep = max_len - len(suffix) - 3
    if keep < 4:
        return name[: max_len - 3] + "..."
    return stem[:keep] + "..." + suffix


def about_text() -> str:
    return (
        f"{APP_NAME}\n"
        f"Version {VERSION}\n\n"
        "A lightweight cross-platform text editor\n"
        "written in pure Python + Tkinter.\n\n"
        f"Python {sys.version.split()[0]}  |  {platform.system()} {platform.release()}"
    )


def documentation_url() -> str:
    # Placeholder – replace with real docs when available
    return "https://github.com/example/simple-multi-tab-editor#readme"


def available_memory_str() -> str:
    """Return a short human-readable available-memory string (stdlib only)."""
    try:
        if sys.platform == "win32":
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            avail = stat.ullAvailPhys / (1024 * 1024)
            total = stat.ullTotalPhys / (1024 * 1024)
            return f"{avail:.0f} MB free / {total:.0f} MB total"
        else:
            # Linux / most Unix
            info = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])  # kB
            avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024
            total = info.get("MemTotal", 0) / 1024
            return f"{avail:.0f} MB free / {total:.0f} MB total"
    except Exception as e:
        return f"(unavailable: {e})"


def is_probably_text_file(path: str, sample_size: int = 8192) -> bool:
    """Heuristic: no NUL in the first chunk and mostly printable/UTF-8."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(sample_size)
        if not chunk:
            return True
        if b"\x00" in chunk:
            return False
        # try utf-8
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            pass
        # latin-1 always decodes; check printable ratio
        text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f"
        nontext = sum(1 for b in chunk if b not in text_chars)
        return (nontext / len(chunk)) < 0.30
    except Exception:
        return False


def file_meta_summary(path: str) -> str:
    p = Path(path)
    try:
        st = p.stat()
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"File: {p.name}\n"
            f"Path: {p.resolve()}\n"
            f"Size: {size:,} bytes\n"
            f"Last modified: {mtime}\n"
        )
    except Exception as e:
        return f"File: {path}\n(error reading metadata: {e})\n"


def discover_tab_plugins() -> list:
    """
    Find all *_tab.py modules beside the application (and cwd).
    Returns list of loaded modules that have an onload callable.
    """
    import importlib.util
    search_dirs = []
    # directory containing main.py / this package
    try:
        app_dir = Path(__file__).resolve().parent
        search_dirs.append(app_dir)
    except Exception:
        pass
    cwd = Path.cwd()
    if cwd not in search_dirs:
        search_dirs.append(cwd)

    modules = []
    seen = set()
    for d in search_dirs:
        for py in sorted(d.glob("*_tab.py")):
            key = str(py.resolve())
            #debug(9, key)
            #print("key " + key)
            if key in seen:
                continue
            # skip our own editor_tab / debug_tab implementation files if they
            # don't define a plugin-style onload (they will simply be ignored)
            seen.add(key)
            try:
                spec = importlib.util.spec_from_file_location(py.stem, py)
                if spec is None or spec.loader is None:
                    debug(5, "key " + key + " no spec")
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if callable(getattr(mod, "onload", None)):
                    #debug(9, type(mod))
                    debug(5, f"mod '{mod.__name__} callable")
                    modules.append(mod)
                else:
                    debug(5, f"mod '{mod.__name__}' !onload")
            except Exception as exc:
                debug(5, f"key '{key}' !callable: {exc}")
                continue
    debug(5, f"found {len(modules)} modules")
    return sorted(modules, key=lambda m: m.__name__)


def run_onload_plugins(filepath: str) -> Optional[str]:
    """
    Call onload(filepath) on every *_tab.py plugin.
    Return the first non-false result (expected to be a string to display),
    or None if no plugin claims the file.
    """
    for mod in discover_tab_plugins():
        try:
            debug(5, "run " + mod.__name__)
            result = mod.onload(filepath)
            if result:
                return str(result)
        except Exception as exc:
            debug(5, f"run {mod.__name__}? {exc}")
            continue
    return None

#def debug(level: int, msg: str) -> None:
def debug(level: int, *args, **kwargs):
    """
    Thin wrapper so utils (and anyone) can log without importing debug_tab
    at module load time (avoids circular imports).
    """
    kwargs['depth'] = kwargs.get('depth', 0) + 1  #show my caller
    try:
        from debug_tab import debug as _debug
        return _debug(level, msg, **kwargs)
    except Exception:
        # debug system not loaded yet, or logging disabled – ignore
        pass

#eof