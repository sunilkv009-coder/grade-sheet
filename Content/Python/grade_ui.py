"""Grade Contact Sheet - window process (runs OUTSIDE Unreal).
Started by grade_panel.py. Serves grade_ui.html on 127.0.0.1 and shows it in a
chromeless Edge/Chrome app window. Talks to Unreal through JSON files in
<project>/Saved/GradeCaptures (state.json in, cmd/*.json out).
"""
import os, sys, json, time, threading, subprocess, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAP_DIR = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
CMD_DIR = os.path.join(CAP_DIR, "cmd")
HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(CMD_DIR, exist_ok=True)
sys.path.insert(0, HERE)
import grade_log as glog
glog.setup(CAP_DIR, "window")
PORT = 8765
started = time.time()
last_ping = [0.0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):          # pythonw has no console
        pass

    def send_bytes(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self.send_bytes(f.read(), ctype)
        except Exception:
            self.send_bytes(b"not found", "text/plain", 404)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send_file(os.path.join(HERE, "grade_ui.html"), "text/html; charset=utf-8")
        elif path == "/state":
            self.send_file(os.path.join(CAP_DIR, "state.json"), "application/json")
        elif path == "/shapes":
            self.send_file(os.path.join(CAP_DIR, "shapes.json"), "application/json")
        elif path == "/ping":
            last_ping[0] = time.time()
            self.send_bytes(b"ok", "text/plain")
        elif path.startswith("/img/"):
            name = os.path.basename(path)
            if name.lower().endswith(".png"):
                self.send_file(os.path.join(CAP_DIR, name), "image/png")
            else:
                self.send_bytes(b"not found", "text/plain", 404)
        else:
            self.send_bytes(b"not found", "text/plain", 404)

    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/log":                         # a message from the page (errors, lost connection)
                d = json.loads(body.decode("utf-8") or "{}")
                glog.write(str(d.get("level", "INFO"))[:5].upper(), str(d.get("msg", ""))[:4000], "page")
                return self.send_bytes(b"ok", "text/plain")
            if self.path == "/openlog":                     # the window's Log button
                ok = glog.open_in_editor()
                return self.send_bytes(b"ok" if ok else b"no log yet", "text/plain")
            if self.path == "/shapes":                      # the drawn shapes, kept so they can be edited later
                with open(os.path.join(CAP_DIR, "shapes.json"), "wb") as f:
                    f.write(body)
                return self.send_bytes(b"ok", "text/plain")
            if self.path.startswith("/ref/"):               # reference image (PNG) for one camera of one setup
                rid = "".join(ch for ch in self.path[5:] if ch.isalnum() or ch in "_-")
                with open(os.path.join(CAP_DIR, "ref_%s.png" % rid), "wb") as f:
                    f.write(body)
                return self.send_bytes(b"ok", "text/plain")
            if self.path.startswith("/mask/"):              # rasterised shape mask (PNG) for one camera
                cam = "".join(ch for ch in self.path[6:] if ch.isalnum() or ch in "_-")
                tmp = os.path.join(CAP_DIR, "mask_%s.png.tmp" % cam)
                with open(tmp, "wb") as f:
                    f.write(body)
                os.replace(tmp, os.path.join(CAP_DIR, "mask_%s.png" % cam))
                cmd = {"op": "mask", "cam": cam}
            elif self.path == "/cmd":
                cmd = json.loads(body.decode("utf-8"))
                if cmd.get("op") not in ("set", "shapes", "preview"):
                    glog.info("sent to Unreal: %s" % cmd.get("op"))
            else:
                return self.send_bytes(b"not found", "text/plain", 404)
            name = "%d.json" % time.time_ns()
            tmp = os.path.join(CMD_DIR, name + ".tmp")
            with open(tmp, "w") as f:
                json.dump(cmd, f)
            os.replace(tmp, os.path.join(CMD_DIR, name))
            self.send_bytes(b"ok", "text/plain")
        except Exception as e:
            glog.error("request %s failed" % self.path)
            self.send_bytes(str(e).encode("utf-8"), "text/plain", 400)


class Server(ThreadingHTTPServer):
    request_queue_size = 64             # the page asks for every picture at once; the default of 5 can refuse some


def watchdog():
    """Quit when the window has been closed (no ping for a while)."""
    while True:
        time.sleep(2)
        now = time.time()
        if (last_ping[0] and now - last_ping[0] > 60) or (not last_ping[0] and now - started > 60):
            glog.info("window closed (no ping for %d s); helper stops" % (now - (last_ping[0] or started)))
            os._exit(0)


def open_window(url):
    pf = [os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
          os.path.join(os.environ.get("LOCALAPPDATA", ""), "")]
    exes = [os.path.join(p, r"Microsoft\Edge\Application\msedge.exe") for p in pf] + \
           [os.path.join(p, r"Google\Chrome\Application\chrome.exe") for p in pf]
    for exe in exes:
        if os.path.exists(exe):
            glog.info("window opened with " + exe)
            subprocess.Popen([exe, "--app=" + url, "--window-size=1760,1000"])
            return
    glog.warn("Edge / Chrome not found; opening the default browser")
    webbrowser.open(url)


if __name__ == "__main__":
    try:
        try:
            server = Server(("127.0.0.1", PORT), Handler)
        except OSError:
            server = Server(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        glog.info("helper started on port %d (Python %s)" % (port, sys.version.split()[0]))
        with open(os.path.join(CAP_DIR, "ui_port.txt"), "w") as f:
            f.write(str(port))
        threading.Thread(target=watchdog, daemon=True).start()
        if "--no-window" not in sys.argv:
            open_window("http://127.0.0.1:%d/" % port)
        server.serve_forever()
    except Exception:
        glog.error("helper crashed")
        raise
