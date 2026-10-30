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
$$("nav button").forEach((b) => b.addEventListener("click", () => {
  $$("nav button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "tests") loadScenarios();
  if (b.dataset.tab === "setup") loadSettings();
}));

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
function renderLab(lab) {
  if (!lab) { $("#topology").innerHTML = ""; return; }
  const bySeg = {};
  lab.nodes.forEach((n) => (bySeg[n.segment] ||= []).push(n));
  const f = lab.fortigate;
  let html = `<div class="purdue">
    <div class="zone" style="border-left-color:#666"><div class="meta"><b>L4/L5 Enterprise</b><div class="lvl">fysiek netwerk (vmbr0)</div></div>
      <div class="nodes"><div class="node"><div class="n">Jouw IT-netwerk</div><div class="ip">enige toegang loopt via de FortiGate</div></div></div></div>
    <div class="fgt"><span class="dot ${f.status}"></span><b>FortiGate ${f.vmid}</b>
      <span>port1 ${esc(f.wan.ip || f.wan.mode)}</span><span class="muted">status: ${f.status} · REST API: ${f.api ? "gekoppeld" : "nee"}</span></div>`;
  lab.segments.forEach((s) => {
    html += `<div class="zone"><div class="meta"><b>${esc(s.name)}</b>
      <div class="lvl">${esc(s.port)} · ${esc(s.bridge)} ${s.present ? "" : "(ontbreekt)"} · ${esc(s.subnet)}</div></div><div class="nodes">`;
    (bySeg[s.id] || []).forEach((n) => {
      html += `<div class="node ${n.kind}"><div class="n"><span class="dot ${n.status}"></span>${esc(n.name)}</div>
        <div class="ip">${esc(n.ip)} · ${esc(n.role)} · ${n.vmid}</div></div>`;
    });
    html += `</div></div>`;
  });
  $("#topology").innerHTML = html + "</div>";
}

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
                wan_gateway: $("#i-wangw").value.trim(), fgt_mgmt_ip: $("#i-mgmtip").value.trim() }),
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
$("#btn-provision").onclick = () => action("/api/provision");
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
      : "";
    html += `<tr><td><b>${esc(s.title)}</b><div class="why">${esc(s.why)}</div><div class="ev">otprobe ${esc(s.run)}</div></td>
      <td>${esc(s.from)}</td><td>${esc(s.expect)}</td><td>${res}</td><td>${ev}</td>
      <td><button data-run="${esc(s.id)}">Run</button></td></tr>`;
  });
  $("#scenarios").innerHTML = html + "</table>";
  $$("[data-run]").forEach((b) => (b.onclick = () => action(`/api/scenarios/${b.dataset.run}/run`, {}, loadScenarios)));
  $("#adhoc-node").innerHTML = topo.nodes.map((n) => `<option>${esc(n.name)}</option>`).join("");
}
$("#btn-runall").onclick = () => action("/api/scenarios/run-all", {}, loadScenarios);
$("#btn-adhoc").onclick = () => action("/api/adhoc", { node: $("#adhoc-node").value, command: $("#adhoc-cmd").value });

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
refresh();
setInterval(refresh, 10000);
