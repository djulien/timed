"""
audio_tab.py plugin – shows audio waveform + timing marks.

setup:
pip install tinytag
"""


from pathlib import Path
from utils import debug

AUDIO_EXTS = {".mp3", ".mp4", ".wav"}

def onload(filepath: str, canvas=None, text=None, tab=None):
    p = Path(filepath)
    if p.suffix.lower() not in AUDIO_EXTS:
        debug(1, "{red}", p.resolve(), f"'{p.suffix.lower()}' not audio")
        return False   # not handled

    descr = (
        f"{{cyan}}[audio_tab]\n"
        f"{{blue}}Audio: {{cyan}}{p.name}\n"
        f"{{blue}}Path:  {p.resolve()}\n"
    )

    try:
#        __import__(pkg.replace("-", "_").split("[")[0])
        from tinytag import TinyTag  #supports wav, mp3, mp4
    except ImportError as exc:
        debug(1, f"{{red}}tinytag error: {exc}")
    tags = TinyTag.get(p.resolve())
    debug(1, "{green}", p.resolve(), "audio", tags.title)
    try:
        minutes, seconds = divmod(tags.duration, 60)
        msec = int((seconds - int(seconds)) * 1000)
        descr += (
            f"{{blue}}Title: {{cyan}}{tags.title}\n"
            f"{{blue}}Artist: {{cyan}}{tags.artist}\n"
            f"{{blue}}Duration: {{cyan}}{int(minutes)}:{int(seconds)}.{int(msec):03d}\n"  # {tags.duration:.2f}
        )

        if text is not None:
            text.delete("1.0", "end")
#            text.insert("1.0", descr)
            from utils import insert_styled_text
            insert_styled_text(text, descr)  #use styled text

    except Exception as exc:
        debug(1, f"{{red}}audio error: {exc}")

    return True

#eof