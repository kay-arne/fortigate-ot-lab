"""Web UI en API van de OT Lab controller."""
import re
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import deployer, fortigate, jobs, scenarios
from .config import DATA, UPLOADS, get_state, inventory, load_topology, update_state

HERE = Path(__file__).parent
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    pw_file = DATA / "ui_password"
    expected = pw_file.read_text().strip() if pw_file.exists() else "admin"
    ok = secrets.compare_digest(creds.username, "admin") and secrets.compare_digest(creds.password, expected)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="FortiGate OT Lab", dependencies=[Depends(auth)])
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "templates" / "index.html").read_text()


# --- status & instellingen -----------------------------------------------
def public_state() -> dict:
    s = get_state()
    return {
        "wan": s.get("wan", {}),
        "fgt_image": s.get("fgt_image"),
        "license_mode": s.get("license_mode", "none"),
        "license_file": s.get("license_file"),
        "fortiflex_token_set": bool(s.get("fortiflex_token")),
        "admin_password_set": bool(s.get("fgt_admin_password")),
        "fgt_mgmt_ip": s.get("fgt_mgmt_ip"),
        "deployed": s.get("deployed", False),
        "provisioned": s.get("fgt_provisioned", False),
    }


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
        changes["wan"] = {"mode": s.wan_mode, "ip": s.wan_ip or "", "gateway": s.wan_gateway or ""}
    if s.fgt_mgmt_ip is not None:
        changes["fgt_mgmt_ip"] = s.fgt_mgmt_ip
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
