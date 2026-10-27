"""Against a real Bambuddy. Skipped unless BAMBUDDY_TEST_URL is set.

  BAMBUDDY_TEST_URL      e.g. http://127.0.0.1:8000 (or a demo.bambuddy.cool session URL)
  BAMBUDDY_TEST_KEY      API key with can_read_status, can_queue, can_manage_library (optional
                         when the server has authentication off)
  BAMBUDDY_TEST_RO_KEY   optional: a key with only can_read_status, to check the permission errors
  BAMBUDDY_TEST_QUEUE    set to 1 to also create a staged (manual start) queue item, removed again

Uploads one small sliced file (tests/fixtures/cube_x1c.gcode.3mf) and deletes it again.
"""

import os
import unittest

from fakes import FIXTURES

from orca_bambuddy.bambuddy import BambuddyClient, BambuddyError, Connection
from orca_bambuddy.service import test_connection as run_connection_test

URL = os.environ.get("BAMBUDDY_TEST_URL", "")
KEY = os.environ.get("BAMBUDDY_TEST_KEY", "")
RO_KEY = os.environ.get("BAMBUDDY_TEST_RO_KEY", "")


@unittest.skipUnless(URL, "BAMBUDDY_TEST_URL not set")
class LiveTest(unittest.TestCase):
    def setUp(self):
        self.client = BambuddyClient(Connection(URL, api_key=KEY, timeout=20))

    def test_connection_report(self):
        report = run_connection_test(self.client)
        print("\n".join(f"  [{level}] {text}" for level, text in report["lines"]))
        self.assertTrue(report["ok"])

    def test_printers_and_status(self):
        auth = self.client.auth_status().get("auth_enabled")
        for p in self.client.printers():
            status = self.client.printer_status(p["id"])
            self.assertIn("connected", status)
            if auth:  # with authentication off Bambuddy includes it for everybody
                self.assertNotIn("access_code", p)

    def test_upload_and_staged_queue_item(self):
        resp = self.client.upload_library_file(os.path.join(FIXTURES, "cube_x1c.gcode.3mf"), "orca-bambuddy-test.gcode.3mf")
        try:
            self.assertEqual(resp["file_type"], "gcode.3mf")
            info = self.client.library_file(resp["id"])
            self.assertEqual(info["metadata"].get("sliced_for_model"), "X1C")
            if os.environ.get("BAMBUDDY_TEST_QUEUE") == "1":
                printers = [p for p in self.client.printers() if p.get("is_active", True)]
                if printers:
                    item = self.client.add_to_queue(resp["id"], printer_id=printers[0]["id"], manual_start=True, plate_id=1)
                    self.assertEqual((item["status"], item["manual_start"]), ("pending", True))
                    self.client._request("DELETE", f"/queue/{item['id']}")
        finally:
            self.client.delete_library_file(resp["id"])

    @unittest.skipUnless(RO_KEY, "BAMBUDDY_TEST_RO_KEY not set")
    def test_read_only_key(self):
        ro = BambuddyClient(Connection(URL, api_key=RO_KEY, timeout=20))
        report = run_connection_test(ro)
        self.assertFalse(report["ok"])
        with self.assertRaises(BambuddyError) as e:
            ro.upload_library_file(os.path.join(FIXTURES, "cube_x1c.gcode.3mf"))
        self.assertEqual(e.exception.kind, "permission")
        self.assertIn("can_manage_library", e.exception.message)

    def test_wrong_key(self):
        bad = BambuddyClient(Connection(URL, api_key="bb_wrong", timeout=20))
        if not bad.auth_status().get("auth_enabled"):
            self.skipTest("server has authentication off")
        report = run_connection_test(bad)
        self.assertFalse(report["ok"])
        self.assertIn("not valid", report["lines"][-1][1])


if __name__ == "__main__":
    unittest.main()
