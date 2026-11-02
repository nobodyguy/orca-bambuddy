# CLAUDE.md

OrcaSlicer plugin that sends sliced plates to a Bambuddy server: a printer agent (OrcaSlicer's own
Print/Send buttons and Device tab) plus a "Bambuddy" tab. Pure Python, standard library only.
User docs are in `README.md`; research, OrcaSlicer internals and the verification log are in
`NOTES.md`. Read `NOTES.md` before changing how the plugin talks to OrcaSlicer.

## Rules

- **Never write the real hostname or URL of the user's production Bambuddy instance** anywhere:
  code, tests, docs, commit messages, PR text, logs or examples. Use `bambuddy.example.com` or
  `127.0.0.1` instead.
- Never write API keys, access codes or Cloudflare credentials into the repo.
- The printer-agent path (Print/Send through OrcaSlicer's dialogs) is the primary feature. Don't
  defer it or replace it with the Bambuddy-tab fallback.
- Supported: OrcaSlicer **2.5.0-alpha, 2.5.0-beta and 2.5.0-rc**, all at once, and Bambuddy
  **1.2.5.7**. The alpha's `orca.request_permissions` takes only `fs_read` (a `TypeError` means
  alpha). The beta and rc audit plugin threads. A change must work on all three.
- The OrcaSlicer wiki describes a newer API than these releases. Verify against the source
  (`/home/devel/repos/OrcaSlicer`, tags `v2.5.0-alpha`, `v2.5.0-beta`, `v2.5.0-rc`) and the
  AppImages in `~/Downloads`.

## Layout

- `src/orca_bambuddy/orca_adapter.py`: everything that touches the `orca` module (plugin
  registration, printer agent, pages, permissions). Keep plugin-API calls in this file.
  `__version__` here is the plugin version; the wheel build reads it.
- `service.py`: settings, the worker, send and device threads, jobs and tracking.
- `bambuddy.py`: REST client (API prefix `/api/v1`, `X-API-Key`, Cloudflare Access, extra headers,
  no redirect following).
- `bambu_dialect.py`: Bambuddy data to the Bambu-style JSON OrcaSlicer's device layer parses
  (SSDP records, `push_status`, two-nozzle fields).
- `camera_relay.py`: the local HTTP relay on 127.0.0.1 that the Device tab camera plays from
  (OrcaSlicer's FFmpeg cannot send the API key or proxy headers).
- `matching.py`: printer/model matching (Bambuddy's model table). `sliced3mf.py`: `.gcode.3mf`
  reader. `ui.py`: HTML of the tab, the settings form and the action chooser.
- `tests/`: `fakes.py` has a scripted Bambuddy server (`FakeServer`, `bambuddy_routes`,
  `H2D_STATUS`). `tests/fixtures/` holds real OrcaSlicer exports.

## Commands

- Tests: `python3 -m unittest discover -s tests`. The live tests skip unless `BAMBUDDY_TEST_URL`
  (and `BAMBUDDY_TEST_KEY`) are set; point them at a local Docker Bambuddy
  (`ghcr.io/maziggy/bambuddy:1.2.5.7`), not at a production instance.
- Wheel: `python3 tools/build_wheel.py` writes `dist/orca_bambuddy_any.whl`. The file name
  carries no version on purpose: OrcaSlicer keys settings, storage and permissions by it.
- Releases: `.github/workflows/release.yml` runs on a published GitHub release and publishes to
  OrcaCloud.

## Testing in OrcaSlicer

Run the AppImages headless: `Xvfb :99` with `xfwm4 --compositor=off` on `:99` (popups need a
window manager), `--datadir` pointing to a scratch folder, and drive it with `xdotool` and
`import -window root`. Never use the user's display or the real data dir `~/.config/OrcaSlicer`,
and stop only the processes you started (by PID or by the scratch `--datadir`). How to seed such a
data dir is in `NOTES.md` and the verification logs there. Record new checks in the
"Verification log" of `NOTES.md`.

## Style

- Comments explain why, briefly, and name the OrcaSlicer source they rely on
  (`File.cpp` / function), as the existing code does.
- Docs are plain and factual; update `README.md` (user-facing) and `NOTES.md` (findings) together
  with behaviour changes.
