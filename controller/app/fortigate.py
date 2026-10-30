"""FortiGate: bootstrap-config (cloud-init), licentie en OT-policies via REST.

Doel-versie: FortiOS 7.6.x. Basisconfig gaat via de config drive bij de eerste
boot; alles wat FortiGuard/OT-signatures nodig heeft (application control,
IPS) wordt daarna via de REST API uitgerold, zodat signature-ID's dynamisch
opgezocht kunnen worden in plaats van hard-coded.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time

import paramiko
import requests
import urllib3

from .config import get_state, inventory, netmask, segment_map, update_state, vm_name

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_USER = "otlab-controller"


# --------------------------------------------------------------------------
# bootstrap (cloud-init user_data)
# --------------------------------------------------------------------------
def bootstrap_config(topo, state) -> str:
    fgt = topo["fortigate"]
    wan = fgt["wan"]
    lines = [
        "config system global",
        f'    set hostname "{vm_name(topo, fgt["name"])}"',
        "    set admintimeout 60",
        f"    set admin-sport {fgt.get('admin_https_port', 443)}",
        "end",
        "config system admin",
        "    edit admin",
        f'        set password "{state["fgt_admin_password"]}"',
        "    next",
        "end",
        "config system interface",
        "    edit port1",
        '        set alias "WAN-Uplink"',
        "        set role wan",
        "        set allowaccess ping https ssh",
    ]
    if wan["mode"] == "dhcp":
        lines.append("        set mode dhcp")
    else:
        iface = ipaddress.ip_interface(wan["ip"])
        lines += ["        set mode static", f"        set ip {iface.ip} {iface.netmask}"]
    lines.append("    next")
    for seg in topo["segments"]:
        lines += [
            f"    edit {seg['port']}",
            f'        set alias "{seg["name"][:25]}"',
            "        set role lan",
            "        set mode static",
            f"        set ip {seg['gateway']} {netmask(seg['subnet'])}",
            "        set allowaccess ping",
            "        set device-identification enable",
            "    next",
        ]
    lines.append("end")
    if wan["mode"] == "static" and wan.get("gateway"):
        lines += ["config router static", "    edit 1",
                  f"        set gateway {wan['gateway']}", "        set device port1",
                  "    next", "end"]
    # DNS-proxy op elke segment-gateway (devices gebruiken de FGT als resolver)
    lines.append("config system dns-server")
    for seg in topo["segments"]:
        lines += [f'    edit "{seg["port"]}"', "        set mode forward-only", "    next"]
    lines.append("end")
    # OT/ICS-signatures meenemen in IPS (standaard uitgesloten)
    lines += ["config ips global", "    set exclude-signatures none", "end"]
    # Logging in memory zodat de controller resultaten kan uitlezen zonder logdisk-format
    lines += ["config log memory setting", "    set status enable", "end",
              "config log setting", "    set fwpolicy-implicit-log enable",
              "    set local-in-deny-unicast enable", "end"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# verbinding
# --------------------------------------------------------------------------
def mgmt_ip(topo) -> str:
    wan = topo["fortigate"]["wan"]
    if wan["mode"] == "static" and wan.get("ip"):
        return str(ipaddress.ip_interface(wan["ip"]).ip)
    ip = get_state().get("fgt_mgmt_ip")
    if not ip:
        raise RuntimeError("WAN staat op DHCP: vul het management-IP van de FortiGate in de UI in.")
    return ip


def base_url(topo) -> str:
    port = topo["fortigate"].get("admin_https_port", 443)
    return f"https://{mgmt_ip(topo)}:{port}"


def wait_reachable(log, topo, timeout=1200):
    url = base_url(topo)
    log(f"wachten tot {url} bereikbaar is...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url, verify=False, timeout=5)
            log("FortiGate GUI bereikbaar")
            return
        except requests.RequestException:
            time.sleep(10)
    raise RuntimeError("FortiGate werd niet bereikbaar binnen de timeout.")


def ssh_cli(topo, commands: list[str], wait=2.0) -> str:
    """Voer CLI-regels uit over SSH (interactieve shell)."""
    state = get_state()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(mgmt_ip(topo), username="admin", password=state["fgt_admin_password"],
                   look_for_keys=False, allow_agent=False, timeout=15)
    shell = client.invoke_shell(width=250)
    time.sleep(1)
    out = ""
    for cmd in commands:
        shell.send(cmd + "\n")
        time.sleep(wait)
        while shell.recv_ready():
            out += shell.recv(65535).decode(errors="replace")
    client.close()
    return out


def _controller_ip_towards(ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 443))
        return s.getsockname()[0]
    finally:
        s.close()


class Api:
    def __init__(self, topo):
        self.base = base_url(topo)
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers["Authorization"] = f"Bearer {get_state()['fgt_api_key']}"

    def get(self, path, **params):
        r = self.s.get(f"{self.base}/api/v2/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def upsert(self, path, name, body):
        """POST, of PUT als het object al bestaat."""
        r = self.s.post(f"{self.base}/api/v2/{path}", json=body, timeout=30)
        if r.status_code in (424, 500) and ('"error":-5' in r.text.replace(" ", "") or r.status_code == 424):
            r = self.s.put(f"{self.base}/api/v2/{path}/{requests.utils.quote(str(name), safe='')}",
                           json=body, timeout=30)
        if not r.ok:
            raise RuntimeError(f"{path}/{name}: {r.status_code} {r.text[:300]}")
        return r.json()


# --------------------------------------------------------------------------
# provisioning na de eerste boot
# --------------------------------------------------------------------------
def ensure_license(log, topo):
    state = get_state()
    status = ssh_cli(topo, ["get system status"])
    m = re.search(r"License Status:\s*(\S+)", status)
    lic = m.group(1) if m else "unknown"
    log(f"licentiestatus: {lic}")
    if lic.lower() == "valid":
        return
    if state.get("license_mode") == "fortiflex" and state.get("fortiflex_token"):
        log("FortiFlex-token alsnog activeren via CLI (FortiGate herstart)")
        ssh_cli(topo, [f"execute vm-license {state['fortiflex_token']}", "y"], wait=5)
        time.sleep(60)
        wait_reachable(log, topo)
    else:
        log("⚠ licentie nog niet geldig; controleer FortiGuard-bereikbaarheid. "
            "Zonder geldige licentie (en OT-licentie) ontbreken industriële signatures.")


def ensure_api_key(log, topo):
    if get_state().get("fgt_api_key"):
        return
    ctl_ip = _controller_ip_towards(mgmt_ip(topo))
    log(f"REST API-user '{API_USER}' aanmaken (trusthost {ctl_ip}/32)")
    out = ssh_cli(topo, [
        "config system api-user",
        f'edit "{API_USER}"',
        "set accprofile super_admin",
        "set vdom root",
        "config trusthost",
        "edit 1",
        f"set ipv4-trusthost {ctl_ip} 255.255.255.255",
        "end",
        "next",
        "end",
        f"execute api-user generate-key {API_USER}",
        "y",
    ], wait=3)
    m = re.search(r"New API key:\s*(\S+)", out)
    if not m:
        raise RuntimeError(f"Kon API-key niet genereren. CLI-output:\n{out[-800:]}")
    update_state(fgt_api_key=m.group(1))
    log("API-key opgeslagen")


def _resolve_ot_apps(api, log):
    """Zoek OT application-signatures op in de FortiGuard-database van de FortiGate."""
    res = api.get("cmdb/application/name", format="name|id|category")
    apps = res.get("results", [])
    ot = [a for a in apps if re.search(r"modbus|s7|siemens|dnp3|iec\.?104|opc|bacnet|enip|cip", a["name"], re.I)]
    write_like = re.compile(r"write|stop|download|delete|upload|program|reset|restart|force|run|firmware", re.I)
    block = [a for a in ot if write_like.search(a["name"]) and re.search(r"modbus|s7|siemens", a["name"], re.I)]
    cats = sorted({a["category"] for a in ot if a.get("category")})
    log(f"{len(ot)} OT-signatures gevonden; {len(block)} schrijf/stop-acties in 'read-only'-profiel")
    if not ot:
        log("⚠ geen OT-signatures: is de OT-licentie actief en heeft FortiGuard een update gedaan?")
    return block, cats


def provision(log, topo):
    wait_reachable(log, topo)
    time.sleep(20)  # services na GUI-start nog even laten settelen
    ensure_license(log, topo)
    ensure_api_key(log, topo)
    api = Api(topo)
    segs = segment_map(topo)
    nodes = {n["name"]: n for n in inventory(topo)}

    log("adresobjecten")
    for seg in topo["segments"]:
        api.upsert("cmdb/firewall/address", f"NET-{seg['id']}",
                   {"name": f"NET-{seg['id']}", "subnet": f"{ipaddress.ip_network(seg['subnet']).network_address} {netmask(seg['subnet'])}",
                    "comment": seg["name"]})
    for n in nodes.values():
        api.upsert("cmdb/firewall/address", n["name"],
                   {"name": n["name"], "subnet": f"{n['ip']} 255.255.255.255",
                    "comment": f"{n['kind']} {n['role']}"})

    def grp(name, members):
        api.upsert("cmdb/firewall/addrgrp", name,
                   {"name": name, "member": [{"name": m} for m in members]})
    plcs = [n["name"] for n in nodes.values() if n["role"] in ("modbus_plc", "s7_plc")]
    modbus_plcs = [n["name"] for n in nodes.values() if n["role"] == "modbus_plc"]
    io = [n["name"] for n in nodes.values() if n["role"] == "io_device"]
    grp("GRP-PLC", plcs)
    grp("GRP-PLC-Modbus", modbus_plcs)
    grp("GRP-IO", io)

    log("OT-services")
    for name, port in (("OT-Modbus", "502"), ("OT-S7comm", "102"), ("OT-OPCUA", "4840"),
                       ("OT-DNP3", "20000"), ("SSH-2222", "2222")):
        api.upsert("cmdb/firewall.service/custom", name,
                   {"name": name, "tcp-portrange": port})

    log("application control profielen")
    block, cats = _resolve_ot_apps(api, log)
    monitor_entries = [{"id": 1, "category": [{"id": c} for c in cats], "action": "pass", "log": "enable"}] if cats else []
    api.upsert("cmdb/application/list", "OT-monitor", {
        "name": "OT-monitor", "comment": "Alle OT-protocollen zichtbaar maken (log)",
        "deep-app-inspection": "enable", "other-application-log": "enable",
        "unknown-application-log": "enable", "entries": monitor_entries})
    ro_entries = []
    if block:
        ro_entries.append({"id": 1, "application": [{"id": a["id"]} for a in block], "action": "block", "log": "enable"})
    if cats:
        ro_entries.append({"id": 2, "category": [{"id": c} for c in cats], "action": "pass", "log": "enable"})
    api.upsert("cmdb/application/list", "OT-readonly", {
        "name": "OT-readonly", "comment": "OT lezen toegestaan, schrijven/stoppen/programmeren blokkeren",
        "deep-app-inspection": "enable", "other-application-log": "enable",
        "unknown-application-log": "enable", "entries": ro_entries})

    log("IPS-sensor (incl. OT-signatures)")
    api.upsert("cmdb/ips/sensor", "OT-IPS", {
        "name": "OT-IPS", "comment": "IT + OT signatures, block medium+",
        "entries": [{"id": 1, "severity": "medium high critical", "status": "enable",
                     "action": "block", "log": "enable", "log-packet": "enable"},
                    {"id": 2, "severity": "info low", "status": "enable", "action": "pass", "log": "enable"}]})

    log("VIP: remote access naar de DMZ jump host (WAN :2222 -> 22)")
    wan_ip = mgmt_ip(topo)
    api.upsert("cmdb/firewall/vip", "VIP-dmz-jump-ssh", {
        "name": "VIP-dmz-jump-ssh", "extintf": "port1", "extip": wan_ip,
        "mappedip": [{"range": nodes["dmz-jump"]["ip"]}], "portforward": "enable",
        "protocol": "tcp", "extport": "2222", "mappedport": "22"})

    p = {s: segs[s]["port"] for s in segs}
    policies = [
        # naam, srcintf, dstintf, src, dst, service, app-profiel, nat
        ("WAN->DMZ remote access jump", "port1", p["dmz"], "all", "VIP-dmz-jump-ssh", "SSH", None, False),
        ("DMZ jump->L2 EWS (RDP/SSH)", p["dmz"], p["sup"], "dmz-jump", "ews", "SSH", None, False),
        ("DMZ jump->L3 MES", p["dmz"], p["ops"], "dmz-jump", "mes", "HTTP SSH", None, False),
        ("DMZ patch->Internet", p["dmz"], "port1", "dmz-patch", "all", "HTTP HTTPS DNS", None, True),
        ("L3->DMZ historian mirror", p["ops"], p["dmz"], "historian", "dmz-histmirror", "HTTP", None, False),
        ("L3->L2 historian OPC UA", p["ops"], p["sup"], "historian", "opcua-gw", "OT-OPCUA", "OT-monitor", False),
        ("L3->L1 Modbus read-only", p["ops"], p["ctl"], f"NET-ops", "GRP-PLC-Modbus", "OT-Modbus", "OT-readonly", False),
        ("L2 HMI->L1 control", p["sup"], p["ctl"], "scada-hmi", "GRP-PLC", "OT-Modbus OT-S7comm", "OT-monitor", False),
        ("L2 OPC UA gw->L1 read-only", p["sup"], p["ctl"], "opcua-gw", "GRP-PLC-Modbus", "OT-Modbus", "OT-readonly", False),
        ("L2 EWS->L1 engineering", p["sup"], p["ctl"], "ews", "GRP-PLC", "ALL", "OT-monitor", False),
        ("L2 EWS->L0 field I/O", p["sup"], p["proc"], "ews", "GRP-IO", "OT-Modbus", "OT-monitor", False),
        ("L1 PLC->L0 field I/O", p["ctl"], p["proc"], "GRP-PLC-Modbus", "GRP-IO", "OT-Modbus", "OT-monitor", False),
        ("TestCenter->DMZ jump", p["test"], p["dmz"], "NET-test", "dmz-jump", "SSH", None, False),
    ]
    log(f"{len(policies)} firewall-policies (Purdue conduits); de rest valt op implicit deny (gelogd)")
    for pid, (name, si, di, src, dst, svc, app, nat) in enumerate(policies, start=1):
        body = {
            "policyid": pid, "name": name[:35], "action": "accept", "schedule": "always",
            "srcintf": [{"name": si}], "dstintf": [{"name": di}],
            "srcaddr": [{"name": src}], "dstaddr": [{"name": dst}],
            "service": [{"name": s} for s in svc.split()],
            "logtraffic": "all", "nat": "enable" if nat else "disable",
            "utm-status": "enable", "ips-sensor": "OT-IPS",
            "ssl-ssh-profile": "certificate-inspection",
        }
        if app:
            body["application-list"] = app
        api.upsert("cmdb/firewall/policy", pid, body)
    update_state(fgt_provisioned=True)
    log("FortiGate OT-provisioning klaar")


# --------------------------------------------------------------------------
# logs voor de testcenter-validatie
# --------------------------------------------------------------------------
def query_logs(topo, kind: str, srcip: str, since: float, rows=50) -> list[dict]:
    """kind: traffic/forward | app-ctrl | ips"""
    api = Api(topo)
    path = f"log/memory/{kind}"
    try:
        res = api.get(path, filter=f"srcip=={srcip}", rows=rows)
    except requests.HTTPError:
        return []
    out = []
    for r in res.get("results", []):
        ts = r.get("eventtime") or 0
        ts = ts / 1e9 if ts > 1e15 else ts / 1e6 if ts > 1e12 else ts
        if ts and ts < since - 5:
            continue
        out.append(r)
    return out
