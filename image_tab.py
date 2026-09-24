"""
image_tab.py – plugin for the multi-tab editor.

If the opened file is an image, draw it in the graphical (Canvas) panel
and put a short description in the text area.

onload() is discovered automatically because this file matches *_tab.py.

setup:
pip install Pillow  #for optional jpeg support
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from utils import debug

IMAGE_EXTS = {".png", ".gif", ".pgm", ".ppm", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}

# Formats Tk PhotoImage can load without Pillow (Tk 8.6+)
_TK_NATIVE = {".png", ".gif", ".pgm", ".ppm"}


def onload(filepath: str, canvas=None, text=None, tab=None):
    """
    Return a non-false value to claim this file.
    When canvas is provided, draw the image into the graphical panel.
    """
    p = Path(filepath)
    ext = p.suffix.lower()
    if ext not in IMAGE_EXTS:
        debug(1, f"{{red}}{p.resolve()} '{p.suffix.lower()} not image")
        return False   # not handled

    descr = (
        f"{{cyan}}[image_tab]\n"
        f"{{blue}}Image: {{cyan}}{p.name}\n"
        f"{{blue}}Path:  {p.resolve()}\n"
    )

    if canvas is None:
        descr += f"{{red}}(No canvas available – image not drawn.)\n"
#        return text

    photo = _load_photo(filepath, canvas)
    if photo is None:
        descr += (
            f"{{red}}Could not load image into the graphical panel.\n"
            f"{{red}}PNG/GIF work with plain Tk; for JPEG/BMP/WebP install Pillow:\n"
            f"{{red}}  pip install Pillow\n"
        )
#        return text

    # Keep a reference on the tab (or canvas) so Tk does not garbage-collect it
    if tab is not None:
        tab._plugin_image = photo
    else:
        canvas._plugin_image = photo

    canvas.delete("plugin_image")

    def _paint(event=None, photo=photo, canvas=canvas):
        canvas.delete("plugin_image")
        cw = max(canvas.winfo_width(), 1)
        ch = max(canvas.winfo_height(), 1)
        iw, ih = photo.width(), photo.height()
        x = max(0, (cw - iw) // 2)
        y = max(0, (ch - ih) // 2)
        canvas.create_image(x, y, anchor="nw", image=photo, tags="plugin_image")

    canvas.bind("<Configure>", _paint, add="+")
    # Paint once after the canvas has a real size
    canvas.after(20, _paint)

    descr += f"{{green}}Displayed in graphical panel ({photo.width()}×{photo.height()} px).\n"
    if text is not None:
        text.delete("1.0", "end")
        from utils import insert_styled_text
#        text.insert("1.0", descr)
        insert_styled_text(text, descr)  #use styled text

    debug(1, f"{{green}}{p.resolve()} image {photo.width()}×{photo.height()} px")
    return True


def _load_photo(filepath: str, canvas):
    """Return a tk.PhotoImage, scaled to fit the canvas if needed."""
    import tkinter as tk

    path = str(filepath)
    ext = Path(path).suffix.lower()
    photo = None

    # 1. Native Tk formats
    if ext in _TK_NATIVE:
        try:
            photo = tk.PhotoImage(file=path, master=canvas)
        except Exception:
            photo = None

    # 2. Pillow for JPEG and other formats (optional, MIT/HPND)
    if photo is None:
        try:
            from PIL import Image, ImageTk  # type: ignore

            img = Image.open(path)
            # Fit inside a reasonable default; final fit happens via subsample below
            max_w, max_h = 1200, 800
            img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img, master=canvas)
            return photo
        except Exception:
            photo = None

    if photo is None:
        return None

    # Scale down large native PhotoImages (integer subsample only)
    try:
        cw = max(int(canvas.winfo_width()), 200)
        ch = max(int(canvas.winfo_height()), 100)
        iw, ih = photo.width(), photo.height()
        factor = 1
        while iw // factor > cw or ih // factor > ch:
            factor += 1
            if factor > 32:
                break
        if factor > 1:
            photo = photo.subsample(factor, factor)
    except Exception:
        pass

    return photo


#eof