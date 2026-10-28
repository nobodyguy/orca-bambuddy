# Bambuddy for OrcaSlicer

Send sliced plates from OrcaSlicer straight to your self-hosted [Bambuddy](https://github.com/maziggy/bambuddy)
server, and watch your printers without leaving the slicer.

- **Print, queue or store**: OrcaSlicer's own **Print** and **Send** buttons hand the sliced plate
  (`.gcode.3mf`, with thumbnails, plate and filament data) to Bambuddy. Each time you choose:
  - **Print now**: the job goes to Bambuddy's queue and starts as soon as the printer is free.
  - **Add to queue**: the job waits in Bambuddy until you press Start there.
  - **Store only**: the file goes into the Bambuddy library.
- **Your Bambuddy printers in OrcaSlicer**: they appear in the Device tab and the print dialog,
  with live state, temperatures, progress, layers and AMS slots. OrcaSlicer maps filaments to AMS
  slots and warns when the plate was sliced for a different printer model.
- **A Bambuddy tab**: every printer at a glance (online/offline, state, job, progress, layer,
  remaining time, errors), a camera snapshot, a connection test, settings, and the sends of this
  session.
- **Notifications** when a job is queued, starts, finishes its first layer, or fails.

Works on your LAN or VPN with just a URL and an API key. Bambuddy behind Cloudflare Zero Trust
works too, with a Cloudflare Access service token, and so does any other reverse proxy that accepts
a header (extra HTTP headers).

> OrcaSlicer's plugin system is an alpha feature, and the native Print/Send integration uses its
> experimental *printer agents*. See [Known limitations](#known-limitations).

## How this differs from Bambuddy's Virtual Printer

Bambuddy can already receive jobs from a slicer through its **Virtual Printer**. Bambuddy then
pretends to be a Bambu Lab printer on your network, and the slicer talks to it with the Bambu LAN
protocols. This plugin instead talks to Bambuddy's **web API**, over the same single HTTP(S) port
you open Bambuddy's web page on.

| | Virtual Printer | This plugin |
|---|---|---|
| Network traffic | several raw TCP/TLS services plus UDP discovery (see below) | HTTP(S) to the Bambuddy URL only |
| Through a reverse proxy, Cloudflare Access or a tunnel | no: these are not HTTP, so HTTP proxies cannot carry them; needs a VPN, port forwarding, or Bambuddy's host networking | yes, like the web page; Cloudflare service token or extra headers supported |
| Discovery | SSDP broadcast, so the same LAN only; across a VPN or router you add the IP by hand | none needed: Bambuddy lists the printers |
| What happens to a job | fixed per virtual printer: its mode (archive, review, queue, proxy) | chosen per send: Print now, Add to queue, Store only |
| Target printer | the virtual printer; its settings decide which real printer, if any, gets the job | a real Bambuddy printer, chosen in OrcaSlicer's dialog |
| Printer status in the slicer | the virtual printer's (in proxy mode, the one printer it forwards) | all your Bambuddy printers, live, plus camera snapshots |
| Slicers | Bambu Studio and any OrcaSlicer version, no plugin | OrcaSlicer 2.5+ with this plugin (alpha) |

Ports the Virtual Printer needs, per virtual printer (Bambuddy 1.2.5.7):

| Port | Protocol | Purpose |
|---|---|---|
| 2021 | UDP | SSDP discovery (broadcast) |
| 3000, 3002 | TCP (plain / TLS) | "bind with access code" handshake |
| 8883 | TCP, TLS | MQTT (commands and status) |
| 990 | TCP, TLS | implicit FTPS (file upload) |
| 50000–50100 | TCP | FTP passive data connections |
| 6000, 322 | TCP, TLS | proxy mode only: file transfer tunnel and RTSP camera |

This plugin needs one: Bambuddy's web port (8000 by default, or 443 behind a reverse proxy).
Use the Virtual Printer for Bambu Studio, older OrcaSlicer versions, or a no-plugin setup on the
same LAN. Use this plugin when Bambuddy is only reachable over HTTP(S), for example behind
Cloudflare Zero Trust, a reverse proxy or a tunnel, or when you want to choose the action and the
printer per send.

## Requirements

- OrcaSlicer **2.5.0-alpha** or **2.5.0-beta** (Windows, macOS, Linux).
- Bambuddy **1.2.5.7** (tested; 1.3.0b1 was tested too).
- A Bambuddy API key. Nothing else to install: the plugin uses only Python's standard library.

## Install

1. Download `orca_bambuddy_any.whl` from the project's GitHub **Releases** (or the
   *orca-bambuddy-wheel* artifact of a *Build* workflow run, or build it yourself with
   `python3 tools/build_wheel.py`). Once it is on OrcaCloud you can instead sign in to OrcaCloud,
   use **Browse plugins** in the Plugins dialog, subscribe to it there, and skip step 2.
2. In OrcaSlicer open **☰ menu > Plugins**, open the arrow next to **Install plugin**, choose
   **Install local plugin** and pick the `.whl` file.
3. Tick **Activate** next to *orca-bambuddy*, then restart OrcaSlicer.
4. The first time the plugin starts, OrcaSlicer asks for permissions. Answer **Yes**: the plugin
   does all network and file work in background threads so the slicer never freezes. OrcaSlicer
   remembers the answers until you install a new version.
   - **2.5.0-alpha** asks once: *Plugin "orca-bambuddy" is requesting permission to create a
     thread.*
   - **2.5.0-beta** asks once, while the plugin loads, for: creating threads; reading OrcaSlicer's
     temp folder (`<temp>/orcaslicer_<uid>/orcaslicer_model`, where OrcaSlicer puts the sliced file
     for Print/Send) and your export folder (default `~/Downloads`); and network access to your
     Bambuddy host. Answering No stops the plugin from loading. The host is known only after you
     enter the URL, so **after setting or changing the Bambuddy URL, restart OrcaSlicer** and answer
     Yes again. Until then the plugin says so instead of connecting.

A **Bambuddy** tab now appears next to Prepare, Preview, Device and Project.

## Set up Bambuddy

### Create the API key

In Bambuddy open **Settings > API Keys > Create New API Key**, name it (for example "OrcaSlicer")
and tick only:

| Permission | Why the plugin needs it |
|---|---|
| **Read Status** | list printers, show their status, follow queued jobs, camera snapshots |
| **Manage Queue** | Print now and Add to queue |
| **Manage Library** | upload the sliced file |

Leave everything else, including **Control Printer**, unticked. You can also limit the key to
some printers. Copy the key: Bambuddy shows it only once.

If authentication is turned off in Bambuddy, the plugin works without a key. Test connection
will point this out.

### Connect the plugin (LAN or VPN)

1. Open the **Bambuddy** tab and expand **Settings**.
2. **Bambuddy URL**: the address you open Bambuddy at, for example `http://192.168.1.50:8000`.
3. **API key**: paste the key.
4. **Save**, then **Test connection**. It checks, in order: the server is reachable and is
   Bambuddy, the Cloudflare token (if set), the API key, and each permission the plugin needs.
   Any missing permission is named with its Bambuddy label.

### Bambuddy behind Cloudflare Zero Trust (optional)

If Bambuddy is published through Cloudflare Access, the plugin must present a service token:

1. In the Cloudflare Zero Trust dashboard go to **Access > Service Auth > Service Tokens > Create
   Service Token**. Copy the **Client ID** and **Client Secret**.
2. Open **Access > Applications**, edit the application that protects Bambuddy, and add a policy
   with **Action: Service Auth** that **includes** that service token. Keep your existing Allow
   policy for browser logins.
3. In the plugin's settings tick **Cloudflare Access** and enter the Client ID and Client Secret.

The plugin then sends `CF-Access-Client-Id` and `CF-Access-Client-Secret` with every request. When
**Cloudflare Access** is off, these headers are never sent. Cloudflare's free plan rejects uploads
larger than **100 MB**, so behind Cloudflare the plugin refuses larger files up front with a clear
message. Raise **Upload limit (MB)** if your plan allows more.

### Other reverse proxies: extra HTTP headers (optional)

If Bambuddy sits behind another proxy that lets clients through with a header (an API token in
Authentik or Authelia, a shared secret checked by nginx, Caddy or Traefik, HTTP basic auth, ...),
enter it under **Extra HTTP headers**, one per line:

```
X-Proxy-Token: 0123456789abcdef
Authorization: Basic dXNlcjpwYXNzd29yZA==
```

- The headers are sent with every request, and to nothing but your Bambuddy URL.
- Values are treated like the API key: they are not shown again after saving (only the header
  names are), and they never appear in logs or notifications. Leave the field empty to keep the
  saved headers, type new lines to replace all of them, or tick **Forget the saved extra
  headers**.
- The plugin authenticates to Bambuddy with `X-API-Key`, so the standard `Authorization` header is
  free for the proxy.
- Headers the plugin sets itself cannot be replaced: `X-API-Key`, `Accept`, `Content-Type`,
  `Content-Length`, `Transfer-Encoding`, `Host`, `Connection`, and the two `CF-Access-*` headers
  (use the Cloudflare option for those). A malformed or reserved line is refused when you save in
  the Bambuddy tab; one saved through the Plugins dialog is skipped and reported by **Test
  connection**.
- **Test connection** lists the header names it sends.

### HTTPS with a self-signed certificate

Either enter the certificate (PEM) in **CA file**, or untick **Verify the server certificate**.

### All settings

| Setting | Default | Meaning |
|---|---|---|
| Bambuddy URL | empty | required |
| API key | empty | required unless Bambuddy has authentication off |
| Cloudflare Access, Client ID, Client secret | off | see above |
| Extra HTTP headers | empty | `Name: value` per line, for other reverse proxies; secret |
| Default action | Print now | preselected choice; the Send button never preselects Print now |
| Ask on every send | on | show the Print now / Add to queue / Store only choice each time |
| Printer choice | preselect best match | for *Send a sliced file*: preselect, or always choose yourself |
| Remember printer | on | remember the printer per printer preset (*Send a sliced file*) |
| Identical file in library | reuse | do not upload a file Bambuddy already has from this plugin |
| Export folder | `~/Downloads` | where *Send a sliced file* looks for exports |
| Verify the server certificate / CA file | on / empty | HTTPS checks |
| Timeout (s) | 15 | per request; uploads allow 120 s between chunks |
| Upload limit (MB) | 0 | 0 = no limit, or 100 MB when Cloudflare is in front |
| Status refresh (s) | 5 | how often the Bambuddy tab refreshes while it is visible |

The same settings are in **Plugins > orca-bambuddy > Config** (select *Bambuddy printer agent*).

## Print with OrcaSlicer's Print and Send buttons

This uses OrcaSlicer's experimental printer agents. Set it up once:

1. **Preferences > Developer**: tick **Use printer agents instead of print hosts**.
2. Select your Bambu Lab printer preset, open its settings (pencil icon), switch to **Advanced**
   mode, and in **Basic information > Advanced** set **Printer Agent** to **Bambuddy**. Save the
   preset; OrcaSlicer saves it as a user preset. Use one such preset per printer model you own.
3. Restart OrcaSlicer, or select another preset and then this one again.
4. In the **Device** tab, open the printer list: your Bambuddy printers are listed. Select one.
   The first time, OrcaSlicer asks for an **access code**. The plugin does not use it, so type
   anything, for example `bambuddy`. OrcaSlicer remembers it for that printer.

Then:

1. Slice, and press **Print plate**. OrcaSlicer's print dialog shows the selected Bambuddy printer,
   maps your filaments to its AMS slots, and offers its usual options (timelapse, bed levelling,
   flow calibration). If the plate was sliced for a different model, OrcaSlicer warns you, and the
   plugin also refuses a model Bambuddy cannot run.
2. Press **Send**. The plugin asks what to do, with **Print now** preselected. Tick **Don't ask
   again** to always use your choice.
3. The upload progress shows in OrcaSlicer's dialog, followed by a notification with an **Open in
   Bambuddy** link.

The **Send** entry of the Print button's menu ("send to printer storage") works the same way, with
**Add to queue** preselected.

Print options from OrcaSlicer's dialog (bed levelling, flow and vibration calibration, timelapse,
layer inspection, AMS mapping) are passed to Bambuddy's queue. When the printer is offline or busy,
OrcaSlicer cannot see its AMS, so Bambuddy maps the filaments when it starts the job.

## Watch your printers

- **Device tab**: the selected Bambuddy printer, live (state, progress, layer, remaining time,
  temperatures, AMS), refreshed every 3 seconds.
- **Bambuddy tab**: all printers. Click a printer for a camera snapshot (**Refresh camera** for a
  new one). The tab polls only while it is visible.
- **Notifications** after a send: *queued* (or *waiting for a manual start*), *started printing*,
  *first layer finished*, and errors (failed or cancelled job, printer HMS errors). The plugin
  follows each job for up to 24 hours while OrcaSlicer is open.

## Without printer agents: Send a sliced file

If you prefer not to enable the experimental printer agents:

1. Slice and use **File > Export > Export plate sliced file** (or **Export all plates sliced
   file**) into your export folder (default `~/Downloads`).
2. In the **Bambuddy** tab expand **Send a sliced file**, pick the export, or enter its path, and
   press **Load**.
3. The plugin reads which printer the file was sliced for and preselects the best Bambuddy
   printer: the one you used last with this printer preset, else the only matching printer, else
   an idle one. If several printers of that model are busy, it offers **Any X1C (first free)**,
   which uses Bambuddy's "any printer of this model" targeting. A printer of the wrong model is
   blocked; an offline one gives a warning.
4. Choose the action, and for files with several plates either one plate or **All plates** (one
   queue item per plate). Press **Send**.

## Troubleshooting

| Message | What to do |
|---|---|
| Connection to *host* refused / host name could not be resolved | wrong address or port, or Bambuddy is not running |
| *host* answered, but not with the Bambuddy API / No Bambuddy API found | the URL points to something else; use the address you open Bambuddy at, without `/api/v1` |
| The server redirected to … | use the final address (often `https://`) |
| The API key was rejected | create a new key in Bambuddy |
| The key lacks 'Manage Library' (can_manage_library) … | create a key with that permission |
| Cloudflare Access returned its login page … | Bambuddy is behind Cloudflare Access: set up the service token |
| Cloudflare Access rejected the service token | check the ID and secret, and the *Service Auth* policy |
| A proxy in front of Bambuddy refused the request (HTTP 401/403/407) | the proxy wants a header: add it under **Extra HTTP headers**, or check the one you entered |
| The server redirected to … (a login page) | a proxy's login page: add the header it expects, or use the final address as the URL |
| Extra header … is set by the plugin itself / is not 'Name: value' | fix that line |
| … is N MB; the upload limit is 100 MB | Cloudflare free plan limit, see above |
| The TLS certificate of *host* is not trusted | set **CA file** or turn off certificate verification |
| *printer* is offline | the job waits in Bambuddy's queue until the printer is back |
| This plate was sliced for X1C, but *printer* is a A1 Mini | select the matching printer preset and slice again |
| The plugin is not allowed to start its background threads | you answered No to the thread prompt: reinstall the plugin, or restart and answer Yes |
| OrcaSlicer has not allowed the plugin to connect to *host* yet / did not allow the plugin to connect | OrcaSlicer 2.5.0-beta: restart OrcaSlicer after setting the URL, and answer Yes to the network prompt |
| The plugin may not read *file* … audit permission required | OrcaSlicer 2.5.0-beta refused a file read. For Print/Send, update the plugin and restart. For a file picked in the Bambuddy tab outside the export folder, answer Yes to OrcaSlicer's prompt and send again |

The plugin logs to OrcaSlicer's Python log, `<data dir>/log/python_*.log`, with lines starting
with `[bambuddy]`. Secrets are never logged or shown in notifications.

## Where settings and secrets are stored

OrcaSlicer's data directory (`<data dir>`) is `%APPDATA%\OrcaSlicer` on Windows,
`~/Library/Application Support/OrcaSlicer` on macOS, and `~/.config/OrcaSlicer` on Linux.

| What | Where |
|---|---|
| Settings, **including the API key, Cloudflare secret and extra headers, in plain text** | `<data dir>/orca_plugins/config.json` (OrcaSlicer's plugin configuration store) |
| Printer remembered per preset, hashes of uploaded files | `<data dir>/orca_plugins/plugin_data/<plugin key>/state.json` |
| The access code you typed per printer | OrcaSlicer's own `OrcaSlicer.conf` |
| Permissions granted to the plugin | `<data dir>/orca_plugins/<plugin folder>/.install_state.json` |
| A copy of the Bambuddy URL and export folder, for the permission request at load (fallback) | `<data dir>/orca_plugins/plugin_data/<plugin key>/state.json` |

OrcaSlicer 2.5 offers plugins no encrypted secret store. Anyone who can read your OrcaSlicer data
directory can read the API key, so give the key only the three permissions above. The settings
pages never show a saved secret again: leave the field empty to keep it, or tick **Forget the
saved API key**.

## Known limitations

- **Experimental OrcaSlicer features.** The plugin system is alpha and printer agents sit behind a
  developer preference. A future OrcaSlicer release may change the API; everything that touches it
  is in one file (`orca_adapter.py`).
- **Access code prompt.** OrcaSlicer asks for an access code the first time each printer is
  selected. Any value works.
- **No printer control yet.** Pause, resume, stop, temperatures, fans and AMS actions from the
  Device tab are reported as not supported (phase 2).
- **No live camera** in the Device tab; the Bambuddy tab shows snapshots.
- **"Any printer of this model"** is only available in *Send a sliced file*. OrcaSlicer's print
  dialog always targets one printer.
- **The plugin cannot export a plate by itself.** OrcaSlicer 2.5's plugin API cannot trigger the
  sliced-file export. That is why printing goes through OrcaSlicer's Print button (printer agents)
  or through an exported file.
- **Job tracking** runs only while OrcaSlicer is open, and for up to 24 hours per job.
- **"Print all plates"** from OrcaSlicer's dialog should queue one item per plate, but it was not
  tested.
- **The Send menu entry** (send to printer storage) shares its code with the Print button and is
  covered by automated tests, but was not driven through OrcaSlicer's dialog during testing.

## Phase 2 roadmap

| Feature | Where it plugs in |
|---|---|
| Push updates via Bambuddy's WebSocket (`/api/v1/ws`) instead of polling | `Service._run` polling loop, `refresh_status`, `agent_push_status` |
| Print controls: pause, resume, stop | `Service.agent_command` (Bambu JSON → `POST /printers/{id}/print/pause` etc.; needs **Control Printer**) |
| Live camera stream | `PrinterAgentBase.get_camera_stream_mode` / `get_camera_url` in `orca_adapter.py`, `GET /printers/{id}/camera/stream` |
| AMS and filament overview | `printer_view` / page; `bambu_dialect._ams` already converts AMS data |
| Queue view in the Bambuddy tab | `GET /queue/`, new page section |
| HMS errors in the Device tab | `bambu_dialect.push_status` (`hms` list) |
| Fuller device emulation | `bambu_dialect.push_status` (more fields), `agent_command` |

## Development

```
python3 -m unittest discover -s tests                       # 99 tests; no OrcaSlicer, no printers
BAMBUDDY_TEST_URL=http://127.0.0.1:8000 BAMBUDDY_TEST_KEY=bb_... \
  python3 -m unittest discover -s tests -p 'test_live*'      # against a real Bambuddy
python3 tools/build_wheel.py                                # writes dist/*.whl (standard library only)
```

CI: `.github/workflows/build.yml` runs the unit tests on Python 3.10 to 3.13 and builds the
wheel on every push and pull request (downloadable as the *orca-bambuddy-wheel* artifact).

The wheel is always named `orca_bambuddy_any.whl`, without a version. OrcaSlicer keys a locally
installed plugin by its file name, and that key labels the saved settings, the plugin's storage
folder, the thread permission and the plugin references in printer presets. A versioned name
would lose all of them on every update. `_any` is the target-OS suffix OrcaCloud requires.

### Releasing and publishing to OrcaCloud

OrcaCloud (cloud.orcaslicer.com) is OrcaSlicer's plugin registry: plugins listed in its **Plugin
Hub** can be found with **Browse plugins** in OrcaSlicer, subscribed to, and updated from the
Plugins dialog. Plugins are a Beta feature there.

One-time setup:

1. Sign in at <https://cloud.orcaslicer.com>, open **Plugins > Shared Plugins** and click **+**.
2. Upload `dist/orca_bambuddy_any.whl`, add `assets/listing.png` as the plugin image (1024×1024;
   OrcaSlicer also shows it as the thumbnail in its Plugins dialog), check the name, and write the
   description (the README's first sections work well). The image is rendered from
   `assets/listing.svg`: `inkscape assets/listing.svg -o assets/listing.png -w 1024 -h 1024`.
3. Set the **Version** (`X.Y.Z`, no suffixes), the **Type**, and **Compatible OrcaSlicer
   Version** (2.5.0-alpha or later). Add a changelog and tags (for example `bambuddy, bambu lab,
   printer, queue`), tick **Public Plugin**, and click **Create plugin**.
4. To publish later versions from GitHub: in the plugin's **Edit plugin** dialog, under **GitHub
   publishing**, connect this repository (`owner/repository`). Then set the repository variable
   `ORCACLOUD_PUBLISH` to `true` (GitHub: Settings > Secrets and variables > Actions >
   Variables). No secrets are needed: the workflow authenticates with GitHub's OIDC token.

Each release:

1. Bump `__version__` in `src/orca_bambuddy/orca_adapter.py` (higher than the version on
   OrcaCloud) and push.
2. Publish a GitHub release with the tag `v<version>` and release notes, either in the GitHub UI
   or with `gh release create v0.2.0 --notes "..."`. Create it yourself: a release created by
   another workflow does not start `release.yml`.
3. `.github/workflows/release.yml` runs the tests, checks the tag against `__version__`, attaches
   `orca_bambuddy_any.whl` to the GitHub release, and, with `ORCACLOUD_PUBLISH` set, uploads it to
   OrcaCloud as the new version. The release notes become the OrcaCloud changelog.

Without the GitHub connection, update the listing by hand: **Edit plugin**, replace the file,
bump the version, add the changelog.

Author: Jan Gnip.

Layout: `bambuddy.py` (REST client), `matching.py`, `sliced3mf.py`, `bambu_dialect.py` (Bambuddy →
Bambu device JSON), `service.py` (threads, sends, polling, agent logic), `ui.py` (pages), and
`orca_adapter.py` (the only module that imports OrcaSlicer's `orca` API). See [NOTES.md](NOTES.md)
for the research behind it.
