const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(location.origin + path, { headers: { "Content-Type": "application/json" }, ...opts });
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
function renderSettings(s) {
  $("#cur-image").textContent = s.fgt_image || "-";
  $("#cur-license").textContent = s.license_file || "-";
  $("#cur-token").textContent = s.fortiflex_token_set ? "ja" : "nee";
  $("#cur-pw").textContent = s.admin_password_set ? "ja" : "nee";
  const lic = $(`input[name=lic][value=${s.license_mode || "none"}]`);
  if (lic && !document.activeElement.closest("#tab-setup")) lic.checked = true;
  if (s.wan && !$("#i-wanip").value) {
    $("#i-wanmode").value = s.wan.mode || "static";
    $("#i-wanip").value = s.wan.ip || "";
    $("#i-wangw").value = s.wan.gateway || "";
  }
  if (s.fgt_mgmt_ip && !$("#i-mgmtip").value) $("#i-mgmtip").value = s.fgt_mgmt_ip;
  toggleLic();
}
function toggleLic() {
  const v = ($("input[name=lic]:checked") || {}).value;
  $("#lic-file").hidden = v !== "file";
  $("#lic-flex").hidden = v !== "fortiflex";
}
$$("input[name=lic]").forEach((r) => r.addEventListener("change", toggleLic));

function upload(inputSel, url, progressSel) {
  const file = $(inputSel).files[0];
  if (!file) return alert("Kies eerst een bestand");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `${location.origin}${url}?filename=${encodeURIComponent(file.name)}`);
  if (progressSel) {
    $(progressSel).hidden = false;
    xhr.upload.onprogress = (e) => ($(progressSel).value = (e.loaded / e.total) * 100);
  }
  xhr.onload = () => { if (xhr.status >= 400) alert(xhr.responseText); refresh(); };
  xhr.send(file);
}
$("#btn-upload-image").onclick = () => upload("#f-image", "/api/upload/image", "#p-image");
$("#btn-upload-license").onclick = () => upload("#f-license", "/api/upload/license");

$("#btn-save").onclick = async () => {
  try {
    await post("/api/settings", {
      wan_mode: $("#i-wanmode").value,
      wan_ip: $("#i-wanip").value.trim(),
      wan_gateway: $("#i-wangw").value.trim(),
      fgt_mgmt_ip: $("#i-mgmtip").value.trim(),
      admin_password: $("#i-adminpw").value || null,
      license_mode: ($("input[name=lic]:checked") || {}).value,
      fortiflex_token: $("#i-token").value.trim() || null,
    });
    $("#i-adminpw").value = ""; $("#i-token").value = "";
    refresh();
    alert("Opgeslagen");
  } catch (e) { alert(e.message); }
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
    renderSettings(st.settings);
    renderLab(st.lab);
    const hs = $("#hoststatus");
    hs.textContent = st.host_error ? "Proxmox: geen verbinding" : "Proxmox: verbonden";
    hs.className = "pill " + (st.host_error ? "bad" : "ok");
    hs.title = st.host_error || "";
  } catch (e) { console.error(e); }
}
refresh();
setInterval(refresh, 10000);
