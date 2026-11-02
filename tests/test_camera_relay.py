"""The local camera relay the Device tab plays from."""

import threading
import time
import unittest
import urllib.error
import urllib.request

from fakes import FakeServer

from orca_bambuddy.bambuddy import BambuddyClient, Connection
from orca_bambuddy.camera_relay import NO_PICTURE, CameraRelay

MJPEG = "multipart/x-mixed-replace; boundary=frame"
FRAMES = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8one\xff\xd9\r\n--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8two\xff\xd9\r\n"


class EndlessStream:
    """An upstream that never ends by itself, like a live camera."""

    headers = {"Content-Type": MJPEG}

    def __init__(self):
        self.closed = threading.Event()

    def read1(self, n):
        time.sleep(0.02)
        return b"--frame\r\n"

    def close(self):
        self.closed.set()


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.route("POST", "/api/v1/printers/camera/stream-token", body={"token": "t1"})
        self.server.route("GET", "/api/v1/printers/1/camera/stream", body=FRAMES, headers={"Content-Type": MJPEG})
        self.client = BambuddyClient(Connection(self.server.url, api_key="k", timeout=5))
        self.errors, self.opened = [], []
        self.stop = threading.Event()
        self.open_stream = lambda pid: self.client.open_camera_stream(pid)
        self.relay = CameraRelay(lambda pid: self.open_stream(pid), self.stop, self.errors.append, lambda: self.opened.append(1))
        self.relay.bind()
        self.thread = threading.Thread(target=self.relay.serve, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        self.server.close()

    def test_passes_the_stream_through(self):
        url = self.relay.url(1)
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        with urllib.request.urlopen(url, timeout=5) as resp:
            self.assertEqual(resp.headers["Content-Type"], MJPEG)
            self.assertEqual(resp.read(), FRAMES)
        self.assertEqual(self.server.requests[-1]["path"], "/api/v1/printers/1/camera/stream?token=t1&fps=10")
        self.assertEqual((self.errors, self.opened), ([], [1]))

    def test_the_url_is_stable(self):
        self.assertEqual(self.relay.url(1), self.relay.url(1))
        self.assertNotEqual(self.relay.url(1), self.relay.url(2))

    def test_unknown_paths_are_refused(self):
        port = self.relay.url(1).split(":")[2].split("/")[0]
        for path in ("/1", "/wrong/1", f"/{self.relay.secret}/x", f"/{self.relay.secret}/1/2"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
            self.assertEqual(e.exception.code, 404)
        self.assertFalse(any("/camera/stream" in r["path"] for r in self.server.requests))

    def test_bambuddy_error_becomes_502_and_is_reported(self):
        self.server.route("GET", "/api/v1/printers/1/camera/stream", status=503, body={"detail": "Camera not reachable"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.relay.url(1), timeout=5)
        self.assertEqual(e.exception.code, 502)
        self.assertEqual(self.errors, ["Camera not reachable"])

    def test_a_stream_that_ends_without_a_picture_is_reported(self):
        # What Bambuddy does when the camera fails after the response has started.
        self.server.route("GET", "/api/v1/printers/1/camera/stream", body=b"", headers={"Content-Type": MJPEG})
        with urllib.request.urlopen(self.relay.url(1), timeout=5) as resp:
            self.assertEqual(resp.read(), b"")
        self.assertEqual((self.errors, self.opened), ([NO_PICTURE], []))

    def test_a_new_viewer_lets_the_old_upstream_go(self):
        streams = []

        def endless(pid):
            streams.append(EndlessStream())
            return streams[-1]

        self.open_stream = endless
        first = urllib.request.urlopen(self.relay.url(1), timeout=5)
        first.read(9)
        second = urllib.request.urlopen(self.relay.url(1), timeout=5)
        second.read(9)
        self.assertTrue(streams[0].closed.wait(2))
        self.assertFalse(streams[1].closed.is_set())
        first.close()
        self.stop.set()
        self.assertTrue(streams[1].closed.wait(2))
        second.close()

    def test_stop_ends_serving(self):
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
