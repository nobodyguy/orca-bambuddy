"""Send, status and printer-agent logic, independent of OrcaSlicer.

All network and file I/O runs on threads this module starts: OrcaSlicer audits plugin
calls per URL and path, and denies a first access made off the UI thread, while the
work of a plugin-started thread is not audited (see NOTES.md). The slicer is reached
only through the ``host`` object (orca_adapter.Host), whose methods may be called from
any thread.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from . import bambu_dialect as bd
from . import matching, sliced3mf
from .bambuddy import (
    CLOUDFLARE_FREE_UPLOAD_LIMIT_MB,
    REQUIRED_PERMISSIONS,
    SCOPE_LABELS,
    BambuddyClient,
    BambuddyError,
    Connection,
    parse_extra_headers,
)

log = logging.getLogger("orca_bambuddy")

DEFAULT_SETTINGS = {
    "base_url": "",
    "api_key": "",
    "cf_access_enabled": False,
    "cf_client_id": "",
    "cf_client_secret": "",
    "extra_headers": "",  # "Name: value" per line, sent with every request (reverse proxies)
    "verify_tls": True,
    "ca_file": "",
    "timeout_s": 15,
    "default_action": "print",  # print | queue | store
    "ask_action": True,  # ask on every Print/Send, with default_action preselected
    "printer_selection": "auto",  # auto: preselect the best match | manual: always choose
    "remember_printer": True,
    "export_folder": "",  # empty: ~/Downloads
    "duplicate_policy": "reuse",  # reuse | keep
    "upload_limit_mb": 0,  # 0: no limit, or 100 when Cloudflare is in front
    "poll_interval_s": 5,
}
SECRET_KEYS = ("api_key", "cf_client_secret", "extra_headers")
ACTIONS = ("print", "queue", "store")

PAGE_TIMEOUT_S = 20  # the page sends a heartbeat every 5 s while visible
TRACK_INTERVAL_S = 10
TRACK_MAX_AGE_S = 24 * 3600
PRINTER_LIST_INTERVAL_S = 60
ANNOUNCE_INTERVAL_S = 30
DEVICE_PUSH_INTERVAL_S = 3
NO_THREADS = (
    "The plugin is not allowed to start its background threads. Restart OrcaSlicer, open the "
    "Bambuddy tab and answer Yes to the permission prompt."
)


def merge_settings(stored: dict | None) -> dict:
    cfg = dict(DEFAULT_SETTINGS)
    cfg.update({k: v for k, v in (stored or {}).items() if k in DEFAULT_SETTINGS})
    if cfg["default_action"] not in ACTIONS:
        cfg["default_action"] = "print"
    return cfg


def public_settings(cfg: dict) -> dict:
    """Settings for a page: secrets are replaced by whether they are set."""
    out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    for k in SECRET_KEYS:
        out["has_" + k] = bool(cfg.get(k))
    # Header names are not secret and tell the user what is saved.
    out["extra_header_names"] = [name for name, _ in parse_extra_headers(cfg.get("extra_headers", ""))[0]]
    return out


def apply_settings_update(cfg: dict, update: dict) -> dict:
    """Merge a page's settings form; an empty secret field keeps the stored secret.

    Raises ValueError with a user-facing reason when the extra headers are malformed.
    """
    if update.get("extra_headers") and not update.get("clear_extra_headers"):
        problems = parse_extra_headers(str(update["extra_headers"]))[1]
        if problems:
            raise ValueError(" ".join(problems))
    new = dict(cfg)
    for k, v in update.items():
        if k not in DEFAULT_SETTINGS:
            continue
        if k in SECRET_KEYS and not v and not update.get("clear_" + k):
            continue
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = default
        else:
            v = str(v).strip()
        new[k] = v
    for k in SECRET_KEYS:
        if update.get("clear_" + k):
            new[k] = ""
    return merge_settings(new)


def export_folder(cfg: dict) -> str:
    folder = cfg.get("export_folder") or os.path.join(os.path.expanduser("~"), "Downloads")
    return os.path.expanduser(folder)


def make_connection(cfg: dict) -> Connection:
    cf = bool(cfg.get("cf_access_enabled"))
    headers, problems = parse_extra_headers(cfg.get("extra_headers", ""))
    for problem in problems:
        log.warning("Ignoring a setting: %s", problem)
    return Connection(
        base_url=cfg.get("base_url", ""),
        api_key=cfg.get("api_key", ""),
        cf_client_id=cfg.get("cf_client_id", "") if cf else "",
        cf_client_secret=cfg.get("cf_client_secret", "") if cf else "",
        verify_tls=bool(cfg.get("verify_tls", True)),
        ca_file=cfg.get("ca_file", ""),
        timeout=float(cfg.get("timeout_s") or 15),
        extra_headers=tuple(headers),
        header_problems=tuple(problems),
    )


def test_connection(client: BambuddyClient) -> dict:
    """Reachability, Cloudflare, API key and its permissions, as a list of findings."""
    lines: list[tuple[str, str]] = [("warn", p) for p in client.conn.header_problems]
    if client.conn.extra_headers:
        names = ", ".join(name for name, _ in client.conn.extra_headers)
        lines.append(("ok", f"Sending extra headers: {names}."))
    try:
        version = client.version()
    except BambuddyError as e:
        return {"ok": False, "lines": lines + [("error", e.message)]}
    via = " through Cloudflare" if client.behind_cloudflare else ""
    lines.append(("ok", f"Reached Bambuddy {version} at {client.base_url}{via}."))
    if client.conn.uses_cloudflare_access:
        lines.append(("ok", "Cloudflare Access accepted the service token."))
    try:
        auth_enabled = bool(client.auth_status().get("auth_enabled"))
    except BambuddyError as e:
        return {"ok": False, "lines": lines + [("error", e.message)]}
    if not client.conn.api_key:
        if auth_enabled:
            return {"ok": False, "lines": lines + [("error", "Bambuddy requires login: enter an API key.")]}
        lines.append(("warn", "No API key set. This server has authentication turned off, so it works without one."))
        return {"ok": True, "lines": lines}
    try:
        me = client.me()
    except BambuddyError as e:
        if e.status == 401:
            msg = "The API key is not valid (unknown, disabled or expired). Create one in Bambuddy > Settings > API Keys."
            return {"ok": False, "lines": lines + [("error", msg)]}
        return {"ok": False, "lines": lines + [("error", e.message)]}
    if not auth_enabled:
        lines.append(("warn", "Authentication is turned off on this server; the key is valid but not enforced."))
    lines.append(("ok", f"API key accepted (owner: {me.get('username', '?')})."))
    have = set(me.get("permissions") or [])
    missing: dict[str, list[str]] = {}
    for perm, (flag, what) in REQUIRED_PERMISSIONS.items():
        if perm not in have:
            missing.setdefault(flag, []).append(what)
    ok = True
    for flag, whats in missing.items():
        level = "error" if flag in ("can_manage_library", "can_read_status") else "warn"
        ok = ok and level != "error"
        lines.append(
            (level, f"The key lacks '{SCOPE_LABELS[flag]}' ({flag}), needed to {', '.join(whats)}. "
             "Create a key with that permission in Bambuddy > Settings > API Keys.")
        )
    if not missing:
        lines.append(("ok", "The key has every permission the plugin needs."))
    try:
        printers = client.printers()
        lines.append(("ok", f"{len(printers)} printer(s) visible."))
    except BambuddyError as e:
        lines.append(("warn", f"Could not list printers: {e.message}"))
    return {"ok": ok, "lines": lines}


def printer_view(printer: dict, status: dict | None) -> dict:
    """The fields the status table shows."""
    s = status or {}
    errors = [
        {"code": e.get("full_code") or e.get("code"), "severity": e.get("severity"), "text": e.get("description")}
        for e in s.get("hms_errors") or []
        if e.get("severity") in (1, 2)
    ]
    return {
        "id": printer["id"],
        "name": printer.get("name"),
        "model": printer.get("model"),
        "location": printer.get("location"),
        "online": bool(s.get("connected")) if s else None,
        "state": s.get("state"),
        "stage": s.get("stg_cur_name"),
        "job": s.get("subtask_name") or s.get("current_print"),
        "progress": s.get("progress"),
        "layer": s.get("layer_num"),
        "layers": s.get("total_layers"),
        "remaining_min": s.get("remaining_time"),
        "errors": errors,
        "awaiting_plate_clear": bool(s.get("awaiting_plate_clear")),
    }


@dataclass
class SendJob:
    """One send: a sliced file, what to do with it, and where."""

    path: str
    action: str  # print | queue | store
    printer_id: int | None = None
    target_model: str | None = None
    label: str = "printer"  # target name for messages
    plate: int | None = None  # None: every plate in the file
    options: dict = field(default_factory=dict)  # Bambuddy queue print options
    remember_key: str | None = None
    remember_target: str | None = None
    name: str | None = None  # file name in Bambuddy; default: the file's own name


@dataclass
class TrackedJob:
    item_id: int
    label: str
    target_label: str
    manual_start: bool
    printer_id: int | None = None
    created: float = field(default_factory=time.time)
    milestones: set = field(default_factory=set)
    reported_errors: set = field(default_factory=set)


class Cancelled(Exception):
    pass


class StateStore:
    """Small JSON file in the plugin's storage folder: remembered printers and uploads."""

    def __init__(self, folder: str | None):
        self.path = os.path.join(folder, "state.json") if folder else None
        self.data = {"remember": {}, "uploads": {}}
        if self.path and os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except (OSError, ValueError) as e:
                log.warning("Ignoring unreadable %s: %s", self.path, e)

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Could not save %s: %s", self.path, e)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Service:
    def __init__(self, host, storage_dir: str | None):
        self.host = host
        self.state = StateStore(storage_dir)
        self.cfg = merge_settings({})
        self.client: BambuddyClient | None = None
        self.server_version = ""
        self._tasks: queue.Queue = queue.Queue()
        self._jobs: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._starting = False
        self._page_seen = 0.0
        self._focus_printer: int | None = None
        self._printers: list[dict] = []
        self._statuses: dict[int, dict] = {}
        self._next_printer_list = 0.0
        self._next_status = 0.0
        self._next_track = 0.0
        self._tracked: dict[int, TrackedJob] = {}
        self._sending = False
        self.agent = AgentState()

    # ---- lifecycle ------------------------------------------------------------

    def ensure_started(self) -> bool:
        """Start the two plugin threads: a worker for requests and polling, and a runner
        for sends. Call it on the UI thread: OrcaSlicer asks the user once for permission
        to create threads, and off the UI thread the first attempt is refused."""
        if self._thread is not None:
            return True
        # The permission prompt runs a nested event loop, which can deliver another
        # page message and bring us back here; one prompt is enough.
        if self._starting:
            return False
        self._starting = True
        try:
            worker = threading.Thread(target=self._run, name="bambuddy-worker", daemon=True)
            worker.start()
            self._thread = worker
            threading.Thread(target=self._run_jobs, name="bambuddy-send", daemon=True).start()
        except (PermissionError, RuntimeError) as e:
            log.warning("Could not start the plugin threads: %s", e)
        finally:
            self._starting = False
        return self._thread is not None

    @property
    def started(self) -> bool:
        return self._thread is not None

    def stop(self) -> None:
        self._stop.set()
        self._tasks.put(None)
        self._jobs.put(None)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def configure(self, cfg: dict) -> None:
        self.cfg = merge_settings(cfg)
        self.client = BambuddyClient(make_connection(self.cfg)) if self.cfg["base_url"] else None
        self._next_printer_list = 0.0
        self.agent.next_announce = 0.0

    def submit(self, fn, *args) -> None:
        self._tasks.put((fn, args))

    # ---- worker ---------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._tasks.get(timeout=1.0)
            except queue.Empty:
                task = None
            if self._stop.is_set():
                break
            if task:
                fn, args = task
                self._safely(fn, *args)
            now = time.monotonic()
            if self.page_active and now >= self._next_status:
                self._next_status = now + max(2, int(self.cfg["poll_interval_s"]))
                self._safely(self.refresh_status)
            if self._tracked and now >= self._next_track:
                self._next_track = now + TRACK_INTERVAL_S
                self._safely(self.poll_tracked)
            if self.agent.live and now >= self.agent.next_announce:
                self.agent.next_announce = now + ANNOUNCE_INTERVAL_S
                self._safely(self.agent_announce)
            if self.agent.connected and now >= self.agent.next_push:
                self.agent.next_push = now + DEVICE_PUSH_INTERVAL_S
                self._safely(self.agent_push_status)

    def _run_jobs(self) -> None:
        while not self._stop.is_set():
            job = self._jobs.get()
            if job is None:
                break
            self._safely(job)

    def run_job(self, fn) -> bool:
        """Run a send on the send thread. False when the threads could not be started."""
        if not self.started:
            return False
        self._jobs.put(fn)
        return True

    def _safely(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - keep the worker alive, the traceback goes to the python log
            log.exception("Bambuddy plugin task %s failed", getattr(fn, "__name__", fn))

    # ---- page ----------------------------------------------------------------

    @property
    def page_active(self) -> bool:
        return time.monotonic() - self._page_seen < PAGE_TIMEOUT_S

    def page_heartbeat(self, visible: bool) -> None:
        self._page_seen = time.monotonic() if visible else 0.0
        if visible:
            self._next_status = min(self._next_status, time.monotonic())

    def page_snapshot(self) -> dict:
        return {
            "type": "init",
            "settings": public_settings(self.cfg),
            "export_folder": export_folder(self.cfg),
            "focus": self._focus_printer,
            "jobs": [self._job_view(j) for j in self._tracked.values()],
        }

    def run_test(self) -> None:
        if not self.client:
            self.host.post_page({"type": "test", "ok": False, "lines": [("error", "Enter the Bambuddy URL first.")]})
            return
        self.host.post_page({"type": "test", **test_connection(self.client)})

    def _fetch_printers(self, client: BambuddyClient, force: bool = False) -> list[dict]:
        now = time.monotonic()
        if force or now >= self._next_printer_list or not self._printers:
            self._printers = client.printers()
            self._next_printer_list = now + PRINTER_LIST_INTERVAL_S
        return self._printers

    def _fetch_statuses(self, client: BambuddyClient, printers: list[dict]) -> dict[int, dict]:
        statuses = {}
        for p in printers:
            try:
                statuses[p["id"]] = client.printer_status(p["id"])
            except BambuddyError as e:
                if e.kind in ("unreachable", "timeout", "cloudflare", "auth", "tls"):
                    raise
                statuses[p["id"]] = {}
        self._statuses = statuses
        return statuses

    def refresh_status(self, force_list: bool = False) -> None:
        client = self.client
        if client is None:
            self.host.post_page({"type": "printers", "printers": [], "error": "Bambuddy URL not set."})
            return
        try:
            printers = self._fetch_printers(client, force_list)
            statuses = self._fetch_statuses(client, printers)
        except BambuddyError as e:
            self.host.post_page({"type": "printers", "printers": [], "error": e.message})
            return
        views = [printer_view(p, statuses.get(p["id"])) for p in printers]
        self.host.post_page({"type": "printers", "printers": views, "focus": self._focus_printer, "error": None})

    def snapshot(self, printer_id: int) -> None:
        client = self.client
        if client is None:
            return
        try:
            jpeg = client.camera_snapshot(printer_id)
            data = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
            self.host.post_page({"type": "snapshot", "printer_id": printer_id, "data": data, "error": None})
        except BambuddyError as e:
            self.host.post_page({"type": "snapshot", "printer_id": printer_id, "data": None, "error": e.message})

    def focus(self, printer_id: int | None) -> None:
        self._focus_printer = printer_id

    def open_url(self, url: str) -> None:
        # Only links into the configured Bambuddy are opened.
        client = self.client
        if client and isinstance(url, str) and url.startswith(client.base_url + "/"):
            self.submit(self.host.open_url, url)

    # ---- manual send from the page (works without printer agents) -------------

    def list_exports(self) -> None:
        folder = export_folder(self.cfg)
        files = [p for p, _ in sliced3mf.recent_exports(folder)]
        self.host.post_page({"type": "exports", "folder": folder, "files": files})

    def describe_file(self, path: str, context: dict) -> None:
        """File details plus the ranked printer list for the page's send form."""
        try:
            info = sliced3mf.inspect(path)
        except sliced3mf.NotASlicedFile as e:
            self.host.post_page({"type": "file", "path": path, "error": str(e)})
            return
        problem = sliced3mf.filename_problem(info.filename)
        self.host.post_page(
            {"type": "file", "path": path, "file": info.summary(), "error": problem and problem + " Rename the file and export it again."}
        )
        client = self.client
        if client is None:
            self.host.post_page({"type": "targets", "targets": [], "error": "Set the Bambuddy URL and API key first."})
            return
        try:
            printers = self._fetch_printers(client, force=True)
            statuses = self._fetch_statuses(client, printers)
        except BambuddyError as e:
            self.host.post_page({"type": "targets", "targets": [], "error": e.message})
            return
        key = info.printer_preset or context.get("printer_preset")
        remembered = self.state.data["remember"].get(key) if key and self.cfg["remember_printer"] else None
        m = matching.choose(printers, statuses, info.sliced_model, remembered=remembered, host_hint=context.get("print_host"))
        selected = m.selected if self.cfg["printer_selection"] == "auto" else None
        self.host.post_page(
            {
                "type": "targets",
                "targets": [t.as_dict() for t in m.targets],
                "selected": selected,
                "reason": m.reason if selected else "Choose a printer.",
                "sliced_model": info.sliced_model,
                "default_action": self.cfg["default_action"],
                "error": None,
            }
        )

    def page_check(self, path: str, target: dict | None, action: str | None) -> None:
        """Warn or block in the send form as soon as an obvious mismatch is chosen."""
        try:
            info = sliced3mf.inspect(path)
        except sliced3mf.NotASlicedFile:
            return
        fields = matching.Target.__dataclass_fields__
        t = matching.Target(**{k: target.get(k) for k in fields if k in target}) if target else None
        verdict = matching.check_target(info.sliced_model, t, action or "print")
        level, message = verdict if verdict else ("", "")
        self.host.post_page({"type": "check", "level": level, "message": message})

    def page_send(self, request: dict, context: dict) -> None:
        """The page's Send button. Runs the send on its own thread."""
        target = request.get("target") or {}
        action = request.get("action")
        if action not in ACTIONS:
            self.host.post_page({"type": "send_result", "ok": False, "message": "Choose what to do with the file."})
            return
        if action != "store" and not (target.get("printer_id") or target.get("target_model")):
            self.host.post_page({"type": "send_result", "ok": False, "message": "Choose a printer."})
            return
        if self._sending:
            self.host.post_page({"type": "send_result", "ok": False, "message": "A send is already running."})
            return
        plate = request.get("plate")
        job = SendJob(
            path=request.get("path", ""),
            action=action,
            printer_id=target.get("printer_id"),
            target_model=target.get("target_model"),
            label=target.get("label") or "printer",
            plate=None if plate in (None, "", "all") else int(plate),
            remember_key=request.get("remember_key") or context.get("printer_preset"),
            remember_target=target.get("id"),
        )
        self._sending = True

        def run():
            try:
                result = self.send(job, lambda pct, text: self.host.post_page({"type": "progress", "pct": pct, "text": text}))
            finally:
                self._sending = False
            self.host.post_page({"type": "send_result", **result})

        if not self.run_job(run):
            self._sending = False
            self.host.post_page({"type": "send_result", "ok": False, "message": NO_THREADS})

    # ---- the send itself ------------------------------------------------------

    def send(self, job: SendJob, progress, cancelled=lambda: False) -> dict:
        """Upload, then queue or start. Blocking; returns {"ok", "message", "url", ...} and
        notifies the user either way."""
        try:
            result = self._send(job, progress, cancelled)
        except Cancelled:
            return {"ok": False, "message": "Cancelled.", "cancelled": True}
        except BambuddyError as e:
            result = {"ok": False, "message": e.message, "kind": e.kind}
        except sliced3mf.NotASlicedFile as e:
            result = {"ok": False, "message": str(e), "kind": "bad_file"}
        except Exception as e:  # noqa: BLE001
            log.exception("Send failed")
            result = {"ok": False, "message": f"Unexpected error: {e}", "kind": "bug"}
        if result["ok"]:
            url = result["url"]
            self.host.notify("success", result["message"], "Open in Bambuddy", lambda: self.host.open_url(url))
        else:
            self.host.notify("error", "Bambuddy: " + result["message"])
        return result

    def _send(self, job: SendJob, progress, cancelled) -> dict:
        client = self.client
        if client is None:
            raise BambuddyError("bad_url", "Set the Bambuddy URL and API key in the Bambuddy tab first.")
        info = sliced3mf.inspect(job.path)
        name = job.name or info.filename
        problem = sliced3mf.filename_problem(name)
        if problem:
            raise BambuddyError("bad_request", problem)
        if job.action != "store":
            self._check_target(client, job, info)
        limit = int(self.cfg["upload_limit_mb"]) or (CLOUDFLARE_FREE_UPLOAD_LIMIT_MB if client.behind_cloudflare else 0)
        if limit and info.size > limit * 1024 * 1024:
            raise BambuddyError(
                "too_large",
                f"{name} is {info.size / 1048576:.0f} MB; the upload limit is {limit} MB (Cloudflare's free "
                "plan). Reduce the file size, or raise 'Upload limit' if your Cloudflare plan allows more.",
            )

        file_id, how = self._upload(client, info.path, name, progress, cancelled)
        if job.action == "store":
            return {"ok": True, "message": f"{name} {how} the Bambuddy library.", "url": client.web_url("/files"), "file_id": file_id}

        plates = [p.index for p in info.plates] if job.plate is None else [job.plate]
        manual = job.action == "queue"
        progress(100, "Adding to the Bambuddy queue")
        items = []
        for plate_id in plates:
            item = client.add_to_queue(
                file_id,
                printer_id=job.printer_id,
                target_model=job.target_model,
                manual_start=manual,
                plate_id=plate_id,
                options=job.options,
            )
            items.append(item)
            label = name if len(plates) == 1 else f"{name} (plate {plate_id})"
            self._tracked[item["id"]] = TrackedJob(item["id"], label, job.label, manual, item.get("printer_id") or job.printer_id)
        if job.remember_key and job.remember_target and self.cfg["remember_printer"]:
            self.state.data["remember"][job.remember_key] = job.remember_target
            self.state.save()
        if job.printer_id:
            self._focus_printer = job.printer_id
            self.host.post_page({"type": "focus", "printer_id": job.printer_id})
        self._next_track = 0.0
        what = f"{len(items)} plates of {name}" if len(items) > 1 else name
        if manual:
            msg = f"{what} added to the Bambuddy queue for {job.label}; start it in Bambuddy when ready."
        else:
            msg = f"{what} sent to {job.label} via Bambuddy; it starts as soon as the printer is free."
        return {"ok": True, "message": msg, "url": client.web_url("/queue"), "queue_ids": [i["id"] for i in items]}

    def _check_target(self, client: BambuddyClient, job: SendJob, info: sliced3mf.SlicedFile) -> None:
        if job.printer_id is None:
            return
        printer = next((p for p in self._fetch_printers(client) if p["id"] == job.printer_id), None)
        if printer is None:
            printer = next((p for p in self._fetch_printers(client, force=True) if p["id"] == job.printer_id), None)
        if printer is None:
            raise BambuddyError("not_found", f"{job.label} no longer exists in Bambuddy.")
        if matching.models_compatible(info.sliced_model, printer.get("model")) is False:
            raise BambuddyError(
                "bad_request",
                f"This plate was sliced for {info.sliced_model}, but {printer.get('name')} is a "
                f"{printer.get('model')}. Select the matching printer preset and slice again.",
            )

    def _upload(self, client: BambuddyClient, path: str, name: str, progress, cancelled) -> tuple[int, str]:
        """Upload unless the identical file is already in the library. Returns (id, verb)."""
        reuse = self.cfg["duplicate_policy"] == "reuse"
        digest = sha256_of(path)
        uploads = self.state.data["uploads"]
        if reuse and digest in uploads:
            try:
                existing = client.library_file(uploads[digest])
                if existing.get("file_hash") == digest and not existing.get("deleted_at"):
                    progress(100, "Identical file already in the Bambuddy library")
                    return existing["id"], "was already in"
            except BambuddyError as e:
                if e.kind != "not_found":
                    raise
            uploads.pop(digest, None)

        last = [0.0]

        def on_chunk(sent: int, total: int) -> None:
            if cancelled():
                raise Cancelled()
            now = time.monotonic()
            if now - last[0] >= 0.25 or sent == total:
                last[0] = now
                progress(int(sent * 100 / total) if total else 100, f"{sent / 1048576:.1f} of {total / 1048576:.1f} MB")

        resp = client.upload_library_file(path, name, progress=on_chunk)
        file_id, verb = resp["id"], "was uploaded to"
        dup = resp.get("duplicate_of")
        if dup and reuse:
            # Bambuddy keeps both copies and reports the older one; keep the older one.
            try:
                client.delete_library_file(file_id)
                file_id, verb = dup, "was already in"
            except BambuddyError as e:
                log.warning("Could not remove duplicate upload %s: %s", file_id, e.message)
        uploads[digest] = file_id
        self.state.save()
        return file_id, verb

    # ---- milestones ----------------------------------------------------------

    def _job_view(self, job: TrackedJob) -> dict:
        return {"id": job.item_id, "label": job.label, "target": job.target_label, "milestones": sorted(job.milestones)}

    def _milestone(self, job: TrackedJob, name: str, level: str, text: str) -> None:
        if name in job.milestones:
            return
        job.milestones.add(name)
        client = self.client
        url = client.web_url("/queue") if client else ""
        self.host.notify(level, text, "Open in Bambuddy" if url else "", (lambda: self.host.open_url(url)) if url else None)
        self.host.post_page({"type": "job", "job": self._job_view(job)})

    def poll_tracked(self) -> None:
        client = self.client
        if client is None:
            return
        for item_id, job in list(self._tracked.items()):
            if time.time() - job.created > TRACK_MAX_AGE_S:
                self._tracked.pop(item_id, None)
                continue
            try:
                item = client.queue_item(item_id)
            except BambuddyError as e:
                if e.kind == "not_found":
                    self._milestone(job, "removed", "warning", f"{job.label} was removed from the Bambuddy queue.")
                    self._tracked.pop(item_id, None)
                continue
            if self._update_job(client, job, item):
                self._tracked.pop(item_id, None)

    def _update_job(self, client: BambuddyClient, job: TrackedJob, item: dict) -> bool:
        """Advance one job's milestones. True when there is nothing left to watch."""
        status = item.get("status")
        job.printer_id = item.get("printer_id") or job.printer_id
        if status == "pending":
            if job.manual_start:
                self._milestone(job, "staged", "info", f"{job.label} is waiting in the Bambuddy queue for a manual start.")
            else:
                self._milestone(job, "queued", "info", f"{job.label} is queued in Bambuddy for {job.target_label}.")
            return False
        if status in ("failed", "cancelled", "skipped"):
            reason = item.get("error_message") or status
            self._milestone(job, "error", "error", f"{job.label} did not print: {reason}")
            return True
        if status not in ("printing", "completed"):
            return False
        self._milestone(job, "started", "info", f"{job.label} started printing on {job.target_label}.")
        if status == "completed":
            self._milestone(job, "first_layer", "success", f"{job.label} finished.")
            return True
        if job.printer_id is None:
            return False
        try:
            st = client.printer_status(job.printer_id)
        except BambuddyError:
            return False
        for err in st.get("hms_errors") or []:
            code = err.get("full_code") or err.get("code")
            if err.get("severity") in (1, 2) and code not in job.reported_errors:
                job.reported_errors.add(code)
                self.host.notify("error", f"{job.label}: the printer reports an error: {err.get('description') or 'HMS ' + str(code)}")
        if st.get("state") == "FAILED":
            self._milestone(job, "error", "error", f"{job.label} failed on the printer.")
            return True
        if (st.get("layer_num") or 0) >= 2:
            self._milestone(job, "first_layer", "success", f"{job.label}: first layer finished.")
            return True
        return False

    # ---- printer agent ----------------------------------------------------------

    def agent_announce(self) -> None:
        """Report every active Bambuddy printer to OrcaSlicer's device list."""
        client, fn = self.client, self.agent.callbacks.get("ssdp")
        if client is None or fn is None:
            return
        try:
            printers = self._fetch_printers(client, force=True)
        except BambuddyError as e:
            log.warning("Bambuddy printer discovery failed: %s", e.message)
            return
        host = urllib.parse.urlsplit(client.base_url).netloc
        for p in printers:
            if p.get("is_active", True):
                fn(bd.ssdp_record(p, host))

    def agent_connect(self, dev: str, access_code: str) -> None:
        """connect_printer(): report the version, a first status, then 'connected'."""
        client = self.client
        if client is None or self.printer_for_dev(dev) is None:
            self.agent.emit("local_connect", 1, dev, "Bambuddy is not configured or the printer is gone")
            return
        try:
            self.server_version = self.server_version or client.version()
        except BambuddyError as e:
            self.agent.emit("local_connect", 1, dev, e.message)
            self.host.notify("error", "Bambuddy: " + e.message)
            return
        self.agent.access_code = access_code
        self.agent.emit("local_message", dev, bd.version_info(self.server_version))
        self.agent_push_status()
        self.agent.emit("local_connect", 0, dev, "0")
        self.agent.emit("printer_connected", dev)

    def agent_push_status(self) -> None:
        dev = self.agent.connected
        client = self.client
        printer = bd.find_printer(self._printers, dev or "")
        if not dev or client is None or printer is None:
            return
        try:
            status = client.printer_status(printer["id"])
        except BambuddyError as e:
            log.warning("Status of %s failed: %s", dev, e.message)
            return
        self.agent.sequence += 1
        self.agent.emit("local_message", dev, bd.push_status(status, self.agent.sequence))

    def agent_command(self, dev: str, json_str: str) -> int:
        """send_message*(): answer the queries the device layer needs; no printer control yet."""
        section, command = bd.command_of(json_str)
        if section == "info" and command == "get_version":
            self.submit(lambda: self.agent.emit("local_message", dev, bd.version_info(self.server_version or "?")))
            return bd.OK
        if section == "system" and command == "get_access_code":
            self.submit(lambda: self.agent.emit("local_message", dev, bd.access_code_reply(self.agent.access_code)))
            return bd.OK
        if section == "pushing" or (section == "print" and command in ("push_status", "pushall")):
            self.agent.next_push = 0.0
            return bd.OK
        # Pause/resume/stop, temperatures, AMS and camera commands are phase 2.
        return bd.ERR_NOT_SUPPORTED

    def printer_for_dev(self, dev: str) -> dict | None:
        printer = bd.find_printer(self._printers, dev)
        if printer is None and self.client is not None:
            try:
                printer = bd.find_printer(self._fetch_printers(self.client, force=True), dev)
            except BambuddyError:
                pass
        return printer

    def printer_name(self, dev: str) -> str:
        printer = bd.find_printer(self._printers, dev)
        return (printer or {}).get("name") or dev

    def agent_job(self, kind: str, params, update, cancelled, choose) -> int:
        """start_local_print / start_send_gcode_to_sdcard on OrcaSlicer's job thread.

        ``kind`` is "print" (Print button) or "send" (Send button). ``choose(default)`` asks
        the user for the action when the settings say so and returns None on cancel. The
        upload itself runs on a plugin thread; this thread relays its progress.
        """
        dev = getattr(params, "dev_id", "")
        printer = bd.find_printer(self._printers, dev)
        if printer is None:
            self.host.notify("error", "Bambuddy: the selected printer is not known to Bambuddy (any more).")
            return bd.ERR_FAILED
        pid = printer["id"]
        default = self.cfg["default_action"]
        if kind == "send" and default == "print":
            default = "queue"  # the Send button never starts a print on its own
        action = choose(default) if self.cfg["ask_action"] else default
        if action is None:
            return bd.ERR_CANCELED
        name = printer.get("name") or dev
        plate = int(getattr(params, "plate_index", 0) or 0)
        job = SendJob(
            path=params.filename,
            action=action,
            printer_id=pid,
            label=name,
            plate=plate if plate > 0 else None,
            options=bd.queue_options(params) if kind == "print" else {},
            name=job_name_from_params(params, self.agent.job_titles.pop(dev, None)),
        )

        events: queue.Queue = queue.Queue()
        abort = threading.Event()
        result: dict = {}

        def worker():
            result.update(self.send(job, lambda pct, text: events.put((pct, text)), abort.is_set))
            events.put(None)

        update(bd.STAGE_CREATE, 0, "Preparing")
        if not self.run_job(worker):
            update(bd.STAGE_ERROR, bd.ERR_UPLOAD_FAILED, NO_THREADS)
            self.host.notify("error", "Bambuddy: " + NO_THREADS)
            return bd.ERR_FAILED
        while True:
            try:
                ev = events.get(timeout=0.2)
            except queue.Empty:
                if cancelled():
                    abort.set()
                continue
            if ev is None:
                break
            update(bd.STAGE_UPLOAD, ev[0], ev[1])
        if result.get("cancelled"):
            return bd.ERR_CANCELED
        if not result.get("ok"):
            update(bd.STAGE_ERROR, bd.ERR_UPLOAD_FAILED, result.get("message", ""))
            return bd.ERR_UPLOAD_FAILED if kind == "print" else bd.ERR_SEND_UPLOAD_FAILED
        update(bd.STAGE_FINISHED, 100, "3")
        return bd.OK


def job_name_from_params(params, title: str | None = None) -> str:
    """The job's name in Bambuddy, with the characters a printer's SD card cannot store replaced.

    ``title`` is the name shown (and editable) in OrcaSlicer's print dialog, taken from the
    PrintJobStarted/SendJobStarted event. It is preferred over ``params.project_name``, which
    OrcaSlicer replaces with the MakerWorld model title for projects downloaded from MakerWorld.
    """
    name = (title or "").strip()
    for attr in ("project_name", "task_name"):
        if name:
            break
        value = (getattr(params, attr, "") or "").strip()
        if value and value != "verify_job":
            name = value
    name = name or os.path.basename(getattr(params, "filename", "") or "") or "print"
    for suffix in (".gcode.3mf", ".3mf"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    name = "".join("_" if c in sliced3mf.INVALID_FILENAME_CHARS or ord(c) < 0x20 else c for c in name)
    name = name.rstrip(" .")[:200] or "print"
    return name + ".gcode.3mf"


@dataclass
class AgentState:
    live: bool = False
    connected: str | None = None
    selected: str = ""
    access_code: str = ""
    sequence: int = 0
    next_announce: float = 0.0
    next_push: float = 0.0
    callbacks: dict = field(default_factory=dict)
    job_titles: dict = field(default_factory=dict)  # dev_id -> name from the print dialog

    def emit(self, key: str, *args) -> None:
        fn = self.callbacks.get(key)
        if fn is not None:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                log.exception("OrcaSlicer callback %s failed", key)
