"""OrcaSlicer's plugin audit (2.5.0-beta): load-time permission request and the network gate."""

import importlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

from fakes import FakeHost, FakeServer, bambuddy_routes

from orca_bambuddy import service as svc
from orca_bambuddy import sliced3mf
from orca_bambuddy.bambuddy import BambuddyClient, BambuddyError, Connection

AUDIT_DENIAL = PermissionError('Plugin attempted audited operation "open" without permission: audit permission required')


def fake_orca(storage: str, request_permissions) -> types.ModuleType:
    """Just enough of OrcaSlicer's embedded ``orca`` module to import orca_adapter."""
    orca = types.ModuleType("orca")
    orca.base = type("base", (), {})
    orca.plugin = lambda cls: cls
    orca.register_capability = lambda cls: None
    orca.request_permissions = request_permissions
    orca.printer_agent = types.SimpleNamespace(PrinterAgentBase=type("PrinterAgentBase", (), {}))
    orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=type("PagesPluginCapabilityBase", (), {}))
    orca.host = types.SimpleNamespace(plugin=types.SimpleNamespace(storage=lambda: storage))
    return orca


class LoadTimeRequestTest(unittest.TestCase):
    def setUp(self):
        self.storage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.storage)
        self.addCleanup(sys.modules.pop, "orca_bambuddy.orca_adapter", None)
        self.addCleanup(sys.modules.pop, "orca", None)

    def declare(self, request_permissions, settings=None, state=None):
        """Load the adapter as if installed as a local wheel in a data dir under self.storage."""
        plugins = os.path.join(self.storage, "orca_plugins")
        module_file = os.path.join(plugins, "orca_bambuddy_any.whl", "__whl_extracted__", "orca_bambuddy", "orca_bambuddy", "orca_adapter.py")
        os.makedirs(os.path.join(plugins, "plugin_data", "orca_bambuddy_any"))
        entries = [{"plugin_key": "orca_bambuddy-0.1.0-py3-none-any", "capability": "Bambuddy printer agent", "cap_config": {"base_url": "http://old"}}]
        if settings is not None:
            entries.append({"plugin_key": "orca_bambuddy_any", "capability": "Bambuddy printer agent", "cap_config": settings})
        with open(os.path.join(plugins, "config.json"), "w") as f:
            json.dump({"config": entries}, f)
        if state is not None:
            with open(os.path.join(plugins, "plugin_data", "orca_bambuddy_any", "state.json"), "w") as f:
                json.dump({"permission_targets": state}, f)

        def storage():
            raise RuntimeError("plugin.storage() must be called from a plugin callback")  # as in 2.5.0-beta

        orca = fake_orca(self.storage, request_permissions)
        orca.host.plugin.storage = storage
        with mock.patch.dict(sys.modules, {"orca": orca}):
            sys.modules.pop("orca_bambuddy.orca_adapter", None)
            adapter = importlib.import_module("orca_bambuddy.orca_adapter")
            with mock.patch.object(adapter, "__file__", module_file), mock.patch.object(
                adapter.load_permission_targets, "__defaults__", (module_file,)
            ):
                return adapter.declare_permissions()

    def test_beta_asks_for_temp_tree_export_folder_threads_and_host(self):
        calls = []
        origin = self.declare(
            lambda **kw: calls.append(kw), settings={"base_url": "http://Bambuddy.lan:8000/", "export_folder": "/data/exports"}
        )
        self.assertEqual(origin, "http://bambuddy.lan")
        self.assertEqual(
            calls,
            [{
                "fs_read": [os.path.join(svc.orca_model_temp_dir(), "*"), os.path.join("/data/exports", "*")],
                "threading": True,
                "network": ["http://bambuddy.lan"],
                "process": [str(["open" if sys.platform == "darwin" else "xdg-open", "http://Bambuddy.lan:8000/"])]
                if sys.platform != "win32" else ["http://Bambuddy.lan:8000/"],
            }],
        )

    def test_state_json_copy_when_the_config_store_has_no_entry(self):
        calls = []
        self.assertEqual(self.declare(lambda **kw: calls.append(kw), state={"base_url": "https://bb.example"}), "https://bb.example")

    def test_beta_without_saved_url_asks_for_no_host(self):
        calls = []
        self.assertEqual(self.declare(lambda **kw: calls.append(kw)), "")
        self.assertEqual((calls[0]["network"], calls[0]["process"]), ([], []))
        self.assertEqual(calls[0]["fs_read"][1], os.path.join(svc.export_folder({}), "*"))

    def test_alpha_signature_means_unaudited_threads(self):
        def alpha(fs_read=()):  # 2.5.0-alpha: fs_read only
            raise AssertionError("not reached")

        self.assertIsNone(self.declare(alpha))


class TempFolderTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX layout")
    def test_per_user_folder_below_tmpdir(self):
        with mock.patch.dict(os.environ, {"TMPDIR": "/var/tmp/x/"}):
            self.assertEqual(svc.orca_temp_root(), f"/var/tmp/x/orcaslicer_{os.getuid()}")
        with mock.patch.dict(os.environ, {"TMPDIR": "", "TMP": "", "TEMP": ""}):
            self.assertEqual(svc.orca_model_temp_dir(), f"/tmp/orcaslicer_{os.getuid()}/orcaslicer_model")

    def test_network_origin(self):
        self.assertEqual(svc.network_origin("HTTPS://Host.example:8443/sub/api/v1"), "https://host.example")
        self.assertEqual(svc.network_origin("192.168.1.5:8000"), "http://192.168.1.5")
        self.assertEqual(svc.network_origin(""), "")
        self.assertEqual(svc.network_origin("ftp://host"), "")


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        bambuddy_routes(self.server)
        self.host = FakeHost()
        self.storage = tempfile.mkdtemp()
        self.service = svc.Service(self.host, self.storage)

    def tearDown(self):
        self.service.stop()
        self.server.close()
        shutil.rmtree(self.storage)


class NetworkGateTest(ServiceCase):
    def test_granted_host_is_used(self):
        self.service.network_grant = "http://127.0.0.1"
        self.service.configure({"base_url": self.server.url})
        self.assertIsNotNone(self.service.client)

    def test_other_host_needs_a_restart(self):
        self.service.network_grant = ""
        self.service.configure({"base_url": self.server.url})
        self.assertIsNone(self.service.client)
        self.service.refresh_status()
        self.assertIn("Restart OrcaSlicer", self.host.last("printers")["error"])
        self.assertEqual(self.server.requests, [])

    def test_alpha_has_no_gate(self):
        self.service.configure({"base_url": self.server.url})
        self.assertIsNotNone(self.service.client)

    def test_url_is_kept_for_the_next_load(self):
        self.service.configure({"base_url": self.server.url + "/", "export_folder": "/data/exports"})
        with open(os.path.join(self.storage, "state.json")) as f:
            targets = json.load(f)["permission_targets"]
        self.assertEqual(targets, {"base_url": self.server.url, "export_folder": "/data/exports"})


class OpenBambuddyTest(ServiceCase):
    def test_beta_opens_the_dashboard_on_the_calling_thread(self):
        self.service.network_grant = "http://127.0.0.1"
        self.service.configure({"base_url": self.server.url + "/api/v1"})
        self.service.open_bambuddy()
        self.assertEqual(self.host.urls, [self.server.url + "/"])

    def test_alpha_opens_it_on_the_worker(self):
        self.service.configure({"base_url": self.server.url})
        with mock.patch.object(self.service, "submit") as submit:
            self.service.open_bambuddy()
        submit.assert_called_once_with(self.host.open_url, self.server.url + "/")

    def test_notification_link_opens_the_dashboard(self):
        self.service.network_grant = "http://127.0.0.1"
        self.service.configure({"base_url": self.server.url})
        clicks = []
        self.host.notify = lambda level, text, link_text="", on_click=None: clicks.append(on_click)
        self.service.send(svc.SendJob(path=os.path.join(os.path.dirname(__file__), "fixtures", "cube_x1c.gcode.3mf"), action="store"), lambda *a: None)
        clicks[-1]()
        self.assertEqual(self.host.urls, [self.server.url + "/"])


class LauncherTest(unittest.TestCase):
    def test_one_process_with_a_fixed_command(self):
        self.addCleanup(sys.modules.pop, "orca_bambuddy.orca_adapter", None)
        with mock.patch.dict(sys.modules, {"orca": fake_orca("", lambda **kw: None)}):
            sys.modules.pop("orca_bambuddy.orca_adapter", None)
            adapter = importlib.import_module("orca_bambuddy.orca_adapter")
            with mock.patch.object(adapter.subprocess, "Popen") as popen, mock.patch.object(adapter.sys, "platform", "linux"), \
                    mock.patch.object(adapter.webbrowser, "open") as browser:
                adapter.Host().open_url("http://bambuddy.lan:8000/")
        popen.assert_called_once_with(["xdg-open", "http://bambuddy.lan:8000/"], start_new_session=True)
        browser.assert_not_called()

    def test_load_request_names_the_launch_exactly(self):
        # OrcaSlicer matches a process grant against str(args) of subprocess.Popen, verbatim.
        hook_cmd = ["xdg-open", "https://bb.example/"]
        with mock.patch.dict(sys.modules, {"orca": fake_orca("", lambda **kw: None)}):
            sys.modules.pop("orca_bambuddy.orca_adapter", None)
            adapter = importlib.import_module("orca_bambuddy.orca_adapter")
            with mock.patch.object(adapter.sys, "platform", "linux"):
                command, target = adapter.launch_command(svc.dashboard_url("https://bb.example/api/v1"))
        self.assertEqual((command, target), (hook_cmd, str(hook_cmd)))


class DenialMessageTest(unittest.TestCase):
    def test_unreadable_file_is_not_called_a_bad_3mf(self):
        with mock.patch("zipfile.ZipFile", side_effect=AUDIT_DENIAL):
            with self.assertRaises(sliced3mf.NotASlicedFile) as e:
                sliced3mf.inspect(__file__)
        self.assertIn("may not read", str(e.exception))
        self.assertNotIn("Not a 3MF", str(e.exception))

    def test_refused_connection_is_reported_as_blocked(self):
        client = BambuddyClient(Connection("http://bambuddy.lan:8000"))
        denial = PermissionError('Plugin attempted audited operation "urllib.Request" without permission')
        with mock.patch.object(client._opener, "open", side_effect=urllib.error.URLError(denial)):
            with self.assertRaises(BambuddyError) as e:
                client.version()
        self.assertEqual(e.exception.kind, "blocked")
        self.assertIn("Restart OrcaSlicer", e.exception.message)


if __name__ == "__main__":
    unittest.main()
