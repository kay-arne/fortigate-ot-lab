const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(location.origin + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { location.href = "/login"; throw new Error("niet ingelogd"); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

// --- tabs ------------------------------------------------------------------
function goTo(tab) {
  $$("nav button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + tab));
  if (tab === "tests") { loadScenarios(); loadAccess(); }
  if (tab === "setup") loadSettings();
  if (tab === "updates") loadVersion();
}
$$("nav button").forEach((b) => b.addEventListener("click", () => goTo(b.dataset.tab)));
$$("[data-goto]").forEach((b) => b.addEventListener("click", () => goTo(b.dataset.goto)));

// --- job log ---------------------------------------------------------------
let logTimer = null;
function followJob(job, onDone) {
  document.body.classList.add("logopen");
  $("#log-title").textContent = job.title;
  $("#log-body").textContent = "";
  clearInterval(logTimer);
  let offset = 0;
  logTimer = setInterval(async () => {
    const j = await api(`/api/jobs/${job.id}?offset=${offset}`);
    offset = j.offset;
    if (j.lines.length) {
      $("#log-body").textContent += j.lines.join("\n") + "\n";
      $("#log-body").scrollTop = $("#log-body").scrollHeight;
    }
    if (j.status !== "running") {
      clearInterval(logTimer);
      refresh();
      onDone && onDone();
    }
  }, 1500);
}
$("#log-close").onclick = () => document.body.classList.remove("logopen");

async function action(path, data, onDone) {
  try { followJob(await post(path, data), onDone); } catch (e) { alert(e.message); }
}

// --- lab view --------------------------------------------------------------
// --- componentselectie -----------------------------------------------------
const selected = new Set();
const CLABEL = { network: "Netwerk (bridges)", fortigate: "FortiGate-VM", template: "LXC-template", policies: "FortiGate-policies" };

function updateSelBar() {
  const n = selected.size;
  $("#sel-count").textContent = n ? `${n} geselecteerd: ${[...selected].map((i) => CLABEL[i] || i.replace("node:", "")).join(", ")}` : "Niets geselecteerd";
  ["#btn-sel-deploy", "#btn-sel-recreate", "#btn-sel-destroy", "#btn-sel-clear"].forEach((b) => ($(b).disabled = !n));
  $$("[data-comp]").forEach((el) => {
    const on = selected.has(el.dataset.comp);
    el.classList.toggle("selected", on);
    const cb = el.querySelector("input[type=checkbox]");
    if (cb) cb.checked = on;
  });
  $$("[data-seg-select]").forEach((cb) => {
    const ids = [...$$(`[data-seg="${cb.dataset.segSelect}"]`)].map((el) => el.dataset.comp);
    cb.checked = ids.length > 0 && ids.every((i) => selected.has(i));
  });
}

const check = () => `<input type="checkbox" tabindex="-1" aria-label="selecteer">`;

function renderLab(lab) {
  if (!lab) { $("#topology").innerHTML = ""; return; }
  const bySeg = {};
  lab.nodes.forEach((n) => (bySeg[n.segment] ||= []).push(n));
  const f = lab.fortigate;
  const t = lab.template || { vmid: "-", status: "unknown" };
  const bridges = lab.segments.filter((s) => s.present).length;
  let html = `<div class="purdue">
    <div class="zone" style="border-left-color:#666"><div class="meta"><b>L4/L5 Enterprise</b><div class="lvl">fysiek netwerk (vmbr0)</div></div>
      <div class="nodes"><div class="node"><div class="n">Jouw IT-netwerk</div><div class="ip">enige toegang loopt via de FortiGate</div></div></div></div>
    <div class="infra">
      <div class="comp${f.api_ok ? " comp-ok" : ""}" data-comp="fortigate">${check()}<span class="dot ${f.status}"></span>
        <div><b>${esc(f.name || "FortiGate")} · ${f.vmid}</b><div class="ip">port1 ${esc(f.wan.ip || f.wan.mode)} · status: ${esc(f.status)}${f.version ? ` · ${esc(f.version)}` : ""}</div></div></div>
      <div class="comp" data-comp="policies">${check()}<span class="dot ${f.api_ok ? "running" : f.api ? "stopped" : "absent"}"></span>
        <div><b>FortiGate-policies</b><div class="ip" title="${esc(f.api_error || "")}">REST API ${f.api_ok ? "antwoordt" : f.api ? "antwoordt niet" : "nog niet gekoppeld"} · adressen, conduits, OT-profielen</div></div></div>
      <div class="comp" data-comp="network">${check()}<span class="dot ${bridges === lab.segments.length ? "running" : bridges ? "stopped" : "absent"}"></span>
        <div><b>Netwerk</b><div class="ip">${bridges}/${lab.segments.length} Purdue-bridges aanwezig</div></div></div>
      <div class="comp" data-comp="template">${check()}<span class="dot ${t.status === "stopped" ? "running" : t.status}"></span>
        <div><b>LXC-template · ${t.vmid}</b><div class="ip">${t.status === "absent" ? "nog niet gebouwd" : "basis voor alle containers"}</div></div></div>
    </div>`;
  lab.segments.forEach((s) => {
    html += `<div class="zone"><div class="meta"><label class="segsel"><input type="checkbox" data-seg-select="${esc(s.id)}"> <b>${esc(s.name)}</b></label>
      <div class="lvl">${esc(s.port)} · ${esc(s.bridge)} ${s.present ? "" : "(ontbreekt)"} · ${esc(s.subnet)}</div></div><div class="nodes">`;
    (bySeg[s.id] || []).forEach((n) => {
      html += `<div class="node ${n.kind}" data-comp="node:${esc(n.name)}" data-seg="${esc(s.id)}">
        <div class="n">${check()}<span class="dot ${n.status}"></span>${esc(n.hostname || n.name)}</div>
        <div class="ip">${esc(n.ip)} · ${esc(n.role)} · ${n.vmid}</div></div>`;
    });
    html += `</div></div>`;
  });
  $("#topology").innerHTML = html + "</div>";
  $$("#topology [data-comp]").forEach((el) => el.addEventListener("click", () => {
    const id = el.dataset.comp;
    selected.has(id) ? selected.delete(id) : selected.add(id);
    updateSelBar();
  }));
  $$("#topology [data-seg-select]").forEach((cb) => cb.addEventListener("change", () => {
    $$(`[data-seg="${cb.dataset.segSelect}"]`).forEach((el) => (cb.checked ? selected.add(el.dataset.comp) : selected.delete(el.dataset.comp)));
    updateSelBar();
  }));
  updateSelBar();
}

function selAction(path, recreate, verb) {
  const items = [...selected];
  const names = items.map((i) => CLABEL[i] || i.replace("node:", "")).join("\n- ");
  if (!confirm(`${verb}:\n- ${names}`)) return;
  action(path, { items, recreate }, () => { selected.clear(); updateSelBar(); });
}
$("#btn-sel-deploy").onclick = () => selAction("/api/deploy/selected", false, "Uitrollen (bestaande worden overgeslagen)");
$("#btn-sel-recreate").onclick = () => selAction("/api/deploy/selected", true, "Verwijderen en opnieuw uitrollen");
$("#btn-sel-destroy").onclick = () => selAction("/api/destroy/selected", false, "Verwijderen");
$("#btn-sel-clear").onclick = () => { selected.clear(); updateSelBar(); };

// --- setup -----------------------------------------------------------------
const fmtSize = (b) => (b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB");

function renderChecks(checks) {
  $("#checklist").innerHTML = checks.map((c) =>
    `<li class="${c.ok ? "ok" : c.optional ? "opt" : "todo"}">${c.ok ? "✓" : c.optional ? "○" : "•"} ${esc(c.label)}${c.optional ? " <small>(optioneel)</small>" : ""}</li>`).join("");
  checks.forEach((c) => {
    const b = $(`.badge[data-check=${c.id}]`);
    if (!b) return;
    b.className = "badge " + (c.ok ? "ok" : c.optional ? "opt" : "todo");
    b.textContent = c.ok ? "ingesteld" : c.optional ? "optioneel" : "nog invullen";
  });
}

// Vult de formulieren; alleen bij laden en na opslaan, zodat polling je invoer niet overschrijft.
function fillSettings(s) {
  renderChecks(s.checks);
  $("#cur-image").textContent = s.fgt_image ? `${s.fgt_image} (${fmtSize(s.fgt_image_size || 0)})` : "geen";
  $("#cur-license").textContent = s.license_file || "geen";
  $("#i-wanmode").value = s.wan.mode || "static";
  $("#i-wanip").value = s.wan.ip || "";
  $("#i-wangw").value = s.wan.gateway || "";
  $("#i-mgmtip").value = s.fgt_mgmt_ip || "";
  const sugg = s.dns_suggestion || [];
  $("#i-dns1").value = s.wan.dns1 || "";
  $("#i-dns2").value = s.wan.dns2 || "";
  $("#dns-suggest").innerHTML = sugg.length && !s.wan.dns1
    ? `<br>Voorstel (DNS van de controller): <a href="#" id="dns-use">${sugg.map(esc).join(", ")} gebruiken</a>` : "";
  const use = $("#dns-use");
  if (use) use.onclick = (e) => { e.preventDefault(); $("#i-dns1").value = sugg[0] || ""; $("#i-dns2").value = sugg[1] || ""; };
  $("#i-adminpw").value = s.fgt_admin_password || "";
  $("#o-apikey").value = s.fgt_api_key || "";
  $("#i-token").value = s.fortiflex_token || "";
  const lic = $(`input[name=lic][value=${s.license_mode || "none"}]`);
  if (lic) lic.checked = true;
  toggleWan();
  toggleLic();
}

async function loadSettings() {
  fillSettings(await api("/api/settings"));
}

function toggleWan() {
  const mode = $("#i-wanmode").value;
  $$("[data-wan]").forEach((el) => (el.hidden = el.dataset.wan !== mode));
}
$("#i-wanmode").addEventListener("change", toggleWan);

function toggleLic() {
  const v = ($("input[name=lic]:checked") || {}).value;
  $("#lic-file").hidden = v !== "file";
  $("#lic-flex").hidden = v !== "fortiflex";
}
$$("input[name=lic]").forEach((r) => r.addEventListener("change", toggleLic));

// oogje: wachtwoord/token tonen of verbergen
$$("[data-eye]").forEach((b) => b.addEventListener("click", () => {
  const input = $("#" + b.dataset.eye);
  input.type = input.type === "password" ? "text" : "password";
  b.classList.toggle("open", input.type === "text");
}));
$$("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
  const v = $("#" + b.dataset.copy).value;
  if (!v) return;
  try { await navigator.clipboard.writeText(v); b.textContent = "Gekopieerd"; }
  catch { b.textContent = "Niet gelukt"; }
  setTimeout(() => (b.textContent = "Kopieer"), 1500);
}));
$("#btn-genpw").onclick = () => {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!#%+-";
  const rnd = crypto.getRandomValues(new Uint32Array(16));
  $("#i-adminpw").value = Array.from(rnd, (n) => chars[n % chars.length]).join("");
  $("#i-adminpw").type = "text";
  $("[data-eye=i-adminpw]").classList.add("open");
};

function showResult(key, ok, msg) {
  const el = $(`[data-result=${key}]`);
  el.className = "result " + (ok ? "ok" : "bad");
  el.textContent = msg;
  if (ok) setTimeout(() => (el.textContent = ""), 4000);
}

const savePayload = {
  wan: () => ({ wan_mode: $("#i-wanmode").value, wan_ip: $("#i-wanip").value.trim(),
                wan_gateway: $("#i-wangw").value.trim(), fgt_mgmt_ip: $("#i-mgmtip").value.trim(),
                wan_dns1: $("#i-dns1").value.trim(), wan_dns2: $("#i-dns2").value.trim() }),
  admin: () => ({ admin_password: $("#i-adminpw").value }),
  license: () => ({ license_mode: ($("input[name=lic]:checked") || {}).value || "none",
                    fortiflex_token: $("#i-token").value.trim() || null }),
};
$$("[data-save]").forEach((b) => b.addEventListener("click", async () => {
  const key = b.dataset.save;
  try {
    fillSettings(await post("/api/settings", savePayload[key]()));
    showResult(key, true, "Opgeslagen");
  } catch (e) { showResult(key, false, e.message); }
}));

function upload(inputSel, url, progressSel) {
  const file = $(inputSel).files[0];
  if (!file) return alert("Kies eerst een bestand");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `${location.origin}${url}?filename=${encodeURIComponent(file.name)}`);
  if (progressSel) {
    $(progressSel).hidden = false;
    xhr.upload.onprogress = (e) => ($(progressSel).value = (e.loaded / e.total) * 100);
  }
  xhr.onload = () => {
    if (xhr.status === 401) return (location.href = "/login");
    if (xhr.status >= 400) alert(xhr.responseText);
    else fillSettings(JSON.parse(xhr.responseText));
    if (progressSel) $(progressSel).hidden = true;
  };
  xhr.send(file);
}
$("#btn-upload-image").onclick = () => upload("#f-image", "/api/upload/image", "#p-image");
$("#btn-upload-license").onclick = () => upload("#f-license", "/api/upload/license");

// --- Proxmox-verbinding ------------------------------------------------------
function secretRow(id, value) {
  return `<div class="secret"><input id="${id}" type="password" readonly value="${esc(value)}">
    <button class="eye" data-eye="${id}" type="button" title="Tonen/verbergen"></button></div>`;
}
async function loadHost() {
  const h = await api("/api/host");
  if (!h.configured) {
    $("#host-info").innerHTML = `<p class="warn">Geen <code>host.json</code> gevonden: draai de installer op de Proxmox host.</p>`;
    return;
  }
  const rows = [
    ["Proxmox host", `${esc(h.node)} <span class="mono">${esc(h.host)}</span>`, "Adres waarop de controller via SSH (TCP 22) inlogt."],
    ["SSH-gebruiker", `<span class="mono">${esc(h.ssh_user)}</span> ${h.sudo ? '<span class="tag">sudo: qm, pct, pvesh</span>' : '<span class="tag warn">root</span>'}`,
      h.sudo ? "Aparte gebruiker; intrekken met <code>otlab-revoke</code> op de host." : "Sleutel staat bij root; intrekken met <code>otlab-revoke</code> op de host."],
    ["SSH-sleutel", `<span class="mono">${esc(h.ssh_key)}</span>`, "Private key in deze controller; de publieke sleutel staat op de host."],
    ["Werkmap op host", `<span class="mono">${esc(h.workdir)}</span>`, "Hier komen het FortiGate-image en de config-ISO."],
    ["Lab-storage", `<span class="mono">${esc(h.storage)}</span>`, "Disks van de FortiGate en de OT-containers."],
    ["LXC-template", `<span class="mono">${esc(h.template)}</span>`, "Debian-basis voor het OT-template."],
    ["FortiGate uplink", `<span class="mono">${esc(h.uplink_bridge)}</span>${h.uplink_vlan ? ` VLAN <span class="mono">${esc(h.uplink_vlan)}</span>` : " (geen VLAN)"}`, "Bridge van port1 naar je fysieke netwerk."],
    ["Template-build", `<span class="mono">${esc(h.template_net)}</span>`, "Tijdelijk netwerk om packages te downloaden."],
    ["Naamprefix", `<span class="mono">${esc(h.name_prefix)}-</span>`, `VM's en containers heten <code>${esc(h.name_prefix)}-&lt;naam&gt;</code>, bijv. <code>${esc(h.name_prefix)}-fgt</code>.`],
    ["VMID's", `lab <span class="mono">${esc(h.vmid_range)}</span> · FortiGate ${h.fortigate_vmid} · template ${h.template_vmid} · controller ${esc(h.controller_ctid)}`, ""],
    ["Web UI-login", `<span class="mono">admin</span> / ${secretRow("o-uipw", h.ui_password)}`, "Ook te vinden in de controller: <code>/opt/otlab/data/ui_password</code>."],
  ];
  $("#host-info").innerHTML = `<table class="kv">${rows.map(([k, v, hint]) =>
    `<tr><th>${k}</th><td>${v}${hint ? `<div class="hint">${hint}</div>` : ""}</td></tr>`).join("")}</table>`;
  $("#wan-bridge").textContent = h.uplink_bridge + (h.uplink_vlan ? ` (VLAN ${h.uplink_vlan})` : "");
  $$("#host-info [data-eye]").forEach((b) => b.addEventListener("click", () => {
    const input = $("#" + b.dataset.eye);
    input.type = input.type === "password" ? "text" : "password";
    b.classList.toggle("open", input.type === "text");
  }));
}
$("#btn-host-test").onclick = async () => {
  const el = $("#host-test-result");
  el.className = "result"; el.textContent = "testen…";
  try {
    const r = await post("/api/host/test");
    el.className = "result " + (r.ok ? "ok" : "bad");
    el.textContent = r.detail;
  } catch (e) { el.className = "result bad"; el.textContent = e.message; }
};

// --- acties ----------------------------------------------------------------
$("#btn-deploy").onclick = () => confirm("Volledig lab uitrollen op de Proxmox host?") && action("/api/deploy");
$("#btn-update").onclick = () => action("/api/update-sims");
$("#btn-destroy").onclick = () => {
  if (!confirm("Alle lab-VM's en containers verwijderen?")) return;
  const template = confirm("Ook het LXC-template verwijderen?");
  const network = confirm("Ook de OT-bridges op de host verwijderen?");
  action("/api/destroy", { template, network });
};

// --- testcenter ------------------------------------------------------------
async function loadScenarios() {
  const [{ scenarios, results }, topo] = await Promise.all([api("/api/scenarios"), api("/api/topology")]);
  let html = `<table><tr><th>Test</th><th>Vanaf</th><th>Verwacht</th><th>Resultaat</th><th>FortiGate</th><th></th></tr>`;
  let group = null;
  scenarios.forEach((s) => {
    if (s.group !== group) { group = s.group; html += `<tr class="grp"><td colspan="6">${esc(group)}</td></tr>`; }
    const r = results[s.id];
    const res = r ? `<span class="res ${r.passed ? "pass" : "fail"}">${r.passed ? "PASS" : "FAIL"}</span> (${esc(r.outcome)})` : "-";
    const ev = r && r.evidence.length
      ? r.evidence.slice(0, 3).map((e) => `<div class="ev">${esc(e.type)}: ${esc(e.action)} · ${esc(e.policy)} ${e.app ? "· " + esc(e.app) : ""}${e.attack ? " · " + esc(e.attack) : ""}</div>`).join("")
      : r && typeof r.detail === "string" ? `<div class="ev">${esc(r.detail)}</div>` : "";
    const manual = s.manual ? ` <span class="tag warn">demo, niet in 'alle tests'</span>` : "";
    html += `<tr><td><b>${esc(s.title)}</b>${manual}<div class="why">${esc(s.why)}</div><div class="ev">${s.check ? "controller:" : "otprobe"} ${esc(s.run)}</div></td>
      <td>${esc(s.from)}</td><td>${esc(s.expect)}</td><td>${res}</td><td>${ev}</td>
      <td><button data-run="${esc(s.id)}">Run</button></td></tr>`;
  });
  $("#scenarios").innerHTML = html + "</table>";
  $$("[data-run]").forEach((b) => (b.onclick = () => action(`/api/scenarios/${b.dataset.run}/run`, {}, loadScenarios)));
  $("#adhoc-node").innerHTML = topo.nodes.map((n) => `<option value="${esc(n.name)}">${esc(n.hostname || n.name)}</option>`).join("");
}
$("#btn-runall").onclick = () => action("/api/scenarios/run-all", {}, loadScenarios);

// --- toegang voor de kandidaat ---------------------------------------------
const VIEW_LABEL = { hmi: "SCADA HMI", historian: "Historian" };
async function loadAccess() {
  const a = await api("/api/access");
  const wan = a.wan_ip || "&lt;WAN-IP&gt;";
  const viewers = a.viewers.map((v) =>
    `<button class="ghost" data-view="${esc(v.name)}" title="${esc(v.hostname)} · ${esc(v.ip)}">${esc(v.title || VIEW_LABEL[v.role] || v.hostname)}</button>`).join(" ");
  $("#access").innerHTML = `
    <div class="acc"><h3>1. FortiGate-GUI <span class="tag">security-team</span></h3>
      <p>Logs, policies en assets: hier zie je wát er gebeurde.</p>
      <table class="kv"><tr><th>URL</th><td>${a.fgt_url ? `<a href="${esc(a.fgt_url)}" target="_blank" rel="noopener">${esc(a.fgt_url)}</a>` : "-"}</td></tr>
        <tr><th>Login</th><td><span class="mono">${esc(a.fgt_user)}</span> / ${secretRow("acc-fgtpw", a.fgt_password)}</td></tr></table></div>
    <div class="acc"><h3>2. Contractor via jump host <span class="tag">door de FortiGate</span></h3>
      <p>Het pad van Remco: SSH via de VIP naar de jump host in de DMZ, en van daar alleen naar de EWS.</p>
      <table class="kv"><tr><th>Jump host</th><td><code>ssh -p ${a.jump_port} ${esc(a.user)}@${wan}</code></td></tr>
        <tr><th>Daarna EWS</th><td><code>ssh ${esc(a.user)}@${esc(a.ews_ip)}</code></td></tr>
        <tr><th>Wachtwoord</th><td>${secretRow("acc-pw", a.password)}</td></tr>
        <tr><th>MFA-code</th><td><span class="mono" id="acc-totp">${esc(a.totp_code)}</span>
          <span class="hint">alleen jump host, wisselt elke 30 s</span></td></tr>
        <tr><th>MFA-geheim</th><td>${secretRow("acc-totp-secret", a.totp_secret)}
          <span class="hint">voor een authenticator-app</span></td></tr></table>
      <p class="hint">Op beide machines staat <code>otprobe</code>, bijv. <code>otprobe s7-read 10.10.0.20</code>.</p>
      <div class="row"><button class="ghost" id="btn-recordings">Sessie-opnames jump host</button></div></div>
    <div class="acc"><h3>3. Op de vloer <span class="tag">via Proxmox</span></h3>
      <p>Tests en ad-hoc commando's draaien ín het gekozen device. Het kijkvenster toont de webinterface van een device,
        zonder gat in de firewall.</p>
      <div class="row">${viewers || "-"}</div></div>`;
  $$("#access [data-eye]").forEach((b) => b.addEventListener("click", () => {
    const input = $("#" + b.dataset.eye);
    input.type = input.type === "password" ? "text" : "password";
    b.classList.toggle("open", input.type === "text");
  }));
  $$("#access [data-view]").forEach((b) => (b.onclick = () => openViewer(`/api/view/${encodeURIComponent(b.dataset.view)}`, b.textContent)));
  $("#btn-recordings").onclick = () => openViewer("/api/recordings", "Sessie-opnames jump host");
  clearInterval(totpTimer);
  totpTimer = setInterval(async () => {
    if (!$("#acc-totp")) return clearInterval(totpTimer);
    try { $("#acc-totp").textContent = (await api("/api/access")).totp_code; } catch (e) { /* volgende ronde */ }
  }, 10000);
}
let totpTimer = null;

let viewerTimer = null;
async function refreshViewer(path) {
  try {
    const r = await fetch(path, { credentials: "same-origin" });
    if (r.status === 401) { location.href = "/login"; return; }
    const body = await r.text();
    if (!r.ok) throw new Error((JSON.parse(body).detail) || r.statusText);
    $("#viewer-frame").srcdoc = body;
    $("#viewer-status").textContent = "bijgewerkt " + new Date().toLocaleTimeString("nl-NL");
  } catch (e) { $("#viewer-status").textContent = "fout: " + e.message; }
}
function openViewer(path, title) {
  $("#viewer-title").textContent = title;
  $("#viewer-frame").srcdoc = "";
  $("#viewer").hidden = false;
  refreshViewer(path);
  clearInterval(viewerTimer);
  viewerTimer = setInterval(() => refreshViewer(path), 5000);
}
$("#viewer-close").onclick = () => { $("#viewer").hidden = true; clearInterval(viewerTimer); };
$("#btn-adhoc").onclick = () => action("/api/adhoc", { node: $("#adhoc-node").value, command: $("#adhoc-cmd").value });

// --- updates -----------------------------------------------------------------
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString("nl-NL", { dateStyle: "medium", timeStyle: "short" }) : "");
let currentCommit = null;

async function loadVersion() {
  const v = await api("/api/version");
  if (!v.git) {
    $("#version-pill").textContent = "geen git";
    $("#upd-current").textContent = "Geen git-checkout gevonden; bijwerken via de UI is niet mogelijk.";
    return v;
  }
  currentCommit = v.commit;
  $("#version-pill").textContent = v.commit;
  $("#upd-current").innerHTML = `<span class="mono">${esc(v.commit)}</span> ${esc(v.subject)} <span class="hint">(${esc(fmtDate(v.date))})</span>`;
  $("#upd-branch").textContent = v.branch;
  $("#otsim-banner").hidden = !v.otsim_outdated;
  if (v.updating && $("#upd-log").hidden) followUpdate();
  return v;
}

function markUpdateAvailable(n) {
  $("#update-dot").hidden = !n;
  $("#version-pill").classList.toggle("new", !!n);
  $("#version-pill").title = n ? `${n} update(s) beschikbaar` : "Versie van de controller";
  const b = $("#upd-badge");
  b.className = "badge " + (n ? "todo" : "ok");
  b.textContent = n ? `${n} update(s) beschikbaar` : "up-to-date";
}

async function checkUpdates(silent) {
  const el = $("#upd-result");
  if (!silent) { el.className = "result"; el.textContent = "controleren…"; }
  try {
    const r = await post("/api/update/check");
    markUpdateAvailable(r.behind);
    if (silent) return;
    el.textContent = "";
    let html = "";
    if (r.behind) {
      html += `<ul class="commits">${r.commits.map((c) =>
        `<li><span class="mono">${esc(c.commit)}</span>${esc(c.subject)} <span class="hint">${esc(fmtDate(c.date))}</span></li>`).join("")}</ul>`;
      if (r.otsim_changed) html += `<p class="help">Deze update wijzigt ook de OT-simulators; klik daarna onder <b>2. Lab</b> op <b>Simulators bijwerken</b>.</p>`;
    } else {
      el.className = "result ok"; el.textContent = "De controller is up-to-date.";
    }
    if (r.dirty.length) {
      html += `<p class="warn">Lokale wijzigingen in <code>/opt/otlab</code> blokkeren de update:</p><pre class="logbox">${esc(r.dirty.join("\n"))}</pre>`;
    }
    $("#upd-commits").innerHTML = html;
    $("#btn-upd-run").hidden = !r.behind || r.dirty.length > 0;
  } catch (e) {
    if (!silent) { el.className = "result bad"; el.textContent = e.message; }
  }
}
$("#btn-upd-check").onclick = () => checkUpdates(false);

$("#btn-upd-run").onclick = async () => {
  if (!confirm("Controller bijwerken en herstarten? Het lab zelf blijft draaien.")) return;
  try { await post("/api/update/run"); followUpdate(); }
  catch (e) { $("#upd-result").className = "result bad"; $("#upd-result").textContent = e.message; }
};

function followUpdate() {
  const box = $("#upd-log");
  box.hidden = false; box.textContent = "";
  $("#btn-upd-run").hidden = true;
  const before = currentCommit;
  let offset = 0, restarting = false;
  const timer = setInterval(async () => {
    try {
      if (!restarting) {
        const r = await api(`/api/update/log?offset=${offset}`);
        offset = r.offset;
        if (r.lines.length) { box.textContent += r.lines.join("\n") + "\n"; box.scrollTop = box.scrollHeight; }
        if (!r.running) {
          if (/FOUT|Al up-to-date/.test(box.textContent) && !/herstarten/.test(box.textContent)) { clearInterval(timer); return; }
          restarting = true;
          box.textContent += "Wachten tot de controller weer bereikbaar is…\n";
        }
      } else {
        const v = await api("/api/version");
        if (v.commit && v.commit !== before) { clearInterval(timer); location.reload(); }
      }
    } catch (e) {
      restarting = true; // controller is aan het herstarten
    }
  }, 1500);
}
$("#btn-update-banner").onclick = () => action("/api/update-sims", {}, loadVersion);

// --- polling ---------------------------------------------------------------
async function refresh() {
  try {
    const st = await api("/api/status");
    renderChecks(st.settings.checks);
    renderLab(st.lab);
    const hs = $("#hoststatus");
    hs.textContent = st.host_error ? "Proxmox: geen verbinding" : "Proxmox: verbonden";
    hs.className = "pill " + (st.host_error ? "bad" : "ok");
    hs.title = st.host_error || "";
  } catch (e) { console.error(e); }
}
loadSettings();
loadHost();
loadVersion().then((v) => v.git && checkUpdates(true));
refresh();
setInterval(refresh, 10000);
