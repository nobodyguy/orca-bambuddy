import json
import os
import shutil
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

from fakes import FIXTURES, FakeHost, FakeServer, bambuddy_routes, make_sliced_3mf

from orca_bambuddy import bambu_dialect as bd
from orca_bambuddy import service as svc

X1C_FILE = os.path.join(FIXTURES, "cube_x1c.gcode.3mf")
A1M_FILE = os.path.join(FIXTURES, "cube_a1m.gcode.3mf")


class SettingsTest(unittest.TestCase):
    def test_secrets_are_not_sent_to_pages(self):
        cfg = svc.merge_settings({"base_url": "http://b", "api_key": "k", "cf_client_secret": "s"})
        pub = svc.public_settings(cfg)
        self.assertNotIn("api_key", pub)
        self.assertNotIn("cf_client_secret", pub)
        self.assertTrue(pub["has_api_key"] and pub["has_cf_client_secret"])

    def test_empty_secret_field_keeps_saved_secret(self):
        cfg = svc.merge_settings({"api_key": "old"})
        self.assertEqual(svc.apply_settings_update(cfg, {"api_key": "", "base_url": " http://b "})["api_key"], "old")
        self.assertEqual(svc.apply_settings_update(cfg, {"api_key": "new"})["api_key"], "new")
        self.assertEqual(svc.apply_settings_update(cfg, {"api_key": "", "clear_api_key": True})["api_key"], "")

    def test_types_and_unknown_keys(self):
        cfg = svc.apply_settings_update(svc.merge_settings({}), {"timeout_s": "30", "verify_tls": 0, "evil": 1, "default_action": "x"})
        self.assertEqual((cfg["timeout_s"], cfg["verify_tls"], cfg["default_action"]), (30, False, "print"))
        self.assertNotIn("evil", cfg)

    def test_extra_headers_are_a_secret_setting(self):
        cfg = svc.merge_settings({"extra_headers": "X-Proxy-Token: s3cret\nRemote-User: jan"})
        pub = svc.public_settings(cfg)
        self.assertNotIn("extra_headers", pub)
        self.assertTrue(pub["has_extra_headers"])
        self.assertEqual(pub["extra_header_names"], ["X-Proxy-Token", "Remote-User"])
        self.assertNotIn("s3cret", json.dumps(pub))
        self.assertEqual(svc.apply_settings_update(cfg, {"extra_headers": ""})["extra_headers"], cfg["extra_headers"])
        self.assertEqual(svc.apply_settings_update(cfg, {"extra_headers": "", "clear_extra_headers": True})["extra_headers"], "")
        self.assertEqual(svc.apply_settings_update(cfg, {"extra_headers": "X-A: 1"})["extra_headers"], "X-A: 1")

    def test_malformed_extra_headers_are_refused_on_save(self):
        with self.assertRaises(ValueError) as e:
            svc.apply_settings_update(svc.merge_settings({}), {"extra_headers": "Content-Type: text/plain"})
        self.assertIn("Content-Type", str(e.exception))

    def test_connection_skips_bad_header_lines(self):
        conn = svc.make_connection({"base_url": "http://b", "extra_headers": "X-Ok: 1\nnonsense"})
        self.assertEqual(conn.extra_headers, (("X-Ok", "1"),))
        self.assertEqual(len(conn.header_problems), 1)

    def test_cloudflare_token_only_when_enabled(self):
        cfg = svc.merge_settings({"base_url": "http://b", "cf_client_id": "i", "cf_client_secret": "s"})
        self.assertFalse(svc.make_connection(cfg).uses_cloudflare_access)
        cfg["cf_access_enabled"] = True
        self.assertTrue(svc.make_connection(cfg).uses_cloudflare_access)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.state = bambuddy_routes(self.server)
        self.host = FakeHost()
        self.storage = tempfile.mkdtemp()
        self.service = svc.Service(self.host, self.storage)
        self.service.configure({"base_url": self.server.url, "api_key": "k"})

    def tearDown(self):
        self.service.stop()
        self.server.close()
        shutil.rmtree(self.storage)

    def queued(self):
        return [json.loads(r["body"]) for r in self.server.requests if r["path"] == "/api/v1/queue/"]


class SendTest(ServiceTestBase):
    def send(self, **kw):
        job = svc.SendJob(**{"path": X1C_FILE, "action": "print", "printer_id": 1, "label": "X1C Office", **kw})
        progress = []
        result = self.service.send(job, lambda pct, text: progress.append(pct))
        return result, progress

    def test_print_now(self):
        result, progress = self.send()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.queued(), [{"library_file_id": 1, "printer_id": 1, "manual_start": False, "plate_id": 1}])
        self.assertIn("starts as soon as the printer is free", result["message"])
        self.assertEqual(progress[-1], 100)
        self.assertEqual(self.host.notes[-1][0], "success")
        self.assertEqual(self.host.last("focus")["printer_id"], 1)

    def test_add_to_queue_is_staged(self):
        result, _ = self.send(action="queue")
        self.assertTrue(result["ok"])
        self.assertTrue(self.queued()[0]["manual_start"])

    def test_store_only_does_not_queue(self):
        result, _ = self.send(action="store", printer_id=None)
        self.assertTrue(result["ok"])
        self.assertEqual(self.queued(), [])
        self.assertIn("library", result["message"])

    def test_any_printer_of_model(self):
        result, _ = self.send(printer_id=None, target_model="X1C", label="Any X1C")
        self.assertTrue(result["ok"])
        self.assertEqual(self.queued()[0]["target_model"], "X1C")

    def test_wrong_model_is_blocked_before_upload(self):
        result, _ = self.send(path=A1M_FILE)
        self.assertFalse(result["ok"])
        self.assertIn("sliced for A1 Mini", result["message"])
        self.assertEqual(self.state["files"], {})
        self.assertEqual(self.host.notes[-1][0], "error")

    def test_all_plates_become_one_queue_item_each(self):
        with tempfile.TemporaryDirectory() as d:
            path = make_sliced_3mf(os.path.join(d, "all.gcode.3mf"), plates=(1, 2, 3))
            result, _ = self.send(path=path)
            self.assertTrue(result["ok"])
            self.assertEqual([q["plate_id"] for q in self.queued()], [1, 2, 3])
            result, _ = self.send(path=path, plate=2, action="queue")
        self.assertEqual(self.queued()[-1]["plate_id"], 2)

    def test_identical_file_is_uploaded_once(self):
        self.send()
        self.server.route("GET", "/api/v1/library/files/1", body={"id": 1, "file_hash": svc.sha256_of(X1C_FILE)})
        self.send()
        self.assertEqual(len(self.state["files"]), 1)
        self.assertEqual([q["library_file_id"] for q in self.queued()], [1, 1])
        # The cache survives a restart: it lives in the plugin's storage folder.
        with open(os.path.join(self.storage, "state.json")) as f:
            self.assertIn(svc.sha256_of(X1C_FILE), json.load(f)["uploads"])

    def test_server_side_duplicate_keeps_the_older_file(self):
        self.server.routes[("POST", "/api/v1/library/files")] = lambda req: (200, {}, {"id": 9, "duplicate_of": 4})
        self.server.route("DELETE", "/api/v1/library/files/9", body={"message": "moved to trash"})
        result, _ = self.send()
        self.assertTrue(result["ok"])
        self.assertEqual(self.queued()[0]["library_file_id"], 4)
        self.assertIn("DELETE", [r["method"] for r in self.server.requests])

    def test_keep_policy_uploads_again(self):
        self.service.configure({"base_url": self.server.url, "duplicate_policy": "keep"})
        self.send()
        self.send()
        self.assertEqual(len(self.state["files"]), 2)

    def test_upload_limit_behind_cloudflare(self):
        self.service.configure({"base_url": self.server.url, "upload_limit_mb": 0, "cf_access_enabled": True,
                                "cf_client_id": "i", "cf_client_secret": "s"})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(svc, "CLOUDFLARE_FREE_UPLOAD_LIMIT_MB", 1):
            big = make_sliced_3mf(os.path.join(d, "big.gcode.3mf"))
            with zipfile.ZipFile(big, "a", zipfile.ZIP_STORED) as zf:
                zf.writestr("Metadata/plate_1.png", os.urandom(2 * 1024 * 1024))
            result, _ = self.send(path=big)
        self.assertFalse(result["ok"])
        self.assertIn("upload limit is 1 MB", result["message"])
        self.assertEqual(self.state["files"], {})

    def test_not_configured(self):
        self.service.configure({})
        result, _ = self.send()
        self.assertFalse(result["ok"])
        self.assertIn("URL", result["message"])


class AgentTest(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.events = []
        for key in ("ssdp", "local_message", "local_connect", "printer_connected"):
            self.service.agent.callbacks[key] = lambda *a, k=key: self.events.append((k, a))
        self.service.ensure_started()

    def params(self, **kw):
        base = dict(dev_id="bambuddy-00M01", dev_name="", filename=X1C_FILE, project_name="cube", task_name="",
                    plate_index=1, auto_bed_leveling=2, task_bed_leveling=False, auto_flow_cali=2, task_flow_cali=False,
                    auto_offset_cali=2, task_vibration_cali=True, task_layer_inspect=False, task_record_timelapse=False,
                    task_use_ams=True, ams_mapping="[0]")
        base.update(kw)
        return SimpleNamespace(**base)

    def test_discovery_announces_every_active_printer(self):
        self.service.agent_announce()
        recs = [json.loads(a[0]) for k, a in self.events if k == "ssdp"]
        self.assertEqual([r["dev_id"] for r in recs], ["bambuddy-00M01", "bambuddy-00M02", "bambuddy-03A01"])
        self.assertEqual([r["dev_type"] for r in recs], ["BL-P001", "BL-P001", "N1"])

    def test_connect_reports_version_status_then_connected(self):
        self.service.agent_announce()
        self.events.clear()
        self.service.agent.connected = "bambuddy-00M01"
        self.service.agent_connect("bambuddy-00M01", "code")
        kinds = [k for k, _ in self.events]
        self.assertEqual(kinds, ["local_message", "local_message", "local_connect", "printer_connected"])
        self.assertIn("get_version", self.events[0][1][1])
        self.assertIn("push_status", self.events[1][1][1])
        self.assertEqual(self.events[2][1][0], 0)

    def test_connect_to_unknown_printer_fails_cleanly(self):
        self.service.agent_connect("bambuddy-gone", "code")
        self.assertEqual(self.events[-1][0], "local_connect")
        self.assertEqual(self.events[-1][1][0], 1)

    def test_commands(self):
        self.assertEqual(self.service.agent_command("d", '{"info":{"command":"get_version"}}'), bd.OK)
        self.assertEqual(self.service.agent_command("d", '{"pushing":{"command":"pushall"}}'), bd.OK)
        self.assertEqual(self.service.agent_command("d", '{"print":{"command":"pause"}}'), bd.ERR_NOT_SUPPORTED)

    def run_job(self, kind="print", choose=lambda default: default, **kw):
        self.service.agent_announce()
        updates = []
        code = self.service.agent_job(kind, self.params(**kw), lambda *a: updates.append(a), lambda: False, choose)
        return code, updates

    def test_print_button_queues_with_dialog_options(self):
        code, updates = self.run_job()
        self.assertEqual(code, bd.OK)
        q = self.queued()[0]
        self.assertEqual((q["printer_id"], q["manual_start"], q["plate_id"], q["ams_mapping"]), (1, False, 1, [0]))
        self.assertEqual(updates[0][0], bd.STAGE_CREATE)
        self.assertEqual(updates[-1][0], bd.STAGE_FINISHED)
        self.assertIn(b'filename="cube.gcode.3mf"', self.state["files"][1])

    def test_name_from_print_dialog_wins_over_makerworld_title(self):
        self.service.agent.job_titles["bambuddy-00M01"] = "mesic_maglajz"
        code, _ = self.run_job(project_name="LALuna2")
        self.assertEqual(code, bd.OK)
        self.assertIn(b'filename="mesic_maglajz.gcode.3mf"', self.state["files"][1])
        self.assertNotIn("bambuddy-00M01", self.service.agent.job_titles)

    def test_send_button_never_prints_by_default(self):
        seen = []
        code, _ = self.run_job("send", choose=lambda default: seen.append(default) or default)
        self.assertEqual(code, bd.OK)
        self.assertEqual(seen, ["queue"])
        self.assertTrue(self.queued()[0]["manual_start"])

    def test_user_cancels_the_choice(self):
        code, _ = self.run_job(choose=lambda default: None)
        self.assertEqual(code, bd.ERR_CANCELED)
        self.assertEqual(self.state["files"], {})

    def test_no_question_when_ask_is_off(self):
        self.service.configure({"base_url": self.server.url, "ask_action": False, "default_action": "store"})
        code, _ = self.run_job(choose=lambda default: self.fail("asked"))
        self.assertEqual(code, bd.OK)
        self.assertEqual(self.queued(), [])

    def test_failure_is_reported_to_orcaslicer(self):
        self.server.route("POST", "/api/v1/library/files", status=403, body={"detail": "API key does not have 'can_manage_library' permission"})
        code, updates = self.run_job()
        self.assertEqual(code, bd.ERR_UPLOAD_FAILED)
        self.assertEqual(updates[-1][0], bd.STAGE_ERROR)
        self.assertIn("can_manage_library", updates[-1][2])


class MilestoneTest(ServiceTestBase):
    def test_start_first_layer_and_error(self):
        self.service._tracked[5] = svc.TrackedJob(5, "cube.gcode.3mf", "X1C Office", False, 1)
        self.server.route("GET", "/api/v1/queue/5", body={"id": 5, "status": "pending", "printer_id": 1})
        self.service.poll_tracked()
        self.assertIn("queued", self.host.notes[-1][1])
        self.server.route("GET", "/api/v1/queue/5", body={"id": 5, "status": "printing", "printer_id": 1})
        self.server.route("GET", "/api/v1/printers/1/status", body={"state": "RUNNING", "layer_num": 1,
                          "hms_errors": [{"code": "0300_0100", "full_code": "03000100000100", "severity": 2, "description": "Nozzle clog"}]})
        self.service.poll_tracked()
        texts = [t for _, t in self.host.notes]
        self.assertTrue(any("started printing" in t for t in texts))
        self.assertTrue(any("Nozzle clog" in t for t in texts))
        self.server.route("GET", "/api/v1/printers/1/status", body={"state": "RUNNING", "layer_num": 2})
        self.service.poll_tracked()
        self.assertIn("first layer finished", self.host.notes[-1][1])
        self.assertNotIn(5, self.service._tracked)
        self.assertEqual(self.host.last("job")["job"]["milestones"], ["first_layer", "queued", "started"])

    def test_failed_queue_item(self):
        self.service._tracked[6] = svc.TrackedJob(6, "cube", "X1C", False, 1)
        self.server.route("GET", "/api/v1/queue/6", body={"id": 6, "status": "failed", "error_message": "Printer offline"})
        self.service.poll_tracked()
        self.assertEqual(self.host.notes[-1], ("error", "cube did not print: Printer offline"))
        self.assertNotIn(6, self.service._tracked)


class PageTest(ServiceTestBase):
    def test_status_table(self):
        self.service.refresh_status()
        msg = self.host.last("printers")
        self.assertIsNone(msg["error"])
        self.assertEqual([p["name"] for p in msg["printers"]], ["X1C Office", "X1C Lab", "A1 mini"])

    def test_status_table_reports_connection_problem(self):
        self.server.route("GET", "/api/v1/printers/", status=401, body={"detail": "Invalid API key"})
        self.service.refresh_status(force_list=True)
        self.assertIn("API key", self.host.last("printers")["error"])

    def test_describe_file_ranks_printers(self):
        self.service.describe_file(X1C_FILE, {"printer_preset": "Bambu Lab X1 Carbon 0.4 nozzle"})
        self.assertEqual(self.host.last("file")["file"]["sliced_model"], "X1C")
        targets = self.host.last("targets")
        self.assertEqual(targets["selected"], "p:2")  # both X1C idle: first by name, "X1C Lab"
        self.assertIn("m:X1C", [t["id"] for t in targets["targets"]])

    def test_page_check_blocks_wrong_printer(self):
        target = {"id": "p:3", "label": "A1 mini", "printer_id": 3, "model": "A1 Mini", "compatible": False}
        self.service.page_check(X1C_FILE, target, "print")
        self.assertEqual(self.host.last("check")["level"], "block")


if __name__ == "__main__":
    unittest.main()
