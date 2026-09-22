"""Example *_tab.py plugin – shows a note for image files."""

from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

def onload(filepath: str):
    p = Path(filepath)
    if p.suffix.lower() in IMAGE_EXTS:
        return (
            f"[image_tab plugin]\n"
            f"Image file: {p.name}\n"
            f"Full path: {p.resolve()}\n"
            f"(Graphical preview can be drawn in the top panel later.)\n"
        )
    return False   # not handled

#eof