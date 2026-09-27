"""Local HTTPS-free test server with Range support and injectable failures."""
import hashlib
import http.server
import threading


class State:
    def __init__(self):
        self.files = {}          # path -> bytes
        self.cut_after = {}      # path -> bytes to send before dropping the connection (once)
        self.ignore_range = False
        self.requests = []


def make_server(state: State):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state.requests.append((self.path, self.headers.get("Range")))
            data = state.files.get(self.path)
            if data is None:
                self.send_error(404)
                return
            etag = '"' + hashlib.md5(data).hexdigest() + '"'
            start = 0
            rng = self.headers.get("Range")
            if rng and not state.ignore_range and (self.headers.get("If-Range") in (None, etag)):
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(data):
                    self.send_response(416); self.end_headers(); return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            else:
                self.send_response(200)
            body = data[start:]
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.end_headers()
            cut = state.cut_after.pop(self.path, None)
            if cut is not None:
                self.wfile.write(body[:cut])
                self.wfile.flush()
                self.connection.shutdown(2)      # simulate a dropped connection
                return
            for i in range(0, len(body), 65536):
                self.wfile.write(body[i:i + 65536])

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class FakeRun:
    """Stands in for subprocess.run for pip / python probes."""
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)
        self.installed = set()

    def __call__(self, argv):
        import json
        from types import SimpleNamespace
        self.calls.append(argv)
        if "-c" in argv and argv[-1].startswith("{"):          # dependency check
            names = json.loads(argv[-1])
            return SimpleNamespace(returncode=0, stdout=json.dumps({p: p in self.installed for p in names}), stderr="")
        if "install" in argv and "pip" in argv:
            pkg = argv[-1]
            if pkg in self.fail:
                return SimpleNamespace(returncode=1, stdout="", stderr=f"ERROR: No matching distribution found for {pkg}")
            self.installed.add(pkg)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
