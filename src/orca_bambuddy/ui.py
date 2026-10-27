"""HTML for the plugin's pages. Self-contained: OrcaSlicer injects window.orca and a theme
stylesheet (--orca-* CSS variables) into every plugin page."""

import json

_STYLE = """
<style>
  body { font-family: var(--orca-font, sans-serif); margin: 0; padding: 12px 16px; }
  h2 { font-size: 16px; margin: 18px 0 8px; }
  .muted { color: var(--orca-muted, #888); }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .card { border: 1px solid var(--orca-border, #ccc); border-radius: 6px; padding: 10px 12px; margin: 8px 0; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--orca-border, #ddd); font-size: 13px; }
  tr.sel td { background: color-mix(in srgb, var(--orca-accent, #009688) 15%, transparent); }
  tbody tr { cursor: pointer; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: #999; }
  .on { background: #2e9d4f; } .off { background: #c0392b; } .busy { background: #e09b1a; }
  .ok { color: #2e9d4f; } .warn { color: #c77c00; } .error { color: #c0392b; }
  label { font-size: 13px; }
  input[type=text], input[type=password], input[type=number], select { min-width: 260px; }
  textarea { width: 100%; box-sizing: border-box; font-family: monospace; }
  .grid { display: grid; grid-template-columns: max-content 1fr; gap: 6px 12px; align-items: center; }
  progress { width: 100%; }
  img.cam { max-width: min(100%, 640px); max-height: 360px; object-fit: contain; border-radius: 4px;
            display: block; margin-top: 8px; cursor: zoom-in; }
  img.cam.large { max-width: 100%; max-height: none; cursor: zoom-out; }
  .err { font-size: 12px; }
  details summary { cursor: pointer; font-weight: bold; margin: 12px 0 6px; }
</style>
"""

_SETTINGS_FORM = """
<div class="grid">
  <label for="base_url">Bambuddy URL</label>
  <input type="text" id="base_url" placeholder="http://192.168.1.50:8000">
  <label for="api_key">API key</label>
  <input type="password" id="api_key" autocomplete="off">
  <label for="cf_access_enabled">Cloudflare Access</label>
  <label><input type="checkbox" id="cf_access_enabled"> Send a Cloudflare Access service token</label>
  <label for="cf_client_id" class="cf">Client ID</label>
  <input type="text" id="cf_client_id" class="cf" autocomplete="off">
  <label for="cf_client_secret" class="cf">Client secret</label>
  <input type="password" id="cf_client_secret" class="cf" autocomplete="off">
  <label for="extra_headers">Extra HTTP headers</label>
  <div>
    <textarea id="extra_headers" rows="2" autocomplete="off" spellcheck="false"
      data-empty="optional, for a reverse proxy: Name: value (one per line)"></textarea>
    <div class="muted" style="font-size:12px">Sent with every request; values are kept secret.</div>
  </div>
  <label for="default_action">Default action</label>
  <select id="default_action">
    <option value="print">Print now (starts when the printer is free)</option>
    <option value="queue">Add to queue (start manually in Bambuddy)</option>
    <option value="store">Store only (library)</option>
  </select>
  <label for="ask_action">Ask on every send</label>
  <label><input type="checkbox" id="ask_action"> Show the action choice when Print or Send is pressed</label>
  <label for="printer_selection">Printer choice</label>
  <select id="printer_selection">
    <option value="auto">Preselect the best matching printer</option>
    <option value="manual">Always choose myself</option>
  </select>
  <label for="remember_printer">Remember printer</label>
  <label><input type="checkbox" id="remember_printer"> Per printer preset</label>
  <label for="duplicate_policy">Identical file in library</label>
  <select id="duplicate_policy">
    <option value="reuse">Reuse the existing file</option>
    <option value="keep">Upload another copy</option>
  </select>
  <label for="export_folder">Export folder</label>
  <input type="text" id="export_folder" placeholder="~/Downloads">
  <label for="verify_tls">TLS</label>
  <label><input type="checkbox" id="verify_tls"> Verify the server certificate</label>
  <label for="ca_file">CA file</label>
  <input type="text" id="ca_file" placeholder="optional, PEM file for a self-signed certificate">
  <label for="timeout_s">Timeout (s)</label>
  <input type="number" id="timeout_s" min="3" max="120">
  <label for="upload_limit_mb">Upload limit (MB)</label>
  <input type="number" id="upload_limit_mb" min="0" placeholder="0 = automatic">
  <label for="poll_interval_s">Status refresh (s)</label>
  <input type="number" id="poll_interval_s" min="2" max="120">
</div>
"""

_SETTINGS_JS = """
const SECRET = ["api_key", "cf_client_secret", "extra_headers"];
const FIELDS = ["base_url","api_key","cf_access_enabled","cf_client_id","cf_client_secret","extra_headers","default_action",
  "ask_action","printer_selection","remember_printer","duplicate_policy","export_folder","verify_tls","ca_file",
  "timeout_s","upload_limit_mb","poll_interval_s"];
function fillSettings(s) {
  for (const k of FIELDS) {
    const el = document.getElementById(k); if (!el) continue;
    if (SECRET.includes(k)) {
      const names = k === "extra_headers" && (s.extra_header_names || []).length ? s.extra_header_names.join(", ") + " " : "";
      el.value = ""; el.placeholder = s["has_" + k] ? `saved ${names}- leave empty to keep` : (el.dataset.empty || "");
      continue;
    }
    if (el.type === "checkbox") el.checked = !!s[k]; else el.value = s[k] ?? "";
  }
  toggleCf();
}
function readSettings() {
  const out = {};
  for (const k of FIELDS) {
    const el = document.getElementById(k); if (!el) continue;
    out[k] = el.type === "checkbox" ? el.checked : (el.type === "number" ? Number(el.value || 0) : el.value);
  }
  return out;
}
function toggleCf() {
  const on = document.getElementById("cf_access_enabled").checked;
  document.querySelectorAll(".cf").forEach(e => e.style.display = on ? "" : "none");
}
document.getElementById("cf_access_enabled").addEventListener("change", toggleCf);
"""

PAGE_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    + _STYLE
    + """</head><body>
<div class="row">
  <span id="conn" class="dot"></span><b>Bambuddy</b><span id="server" class="muted"></span>
  <span style="flex:1"></span>
  <button id="btn-test">Test connection</button>
  <button id="btn-open">Open Bambuddy</button>
</div>
<div id="test" class="card" style="display:none"></div>
<div id="agent-hint" class="card muted" style="font-size:12px">
  To send with OrcaSlicer's own <b>Print</b> and <b>Send</b> buttons: enable
  <i>Preferences &gt; Developer &gt; Use printer agents instead of print hosts</i>, then in the printer
  preset set <i>Basic information &gt; Advanced &gt; Printer Agent</i> to <b>Bambuddy</b> and save it.
  Your Bambuddy printers then appear in the print dialog. Without that, use <i>Send a sliced file</i> below.
</div>

<h2>Printers</h2>
<div id="printers-error" class="error err"></div>
<table><thead><tr><th>Printer</th><th>Model</th><th>Location</th><th>State</th><th>Job</th><th>Progress</th><th>Layer</th><th>Remaining</th><th>Errors</th></tr></thead>
<tbody id="printers"></tbody></table>
<div id="detail" class="card" style="display:none">
  <div class="row"><b id="detail-name"></b><span style="flex:1"></span><button id="btn-snap">Refresh camera</button></div>
  <div id="snap-error" class="error err"></div>
  <img id="snap" class="cam" alt="" title="Click to enlarge">
</div>

<h2>Recent sends</h2>
<div id="jobs" class="muted">Nothing sent in this session.</div>

<details id="send-box"><summary>Send a sliced file</summary>
<div class="card">
  <div class="muted" style="font-size:12px">Export with <i>File &gt; Export &gt; Export plate sliced file</i>
   into <span id="folder"></span>, or enter the path of a .gcode.3mf file.</div>
  <div class="row" style="margin-top:6px">
    <select id="files"></select><button id="btn-files">Refresh</button>
  </div>
  <div class="row" style="margin-top:6px"><input type="text" id="path" placeholder="/path/to/plate.gcode.3mf" style="flex:1">
    <button id="btn-load">Load</button></div>
  <div id="file-info" class="muted" style="margin:6px 0"></div>
  <div id="send-form" style="display:none">
    <div class="row">
      <label><input type="radio" name="action" value="print"> Print now</label>
      <label><input type="radio" name="action" value="queue"> Add to queue</label>
      <label><input type="radio" name="action" value="store"> Store only</label>
    </div>
    <div class="row" style="margin-top:6px"><label>Printer</label><select id="target"></select>
      <label id="plate-label">Plate</label><select id="plate"></select></div>
    <div id="reason" class="muted" style="font-size:12px"></div>
    <div id="check" class="err"></div>
    <div class="row" style="margin-top:8px"><button id="btn-send">Send</button></div>
    <progress id="progress" max="100" value="0" style="display:none"></progress>
    <div id="progress-text" class="muted" style="font-size:12px"></div>
    <div id="send-result"></div>
  </div>
</div>
</details>

<details id="settings-box"><summary>Settings</summary>
<div class="card">"""
    + _SETTINGS_FORM
    + """<div class="row" style="margin-top:10px"><button id="btn-save">Save</button>
  <label><input type="checkbox" id="clear_api_key"> Forget the saved API key</label>
  <label><input type="checkbox" id="clear_cf_client_secret"> Forget the saved Cloudflare secret</label>
  <label><input type="checkbox" id="clear_extra_headers"> Forget the saved extra headers</label>
  <span id="saved" class="ok"></span></div>
</div>
</details>

<script>
"""
    + _SETTINGS_JS
    + """
const $ = id => document.getElementById(id);
const post = m => orca.postMessage(m);
let printers = [], focus = null, targets = [], file = null, serverUrl = "";
const STATE = {IDLE:"Idle", PREPARE:"Preparing", RUNNING:"Printing", PAUSE:"Paused", FINISH:"Finished", FAILED:"Failed", SLICING:"Slicing"};
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function active(p) { return p.online && ["RUNNING", "PAUSE", "PREPARE"].includes(p.state); }
function dotClass(p) { if (!p.online) return "off"; return ["RUNNING","PAUSE","PREPARE"].includes(p.state) ? "busy" : "on"; }
function fmtMin(m) { if (m == null) return ""; const h = Math.floor(m / 60); return h ? `${h} h ${m % 60} min` : `${m} min`; }

function renderPrinters() {
  $("printers").innerHTML = printers.map(p => `<tr data-id="${p.id}" class="${p.id === focus ? "sel" : ""}">
    <td><span class="dot ${dotClass(p)}"></span>${esc(p.name)}</td><td>${esc(p.model)}</td><td>${esc(p.location)}</td>
    <td>${p.online === false ? "Offline" : esc(STATE[p.state] || p.state || "")}${p.stage && p.state !== "IDLE" ? `<div class="muted">${esc(p.stage)}</div>` : ""}</td>
    <td>${esc(p.job || "")}</td><td>${active(p) && p.progress != null ? p.progress + " %" : ""}</td>
    <td>${active(p) && p.layers ? `${p.layer ?? 0} / ${p.layers}` : ""}</td><td>${p.state === "RUNNING" ? fmtMin(p.remaining_min) : ""}</td>
    <td class="error">${p.errors.map(e => esc(e.text || e.code)).join("<br>")}${p.awaiting_plate_clear ? '<span class="warn">Clear the plate</span>' : ""}</td></tr>`).join("");
  const p = printers.find(x => x.id === focus);
  $("detail").style.display = p ? "" : "none";
  if (p) $("detail-name").textContent = p.name;
}
$("printers").addEventListener("click", ev => {
  const tr = ev.target.closest("tr"); if (!tr) return;
  focus = Number(tr.dataset.id); renderPrinters(); $("snap").removeAttribute("src");
  post({type: "select", printer_id: focus}); post({type: "snapshot", printer_id: focus});
});
$("btn-snap").onclick = () => focus != null && post({type: "snapshot", printer_id: focus});
$("snap").onclick = () => $("snap").classList.toggle("large");
$("btn-test").onclick = () => { $("test").style.display = ""; $("test").textContent = "Testing..."; post({type: "test"}); };
$("btn-open").onclick = () => serverUrl && post({type: "open_url", url: serverUrl + "/"});
$("btn-save").onclick = () => {
  const s = readSettings();
  for (const k of SECRET) s["clear_" + k] = $("clear_" + k).checked;
  post({type: "save_settings", settings: s});
};
$("btn-files").onclick = () => post({type: "list_exports"});
$("files").onchange = () => { if ($("files").value) { $("path").value = $("files").value; post({type: "describe", path: $("files").value}); } };
$("btn-load").onclick = () => $("path").value && post({type: "describe", path: $("path").value.trim()});
$("send-box").addEventListener("toggle", () => $("send-box").open && post({type: "list_exports"}));
$("target").onchange = () => post({type: "check", path: file && file.path, target: targets.find(t => t.id === $("target").value) || null, action: action()});
document.querySelectorAll("input[name=action]").forEach(r => r.onchange = $("target").onchange);
function action() { const r = document.querySelector("input[name=action]:checked"); return r ? r.value : null; }
$("btn-send").onclick = () => {
  $("send-result").textContent = ""; $("progress").style.display = ""; $("progress").value = 0;
  post({type: "send", path: file.path, action: action(), plate: $("plate").value,
        target: targets.find(t => t.id === $("target").value) || null, remember_key: file.printer_preset});
};

orca.onMessage(m => {
  if (m.type === "init") {
    fillSettings(m.settings); serverUrl = (m.settings.base_url || "").replace(/\\/+$/, "");
    $("server").textContent = serverUrl ? " - " + serverUrl : " - not configured";
    $("folder").textContent = m.export_folder; focus = m.focus;
    if (!serverUrl) $("settings-box").open = true;
    renderJobs(m.jobs);
  } else if (m.type === "printers") {
    printers = m.printers; if (m.focus != null && focus == null) focus = m.focus;
    $("printers-error").textContent = m.error || ""; $("conn").className = "dot " + (m.error ? "off" : "on");
    renderPrinters();
  } else if (m.type === "focus") { focus = m.printer_id; renderPrinters(); post({type: "snapshot", printer_id: focus}); }
  else if (m.type === "snapshot") {
    if (m.printer_id !== focus) return;
    $("snap-error").textContent = m.error || "";
    if (m.data) $("snap").src = m.data;
  } else if (m.type === "test") {
    $("test").style.display = "";
    $("test").innerHTML = m.lines.map(([lvl, text]) => `<div class="${lvl}">${lvl === "ok" ? "&#10003;" : lvl === "warn" ? "!" : "&#10007;"} ${esc(text)}</div>`).join("");
  } else if (m.type === "saved") {
    $("saved").textContent = m.ok ? "Saved." : (m.error || "Not saved.");
    $("saved").className = m.ok ? "ok" : "error";
    if (m.ok) {  // on failure keep what the user typed, so it can be corrected
      for (const k of SECRET) $("clear_" + k).checked = false;
      fillSettings(m.settings); serverUrl = (m.settings.base_url || "").replace(/\\/+$/, ""); $("server").textContent = " - " + serverUrl;
    }
  } else if (m.type === "job") { upsertJob(m.job); }
  else if (m.type === "exports") {
    $("folder").textContent = m.folder;
    $("files").innerHTML = `<option value="">${m.files.length ? "Recent exports..." : "No .gcode.3mf files in the folder"}</option>` +
      m.files.map(f => `<option value="${esc(f)}">${esc(f.split(/[\\\\/]/).pop())}</option>`).join("");
  } else if (m.type === "file") {
    file = m.file ? Object.assign({path: m.path}, m.file) : null;
    $("send-form").style.display = file ? "" : "none";
    $("file-info").innerHTML = m.error ? `<span class="error">${esc(m.error)}</span>` : file ?
      `${esc(file.filename)}: sliced for ${esc(file.sliced_model || "an unknown printer")} (${esc(file.printer_preset || "")}), plates ${file.plates.join(", ")}` +
      (file.prediction_s ? `, ${fmtMin(Math.round(file.prediction_s / 60))}` : "") + (file.weight_g ? `, ${file.weight_g} g` : "") : "";
    if (file) {
      $("plate").innerHTML = (file.plates.length > 1 ? '<option value="all">All plates</option>' : "") + file.plates.map(i => `<option value="${i}">${i}</option>`).join("");
      $("plate").style.display = $("plate-label").style.display = file.plates.length > 1 ? "" : "none";
    }
  } else if (m.type === "targets") {
    targets = m.targets;
    $("target").innerHTML = '<option value="">Choose a printer...</option>' + targets.map(t => `<option value="${esc(t.id)}">${esc(t.label)}${t.model && !t.target_model ? " - " + esc(t.model) : ""}${t.location ? " - " + esc(t.location) : ""}${t.online === false ? " (offline)" : t.state && t.state !== "IDLE" ? " (" + esc(STATE[t.state] || t.state) + ")" : ""}${t.compatible === false ? " - wrong model" : ""}</option>`).join("");
    $("target").value = m.selected || "";
    $("reason").textContent = m.error || m.reason || "";
    if (m.default_action && !action()) document.querySelector(`input[name=action][value=${m.default_action}]`).checked = true;
    $("target").onchange();
  } else if (m.type === "check") {
    $("check").className = "err " + (m.level || ""); $("check").textContent = m.message || "";
    $("btn-send").disabled = m.level === "block";
  } else if (m.type === "progress") { $("progress").value = m.pct; $("progress-text").textContent = m.text; }
  else if (m.type === "send_result") {
    $("progress").style.display = "none"; $("progress-text").textContent = "";
    $("send-result").innerHTML = `<span class="${m.ok ? "ok" : "error"}">${esc(m.message)}</span>`;
  }
});
let jobs = [];
function renderJobs(list) {
  jobs = list || jobs;
  const NAMES = {queued: "queued", staged: "waiting for manual start", started: "started", first_layer: "first layer done", error: "error", removed: "removed"};
  $("jobs").innerHTML = jobs.length ? jobs.map(j => `<div>${esc(j.label)} &rarr; ${esc(j.target)}: ${j.milestones.map(x => NAMES[x] || x).join(", ") || "sent"}</div>`).join("") : "Nothing sent in this session.";
}
function upsertJob(j) { const i = jobs.findIndex(x => x.id === j.id); if (i >= 0) jobs[i] = j; else jobs.unshift(j); renderJobs(); }
function beat() { post({type: "heartbeat", visible: !document.hidden}); }
document.addEventListener("visibilitychange", beat);
setInterval(() => { if (!document.hidden) beat(); }, 5000);
post({type: "ready"}); beat();
</script></body></html>"""
)


def chooser_html(context: dict) -> str:
    """The per-send action choice. ``context``: file, target, default, kind."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        + _STYLE
        + """</head><body>
<h2 id="title"></h2>
<div id="where" class="muted"></div>
<div style="margin:12px 0; line-height:2">
  <label><input type="radio" name="action" value="print"> <b>Print now</b> <span class="muted">- starts as soon as the printer is free</span></label><br>
  <label><input type="radio" name="action" value="queue"> <b>Add to queue</b> <span class="muted">- waits in Bambuddy for a manual start</span></label><br>
  <label><input type="radio" name="action" value="store"> <b>Store only</b> <span class="muted">- upload to the Bambuddy library</span></label>
</div>
<label><input type="checkbox" id="dont_ask"> Don't ask again (change it in the Bambuddy tab settings)</label>
<div class="row" style="margin-top:14px; justify-content:flex-end">
  <button id="cancel">Cancel</button><button id="ok">Send to Bambuddy</button>
</div>
<script>
const ctx = """
        + json.dumps(context).replace("</", "<\\/")
        + """;
document.getElementById("title").textContent = ctx.file;
document.getElementById("where").textContent = "Bambuddy printer: " + ctx.target;
document.querySelector(`input[value=${ctx.default}]`).checked = true;
document.getElementById("ok").onclick = () => orca.submit({action: document.querySelector("input[name=action]:checked").value,
  dont_ask: document.getElementById("dont_ask").checked});
document.getElementById("cancel").onclick = () => orca.submit({action: null});
</script></body></html>"""
    )


CONFIG_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    + _STYLE
    + "</head><body><p class='muted'>Bambuddy connection settings. Use the <b>Bambuddy</b> tab to test the connection.</p>"
    + _SETTINGS_FORM
    + """<div class="row" style="margin-top:10px"><button id="save">Save</button><span id="saved" class="ok"></span></div>
<script>
"""
    + _SETTINGS_JS
    + """
const current = (window.orca && orca.getConfig && orca.getConfig()) || {};
const shown = Object.assign({}, current);
for (const k of SECRET) { shown["has_" + k] = !!current[k]; }
shown.extra_header_names = (current.extra_headers || "").split("\n").map(l => l.split(":")[0].trim()).filter(Boolean);
fillSettings(Object.assign({default_action: "print", ask_action: true, printer_selection: "auto", remember_printer: true,
  duplicate_policy: "reuse", verify_tls: true, timeout_s: 15, upload_limit_mb: 0, poll_interval_s: 5}, shown));
document.getElementById("save").onclick = () => {
  const next = Object.assign({}, current, readSettings());
  for (const k of SECRET) if (!next[k]) next[k] = current[k] || "";
  orca.saveConfig(next);
  document.getElementById("saved").textContent = "Saved.";
};
</script></body></html>"""
)
