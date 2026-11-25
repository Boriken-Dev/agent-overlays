"""Logging proxy to Google storage for measuring what the Flutter tool downloads.

Usage: $AGENTS_PYTHON -I $RELEASE_OVERLAY_ROOT/flutter/flutter_logproxy.py PORT LOGFILE, then run flutter with
FLUTTER_STORAGE_BASE_URL=http://127.0.0.1:PORT; each request is logged as METHOD, status, size, path."""
import sys, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
UP = "https://storage.googleapis.com"
LOG = open(sys.argv[2], "a", buffering=1)
lock = threading.Lock()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do(self, method):
        req = urllib.request.Request(UP + self.path, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=120)
            code, hdrs = r.status, r.headers
        except urllib.error.HTTPError as e:
            r, code, hdrs = e, e.code, e.headers
        n = 0
        self.send_response(code)
        for k in ("Content-Type", "Content-Length"):
            if hdrs.get(k): self.send_header(k, hdrs[k])
        self.send_header("Connection", "close")
        self.end_headers()
        if method == "GET":
            while True:
                b = r.read(1 << 20)
                if not b: break
                n += len(b)
                self.wfile.write(b)
        with lock:
            LOG.write(f"{method}\t{code}\t{hdrs.get('Content-Length','')}\t{self.path}\n")
    def do_GET(self): self.do("GET")
    def do_HEAD(self): self.do("HEAD")

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
