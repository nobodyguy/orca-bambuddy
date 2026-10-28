"""Everything that touches OrcaSlicer's plugin API (the embedded ``orca`` module).

Verified against OrcaSlicer v2.5.0-alpha (git 3880f2c, 2026-10-16): src/slic3r/plugin/*,
the printerAgent and pages capability bindings, and plugin/host/PluginHostUi.cpp; the
permission model also against v2.5.0-beta. The plugin API is alpha; keep calls to it in
this file.
"""

from __future__ import annotations

# Codecs the stdlib imports on first use: socket.getaddrinfo needs idna (and stringprep),
# zipfile needs cp437. Import them while the plugin loads. On the plugin's threads,
# OrcaSlicer 2.5.0-beta refuses the bytecode-cache write of a first import and queues a
# permission prompt for a target-less "open" (see NOTES.md).
import encodings.cp437  # noqa: F401
import encodings.idna  # noqa: F401
import json
import logging
import os
import subprocess
import sys
import threading
import webbrowser

import orca

from . import bambu_dialect as bd
from . import service as svc
from . import ui

__version__ = "0.3.0"
AGENT_CAPABILITY = "Bambuddy printer agent"
PAGE_CAPABILITY = "Bambuddy"

log = logging.getLogger("orca_bambuddy")
if not log.handlers:
    # stderr is teed into OrcaSlicer's data_dir()/log/python_*.log
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[bambuddy] %(levelname)s %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)


class Host:
    """The slicer side of service.Service. Every method may be called from any thread."""

    # OrcaSlicer fades Regular/PrintInfo after 10 s and Important after 20 s; Warning and
    # Error stay until dismissed (NotificationManager::get_standard_duration). After a print
    # OrcaSlicer jumps to the Device tab, so anything shorter than Important is easily missed.
    LEVELS = {
        "info": "ImportantNotificationLevel",
        "success": "ImportantNotificationLevel",
        "warning": "WarningNotificationLevel",
        "error": "ErrorNotificationLevel",
    }

    def __init__(self):
        self.page = None  # BambuddyPage capability, once loaded

    def post_page(self, message: dict) -> None:
        page = self.page
        if page is not None:
            try:
                page.post_message(message)  # marshalled to the UI thread by the host
            except Exception:  # noqa: BLE001 - the page may be gone
                log.debug("post_message failed", exc_info=True)

    def notify(self, level: str, text: str, link_text: str = "", on_click=None) -> None:
        lvl = getattr(orca.host.ui.NotificationLevel, self.LEVELS.get(level, "RegularNotificationLevel"))

        def clicked():
            if on_click and not RUNTIME.stopped:
                on_click()
            return True

        try:
            orca.host.ui.push_notification(lvl, text, hyper_text=link_text or "", on_click=clicked if on_click else None)
        except Exception:  # noqa: BLE001 - e.g. no plater yet during startup
            log.warning("Notification not shown: %s", text)

    def open_url(self, url: str) -> None:
        # One process with one fixed command line (see launch_command). No DEVNULL redirects:
        # opening os.devnull is a file access OrcaSlicer would ask about too.
        command, _ = launch_command(url)
        try:
            if command is None:
                os.startfile(url)  # noqa: S606
            else:
                subprocess.Popen(command, start_new_session=True)  # noqa: S603
        except FileNotFoundError:
            webbrowser.open(url)  # no xdg-open: let Python look for a browser
        except OSError as e:
            log.warning("Could not open %s: %s", url, e)
            self.notify("warning", f"Bambuddy: could not open the browser ({e}). Open {url} yourself.")


class Runtime:
    """Shared state of the plugin's capabilities: one service, one config holder."""

    def __init__(self):
        self.host = Host()
        self.service: svc.Service | None = None
        self.agent = None  # the capability that stores the settings
        self.users = 0
        self.network_grant: str | None = None  # see declare_permissions()

    @property
    def stopped(self) -> bool:
        return self.service is None or self.service.stopped

    def attach(self) -> svc.Service:
        if self.service is None:
            storage = None
            try:
                storage = orca.host.plugin.storage()
            except Exception:  # noqa: BLE001
                log.warning("No plugin storage folder; remembered printers will not persist")
            self.service = svc.Service(self.host, storage)
            self.service.network_grant = self.network_grant
        self.users += 1
        return self.service

    def detach(self) -> None:
        self.users -= 1
        if self.users <= 0 and self.service is not None:
            self.service.stop()
            self.service = None

    def reload_config(self) -> None:
        """Re-read the stored settings (call on the UI thread)."""
        if self.agent is None or self.service is None:
            return
        try:
            stored = json.loads(self.agent.get_config() or "{}")
        except ValueError:
            stored = {}
        if svc.merge_settings(stored) != self.service.cfg:
            self.service.configure(stored)

    def save_settings(self, update: dict) -> tuple[bool, str]:
        if self.agent is None or self.service is None:
            return False, f"Enable the '{AGENT_CAPABILITY}' capability in the Plugins dialog; it stores the settings."
        try:
            cfg = svc.apply_settings_update(self.service.cfg, update)
        except ValueError as e:
            return False, str(e)
        if not self.agent.save_config(json.dumps(cfg)):
            return False, "OrcaSlicer could not save the settings."
        self.service.configure(cfg)
        return True, ""


RUNTIME = Runtime()


def launch_command(url: str) -> tuple[list[str] | None, str]:
    """How Host.open_url starts the browser: (command, or None for os.startfile; the target
    OrcaSlicer's audit hook reports for it).

    One system opener instead of webbrowser, which runs xdg-settings and then tries browser
    after browser: on 2.5.0-beta every attempt is a permission prompt for that exact command
    line. The target is what declare_permissions asks for, so it must match verbatim:
    str(args) for subprocess.Popen, the path for os.startfile.
    """
    if sys.platform == "win32":
        return None, url
    command = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
    return command, str(command)


def load_permission_targets(module_file: str = __file__) -> dict:
    """The Bambuddy URL and export folder, before the capabilities exist.

    orca.host.plugin.storage() is not available while the plugin loads (2.5.0-beta), so the
    folders are found from this file's path: a local wheel is extracted to
    <data dir>/orca_plugins/<key>.whl/__whl_extracted__/..., and its plugin key is the wheel's
    stem. Read first from OrcaSlicer's plugin config store (orca_plugins/config.json), else
    from the copy the service keeps in plugin_data/<key>/state.json.
    """
    parts = os.path.normpath(os.path.abspath(module_file)).split(os.sep)
    if "__whl_extracted__" not in parts:
        return {}
    i = parts.index("__whl_extracted__")
    key = parts[i - 1][: -len(".whl")] if parts[i - 1].endswith(".whl") else parts[i - 1]
    plugins_dir = os.sep.join(parts[: i - 1])
    while plugins_dir and not os.path.isfile(os.path.join(plugins_dir, "config.json")):
        parent = os.path.dirname(plugins_dir)
        if parent == plugins_dir:
            return {}
        plugins_dir = parent
    try:
        with open(os.path.join(plugins_dir, "config.json"), encoding="utf-8") as f:
            entries = json.load(f).get("config") or []
        for entry in entries:
            if entry.get("plugin_key") == key and entry.get("capability") == AGENT_CAPABILITY:
                cfg = svc.merge_settings(entry.get("cap_config"))
                return {"base_url": cfg["base_url"], "export_folder": svc.export_folder(cfg)}
    except (OSError, ValueError, AttributeError) as e:
        log.info("Plugin config store not readable at load (%s); using state.json", e)
    return svc.StateStore(os.path.join(plugins_dir, "plugin_data", key)).data.get("permission_targets") or {}


def declare_permissions() -> str | None:
    """Ask OrcaSlicer for what the plugin's threads need, while the plugin loads.

    OrcaSlicer 2.5.0-beta audits threads a plugin starts, and refuses a first access made
    off the UI thread. Grants made at runtime are exact paths and URLs, which never match
    the next job's temp file or the next status URL. Only a load-time request gives folder
    trees ("<dir>/*") and a whole host (scheme://host). See NOTES.md.

    Returns the host granted ("" for none), or None on 2.5.0-alpha, whose request_permissions()
    takes only fs_read and whose plugin threads are not audited.
    """
    targets = {}
    try:
        targets = load_permission_targets()
    except Exception:  # noqa: BLE001
        log.warning("Settings not readable at load; asking only for the default folders", exc_info=True)
    folders = [svc.orca_model_temp_dir(), targets.get("export_folder") or svc.export_folder({})]
    origin = svc.network_origin(targets.get("base_url", ""))
    # The links to Bambuddy all start this one command (Service.open_bambuddy).
    process = [launch_command(svc.dashboard_url(targets["base_url"]))[1]] if origin else []
    try:
        orca.request_permissions(
            fs_read=[os.path.join(f, "*") for f in folders],
            threading=True,
            network=[origin] if origin else [],
            process=process,
        )
    except TypeError:
        return None
    log.info("Permissions requested: %s, network %s, process %s", folders, origin or "none", process)
    return origin


def slicer_context() -> dict:
    """Printer preset name and print host, for remembering and matching (UI thread)."""
    ctx = {}
    try:
        bundle = orca.host.preset_bundle()
        ctx["printer_preset"] = bundle.current_printer_preset().name
        ctx["print_host"] = bundle.full_config_value("print_host") or ""
    except Exception:  # noqa: BLE001
        pass
    return ctx


def choose_action(file_label: str, target_label: str, default: str, cancelled) -> str | None:
    """Ask which action to take, from OrcaSlicer's job thread. None means cancel."""
    done = threading.Event()
    answer: dict = {}

    def on_submit(data):
        answer.update(data or {})
        if answer.get("dont_ask") and answer.get("action"):
            RUNTIME.save_settings({"ask_action": False, "default_action": answer["action"]})
        done.set()

    def on_close():
        done.set()

    html = ui.chooser_html({"file": file_label, "target": target_label, "default": default})
    orca.host.ui.create_window(
        html=html, title="Send to Bambuddy", width=520, height=330,
        on_close=on_close, style=orca.host.ui.WINDOW_MODAL, on_submit=on_submit,
    )
    while not done.wait(0.2):
        if cancelled():
            return None
    return answer.get("action")


class BambuddyAgent(orca.printer_agent.PrinterAgentBase):
    """Printer connection: Bambuddy printers in the device list, Print/Send go to Bambuddy."""

    def __init__(self):
        super().__init__()
        self.service = None

    def get_name(self):
        return AGENT_CAPABILITY

    def get_agent_info(self):
        return orca.printer_agent.AgentInfo(bd.AGENT_ID, "Bambuddy", __version__, "Print through a Bambuddy server")

    # -- lifecycle and settings --
    def on_load(self):
        self.service = RUNTIME.attach()
        RUNTIME.agent = self
        RUNTIME.reload_config()

    def on_unload(self):
        if RUNTIME.agent is self:
            RUNTIME.agent = None
        RUNTIME.detach()

    def get_default_config(self):
        return dict(svc.DEFAULT_SETTINGS)

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        return ui.CONFIG_HTML

    # -- host callbacks: store them; they may be None --
    def _store(self, key, fn):
        self.service.agent.callbacks[key] = fn
        return bd.OK

    def set_on_ssdp_msg_fn(self, fn): return self._store("ssdp", fn)
    def set_on_local_connect_fn(self, fn): return self._store("local_connect", fn)
    def set_on_local_message_fn(self, fn): return self._store("local_message", fn)
    def set_on_message_fn(self, fn): return self._store("message", fn)
    def set_on_user_message_fn(self, fn): return self._store("user_message", fn)
    def set_on_printer_connected_fn(self, fn): return self._store("printer_connected", fn)
    def set_on_subscribe_failure_fn(self, fn): return self._store("subscribe_failure", fn)
    def set_server_callback(self, fn): return self._store("server_error", fn)
    def set_queue_on_main_fn(self, fn): return self._store("queue_on_main", fn)

    # -- discovery and selection (UI thread: return at once, work on the worker) --
    def start_discovery(self, start, sending):
        RUNTIME.reload_config()
        self.service.ensure_started()
        log.info("start_discovery(start=%s)", start)
        self.service.agent.live = bool(start)
        self.service.agent.next_announce = 0.0
        return True

    def get_user_selected_machine(self):
        return self.service.agent.selected

    def set_user_selected_machine(self, dev_id):
        self.service.agent.selected = dev_id or ""
        return bd.OK

    def connect_printer(self, params):
        RUNTIME.reload_config()
        self.service.ensure_started()
        log.info("connect_printer(%s)", params.dev_id)
        self.service.agent.connected = params.dev_id
        self.service.submit(self.service.agent_connect, params.dev_id, params.password)
        return bd.OK

    def disconnect_printer(self):
        self.service.agent.connected = None
        return bd.OK

    def send_message(self, dev_id, json_str, qos, flag):
        return self.service.agent_command(dev_id, json_str)

    def send_message_to_printer(self, dev_id, json_str, qos, flag):
        return self.service.agent_command(dev_id, json_str)

    # -- jobs (OrcaSlicer job threads) --
    def _job(self, kind, params, update_fn, cancel_fn):
        log.info("%s job for %s: plate %s, %s", kind, params.dev_id, params.plate_index, params.filename)
        update = update_fn or (lambda *a: None)
        cancelled = cancel_fn or (lambda: False)
        target = self.service.printer_name(params.dev_id)

        def choose(default):
            title = self.service.agent.job_titles.get(params.dev_id)
            return choose_action(svc.job_name_from_params(params, title), target, default, cancelled)

        return self.service.agent_job(kind, params, update, cancelled, choose)

    def on_lifecycle_event(self, event, ctx):
        # OrcaSlicer raises these on the job thread right before start_local_print() /
        # start_send_gcode_to_sdcard(), with the job name from its print dialog. PrintParams
        # does not carry that name for MakerWorld projects (it holds the MakerWorld title).
        if event in (orca.LifecycleEvent.PrintJobStarted, orca.LifecycleEvent.SendJobStarted):
            if ctx.device_id.startswith(bd.DEV_ID_PREFIX) and ctx.name:
                self.service.agent.job_titles[ctx.device_id] = ctx.name

    def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
        if params.project_name == "verify_job":
            return bd.OK  # OrcaSlicer's LAN check before a print; Bambuddy is checked on connect
        return self._job("send", params, update_fn, cancel_fn)

    def start_local_print(self, params, update_fn, cancel_fn):
        return self._job("print", params, update_fn, cancel_fn)

    def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
        return self._job("print", params, update_fn, cancel_fn)

    def start_print(self, params, update_fn, cancel_fn, wait_fn):
        return self._job("print", params, update_fn, cancel_fn)

    def start_sdcard_print(self, params, update_fn, cancel_fn):
        return bd.ERR_NOT_SUPPORTED

    # -- binding and certificates: nothing to do, Bambuddy owns the printers --
    def bind_detect(self, dev_ip, sec_link, detect):
        detect.dev_id = dev_ip
        detect.dev_name = "Bambuddy"
        detect.connect_type = "lan"
        detect.bind_state = "free"
        return bd.OK

    def check_cert(self): return bd.OK
    def ping_bind(self, ping_code): return bd.OK
    def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn): return bd.OK
    def unbind(self, dev_id): return bd.OK
    def request_bind_ticket(self): return (bd.OK, "")


class BambuddyPage(orca.pages.PagesPluginCapabilityBase):
    """The Bambuddy tab: printer status, camera, tracked sends, manual send, settings."""

    def __init__(self):
        super().__init__()
        self.service = None

    def get_name(self):
        return PAGE_CAPABILITY

    def get_ui(self):
        return ui.PAGE_HTML

    def get_icon(self):
        # A 20 px tab icon in the style of OrcaSlicer's own (white strokes on the dark tab bar).
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "bambuddy_tab.svg")

    def on_load(self):
        self.service = RUNTIME.attach()
        RUNTIME.host.page = self

    def on_unload(self):
        if RUNTIME.host.page is self:
            RUNTIME.host.page = None
        RUNTIME.detach()

    def on_message(self, data):
        # UI thread: anything slow goes to the worker.
        s = self.service
        s.ensure_started()
        kind = data.get("type") if isinstance(data, dict) else None
        if kind == "ready":
            RUNTIME.reload_config()
            self.post_message(s.page_snapshot())
            s.page_heartbeat(True)
        elif kind == "heartbeat":
            s.page_heartbeat(bool(data.get("visible")))
        elif kind == "save_settings":
            ok, error = RUNTIME.save_settings(data.get("settings") or {})
            self.post_message(
                {"type": "saved", "ok": ok, "error": error, "notice": s.client_problem, "settings": svc.public_settings(s.cfg)}
            )
            if ok:
                s.submit(s.refresh_status, True)
        elif kind == "test":
            s.submit(s.run_test)
        elif kind == "refresh":
            s.submit(s.refresh_status, True)
        elif kind == "select":
            s.focus(data.get("printer_id"))
        elif kind == "snapshot" and data.get("printer_id") is not None:
            s.submit(s.snapshot, int(data["printer_id"]))
        elif kind == "open_bambuddy":
            s.open_bambuddy()
        elif kind == "list_exports":
            s.submit(s.list_exports)
        elif kind == "describe" and data.get("path"):
            s.submit(s.describe_file, os.path.expanduser(data["path"]), slicer_context())
        elif kind == "check" and data.get("path"):
            s.submit(s.page_check, data["path"], data.get("target"), data.get("action"))
        elif kind == "send":
            s.page_send(data, slicer_context())


@orca.plugin
class BambuddyPlugin(orca.base):
    def register_capabilities(self):
        RUNTIME.network_grant = declare_permissions()
        orca.register_capability(BambuddyAgent)
        orca.register_capability(BambuddyPage)
