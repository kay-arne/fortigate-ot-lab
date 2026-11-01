"""Web UI en API van de OT Lab controller."""
import html
import ipaddress
import json
import re
from urllib.parse import quote
from pathlib import Path
from typing import Optional

import asyncio
import hashlib

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, deployer, fortigate, jobs, scenarios, updates
from .config import (DATA, HOST_FILE, UPLOADS, contractor_credentials, contractor_totp_secret, get_state, totp, inventory, load_host, load_topology,
                     template_vmid, update_state)
from .hostctl import host

HERE = Path(__file__).parent

app = FastAPI(title="FortiGate OT Lab", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

PUBLIC_PATHS = ("/login", "/static/")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PATHS) or auth.valid_session(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "niet ingelogd"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


STATIC_REF = re.compile(r'(/static/[\w.-]+\.(?:css|js))"')
NO_CACHE = {"Cache-Control": "no-cache"}


def _page(name: str) -> str:
    """HTML-template met ?v=<hash> achter de static-bestanden, zodat de browser
    na een controller-update nooit een oude app.js/style.css gebruikt."""
    def versioned(m):
        digest = hashlib.sha1((HERE / m.group(1).lstrip("/")).read_bytes()).hexdigest()[:10]
        return f'{m.group(1)}?v={digest}"'
    return STATIC_REF.sub(versioned, (HERE / "templates" / name).read_text())


def _login_page(error: str = "") -> HTMLResponse:
    html = _page("login.html")
    block = f'<div class="login-error">{error}</div>' if error else ""
    return HTMLResponse(html.replace("<!--ERROR-->", block), status_code=401 if error else 200,
                        headers=NO_CACHE)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if auth.valid_session(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=303)
    return _login_page()


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    if not auth.check_credentials(username, password):
        await asyncio.sleep(1)  # brute force afremmen
        return _login_page("Onjuiste gebruikersnaam of wachtwoord.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.new_session(), max_age=auth.SESSION_SECONDS,
                    httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_page("index.html"), headers=NO_CACHE)


# --- status & instellingen -----------------------------------------------
# Dit is een lab: wachtwoorden en tokens gaan bewust leesbaar naar de UI
# (achter de basic-auth login), zodat ze met het oogje te tonen zijn.
def public_state() -> dict:
    s = get_state()
    topo = load_topology()
    wan = topo["fortigate"]["wan"]
    image = s.get("fgt_image")
    image_size = (UPLOADS / image).stat().st_size if image and (UPLOADS / image).exists() else None
    lic_mode = s.get("license_mode", "none")
    checks = [
        {"id": "host", "label": "Proxmox-koppeling", "ok": HOST_FILE.exists()},
        {"id": "image", "label": "FortiGate-image", "ok": bool(image_size)},
        {"id": "wan", "label": "WAN-adres port1", "ok": wan["mode"] == "dhcp" or bool(wan.get("ip") and wan.get("gateway"))},
        {"id": "admin", "label": "Admin-wachtwoord", "ok": bool(s.get("fgt_admin_password"))},
        {"id": "license", "label": "Licentie", "optional": True,
         "ok": (lic_mode == "file" and bool(s.get("license_file")))
               or (lic_mode == "fortiflex" and bool(s.get("fortiflex_token")))},
    ]
    return {
        "wan": wan,
        "fgt_image": image,
        "fgt_image_size": image_size,
        "license_mode": lic_mode,
        "license_file": s.get("license_file"),
        "fortiflex_token": s.get("fortiflex_token") or "",
        "fgt_admin_password": s.get("fgt_admin_password") or "",
        "fgt_api_key": s.get("fgt_api_key") or "",
        "fgt_mgmt_ip": s.get("fgt_mgmt_ip") or "",
        "dns_suggestion": _resolvers(),
        "deployed": s.get("deployed", False),
        "provisioned": s.get("fgt_provisioned", False),
        "checks": checks,
    }


def _resolvers() -> list[str]:
    """DNS-servers van de controller zelf, als voorstel voor de FortiGate."""
    try:
        lines = Path("/etc/resolv.conf").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            try:
                ip = ipaddress.ip_address(parts[1])
            except ValueError:
                continue
            if ip.version == 4 and not ip.is_loopback:  # FortiOS 'set primary' is alleen IPv4
                out.append(parts[1])
    return out[:2]


@app.get("/api/settings")
def get_settings():
    return public_state()


@app.get("/api/host")
def host_info():
    """Hoe de controller met Proxmox praat (uit data/host.json van de installer)."""
    if not HOST_FILE.exists():
        return {"configured": False}
    h = load_host()
    topo = load_topology()
    base = topo["lab"]["vmid_base"]
    pw_file = DATA / "ui_password"
    return {
        "configured": True,
        "host": h["host"],
        "node": h["node"],
        "ssh_user": h.get("ssh_user", "root"),
        "ssh_key": h["ssh_key"],
        "sudo": h.get("ssh_user", "root") != "root",
        "workdir": h.get("workdir", "/var/lib/vz/otlab"),
        "storage": h["storage"],
        "template": h["template"],
        "controller_ctid": h.get("controller_ctid"),
        "uplink_bridge": topo["lab"]["uplink_bridge"],
        "uplink_vlan": topo["lab"].get("uplink_vlan") or "",
        "template_net": topo["lab"]["template_net"],
        "vmid_range": f"{base}-{base + 99}",
        "name_prefix": topo["lab"]["name_prefix"],
        "fortigate_vmid": topo["fortigate"]["vmid"],
        "template_vmid": template_vmid(topo),
        "ui_password": pw_file.read_text().strip() if pw_file.exists() else "",
    }


@app.post("/api/host/test")
def host_test():
    """Log in op de host en controleer of qm/pct/pvesh via (sudo) werken."""
    try:
        h = host()
        who = h.run("id -un").strip()
        version = h.run(["pvesh", "get", "/version", "--output-format", "json"])
        v = json.loads(version)
        return {"ok": True, "detail": f"ingelogd als {who} op {h.node}; Proxmox VE {v.get('version')} "
                                      f"({'via sudo' if h.sudo else 'als root'})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)}


@app.get("/api/topology")
def topology():
    topo = load_topology()
    return {"segments": topo["segments"], "nodes": inventory(topo), "fortigate": topo["fortigate"]}


@app.get("/api/status")
def status():
    try:
        lab = deployer.lab_status()
        err = None
    except Exception as e:  # noqa: BLE001
        # host onbereikbaar: toon de topologie wel, met onbekende status
        topo = load_topology()
        lab = {"fortigate": {"vmid": topo["fortigate"]["vmid"], "status": "unknown",
                             "name": f"{topo['lab']['name_prefix']}-{topo['fortigate']['name']}",
                             "wan": topo["fortigate"]["wan"], "api": False},
               "template": {"vmid": template_vmid(topo), "status": "unknown"},
               "segments": [{**s, "present": False} for s in topo["segments"]],
               "nodes": [{**n, "status": "unknown"} for n in inventory(topo)]}
        err = str(e)
    return {"settings": public_state(), "lab": lab, "host_error": err, "jobs": jobs.recent()}


class Settings(BaseModel):
    wan_mode: Optional[str] = None
    wan_ip: Optional[str] = None
    wan_gateway: Optional[str] = None
    wan_dns1: Optional[str] = None
    wan_dns2: Optional[str] = None
    fgt_mgmt_ip: Optional[str] = None
    admin_password: Optional[str] = None
    license_mode: Optional[str] = None       # none | file | fortiflex
    fortiflex_token: Optional[str] = None


@app.post("/api/settings")
def save_settings(s: Settings):
    changes = {}
    if s.wan_mode:
        if s.wan_mode not in ("static", "dhcp"):
            raise HTTPException(400, "wan_mode moet static of dhcp zijn")
        ip, gw = (s.wan_ip or "").strip(), (s.wan_gateway or "").strip()
        if s.wan_mode == "static":
            try:
                iface = ipaddress.ip_interface(ip)
                if "/" not in ip or iface.network.prefixlen == 32:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "WAN-IP: geef IP met prefix, bijv. 192.168.1.250/24")
            if gw:
                try:
                    if ipaddress.ip_address(gw) not in iface.network:
                        raise HTTPException(400, f"gateway {gw} ligt niet in {iface.network}")
                except ValueError:
                    raise HTTPException(400, "ongeldige gateway")
        if s.wan_mode == "static" and not gw:
            raise HTTPException(400, "gateway is verplicht: zonder default route geen internet/licentie")
        dns = []
        for d in (s.wan_dns1, s.wan_dns2):
            d = (d or "").strip()
            if d:
                try:
                    if ipaddress.ip_address(d).version != 4:
                        raise ValueError
                except ValueError:
                    raise HTTPException(400, f"ongeldige DNS-server: {d} (IPv4-adres verwacht)")
            dns.append(d)
        changes["wan"] = {"dns1": dns[0], "dns2": dns[1],
                          "mode": s.wan_mode, "ip": ip if s.wan_mode == "static" else "",
                          "gateway": gw if s.wan_mode == "static" else ""}
    if s.fgt_mgmt_ip is not None:
        if s.fgt_mgmt_ip.strip():
            try:
                ipaddress.ip_address(s.fgt_mgmt_ip.strip())
            except ValueError:
                raise HTTPException(400, "ongeldig management-IP")
        changes["fgt_mgmt_ip"] = s.fgt_mgmt_ip.strip()
    if s.admin_password:
        if len(s.admin_password) < 8 or '"' in s.admin_password:
            raise HTTPException(400, "admin-wachtwoord: minimaal 8 tekens, geen aanhalingstekens")
        changes["fgt_admin_password"] = s.admin_password
    if s.license_mode:
        if s.license_mode not in ("none", "file", "fortiflex"):
            raise HTTPException(400, "ongeldige licentiemodus")
        changes["license_mode"] = s.license_mode
    if s.fortiflex_token:
        if not re.fullmatch(r"[A-Za-z0-9]{8,64}", s.fortiflex_token.strip()):
            raise HTTPException(400, "FortiFlex-token heeft een onverwacht formaat")
        changes["fortiflex_token"] = s.fortiflex_token.strip()
    update_state(**changes)
    return public_state()


async def _stream_upload(request: Request, filename: str, allowed: tuple[str, ...]) -> str:
    name = Path(filename).name
    if not name.lower().endswith(allowed):
        raise HTTPException(400, f"verwacht bestand met extensie {', '.join(allowed)}")
    dest = UPLOADS / name
    with open(dest, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return name


@app.post("/api/upload/image")
async def upload_image(request: Request, filename: str):
    name = await _stream_upload(request, filename, (".qcow2",))
    update_state(fgt_image=name)
    return public_state()


@app.post("/api/upload/license")
async def upload_license(request: Request, filename: str):
    name = await _stream_upload(request, filename, (".lic",))
    update_state(license_file=name, license_mode="file")
    return public_state()


# --- updates -----------------------------------------------------------------
@app.get("/api/version")
def version():
    return updates.version()


@app.post("/api/update/check")
def update_check():
    try:
        return updates.check()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"controleren mislukt: {e}")


@app.post("/api/update/run")
def update_run():
    try:
        updates.start()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, str(e))
    return {"started": True}


@app.get("/api/update/log")
def update_log(offset: int = 0):
    return updates.log_lines(offset)


# --- lab-acties ------------------------------------------------------------
def _start(title, fn, *args, exclusive=True):
    try:
        return jobs.start(title, fn, *args, exclusive=exclusive)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/deploy")
def deploy():
    return _start("Deploy lab", deployer.deploy_all)


@app.post("/api/provision")
def provision():
    return _start("FortiGate OT-policies", lambda log: fortigate.provision(log, load_topology()))


@app.post("/api/update-sims")
def update_sims():
    return _start("Simulators bijwerken", deployer.update_simulators)


@app.post("/api/signatures/check")
def signatures_check():
    """Zijn de OT-pakketten (device identification, virtual patching) geladen?"""
    try:
        return fortigate.ot_signature_status(load_topology())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"FortiGate niet bereikbaar: {e}")


@app.post("/api/signatures/update")
def signatures_update():
    """Update-ronde: FortiGuard-update starten en wachten tot de OT-pakketten geladen zijn."""
    return _start("OT-signatures bijwerken",
                  lambda log: fortigate.update_ot_signatures(log, load_topology(), attempts=2))


class Destroy(BaseModel):
    template: bool = False
    network: bool = False


class Selection(BaseModel):
    items: list[str]
    recreate: bool = False


def _check_items(items: list[str]) -> list[str]:
    allowed = set(deployer.component_ids(load_topology()))
    bad = [i for i in items if i not in allowed]
    if bad or not items:
        raise HTTPException(400, f"onbekende of geen componenten: {', '.join(bad) or '-'}")
    return items


@app.post("/api/deploy/selected")
def deploy_selected(sel: Selection):
    items = _check_items(sel.items)
    title = ("Opnieuw uitrollen" if sel.recreate else "Uitrollen") + f" ({len(items)} componenten)"
    return _start(title, deployer.deploy_selected, items, sel.recreate)


@app.post("/api/destroy/selected")
def destroy_selected(sel: Selection):
    items = _check_items(sel.items)
    return _start(f"Verwijderen ({len(items)} componenten)", deployer.destroy_selected, items)


@app.post("/api/destroy")
def destroy(d: Destroy):
    return _start("Lab afbreken", deployer.destroy_all, d.template, d.network)


@app.get("/api/jobs/{job_id}")
def job(job_id: str, offset: int = 0):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    return {**{k: v for k, v in j.items() if k != "lines"}, "lines": j["lines"][offset:],
            "offset": len(j["lines"])}


# --- testcenter -------------------------------------------------------------
@app.get("/api/scenarios")
def list_scenarios():
    return {"scenarios": scenarios.load(), "results": scenarios.results()}


@app.post("/api/scenarios/run-all")
def run_all():
    return _start("Alle validatietests", scenarios.run_all, exclusive=False)


@app.post("/api/scenarios/{sid}/run")
def run_one(sid: str):
    return _start(f"Test {sid}", scenarios.run_scenario, sid, exclusive=False)


# --- toegang voor de kandidaat ------------------------------------------
@app.get("/api/access")
def access():
    """Hoe de kandidaat (die net als de controller buiten de OT-omgeving zit)
    het lab in komt: FortiGate-GUI, jump host via de VIP en het kijkvenster."""
    topo = load_topology()
    nodes = {n["name"]: n for n in inventory(topo)}
    user, pw = contractor_credentials()
    try:
        wan_ip, fgt_url = fortigate.mgmt_ip(topo), fortigate.base_url(topo)
    except RuntimeError:
        wan_ip, fgt_url = "", ""
    jump, ews = nodes.get("dmz-jump"), nodes.get("ews")
    secret = contractor_totp_secret()
    return {
        "totp_secret": secret, "totp_code": totp(secret),
        "totp_uri": f"otpauth://totp/{quote(f'OT Lab:{user}')}?secret={secret}&issuer={quote('OT Lab')}",
        "fgt_url": fgt_url, "fgt_user": "admin", "fgt_password": get_state().get("fgt_admin_password") or "",
        "wan_ip": wan_ip, "jump_port": 2222, "user": user, "password": pw,
        "jump_ip": jump["ip"] if jump else "", "ews_ip": ews["ip"] if ews else "",
        "viewers": [{"name": n["name"], "hostname": n["hostname"], "ip": n["ip"], "role": n["role"],
                     "segment": n["segment"], "title": n["args"].get("title", "")}
                    for n in nodes.values() if deployer.view_port(n)],
    }


VIEW_PATH = re.compile(r"/[\w./?=&%-]*")
META_REFRESH = re.compile(r"<meta[^>]+http-equiv=[\"']?refresh[^>]*>", re.I)


@app.get("/api/view/{name}")
def view(name: str, path: str = "/"):
    """Webinterface van een device, opgehaald in de container zelf."""
    if not VIEW_PATH.fullmatch(path):
        raise HTTPException(400, "ongeldig pad")
    node = next((n for n in inventory(load_topology()) if n["name"] == name), None)
    if not node or not deployer.view_port(node):
        raise HTTPException(404, "geen webinterface")
    try:
        html = deployer.fetch_view(node, path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    # de UI ververst zelf; een meta-refresh zou het venster laten verspringen
    return HTMLResponse(META_REFRESH.sub("", html), headers=NO_CACHE)


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


@app.get("/api/recordings")
def recordings():
    """Opgenomen SSH-sessies van de contractor op de jump host, als pagina voor het kijkvenster."""
    jump = next((n for n in inventory(load_topology()) if n["role"] == "jumphost"), None)
    if not jump:
        raise HTTPException(404, "geen jump host")
    sessions = deployer.recordings(jump)
    body = "".join(f"<h3>{html.escape(s['name'])}</h3><pre>{html.escape(ANSI.sub('', s['text']))}</pre>"
                   for s in sessions) or "<p>Nog geen opgenomen sessies.</p>"
    page = ("<!doctype html><meta charset=utf-8><style>body{font-family:sans-serif;background:#1b222a;color:#e6e6e6;"
            "margin:20px}h3{color:#f0a030;font-size:14px}pre{background:#0f1418;padding:10px;white-space:pre-wrap}</style>"
            f"<h1 style='color:#f0a030'>Sessie-opnames jump host</h1>{body}")
    return HTMLResponse(page, headers=NO_CACHE)


class Adhoc(BaseModel):
    node: str
    command: str


ADHOC_ALLOWED = re.compile(r"^(scan|tcp|modbus-read|modbus-write|s7-read|s7-stop|s7-start|opcua-browse|http|shellshock)\b[\w .,:/=-]*$")


@app.post("/api/adhoc")
def adhoc(a: Adhoc):
    if not ADHOC_ALLOWED.match(a.command.strip()):
        raise HTTPException(400, "alleen otprobe-subcommando's met eenvoudige argumenten")
    return _start(f"{a.node}: {a.command}", scenarios.run_adhoc, a.node, a.command.strip(), exclusive=False)
