"""Local relay for the Device tab camera (standard library only, no ``orca``).

OrcaSlicer plays a printer agent's camera URL with its own FFmpeg
(``wxMediaCtrl3::PlayFfmpeg``), which sends no request headers, and ``MediaPlayCtrl`` stops a
playing stream whenever the URL changes. So the agent hands out a fixed URL on 127.0.0.1, and
this relay opens Bambuddy's MJPEG stream through the plugin's own client for each viewer: with
the API key, Cloudflare Access and extra headers, the CA settings, and a fresh stream token.
See NOTES.md, "Device tab camera".
"""

from __future__ import annotations

# Imported while the plugin loads, not first on a plugin thread (see orca_adapter).
import http.server
import logging
import secrets
import socketserver
import threading
import urllib.parse

from .bambuddy import BambuddyError

RELAY_HOST = "127.0.0.1"
RELAY_ORIGIN = "http://" + RELAY_HOST  # what the plugin asks OrcaSlicer 2.5.0-beta to allow
CHUNK = 64 * 1024
DEFAULT_CONTENT_TYPE = "multipart/x-mixed-replace; boundary=frame"  # Bambuddy's
NO_PICTURE = "Bambuddy sent no picture. Check the printer's camera in Bambuddy; its log names the reason."

log = logging.getLogger("orca_bambuddy")


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    timeout = 0.5  # handle_request() returns this often, so serve() sees a stop

    def server_bind(self):
        # HTTPServer.server_bind looks up the FQDN of the address: a reverse lookup that
        # OrcaSlicer's audit reports, and that a loopback address does not need.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class CameraRelay:
    """Serves ``/<secret>/<printer id>`` on 127.0.0.1 for one viewer at a time.

    ``open_stream(printer_id)`` returns an open response, or raises BambuddyError;
    ``on_error(message)`` reports a stream that failed, ``on_open()`` one that delivers. The
    secret path keeps other local processes from reaching Bambuddy with the plugin's
    credentials.
    """

    def __init__(self, open_stream, stop: threading.Event, on_error=None, on_open=None):
        self.open_stream = open_stream
        self.stop = stop
        self.on_error = on_error
        self.on_open = on_open
        self.secret = secrets.token_urlsafe(16)
        self._server: _Server | None = None
        self._lock = threading.Lock()
        self._viewer = 0  # bumped per viewer; an older one stops relaying

    def bind(self) -> None:
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                relay._handle(self)

            def log_message(self, fmt, *args):
                pass  # the request line carries the secret

        self._server = _Server((RELAY_HOST, 0), Handler)  # any free port

    @property
    def ready(self) -> bool:
        return self._server is not None

    def close(self) -> None:
        if self._server is not None:
            self._server.server_close()

    def url(self, printer_id: int) -> str:
        return f"http://{RELAY_HOST}:{self._server.server_address[1]}/{self.secret}/{int(printer_id)}"

    def serve(self) -> None:
        """Thread body: accept viewers until ``stop`` is set."""
        try:
            while not self.stop.is_set():
                self._server.handle_request()
        finally:
            self.close()

    def _handle(self, handler) -> None:
        parts = urllib.parse.urlsplit(handler.path).path.strip("/").split("/")
        if len(parts) != 2 or not secrets.compare_digest(parts[0], self.secret) or not parts[1].isdigit():
            handler.send_error(404)
            return
        try:
            upstream = self.open_stream(int(parts[1]))
        except BambuddyError as e:
            if self.on_error:
                self.on_error(e.message)
            handler.send_error(502, "Bambuddy camera not available", e.message)
            return
        with self._lock:
            self._viewer += 1
            me = self._viewer
        relayed = 0
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", upstream.headers.get("Content-Type") or DEFAULT_CONTENT_TYPE)
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            # OrcaSlicer opens one stream; when it reconnects, the older one is let go.
            while me == self._viewer and not self.stop.is_set():
                chunk = upstream.read1(CHUNK)
                if not chunk:
                    break
                if not relayed and self.on_open:
                    self.on_open()
                relayed += len(chunk)
                handler.wfile.write(chunk)
            else:
                return  # superseded by a newer viewer, or the plugin is stopping
            if not relayed and self.on_error:
                # Bambuddy answers 200 before it reaches the camera, and ends the stream
                # without a frame when the camera fails (its log names the reason).
                self.on_error(NO_PICTURE)
        except (OSError, ValueError) as e:  # the viewer left, or Bambuddy stopped sending
            log.debug("Camera relay ended: %s", e)
        finally:
            upstream.close()
