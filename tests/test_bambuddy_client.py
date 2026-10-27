import json
import os
import tempfile
import time
import unittest

from fakes import FIXTURES, FakeServer, bambuddy_routes

from orca_bambuddy.bambuddy import BambuddyClient, BambuddyError, Connection, normalize_base_url, parse_extra_headers
from orca_bambuddy.service import test_connection as run_connection_test

KEY = "bb_secret_key_value"
CF_ID, CF_SECRET = "cf-id.access", "cf-secret-value"


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.state = bambuddy_routes(self.server)

    def tearDown(self):
        self.server.close()

    def client(self, **kw):
        return BambuddyClient(Connection(self.server.url, api_key=kw.pop("api_key", KEY), timeout=5, **kw))

    def assertNoSecrets(self, text):
        for secret in (KEY, CF_SECRET):
            self.assertNotIn(secret, text)

    # -- URL handling and headers --

    def test_normalize_base_url(self):
        self.assertEqual(normalize_base_url("192.168.1.50:8000/"), "http://192.168.1.50:8000")
        self.assertEqual(normalize_base_url("https://b.example.com/api/v1/"), "https://b.example.com")
        self.assertEqual(normalize_base_url("  "), "")

    def test_api_key_header_and_no_cloudflare_headers_by_default(self):
        self.client().printers()
        headers = self.server.requests[-1]["headers"]
        self.assertEqual(headers.get("X-API-Key"), KEY)
        self.assertNotIn("CF-Access-Client-Id", headers)
        self.assertNotIn("CF-Access-Client-Secret", headers)

    def test_cloudflare_headers_on_every_request_when_configured(self):
        c = self.client(cf_client_id=CF_ID, cf_client_secret=CF_SECRET)
        c.version()  # public endpoint: no API key, but Cloudflare still needs the token
        c.printers()
        public, private = self.server.requests[-2]["headers"], self.server.requests[-1]["headers"]
        self.assertNotIn("X-API-Key", public)
        for h in (public, private):
            self.assertEqual(h.get("CF-Access-Client-Id"), CF_ID)
            self.assertEqual(h.get("CF-Access-Client-Secret"), CF_SECRET)

    def test_extra_headers_on_every_request(self):
        headers = (("X-Proxy-Token", "proxy-secret"), ("Authorization", "Basic dXNlcjpwYXNz"))
        c = self.client(extra_headers=headers)
        c.version()
        c.printers()
        for req in self.server.requests[-2:]:
            self.assertEqual(req["headers"].get("X-Proxy-Token"), "proxy-secret")
            self.assertEqual(req["headers"].get("Authorization"), "Basic dXNlcjpwYXNz")
        self.assertEqual(self.server.requests[-1]["headers"].get("X-API-Key"), KEY)

    def test_parse_extra_headers(self):
        headers, problems = parse_extra_headers("X-Proxy-Token: abc:def\n\n  Remote-User : jan \nbroken line\n"
                                                "X-API-Key: x\nX-Empty:\nBad Name: v\nx-proxy-token: again")
        self.assertEqual(headers, [("X-Proxy-Token", "abc:def"), ("Remote-User", "jan")])
        self.assertEqual(len(problems), 5)
        joined = " ".join(problems)
        self.assertIn("line 4", joined)
        self.assertIn("X-API-Key is set by the plugin", joined)
        self.assertIn("listed twice", joined)
        self.assertNotIn("abc", joined)  # values never appear in messages
        self.assertEqual(parse_extra_headers(""), ([], []))

    def test_proxy_login_page(self):
        self.server.route("GET", "/api/v1/printers/", status=401, body="<html>Authentik login</html>", headers={"Content-Type": "text/html"})
        with self.assertRaises(BambuddyError) as e:
            self.client().printers()
        self.assertEqual(e.exception.kind, "proxy")
        self.assertIn("Extra HTTP headers", e.exception.message)

    def test_proxy_redirect_mentions_extra_headers(self):
        self.server.route("GET", "/api/v1/updates/version", status=302, body="", headers={"Location": "https://auth.example.com/login"})
        with self.assertRaises(BambuddyError) as e:
            self.client().version()
        self.assertEqual(e.exception.kind, "bad_url")
        self.assertIn("Extra HTTP headers", e.exception.message)
        self.assertEqual(len(self.server.requests), 1)

    # -- failures users hit, each with its own message --

    def test_invalid_api_key(self):
        self.server.route("GET", "/api/v1/auth/me", status=401, body={"detail": "Not authenticated"})
        with self.assertRaises(BambuddyError) as e:
            self.client().me()
        self.assertEqual(e.exception.kind, "auth")
        self.assertNoSecrets(e.exception.message)

    def test_missing_permission_names_the_flag(self):
        self.server.route("POST", "/api/v1/queue/", status=403, body={"detail": "API key does not have 'can_queue' permission"})
        with self.assertRaises(BambuddyError) as e:
            self.client().add_to_queue(1, printer_id=1)
        self.assertEqual(e.exception.kind, "permission")
        self.assertIn("can_queue", e.exception.message)

    def test_wrong_url_404(self):
        self.server.routes.clear()
        with self.assertRaises(BambuddyError) as e:
            self.client().version()
        self.assertEqual(e.exception.kind, "bad_url")
        self.assertIn("No Bambuddy API", e.exception.message)

    def test_wrong_url_html_page(self):
        self.server.route("GET", "/api/v1/updates/version", body="<html>router login</html>", headers={"Content-Type": "text/html"})
        with self.assertRaises(BambuddyError) as e:
            self.client().version()
        self.assertEqual(e.exception.kind, "bad_url")

    def test_cloudflare_login_redirect_is_not_followed(self):
        login = "https://team.cloudflareaccess.com/cdn-cgi/access/login/bambuddy.example.com?redirect_url=%2F"
        self.server.route("GET", "/api/v1/updates/version", status=302, body="", headers={"Location": login})
        with self.assertRaises(BambuddyError) as e:
            self.client().version()
        self.assertEqual(e.exception.kind, "cloudflare")
        self.assertIn("service token", e.exception.message)
        self.assertEqual(len(self.server.requests), 1)  # the API key never went to the login host

    def test_cloudflare_rejects_service_token(self):
        page = "<html><title>Forbidden</title>Cloudflare Access denied</html>"
        self.server.route("GET", "/api/v1/printers/", status=403, body=page, headers={"Content-Type": "text/html", "CF-RAY": "8abc-FRA"})
        with self.assertRaises(BambuddyError) as e:
            self.client(cf_client_id=CF_ID, cf_client_secret=CF_SECRET).printers()
        self.assertEqual(e.exception.kind, "cloudflare")
        self.assertIn("rejected the service token", e.exception.message)
        self.assertNoSecrets(e.exception.message)

    def test_other_redirect_suggests_final_address(self):
        self.server.route("GET", "/api/v1/updates/version", status=301, body="", headers={"Location": "https://bambuddy.example.com/api/v1/updates/version"})
        with self.assertRaises(BambuddyError) as e:
            self.client().version()
        self.assertEqual(e.exception.kind, "bad_url")
        self.assertIn("https://bambuddy.example.com", e.exception.message)

    def test_too_large(self):
        self.server.route("POST", "/api/v1/library/files", status=413, body="<html>413 Request Entity Too Large</html>", headers={"Content-Type": "text/html"})
        with self.assertRaises(BambuddyError) as e:
            self.client().upload_library_file(os.path.join(FIXTURES, "cube_x1c.gcode.3mf"))
        self.assertEqual(e.exception.kind, "too_large")
        self.assertIn("100 MB", e.exception.message)

    def test_proxy_bad_gateway_vs_bambuddy_error(self):
        self.server.route("GET", "/api/v1/printers/", status=502, body="<html>Bad gateway</html>", headers={"Content-Type": "text/html"})
        with self.assertRaises(BambuddyError) as e:
            self.client().printers()
        self.assertEqual(e.exception.kind, "unreachable")
        self.server.route("GET", "/api/v1/printers/", status=503, body={"detail": "Failed to capture camera frame."})
        with self.assertRaises(BambuddyError) as e:
            self.client().printers()
        self.assertEqual((e.exception.kind, e.exception.message), ("server", "Failed to capture camera frame."))

    def test_connection_refused(self):
        url = self.server.url
        self.server.close()
        with self.assertRaises(BambuddyError) as e:
            BambuddyClient(Connection(url, timeout=3)).version()
        self.assertEqual(e.exception.kind, "unreachable")
        self.assertIn("refused", e.exception.message)
        self.server = FakeServer()

    def test_unknown_host(self):
        with self.assertRaises(BambuddyError) as e:
            BambuddyClient(Connection("http://no-such-host.invalid:8000", timeout=3)).version()
        self.assertEqual(e.exception.kind, "unreachable")

    def test_timeout(self):
        self.server.routes[("GET", "/api/v1/updates/version")] = lambda req: (time.sleep(2), (200, {}, {"version": "x"}))[1]
        with self.assertRaises(BambuddyError) as e:
            BambuddyClient(Connection(self.server.url, timeout=0.5)).version()
        self.assertEqual(e.exception.kind, "timeout")

    # -- upload and queue --

    def test_upload_is_multipart_with_progress(self):
        path = os.path.join(FIXTURES, "cube_x1c.gcode.3mf")
        seen = []
        resp = self.client().upload_library_file(path, "My plate.gcode.3mf", progress=lambda s, t: seen.append((s, t)), chunk_size=8192)
        self.assertEqual(resp["id"], 1)
        body = self.state["files"][1]
        self.assertIn(b'filename="My plate.gcode.3mf"', body)
        with open(path, "rb") as f:
            self.assertIn(f.read(), body)
        size = os.path.getsize(path)
        self.assertEqual(seen[-1], (size, size))
        self.assertGreater(len(seen), 3)

    def test_upload_can_be_cancelled(self):
        with self.assertRaises(BambuddyError) as e:
            self.client().upload_library_file(os.path.join(FIXTURES, "cube_x1c.gcode.3mf"), cancelled=lambda: True)
        self.assertEqual(e.exception.kind, "cancelled")

    def test_add_to_queue_payload(self):
        self.client().add_to_queue(5, printer_id=2, manual_start=True, plate_id=3, options={"timelapse": True, "ams_mapping": [0, -1]})
        sent = json.loads(self.server.requests[-1]["body"])
        self.assertEqual(sent, {"library_file_id": 5, "printer_id": 2, "manual_start": True, "plate_id": 3, "timelapse": True, "ams_mapping": [0, -1]})
        self.client().add_to_queue(5, target_model="X1C")
        sent = json.loads(self.server.requests[-1]["body"])
        self.assertEqual(sent, {"library_file_id": 5, "target_model": "X1C", "manual_start": False})

    def test_camera_token_is_reused_and_renewed_after_401(self):
        tokens = iter(["t1", "t2"])
        self.server.routes[("POST", "/api/v1/printers/camera/stream-token")] = lambda req: (200, {}, {"token": next(tokens)})
        self.server.route("GET", "/api/v1/printers/1/camera/snapshot", body=b"\xff\xd8jpeg", headers={"Content-Type": "image/jpeg"})
        c = self.client()
        self.assertEqual(c.camera_snapshot(1), b"\xff\xd8jpeg")
        c.camera_snapshot(1)
        snaps = [r["path"] for r in self.server.requests if "snapshot" in r["path"]]
        self.assertEqual(snaps, ["/api/v1/printers/1/camera/snapshot?token=t1"] * 2)
        self.server.route("GET", "/api/v1/printers/1/camera/snapshot", status=401, body={"detail": "Valid camera stream token required"})
        with self.assertRaises(BambuddyError):
            c.camera_snapshot(1)
        self.server.route("GET", "/api/v1/printers/1/camera/snapshot", body=b"ok", headers={"Content-Type": "image/jpeg"})
        c.camera_snapshot(1)
        self.assertTrue(self.server.requests[-1]["path"].endswith("token=t2"))

    # -- Test connection report --

    def test_connection_report_all_good(self):
        r = run_connection_test(self.client())
        self.assertTrue(r["ok"])
        self.assertIn("1.2.5.7", r["lines"][0][1])
        self.assertTrue(all(level == "ok" for level, _ in r["lines"]))

    def test_connection_report_missing_scopes(self):
        bambuddy_routes(self.server, permissions=["printers:read", "queue:read", "camera:view"])
        r = run_connection_test(self.client())
        self.assertFalse(r["ok"])
        text = " ".join(t for _, t in r["lines"])
        self.assertIn("can_manage_library", text)
        self.assertIn("can_queue", text)

    def test_connection_report_lists_extra_headers_and_skipped_lines(self):
        conn = Connection(self.server.url, api_key=KEY, timeout=5, extra_headers=(("X-Proxy-Token", "s3cret"),),
                          header_problems=("Extra header line 2 is not 'Name: value'.",))
        r = run_connection_test(BambuddyClient(conn))
        self.assertTrue(r["ok"])
        text = " ".join(t for _, t in r["lines"])
        self.assertIn("X-Proxy-Token", text)
        self.assertNotIn("s3cret", text)
        self.assertEqual(r["lines"][0][0], "warn")

    def test_connection_report_without_key_on_open_server(self):
        bambuddy_routes(self.server, auth_enabled=False)
        r = run_connection_test(self.client(api_key=""))
        self.assertTrue(r["ok"])
        self.assertEqual(r["lines"][-1][0], "warn")

    def test_connection_report_without_key_on_protected_server(self):
        r = run_connection_test(self.client(api_key=""))
        self.assertFalse(r["ok"])
        self.assertIn("API key", r["lines"][-1][1])


class TlsTest(unittest.TestCase):
    def test_self_signed_hint(self):
        # A TLS client against a plain-HTTP port fails in the handshake; the message must say
        # what to check rather than leak a raw ssl error string only.
        server = FakeServer()
        try:
            url = server.url.replace("http://", "https://")
            with self.assertRaises(BambuddyError) as e:
                BambuddyClient(Connection(url, timeout=3)).version()
            self.assertIn(e.exception.kind, ("tls", "unreachable"))
        finally:
            server.close()

    def test_ca_file_option_is_used(self):
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
            f.write("not a certificate")
            f.flush()
            with self.assertRaises(Exception):
                BambuddyClient(Connection("https://example.invalid", ca_file=f.name))


if __name__ == "__main__":
    unittest.main()
