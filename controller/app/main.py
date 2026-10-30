"""Web UI en API van de OT Lab controller."""
import ipaddress
import json
import re
from pathlib import Path
from typing import Optional

import asyncio

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, deployer, fortigate, jobs, scenarios
from .config import (DATA, HOST_FILE, UPLOADS, get_state, inventory, load_host, load_topology,
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


def _login_page(error: str = "") -> HTMLResponse:
    html = (HERE / "templates" / "login.html").read_text()
    block = f'<div class="login-error">{error}</div>' if error else ""
    return HTMLResponse(html.replace("<!--ERROR-->", block), status_code=401 if error else 200)


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
    return (HERE / "templates" / "index.html").read_text()


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
        {"id": "wan", "label": "WAN-adres port1", "ok": wan["mode"] == "dhcp" or bool(wan.get("ip"))},
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
        "deployed": s.get("deployed", False),
        "provisioned": s.get("fgt_provisioned", False),
        "checks": checks,
    }


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
                             "wan": topo["fortigate"]["wan"], "api": False},
               "segments": [{**s, "present": False} for s in topo["segments"]],
               "nodes": [{**n, "status": "unknown"} for n in inventory(topo)]}
        err = str(e)
    return {"settings": public_state(), "lab": lab, "host_error": err, "jobs": jobs.recent()}


class Settings(BaseModel):
    wan_mode: Optional[str] = None
    wan_ip: Optional[str] = None
    wan_gateway: Optional[str] = None
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
        changes["wan"] = {"mode": s.wan_mode, "ip": ip if s.wan_mode == "static" else "",
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


class Destroy(BaseModel):
    template: bool = False
    network: bool = False


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


class Adhoc(BaseModel):
    node: str
    command: str


ADHOC_ALLOWED = re.compile(r"^(scan|tcp|modbus-read|modbus-write|s7-read|s7-stop|s7-start|opcua-browse|http)\b[\w .,:/=-]*$")


@app.post("/api/adhoc")
def adhoc(a: Adhoc):
    if not ADHOC_ALLOWED.match(a.command.strip()):
        raise HTTPException(400, "alleen otprobe-subcommando's met eenvoudige argumenten")
    return _start(f"{a.node}: {a.command}", scenarios.run_adhoc, a.node, a.command.strip(), exclusive=False)
