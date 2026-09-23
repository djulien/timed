"""audio_tab.py plugin – shows audio waveform + timing marks."""

from pathlib import Path
from debug_tab import debug

AUDIO_EXTS = {".mp3", ".mp4", ".wav"}

def onload(filepath: str):
    p = Path(filepath)
    if p.suffix.lower() not in AUDIO_EXTS:
        debug(1, p.resolve(), "not audio")
        return False   # not handled

    from tinytag import TinyTag  #supports wav, mp3, mp4
    tags = TinyTag.get(p.resolve())
    debug(1, p.resolve(), "audio", tags.title)
    minutes, seconds = divmod(tags.duration, 60)
    msec = int((seconds - int(seconds)) * 1000)
    return (
        #f"[audio_tab plugin]\n"
        f"Audio file: {p.name}\n"
        f"Title: {tags.title}\n"
        f"Artist: {tags.artist}\n"
        f"Duration: {minutes}:{seconds:02d}.{msec:03d}\n"  # {tags.duration:.2f}
        f"Full path: {p.resolve()}\n"
        #f"(Graphical preview can be drawn in the top panel later.)\n"
    )

#eof