"""A reverse proxy for the desktop scenario that injects upload faults.

It forwards plain HTTP to the TLS test server, rewrites the server's URLs in headers and
JSON/XML bodies so the client keeps talking through the proxy, and:

- corrupts one byte of the first tus creation-with-upload body of CORRUPT_NAME, once,
  so the server must reject that upload with 460,
- drops RESETS PATCH connections after reading their body (the server never sees them),
- forwards one PATCH completely to the server but holds back the response for
  STALL_SECONDS, so the client hits its HTTP timeout while the server already stored the chunk.
"""

import base64
import http.client
import http.server
import socketserver
import ssl
import threading
import time
import urllib.parse

UPSTREAM_HOST, UPSTREAM_PORT = "opencloud.test", 9200
TLS = ssl._create_unverified_context()
HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}


class Chaos:
    def __init__(self, public_base, corrupt_name, resets=2, stall_seconds=330):
        self.public_base = public_base.rstrip("/")
        self.upstream_base = f"https://{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        self.corrupt_name = corrupt_name
        self.resets_left = resets
        self.stall_left = 1 if stall_seconds else 0
        self.stall_seconds = stall_seconds
        self.corrupted = False
        self.lock = threading.Lock()
        self.events = []
        self.statuses = []  # (method, path, status)

    def event(self, msg):
        with self.lock:
            self.events.append(f"{time.strftime('%H:%M:%S')} {msg}")


def make_handler(chaos: Chaos):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _forward(self):
            body = self._body()
            method = self.command
            meta = self.headers.get("Upload-Metadata", "")
            filename = ""
            for pair in meta.split(","):
                k, _, v = pair.strip().partition(" ")
                if k == "filename" and v:
                    filename = base64.b64decode(v).decode(errors="replace")

            # fault: corrupt one byte of the creation-with-upload body of one file, once
            # the desktop client sends the full remote path as filename
            if method == "POST" and filename.rsplit("/", 1)[-1] == chaos.corrupt_name and len(body) > 100:
                with chaos.lock:
                    do = not chaos.corrupted
                    chaos.corrupted = True
                if do:
                    body = body[:100] + bytes([body[100] ^ 0xFF]) + body[101:]
                    chaos.event(f"corrupted byte 100 of the POST body for {filename}")

            # fault: reset a PATCH connection half way through
            if method == "PATCH":
                with chaos.lock:
                    reset = chaos.resets_left > 0 and len(body) > 1024 * 1024
                    if reset:
                        chaos.resets_left -= 1
                if reset:
                    chaos.event(f"dropped the connection of PATCH {self.path[:60]} after reading its body")
                    self.close_connection = True
                    self.connection.close()
                    return

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "host"}
            headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
            if body:
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT, context=TLS, timeout=900)
            conn.request(method, self.path, body=body if body or method in ("POST", "PUT", "PATCH") else None, headers=headers)
            res = conn.getresponse()
            data = res.read()
            conn.close()
            with chaos.lock:
                chaos.statuses.append((method, self.path, res.status, filename))
            if res.status == 460:
                chaos.event(f"server answered 460 to {method} {filename or self.path[:60]}")

            # fault: the server stored the chunk, but the client never hears about it in time
            if method == "PATCH" and res.status == 204:
                with chaos.lock:
                    stall = chaos.stall_left > 0
                    if stall:
                        chaos.stall_left -= 1
                if stall:
                    chaos.event(f"stalling the response to a completed PATCH for {chaos.stall_seconds}s")
                    time.sleep(chaos.stall_seconds)

            ctype = res.getheader("Content-Type", "")
            if any(t in ctype for t in ("json", "xml", "html", "text")):
                data = data.replace(chaos.upstream_base.encode(), chaos.public_base.encode())
            try:
                # send_response() would add a second Date and Server header; pass the upstream ones
                self.send_response_only(res.status)
                for k, v in res.getheaders():
                    if k.lower() in HOP:
                        continue
                    if k.lower() == "location":
                        v = v.replace(chaos.upstream_base, chaos.public_base)
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_PUT = do_PATCH = do_HEAD = do_DELETE = do_OPTIONS = _forward
        do_PROPFIND = do_PROPPATCH = do_MKCOL = do_MOVE = do_COPY = do_LOCK = do_UNLOCK = do_REPORT = do_SEARCH = _forward

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(chaos: Chaos, port=8080):
    srv = Server(("0.0.0.0", port), make_handler(chaos))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
