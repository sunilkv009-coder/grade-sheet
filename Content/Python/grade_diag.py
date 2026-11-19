"""Grade Sheet - diagnostics: what was going on around Unreal when a capture got stuck.

Used by Unreal (grade_panel.py) and by the window's helper (grade_ui.py). Nothing here imports unreal, so the helper,
which keeps running even when Unreal stops ticking, can look at Unreal from the outside.

    foreground()        the window in front right now (Windows), and whether it is Unreal
    gpu_summary()       GPU load, memory and the processes using it (nvidia-smi), e.g. ComfyUI next to Unreal
    unreal_log_tail()   the last lines of Unreal's own log (Saved/Logs/<project>.log)
    bundle()            everything above plus Grade Sheet's own files, zipped for sending
"""
import os
import sys
import glob
import time
import zipfile
import platform
import subprocess

NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # do not flash a console window


def foreground():
    """(title, pid, is_unreal) of the window in front, or None where that cannot be read."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = buf.value
        return title, pid.value, "Unreal Editor" in title
    except Exception:
        return None


def foreground_text(unreal_pid=None):
    f = foreground()
    if not f:
        return "unknown"
    title, pid, is_ue = f
    if unreal_pid:
        is_ue = pid == unreal_pid
    return "%s'%s' (pid %d)" % ("UNREAL " if is_ue else "", title[:80], pid)


def run(cmd, timeout=6):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return (out.stdout or out.stderr or "").strip()
    except Exception as e:
        return "(%s: %s)" % (cmd[0], e)


def gpu_summary(full=False):
    """One line per GPU (load, memory), plus the processes using it. full=True: nvidia-smi's whole report."""
    if full:
        return run(["nvidia-smi"])
    gpus = run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader"])
    procs = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"])
    return "GPU: %s | compute processes: %s" % (gpus.replace("\n", " ; "), procs.replace("\n", " ; ") or "none")


def unreal_log(cap_dir):
    """The newest log in <project>/Saved/Logs (cap_dir is <project>/Saved/GradeCaptures)."""
    logs = [f for f in glob.glob(os.path.join(os.path.dirname(cap_dir), "Logs", "*.log")) if "backup" not in f.lower()]
    return max(logs, key=os.path.getmtime) if logs else None


def unreal_log_tail(cap_dir, lines=60, only=None):
    """The last lines of Unreal's log; only = words, keeps the lines with one of them (case ignored)."""
    path = unreal_log(cap_dir)
    if not path:
        return "(no Unreal log found in Saved/Logs)"
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2000000))
            text = f.read().decode("utf-8", "replace").splitlines()
        if only:
            words = [w.lower() for w in only]
            text = [l for l in text if any(w in l.lower() for w in words)]
        return "\n".join(text[-lines:]) or "(nothing matching)"
    except OSError as e:
        return "(could not read %s: %s)" % (path, e)


STALL_WORDS = ["screenshot", "highres", "error", "warning", "slate", "python", "grade_panel", "gpu", "d3d", "crash",
               "hitch", "shader", "timeout", "out of memory"]


def system_text(cap_dir):
    parts = ["Grade Sheet diagnostics, %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "platform: %s | python %s" % (platform.platform(), sys.version.split()[0]),
             "window in front: %s" % foreground_text(),
             "", "== nvidia-smi ==", gpu_summary(full=True)]
    if os.name == "nt":
        parts += ["", "== processes (tasklist, biggest memory first) =="]
        rows = run(["tasklist", "/fo", "csv", "/nh"], timeout=15).splitlines()

        def mem(r):
            try:
                return int(r.rsplit('","', 1)[-1].strip('"').replace(",", "").replace(".", "").split()[0])
            except Exception:
                return 0
        parts += sorted(rows, key=mem, reverse=True)[:40]
    return "\n".join(parts)


def bundle(cap_dir):
    """Zip of everything needed to see why a capture got stuck. Returns its path."""
    project = os.path.dirname(os.path.dirname(cap_dir))
    out = os.path.join(cap_dir, "GradeSheet_diagnostics_%s.zip" % time.strftime("%Y%m%d_%H%M%S"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("GradeSheet.log", "GradeSheet.old.log", "state.json", "shapes.json"):
            p = os.path.join(cap_dir, name)
            if os.path.exists(p):
                z.write(p, name)
        p = os.path.join(project, "Config", "GradeSheetSetups.json")
        if os.path.exists(p):
            z.write(p, "GradeSheetSetups.json")
        z.writestr("UnrealLog_tail.txt", unreal_log_tail(cap_dir, lines=4000))
        z.writestr("system.txt", system_text(cap_dir))
        z.writestr("pictures.txt", "\n".join("%s  %d bytes  %s" % (os.path.basename(f), os.path.getsize(f),
                                             time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(f))))
                                             for f in sorted(glob.glob(os.path.join(cap_dir, "*.png")))))
    return out


def show_file(path):
    """Open Explorer (or the platform's file manager) with the file selected."""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass
