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

__version__ = "0.3.1"
AGENT_CAPABILITY = "Bambuddy printer agent"
PAGE_CAPABILITY = "Bambuddy"
AGENT_NOT_STARTED = (
    "Bambuddy: OrcaSlicer selected this printer preset before the Bambuddy printer agent was "
    "ready, so the Device tab does not list your Bambuddy printers. Select another printer "
    "preset and then this one again."
)

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
        self.inactive_reported = False  # see BambuddyAgent._check_live()

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


def plugin_location(module_file: str = __file__) -> tuple[str, str, str] | None:
    """(orca_plugins folder, plugin key, storage folder) of this installation.

    Found from this file's path, because orca.host.plugin.storage() is not available while
    the plugin loads (2.5.0-beta). A local wheel is extracted to
    orca_plugins/<key>.whl/__whl_extracted__/ (key: the wheel's stem), an OrcaCloud one to
    orca_plugins/_subscribed/<user>/<key>/__whl_extracted__/ (key: its UUID). Their storage
    folders are plugin_data/<key> and plugin_data/_subscribed/<user>/<key>
    (PluginManager::get_storage_dir).
    """
    parts = os.path.normpath(os.path.abspath(module_file)).split(os.sep)
    if "__whl_extracted__" not in parts:
        return None
    i = parts.index("__whl_extracted__")
    if i >= 3 and parts[i - 3] == "_subscribed":
        root, key = parts[: i - 3], parts[i - 1]
        storage = [*root, "plugin_data", "_subscribed", parts[i - 2], key]
    else:
        folder = parts[i - 1]
        root, key = parts[: i - 1], folder[: -len(".whl")] if folder.endswith(".whl") else folder
        storage = [*root, "plugin_data", key]
    return os.sep.join(root), key, os.sep.join(storage)


def saved_agent_settings(plugins_dir: str, key: str) -> tuple[dict | None, str | None]:
    """(settings, plugin key they belong to) from OrcaSlicer's plugin config store.

    This installation's own settings, or, while it has none, those of another installation
    of this plugin: a local wheel and an OrcaCloud subscription have different plugin keys,
    so switching between them would otherwise start from empty settings. Of several, the one
    with the highest plugin version, then the one saved last. (None, None) when there are
    none. Raises OSError or ValueError when the store cannot be read.
    """
    with open(os.path.join(plugins_dir, "config.json"), encoding="utf-8") as f:
        entries = [e for e in json.load(f).get("config") or [] if e.get("capability") == AGENT_CAPABILITY]
    own = [e for e in entries if e.get("plugin_key") == key]
    if own:
        return own[-1].get("cap_config") or {}, key
    others = [(n, e) for n, e in enumerate(entries) if isinstance(e.get("cap_config"), dict) and e["cap_config"].get("base_url")]
    if not others:
        return None, None

    def rank(item):
        n, entry = item
        version = tuple(int(p) if p.isdigit() else 0 for p in str(entry.get("plugin_version") or "").split("."))
        return version, n

    best = max(others, key=rank)[1]
    return best["cap_config"], best.get("plugin_key")


def load_permission_targets() -> dict:
    """The Bambuddy URL and export folder, before the capabilities exist: from the plugin
    config store (see saved_agent_settings), else from the copy the service keeps in
    state.json."""
    location = plugin_location()
    if location is None:
        return {}
    plugins_dir, key, storage = location
    try:
        cfg, _ = saved_agent_settings(plugins_dir, key)
        if cfg is not None:
            cfg = svc.merge_settings(cfg)
            return {"base_url": cfg["base_url"], "export_folder": svc.export_folder(cfg)}
    except (OSError, ValueError, AttributeError) as e:
        log.info("Plugin config store not readable at load (%s); using state.json", e)
    return svc.StateStore(storage).data.get("permission_targets") or {}


def adopt_settings_of_other_installation(agent) -> None:
    """Save another installation's settings as this one's while it has none of its own,
    the same ones load_permission_targets used for the permission request."""
    location = plugin_location()
    if location is None:
        return
    plugins_dir, key, _ = location
    try:
        cfg, source = saved_agent_settings(plugins_dir, key)
    except (OSError, ValueError, AttributeError):
        return
    if cfg is None or source == key:
        return
    if agent.save_config(json.dumps(svc.merge_settings(cfg))):
        log.info("Settings copied from the plugin installation %s", source)
        RUNTIME.host.notify("info", "Bambuddy: settings copied from the other installation of this plugin.")


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


def selected_printer_agent() -> str:
    """The printer agent id the selected printer preset asks for ("" for the default)."""
    try:
        return orca.host.preset_bundle().full_config_value("printer_agent") or ""
    except Exception:  # noqa: BLE001
        return ""


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
        adopt_settings_of_other_installation(self)
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
        self.service.submit_device(self.service.agent_connect, params.dev_id, params.password)
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
        elif event in (orca.LifecycleEvent.NewProject, orca.LifecycleEvent.ProjectOpened,
                       orca.LifecycleEvent.ObjectAdded, orca.LifecycleEvent.PlateSelected):
            self._check_live()

    def _check_live(self):
        # OrcaSlicer 2.5 picks the printer agent of the selected preset at startup, sometimes
        # before this plugin has registered it (the plugin loads on another thread), and does
        # not pick it again afterwards. The agent is then never started (start_discovery), and
        # the Device tab lists no Bambuddy printers. A plugin cannot select a preset, so say
        # what to do, once.
        if RUNTIME.inactive_reported or self.service is None or self.service.agent.live:
            return
        if selected_printer_agent() != bd.AGENT_ID:
            return
        RUNTIME.inactive_reported = True
        log.warning("The selected printer preset uses the Bambuddy agent, but OrcaSlicer has not started it")
        RUNTIME.host.notify("warning", AGENT_NOT_STARTED)

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
