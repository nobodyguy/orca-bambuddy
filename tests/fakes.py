"""Test doubles: a scriptable HTTP server standing in for Bambuddy (and Cloudflare), a
recording slicer host, and a helper that writes multi-plate .gcode.3mf files."""

import json
import os
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class FakeServer:
    """routes: {(method, path): fn(request) -> (status, headers, body)}; body may be a dict."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                # self.headers is an email.message.Message: lookups ignore case, like HTTP.
                req = {"method": self.command, "path": self.path, "headers": self.headers, "body": body}
                server.requests.append(req)
                fn = server.routes.get((self.command, path))
                status, headers, payload = fn(req) if fn else (404, {}, {"detail": "Not Found"})
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode()
                    headers = {"Content-Type": "application/json", **headers}
                elif isinstance(payload, str):
                    payload = payload.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:  # the client gave up (cancelled upload, timeout test)
                    pass

            do_GET = do_POST = do_DELETE = do_PATCH = _handle

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, method, path, status=200, body=None, headers=None):
        self.routes[(method, path)] = lambda req: (status, headers or {}, {} if body is None else body)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def bambuddy_routes(server, *, printers=None, statuses=None, permissions=None, auth_enabled=True):
    """A minimal Bambuddy 1.2.5.7 with an upload counter and a queue."""
    printers = printers or [
        {"id": 1, "name": "X1C Office", "model": "X1C", "serial_number": "00M01", "location": "Office", "is_active": True},
        {"id": 2, "name": "X1C Lab", "model": "X1C", "serial_number": "00M02", "location": "Lab", "is_active": True},
        {"id": 3, "name": "A1 mini", "model": "A1 Mini", "serial_number": "03A01", "location": "Desk", "is_active": True},
    ]
    statuses = statuses or {p["id"]: {"id": p["id"], "connected": True, "state": "IDLE"} for p in printers}
    perms = permissions if permissions is not None else [
        "printers:read", "queue:read", "camera:view", "library:upload", "queue:create", "library:read",
    ]
    server.route("GET", "/api/v1/updates/version", body={"version": "1.2.5.7", "repo": "maziggy/bambuddy"})
    server.route("GET", "/api/v1/auth/status", body={"auth_enabled": auth_enabled, "requires_setup": False})
    server.route("GET", "/api/v1/auth/me", body={"username": "admin", "permissions": perms})
    server.route("GET", "/api/v1/printers/", body=printers)
    for pid, st in statuses.items():
        server.route("GET", f"/api/v1/printers/{pid}/status", body=st)
    state = {"files": {}, "queue": {}}

    def upload(req):
        fid = len(state["files"]) + 1
        state["files"][fid] = req["body"]
        return 200, {}, {"id": fid, "filename": "x", "file_type": "gcode.3mf", "file_size": len(req["body"]), "duplicate_of": None}

    def add_queue(req):
        data = json.loads(req["body"])
        qid = len(state["queue"]) + 1
        state["queue"][qid] = data
        return 200, {}, {"id": qid, "status": "pending", "printer_id": data.get("printer_id"), **data}

    server.routes[("POST", "/api/v1/library/files")] = upload
    server.routes[("POST", "/api/v1/queue/")] = add_queue
    return state


class FakeHost:
    def __init__(self):
        self.page, self.notes, self.urls, self.windows = [], [], [], []

    def post_page(self, message):
        self.page.append(message)

    def notify(self, level, text, link_text="", on_click=None):
        self.notes.append((level, text))

    def open_url(self, url):
        self.urls.append(url)

    def last(self, kind):
        return next((m for m in reversed(self.page) if m.get("type") == kind), None)


def make_sliced_3mf(path, plates=(1,), printer_model="Bambu Lab X1 Carbon", model_id="", preset="Bambu Lab X1 Carbon 0.4 nozzle"):
    with zipfile.ZipFile(path, "w") as zf:
        for i in plates:
            zf.writestr(f"Metadata/plate_{i}.gcode", f"; plate {i}\nG28\n")
        plate_xml = "".join(
            f'<plate><metadata key="index" value="{i}"/><metadata key="printer_model_id" value="{model_id}"/>'
            f'<metadata key="prediction" value="{600 * i}"/><metadata key="weight" value="{1.5 * i}"/></plate>'
            for i in plates
        )
        zf.writestr("Metadata/slice_info.config", f'<?xml version="1.0"?><config>{plate_xml}</config>')
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps({"printer_model": printer_model, "printer_settings_id": preset, "nozzle_diameter": ["0.4"]}),
        )
    return path
