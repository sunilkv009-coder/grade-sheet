"""Grade Sheet - one log file for the whole tool, so a problem can be traced afterwards.

    <Project>/Saved/GradeCaptures/GradeSheet.log      (the previous one is kept as GradeSheet.old.log)

Unreal, the window's helper process and the page itself all write to it, each line tagged with where it
came from:  [2026-11-16 14:10:03.120] [unreal ] INFO  Capture started: 4 setups
Open it from Grade Sheet > Open log, or with the Log button in the window.
"""
import os
import sys
import time
import traceback

MAX_BYTES = 5 * 1024 * 1024
PATH = None
SOURCE = "?"


def setup(cap_dir, source, rotate=False):
    """Point the log at <cap_dir>/GradeSheet.log. rotate=True starts a fresh file when the old one is large."""
    global PATH, SOURCE
    os.makedirs(cap_dir, exist_ok=True)
    PATH, SOURCE = os.path.join(cap_dir, "GradeSheet.log"), source
    if rotate:
        try:
            if os.path.getsize(PATH) > MAX_BYTES:
                old = os.path.join(cap_dir, "GradeSheet.old.log")
                if os.path.exists(old):
                    os.remove(old)
                os.replace(PATH, old)
        except OSError:
            pass
    return PATH


def write(level, msg, source=None):
    if not PATH:
        return
    t = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".%03d" % int(t % 1 * 1000)
    lines = str(msg).rstrip().splitlines() or [""]
    text = "[%s] [%-7s] %-5s %s\n" % (stamp, (source or SOURCE)[:7], level, lines[0])
    text += "".join(" " * 40 + l + "\n" for l in lines[1:])
    try:
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def info(msg, source=None):
    write("INFO", msg, source)


def warn(msg, source=None):
    write("WARN", msg, source)


def error(msg, source=None, exc=True):
    """Logs msg, plus the current exception's traceback when called inside an except block."""
    if exc and sys.exc_info()[0] is not None:
        msg = "%s\n%s" % (msg, traceback.format_exc())
    write("ERROR", msg, source)


def open_in_editor():
    """Open the log in the system's text editor."""
    if not PATH or not os.path.exists(PATH):
        return False
    if sys.platform.startswith("win"):
        os.startfile(PATH)
    else:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", PATH])
    return True
