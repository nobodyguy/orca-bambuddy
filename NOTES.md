# Research and verification notes

## Versions verified against

| Component | Version | Source |
|---|---|---|
| OrcaSlicer | 2.5.0-alpha, git `3880f2c` (2026-10-16, "Bump version to 2.5.0-alpha") | source in `/home/devel/repos/OrcaSlicer` and the release AppImage `OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0-alpha` (embedded CPython 3.12.13, OpenSSL 1.1.1w) |
| OrcaSlicer | 2.5.0-beta, tag `v2.5.0-beta` (2026-10-27, build `345efec6`) | the plugin audit sources at the tag (`PluginAuditManager.cpp`, `PythonPluginBridge.cpp`); see "Audit changes in 2.5.0-beta" |
| Bambuddy (primary) | 1.2.5.7, tag `v1.2.5.7` = `188df76` | source at the tag; Docker image `ghcr.io/maziggy/bambuddy:1.2.5.7` |
| Bambuddy (extra) | 1.3.0b1 | a `demo.bambuddy.cool` session with simulated printers |

The OrcaSlicer wiki (plugin system, plugin development, API reference, audit hook pages) was read
too, but **it describes a newer API than the 2.5.0-alpha release**; see "Gaps" below. Where they
differed, the source and the AppImage won.

## OrcaSlicer plugin API used

Everything below is in `src/orca_bambuddy/orca_adapter.py`.

| API | Use |
|---|---|
| `.whl` plugin, `METADATA` + `top_level.txt` | packaging; `PluginLoader` installs a pure `py3-none-any` wheel and imports the package. The file name is not parsed (`read_wheel_plugin_metadata` reads `.dist-info`), so the wheel ships as `orca_bambuddy_any.whl` (see below) |
| `@orca.plugin`, `orca.base`, `orca.register_capability` | registration of two capabilities |
| `orca.printer_agent.PrinterAgentBase`, `AgentInfo` | the "Bambuddy" printer agent (capability *Bambuddy printer agent*, type `printer-connection`, agent id `bambuddy`) |
| `orca.pages.PagesPluginCapabilityBase` (`get_ui`, `on_message`, `post_message`) | the Bambuddy tab |
| `get_config` / `save_config` / `get_default_config` / `has_config_ui` / `get_config_ui` | settings, stored on the agent capability; custom HTML form in the Plugins dialog |
| `orca.host.plugin.storage()` | folder for `state.json` |
| `orca.host.preset_bundle()` → `current_printer_preset().name`, `full_config_value("print_host")` | remember key and IP hint for matching |
| `orca.host.ui.push_notification(level, text, hyper_text, on_click)` | notifications |
| `orca.host.ui.create_window(html, ..., style=WINDOW_MODAL, on_submit, on_close)` | the per-send action chooser |
| `on_load` / `on_unload` | shared runtime with reference counting |
| `PagesPluginCapabilityBase.get_icon()` | tab icon `assets/bambuddy_tab.svg`, returned as an absolute path. `PluginPages` strips the extension and calls `create_scaled_bitmap(path, …, 20)`. `Slic3r::var()` keeps an existing absolute path, so the file is loaded at 20 px. Drawn like OrcaSlicer's `tab_*_active.svg`: 20×20, white 1 px strokes, because the tab bar is dark in both themes. |

Printer agent methods implemented: `get_agent_info`, all nine `set_*_fn` callback setters,
`start_discovery`, `get/set_user_selected_machine`, `connect_printer`, `disconnect_printer`,
`send_message`, `send_message_to_printer`, `start_send_gcode_to_sdcard` (including OrcaSlicer's
`verify_job` probe), `start_local_print`, `start_local_print_with_record`, `start_print`,
`start_sdcard_print` (not supported), `bind_detect`, `check_cert`, `ping_bind`, `bind`, `unbind`,
`request_bind_ticket`.

### How OrcaSlicer drives an agent (read from the source, confirmed in the GUI)

- With `use_printer_agents` on and the preset's `printer_agent` = `bambuddy`,
  `GUI_App::switch_printer_agent` makes the agent live and calls `start_discovery(True, False)` on
  the UI thread. An explicit `printer_agent` overrides the Bambu default even for Bambu Lab presets
  (`GUI_App::resolve_printer_agent_id`).
- Discovery: the agent reports one SSDP-style JSON record per Bambuddy printer through the
  `ssdp` callback. `dev_type` must be the model id of OrcaSlicer's Bambu profiles (`BL-P001`,
  `C12`, `N1`, …) so the print dialog can compare printer and preset.
- `dev_id` is `bambuddy-<serial number>`. OrcaSlicer stores the access code per device id, and an
  earlier `bambuddy-<Bambuddy printer id>` scheme collided between two Bambuddy servers.
- A LAN device is selectable only once it has an access code (`MachineObject::has_access_right`,
  `DeviceManager::get_my_machine_list`), and LAN messages are only routed to such devices. So the
  agent cannot set the code itself; OrcaSlicer asks the user once (`ConnectPrinterDialog`).
- Selecting a device calls `connect_printer`. The agent answers like the built-in Moonraker agent:
  - a `get_version` info message (needed for `MachineObject::is_info_ready`);
  - a full `push_status` (`msg: 0`);
  - the `local_connect(0, dev, "0")` and `printer_connected(dev)` callbacks;
  - after that, a `push_status` every 3 s.
- Device names get a "(LAN)" suffix from OrcaSlicer itself (`SelectMachinePop.cpp`,
  `SelectMachine.cpp`, `SendToPrinter.cpp`) for every LAN-mode device. Plugin agents can only
  report LAN devices: "cloud" devices must come from a cloud account's bound-printer list.
- Preselection in the print dialog (`SelectMachineDialog::update_user_printer` /
  `_collect_sorted_machines`): the selected device first; otherwise devices whose `printer_type`
  equals the preset's model id, available ones first, then sorted by name. Only devices with an
  access code are listed. An agent has no API to select a device; `get_user_selected_machine()` is
  consulted only for cloud machines.
- **Print plate** → `SelectMachineDialog` (Bambu's dialog) → `PrintJob` → `start_local_print`.
  `params.filename` is a temp `.3mf` containing the sliced plate(s), `params.plate_index` is
  1-based, and `params.ams_mapping` is a JSON list of global tray ids. The dialog blocks while the
  printer reports a printing state, and needs `sdcard` true in LAN mode.
- **Send** → `SendToPrinterDialog` → `start_send_gcode_to_sdcard`. Not blocked while printing.
- Progress: `update_fn(stage, percent, text)` with `SendingPrintJobStage` values. A failure shows
  OrcaSlicer's generic message, so the plugin also posts an error notification with the reason.
- `SendToPrinterDialog` refuses with "The printer does not support sending to printer storage"
  unless the push has `support_send_to_sd: true` (`SendToPrinter.cpp:1349`); every push carries
  it since 0.4.0.
- AMS mapping: the flat `params.ams_mapping` has -1 for an external spool
  (`SelectMachine.cpp` `get_ams_mapping_result`); `params.ams_mapping2` names it as
  `{ams_id: 255|254, slot_id: 0}` (unmapped: 255/255). The plugin puts 255/254 into the flat list,
  which is what Bambuddy's queue takes (it rebuilds `ams_mapping2` itself, and uses 254 only on
  two-nozzle printers).

### Device tab connection

Read from `DeviceManager.cpp` (2.5.0-alpha):
- "Connecting..." (`is_connecting()`) lasts until one `push_status` is parsed after the printer was
  selected; "Failed to connect to the printer" follows 30 s without any message
  (`DISCONNECT_TIMEOUT`). OrcaSlicer never reconnects by itself; any message brings it back.
  `local_connect` Lost would deselect the printer, so the plugin never sends it.
- SSDP records do not expire; one announce is enough.
- The plugin therefore sends a status every 3 s even when Bambuddy fails: the last known one for
  `STATUS_STALE_S` (30 s), then `gcode_state` OFFLINE, with one warning notification per failure
  streak and an info when it is back. The status request of the device thread times out after
  8 s (`DEVICE_STATUS_TIMEOUT_S`).
- A preset with a print host makes `GUI_App::select_machine` select a device named after that
  address (`dev_id_from_address`). `connect_printer` refuses it with an explanation.

### Two-nozzle printers (H2D, H2D Pro, H2C)

OrcaSlicer takes the extruder count only from `print.device.extruder.state` bits 0-3, and reads
`print.device` only when `print` also has the hex strings `cfg`, `fun`, `aux` and `stat`
(`DeviceManager.cpp` `check_enable_np`, `parse_new_info`). Without it an H2D shows one nozzle,
and the print dialog maps filaments to any AMS regardless of side. Bambuddy (1.2.5.7 = main for
these fields) reports `nozzle_count` in the printer list, and in the status `temperatures.nozzle`
= left (id 1), `nozzle_2` = right (id 0), `nozzles[]`/`nozzle_rack[]` by id (`nozzle_rack` holds
the printer's own type code such as `HH01`), `extruder_slots`, `active_extruder`,
`ams_extruder_map`, `vt_tray` ids 254 (left) and 255 (right).

`bambu_dialect.dual_fields` sends, only when `is_dual` (nozzle count 2, or two extruder slots;
sticky per device):
- `cfg`, `fun`, `stat` empty (OrcaSlicer keeps its defaults), `aux` `"1000"` (bits 12-13 = 1:
  storage present, otherwise LAN printing is blocked). `fun` bit 60 (H2C rack) stays 0.
- `device.extruder`: `state` = 2 | active << 4 | active << 8; `info` in id order 0, 1, each with
  every key the parser reads without checking (`id`, `filam_bak`, `info`, `temp`, `spre`,
  `snow`, `star`, `stat`, `hnow`): a missing key is undefined behaviour, not an exception.
  `temp` = target << 16 | current; `snow` = ams_id << 8 | slot_id, 0xFFFF for none.
- `device.nozzle.info`: id, `diameter` as a number (a string throws), `type`. A plain material
  name means Standard flow, a 4-character code carries the flow (`H` = high flow); `N/A` would
  mean unknown flow, which blocks printing, so it is never sent.
- `vir_slot` with both spools, 255 then 254: the two-nozzle AMS panel asserts or indexes out of
  range without them (`AMSControl.cpp`).
- `ams[].info`: type in bits 0-3, nozzle in bits 8-11 (never 0xE, which hides the unit).

Single-nozzle printers get the same push as 0.3.1 plus `support_send_to_sd` (checked against the
0.3.1 code). The temperature and AMS controls of the Device tab still answer "not supported".

### Agent not started at startup (2.5.0-alpha)

The plugin loads on a background thread; its printer agent is registered on the UI thread
later. At startup `switch_printer_agent` can run in between ("agent ID 'bambuddy' is
unregistered"), and nothing switches again after the registration, so `start_discovery` is never
called and the Device tab lists no Bambuddy printers until another preset is selected. Seen on
every start of the headless 2.5.0-alpha; the 2.5.0-beta switched again after registering. A
plugin cannot select a preset, so `BambuddyAgent._check_live` posts one warning on the first
NewProject, ProjectOpened, ObjectAdded or PlateSelected event while the selected preset asks for
`bambuddy` and the agent has not started (PresetSelected is skipped: it fires around OrcaSlicer's
own switch).

### Threading and the permission (audit) model

- Agent methods run on the UI thread (selection, discovery, connect) or on OrcaSlicer job threads
  (`start_*`). Page `on_message` runs on the UI thread.
- Every plugin call opens an audit context. Network events are keyed by the full URL
  (`urllib.Request` target = URL incl. path and query), and file events by path. Off the UI
  thread a first-time prompt is posted asynchronously and the event is **denied**
  (`PluginAuditManager.cpp::request_permission`).
- In 2.5.0-alpha, threads the plugin starts are not attributed to it (thread-local audit context;
  2.5.0-beta changed this, see below), and thread creation is a single persisted grant
  (`threading: ["thread"]`).
- Therefore the plugin does all network and file I/O on three long-lived threads: the worker
  (Bambuddy tab, job tracking), the send runner, and the device thread (discovery, connect, the
  Device tab status and replies to OrcaSlicer's queries). The device thread is separate so that a
  page refresh of every printer, each request with the full timeout, cannot delay the status
  OrcaSlicer needs within 30 s (see "Device tab connection"). They are started on the first
  **UI-thread** entry (`start_discovery`, `connect_printer` or a page message), so the one thread
  prompt appears inline. A re-entrancy guard is needed
  because the modal prompt runs a nested event loop that delivers further page messages.
  Previously this showed two prompts; now one.
- Host UI calls are safe from those threads: `post_message` uses `CallAfter`, and
  `push_notification` and `create_window` marshal to the UI thread and release the GIL.

### Audit changes in 2.5.0-beta (GitHub issue #1)

0.2.0 failed on 2.5.0-beta with *Not a 3MF file: .2.0.3mf (Plugin attempted audited operation
"open" without permission: audit permission required)*, and with `[AUDIT BLOCKED] ...
event=urllib.Request` in the debug log. Causes:

- `59e96b71d0` "shim python create thread to audit threads (#16682)" wraps
  `_thread.start_new_thread`, so the plugin's worker and send threads now run in the plugin's audit
  context. Their first access to a path or URL is denied, and the prompt comes afterwards
  (`request_permission`, off the UI thread).
- A Yes at runtime saves the exact target. The sliced file of a Print/Send is
  `<temp>/orcaslicer_<uid>/orcaslicer_model/<date>/<time>#<pid>#<id>/Metadata/.<pid>.<n>.3mf`
  (`Model::get_backup_path`, `Plater` → `get_tmp_gcode_path()` with the extension `3mf`). It is new
  for every session and plate. A runtime URL grant is the full URL, including printer ids, queue ids
  and the camera token. Neither matches the next time.
- Printer-agent calls get no scoped allowed root (only SlicingPipeline does). The host API has no
  temp-dir accessor.

What the beta offers (`b728e79f26`, `2410112046`, `6a0223f055`):
`orca.request_permissions(fs_read, fs_readwrite, network_http, network_socket, process,
threading: bool, network)`, only during `register_capabilities()`. One prompt follows. A No
aborts the load. Already granted targets are not asked for again. `"<dir>/*"` grants a folder tree
(fs only, canonical paths). `network` URLs are reduced to `scheme://host` and cover every port,
path and raw socket of that host.

What the plugin does (`orca_adapter.declare_permissions`):
- It asks for `threading=True`, `fs_read` on the temp tree (`service.orca_model_temp_dir()`, which
  mirrors `per_user_temp_dir(wxFileName::GetTempDir(), getuid())`; not `tempfile.gettempdir()`,
  because that probes by creating files) and on the export folder, and `network` for the saved
  Bambuddy URL.
- Capability settings are not available through the API during load, and neither is
  `orca.host.plugin.storage()` ("must be called from a plugin callback"; seen in the beta).
  `load_permission_targets` finds the folders from the module's own path:
  `<data dir>/orca_plugins/<key>.whl/__whl_extracted__/...`, where the key is the wheel's stem. It
  reads the agent's entry for that key from `orca_plugins/config.json`. The file sits in
  `data_dir()`, which is an allowed root, and its name is not on the denied list. The format is
  internal to OrcaSlicer, so `Service.configure` also keeps a copy in
  `plugin_data/<key>/state.json` (`permission_targets`) as a fallback.
- On 2.5.0-alpha the call raises `TypeError` (fs_read only). `declare_permissions` returns None
  then, and nothing changes.
- On the beta `Service.network_grant` is the granted origin. A URL on another host gets no client:
  every request would only produce a useless per-URL prompt and a denial. The plugin shows "restart
  OrcaSlicer and answer Yes" instead.
- Links to Bambuddy (the tab's button and the notifications) start one process: `xdg-open <url>`,
  `open <url>` or `os.startfile(url)`. They always open the dashboard (`<base URL>/`). The beta
  asks once per exact command line. `webbrowser.open` first ran `xdg-settings get
  default-web-browser` and then tried `xdg-open`, `gio`, `x-www-browser`, `firefox`,
  `google-chrome`, … one by one. That gave about five prompts per click, with the URL in each, so
  every page asked again. `declare_permissions` adds that one command line to the load-time
  request (`process`, matched verbatim against `str(args)`; see `launch_command`), so clicking
  a link never prompts. Without a saved URL at load there is one prompt on the first click,
  remembered after Yes. No `DEVNULL`
  redirects, because opening `os.devnull` is a file access the beta would ask about too. On the
  beta the launch runs on the UI thread, which asks; on a plugin thread it would be refused. On the
  alpha it runs on the worker, as before. The host API (beta and `main`) has no URL opener. A link
  in a plugin page loads inside the page (`PluginPage::on_new_window`).
- Denials still reaching the code are reported as such: `NotASlicedFile` "may not read",
  `BambuddyError` kind `blocked`.
- Lazy stdlib imports must not happen first on a plugin thread. The beta sets `pycache_prefix` to
  `<data dir>/python/pycache` (`d3699dd9f6`). On the AppImage the cached path contains the random
  `/tmp/.mount_OrcaSlXXXX` folder, so stdlib modules are compiled again on every launch.
  `importlib._bootstrap_external._write_atomic` wraps an fd in `FileIO`. That raises an `open`
  event without a path, so OrcaSlicer can only grant it as "any open" (`FsReadWrite:open`). On a
  plugin thread it is denied, and importlib ignores the failure. A target-less prompt is still
  queued on every send. `socket.getaddrinfo` imports `encodings.idna` (and `stringprep`), and
  `zipfile` imports `encodings.cp437`. `orca_adapter` imports these while the plugin loads, where
  compiling raises no prompt.

### Plugin image

The Plugins dialog's thumbnail comes only from an OrcaCloud listing image
(`PluginDescriptor::thumbnail_url`, "empty for local plugins"). A wheel cannot carry one, so
`assets/listing.svg`/`.png` is used only for the OrcaCloud listing. It shows a sliced cube in
OrcaSlicer's teal, an arrow and a printer, with the name. It uses no Bambu Lab or Bambuddy logos,
so the plugin is not presented as an official product of either.

### Plugin key, updates and OrcaCloud

- A local plugin's key is its entry file's stem (`assign_discovered_plugin_key` →
  `make_local_plugin_key`). A cloud plugin's key is its OrcaCloud UUID.
- The key labels the capability settings in `orca_plugins/config.json`, the storage folder
  `plugin_data/<key>`, the permissions in `.install_state.json`, and the reference a printer
  preset stores for its `printer_agent` (`<key>;<uuid>;Bambuddy printer agent`, as logged by
  `Registered printer-agent plugin`).
- A versioned wheel name would therefore reset settings and break preset references on every
  update. The build writes `orca_bambuddy_any.whl`; verified in the AppImage, the plugin loads
  under key `orca_bambuddy_any`.
- Switching between a local install and an OrcaCloud subscription changes the key, which used to
  mean empty settings. (A user who installed 0.3.0 from OrcaCloud next to the local 0.2.0
  lost them.) Now, while an installation has no `config.json` entry of its own,
  `saved_agent_settings` uses another installation's "Bambuddy printer agent" entry: the highest
  `plugin_version`, then the last saved. The load-time permission request uses it, so the host
  is asked for on the first start, and `adopt_settings_of_other_installation` saves it as this
  installation's own in the agent's `on_load`. `plugin_location` finds the key and the storage
  folder from the module path. A cloud plugin is extracted to
  `orca_plugins/_subscribed/<user>/<uuid>/` and stores in `plugin_data/_subscribed/<user>/<uuid>`
  (`PluginManager::get_storage_dir`). Verified: a dry run against a real OrcaCloud install, and a
  switch between two local installs in the 2.5.0-beta AppImage (settings adopted, host in the
  first prompt, no restart). An actual OrcaCloud install needs an OrcaCloud login, so it was not
  run headless.
- OrcaCloud (help guide at cloud.orcaslicer.com/wiki, read 2026-10-27):
  - uploads must be `.py`/`.whl` with a target-OS suffix (`_any`, `_linux_x86_64`, …), at most 20
    files of 100 MB;
  - versions must be `X.Y.Z`;
  - GitHub releases can publish through OIDC trusted publishing
    (`POST https://api.orcaslicer.com/api/v1/plugin-publish/releases`, audience `orcacloud`).
  - The publishing step in `release.yml` is the guide's script.
  - Not exercised: it needs an OrcaCloud account and a connected repository.

## Bambuddy endpoints used (API prefix `/api/v1`, auth `X-API-Key`)

| Endpoint | Permission (API-key flag) | Use |
|---|---|---|
| `GET /updates/version` | public | reachability, "is this Bambuddy", version |
| `GET /auth/status` | public | is authentication enabled |
| `GET /auth/me` | any valid key | key validity and the permissions it can actually exercise |
| `GET /printers/` | printers:read (Read Status) | printer list: id, name, model, location, serial, ip, is_active |
| `GET /printers/{id}/status` | printers:read | state (`gcode_state`), progress, layers, remaining, temperatures, nozzles, AMS, HMS errors |
| `POST /printers/camera/stream-token` | camera:view (Read Status) | 60-minute token for snapshots |
| `GET /printers/{id}/camera/snapshot?token=` | stream token | JPEG frame |
| `POST /library/files` (multipart `file`) | library:upload (Manage Library) | upload; response has `duplicate_of` |
| `GET /library/files/{id}` | library:read | confirm a cached upload still exists (`file_hash`) |
| `DELETE /library/files/{id}` | library:delete (Manage Library) | drop a just-uploaded duplicate |
| `POST /queue/` | queue:create (Manage Queue) | queue item: `library_file_id`, `printer_id` or `target_model`, `manual_start`, `plate_id`, print options, `ams_mapping` |
| `GET /queue/{id}` | queue:read (Read Status) | milestones: pending / printing / completed / failed / cancelled / skipped |

### Mapping of the three actions

| Plugin action | Bambuddy | Notes |
|---|---|---|
| Print now | upload, then `POST /queue/` with `manual_start=false`, no `scheduled_time` | Bambuddy's "ASAP": the scheduler dispatches when the printer is idle. The direct `POST /library/files/{id}/print` was removed (HTTP 410). |
| Add to queue | upload, then `POST /queue/` with `manual_start=true` | a "staged" item; started in Bambuddy |
| Store only | upload only | lands in the **library** (File Manager). Bambuddy creates an **archive** only when a print runs, so "store" cannot mean archive. |

Other findings:
- **Raw G-code is not an option.** Bambuddy prints by sending `Metadata/plate_N.gcode` of a
  `.gcode.3mf` to the printer, and rejects raw `.gcode` uploads.
- **Plate numbers.** A file cut from a larger project keeps its original plate number, so the
  plugin always sends `plate_id` explicitly.
- **Duplicates.** Bambuddy has no lookup by hash. The plugin caches `sha256 → library id` for its
  own uploads; for the rest it uses the upload response's `duplicate_of` and deletes the new copy
  (policy `reuse`).
- **"Any printer of this model."** Bambuddy's `target_model` matches `Printer.model` exactly, so
  the plugin offers it only for printers whose stored model is already a short name.
- **Model mismatch.** Bambuddy checks the model only for `target_model` items, so the plugin
  checks `printer_id` sends itself, with Bambuddy's own table (`printer_models.py`: id map, name
  map, the X1/P1 G-code family).
- **Sliced model.** Taken the way Bambuddy derives `sliced_for_model`: `slice_info.config`
  `printer_model_id` first, else `project_settings.config` `printer_model`. OrcaSlicer CLI exports
  leave `printer_model_id` empty.
- **Filenames.** Bambuddy rejects names with `<>:"/\|?*`, control characters, or a trailing space
  or dot. The agent derives the name from OrcaSlicer's project name and replaces those characters.
- **Authentication off.** With auth disabled, `GET /printers/` includes `access_code` for
  everybody. That is Bambuddy's documented behaviour; the plugin never displays it.

### Cloudflare Access behaviour handled

- No token, or not accepted: a 302 to `<team>.cloudflareaccess.com/cdn-cgi/access/login/…`. The
  plugin disables urllib's redirect following, which would otherwise resend `X-API-Key` to the
  login host.
- Token rejected: a 403 HTML page with a `CF-RAY` header.
- Upload over the plan limit: 413.

Each produces its own message. `CF-Access-*` headers are sent only when **Cloudflare Access** is
ticked.

### Other reverse proxies

The **Extra HTTP headers** setting holds `Name: value` lines, and the plugin sends them with every
request:
- The setting is a secret: pages get only `has_extra_headers` and the header names.
- Names must be RFC 7230 tokens. Values must not be empty or contain CR, LF or NUL. Headers the
  plugin sets itself are refused.
- Saving from the Bambuddy tab rejects bad lines. A connection skips them and Test connection lists
  them, which covers config saved through the Plugins dialog's form.
- A 401, 403 or 407 without Bambuddy's JSON `detail` is reported as a proxy refusal rather than a
  bad API key; Bambuddy always answers auth failures with JSON.
- Non-Cloudflare redirects mention proxy login pages. Redirects are never followed, so these headers
  cannot leak to another host.

Verified in the AppImage against Bambuddy 1.2.5.7:
- a reserved header is refused with its reason, keeping the typed text;
- a valid header is saved, and only its name is shown afterwards;
- Test connection lists it.

On the wire it is checked by the unit tests only; there was no real proxy to test against.

## Gaps and workarounds

| Gap (OrcaSlicer 2.5.0-alpha) | Workaround |
|---|---|
| A plugin cannot trigger or observe the export of a sliced `.gcode.3mf`. `Plater.export_3mf_copy()` writes a project without G-code; `GCodeExportFinished` reports only the raw temp G-code; "Export plate sliced file" fires no event. | Primary path: the printer agent, which receives the packaged file from OrcaSlicer's own Print/Send job. Fallback: the user exports, then sends the file from the Bambuddy tab (recent files in the export folder, or a path). |
| `orca.request_permissions()` accepts only `fs_read` in 2.5.0-alpha. The wiki's `network_http`, `network_socket`, `process` and `threading` parameters do not exist there; calling them raises `TypeError`. | Called with the beta parameters, `TypeError` caught. On the alpha the single runtime prompt is for thread creation, and plugin threads are not audited. On the beta see "Audit changes in 2.5.0-beta". |
| 2.5.0-beta: a changed Bambuddy host can be granted only at the next load. | "Restart OrcaSlicer" message instead of requests. |
| The wiki documents `orca.host.ui.show_dialog`, which the release does not have. | `create_window(style=WINDOW_MODAL, on_submit=...)`. |
| No secret storage for plugins. | Settings in `config.json` in plain text; documented; minimal-scope API key recommended; secrets never echoed back to pages or logged. |
| The access code is required by OrcaSlicer for LAN devices; an agent cannot pre-set it. | One prompt per printer; any value. |
| The bundled Python has an empty CA store (OpenSSL's paths point at the CI build machine). | `bambuddy.SYSTEM_CA_FILES` fallback plus a CA file setting. |
| Notifications of level Regular/PrintInfo fade after 10 s, and after a print OrcaSlicer switches to the Device tab. | Successes and milestones use Important (20 s); errors use Error (stay). |
| Activating the plugin mid-session registers the agent but does not make it live for the already-selected preset. | README: restart, or re-select the preset. |
| 2.5.0-alpha: at startup the preset's agent can be looked up before the plugin registers it, and is not looked up again. | A one-time notification asks to re-select the preset (see "Agent not started at startup"). |

## Verification log

Automated (`python3 -m unittest discover -s tests`): 137 tests, 5 of them live tests that skip
without `BAMBUDDY_TEST_URL`. They cover:
- the REST client against a scripted HTTP server: headers, every error class including Cloudflare
  redirect, 403 and 413, timeouts, refused connections, multipart streaming, cancel, camera token
  reuse;
- matching and the Bambuddy compatibility table;
- the sliced-3MF reader (real OrcaSlicer exports for X1C and A1 mini, plus synthetic multi-plate
  files);
- the Bambu dialect conversion;
- the service: the three actions, all plates, mismatch blocking, duplicate reuse, the Cloudflare
  upload limit, the agent job with chooser, cancel and failure, and milestones.

Live tests (`tests/test_live_bambuddy.py`) passed against:
- **Bambuddy 1.2.5.7** (Docker, auth on), with a minimal key and a read-only key: connection
  report, permission errors, upload, a staged queue item;
- **1.3.0b1** (demo, auth off).

In the real OrcaSlicer 2.5.0-alpha AppImage, run headless (Xvfb) and driven through XTest:

| Check | Bambuddy 1.2.5.7 | 1.3.0b1 demo |
|---|---|---|
| Plugin installs from the wheel and activates; thread prompt shown once | ✓ | |
| Settings form saves; secrets not shown again; Test connection report | ✓ | ✓ |
| Bambuddy tab status table | ✓ (offline printers) | ✓ (live printing printer) |
| Camera error shown | | ✓ (demo has no camera frames) |
| Printers in the Device tab, access-code prompt, connect | ✓ | ✓ |
| Live status in the Device tab: job, %, layer, time, temperatures, AMS | | ✓ |
| Print plate → OrcaSlicer dialog → chooser → **Print now** → queue item with dialog options | ✓ | ✓ (AMS tray `[0]` mapped by OrcaSlicer) |
| **Add to queue** (staged item) and **Store only** (library, no queue item), with notifications | | ✓ |
| OrcaSlicer's own model-mismatch warning (X1C plate → P1S) | | ✓ |
| Connect failure (stale device) shown by OrcaSlicer with the plugin's reason | ✓ | |
| Bambuddy tab *Send a sliced file*: model detection, preselection, offline warning, Store only | ✓ | |

On a real installation (Bambuddy 1.2.5.7 with an A1 mini and a P1S): the Bambuddy tab
status table and a real camera snapshot.

Issue #1 fix (2026-10-28), against Bambuddy 1.2.5.7 in Docker (auth on, one offline X1C added
through Bambuddy's models, because 1.2.5.7 requires an MQTT connection to create a printer). The
AppImages ran on Xvfb with a separate `--datadir`.

| Check | 2.5.0-beta | 2.5.0-alpha |
|---|---|---|
| 0.2.0 fails as reported: `[AUDIT BLOCKED] ... urllib.Request` on the worker | ✓ (user's log) | |
| One load prompt: both folder trees, `http://127.0.0.1`, threads; saved as tree and host grants | ✓ | |
| Only the thread prompt; plugin loads (`TypeError` fallback) | | ✓ |
| Discovery, connect, status table, Test connection, camera request, job tracking | ✓ | ✓ (connect) |
| Print plate → chooser → Print now → queue item | ✓ (×6) | |
| Print plate → chooser → Add to queue → staged item | | ✓ |
| No audit denial and no prompt during a send (after the codec pre-import) | ✓ | ✓ |
| Restart: no prompt | ✓ | |
| Bambuddy tab: file in `~/Downloads` read on the worker | ✓ | |
| File outside the granted folders: "may not read" message, path prompt; Yes, then Load works; Store only | ✓ | |
| URL moved to another host: "restart" notice, no requests; after restart only that host is asked for, and it works | ✓ | |

Not verified:
- Real printers: "started", "first layer finished" and HMS milestones were checked only with
  simulated responses (the demo scheduler does not dispatch queue items).
- Cloudflare Access against a real Cloudflare tunnel (only simulated responses).
- Windows and macOS; HTTPS with the system CA fallback on a real server.
- Print all plates from OrcaSlicer's dialog.
- "Open in Bambuddy" links in a real browser. In the headless test a stand-in `xdg-open` on `PATH`
  logged the launches. Beta: `['xdg-open', 'http://localhost:18001/']` is part of the startup
  prompt; the first click and a notification link open it with no prompt. Alpha: no prompt.

0.4.0 (2026-11-01), for two OrcaCloud reports: an H2D shown badly in the Device tab, and a
connection that stayed "waiting". Against a scripted Bambuddy (an X1C, and an H2D with the
status shape of Bambuddy 1.2.5.7: two AMS on different sides, an AMS HT, both external spools,
0.4 left / 0.6 high-flow right), in both AppImages on Xvfb with a separate `--datadir`. Dropdowns
and the printer popup render once a window manager runs on the test display (`xfwm4` on `:99`).

| Check | 2.5.0-alpha | 2.5.0-beta |
|---|---|---|
| H2D in the Device tab: both nozzle temperatures, active nozzle, AMS on the left and right sides, no assert | ✓ | ✓ |
| X1C afterwards: unchanged single-nozzle view | ✓ | |
| Nozzle sync into the preset: 0.4 left, 0.6 high flow right, AMS per nozzle | ✓ (on connect) | ✓ (sync button) |
| Print dialog for the H2D: no blocking message, filament mapped to the right-side AMS; queue item with `ams_mapping` [0] | ✓ | ✓ |
| Send dialog (send to printer storage) opens and queues a staged item | ✓ | |
| `/status` stalled for 45 s: last status, then offline, still connected; one warning; recovers | ✓ | |
| Agent not started at startup: one notification; re-selecting the preset starts it | ✓ (every start) | not seen |
| No audit denial with the third thread | | ✓ |

Not verified: a real H2D, H2D Pro or H2C (the field mapping comes from Bambuddy's and
OrcaSlicer's sources); the print-host case in the GUI (unit-tested).
