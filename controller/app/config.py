"""Paden, topologie en persistente state van de controller."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import string
import struct
import threading
import time
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("OTLAB_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
TOPOLOGY_FILE = ROOT / "lab" / "topology.yaml"
SCENARIO_FILE = ROOT / "lab" / "scenarios.yaml"
STATE_FILE = DATA / "state.json"
HOST_FILE = DATA / "host.json"

DATA.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def load_topology() -> dict:
    with open(TOPOLOGY_FILE) as f:
        topo = yaml.safe_load(f)
    # Keuzes uit de installer (host.json) overschrijven de defaults uit de YAML.
    hcfg = load_host() if HOST_FILE.exists() else {}
    lab = topo["lab"]
    lab["uplink_bridge"] = hcfg.get("uplink_bridge") or lab["uplink_bridge"]
    lab["uplink_vlan"] = str(hcfg.get("uplink_vlan") or lab.get("uplink_vlan") or "")
    lab["template_net"] = hcfg.get("template_net") or lab.get("template_net", "ip=dhcp")
    lab["name_prefix"] = hcfg.get("name_prefix") or lab.get("name_prefix") or "otlab"
    if hcfg.get("vmid_base"):
        lab["vmid_base"] = int(hcfg["vmid_base"])
        topo["fortigate"]["vmid"] = lab["vmid_base"]
    # WAN-instellingen uit de UI overschrijven de defaults uit de YAML.
    topo["fortigate"]["wan"].update(get_state().get("wan", {}))
    return topo


def vm_name(topo: dict, name: str) -> str:
    """Naam van een VM/container in Proxmox: <prefix>-<naam>, bijv. otlab-plc-tank."""
    return f"{topo['lab']['name_prefix']}-{name}"


def uplink_tag(topo: dict) -> str:
    """',tag=<vlan>' voor net-opties op de uplink, of leeg."""
    vlan = topo["lab"].get("uplink_vlan")
    return f",tag={vlan}" if vlan else ""


def load_host() -> dict:
    with open(HOST_FILE) as f:
        return json.load(f)


def get_state() -> dict:
    with _lock:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text())


def update_state(**changes) -> dict:
    with _lock:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        state.update(changes)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)
        return state


# Account van de contractor (Remco in de lab guide) op de jump host en de EWS.
CONTRACTOR_USER = "remco"


def contractor_credentials() -> tuple[str, str]:
    """Gebruiker en wachtwoord voor SSH via de FortiGate-VIP; het wachtwoord
    wordt één keer gegenereerd en staat (leesbaar, het is een lab) in de UI."""
    pw = get_state().get("contractor_password")
    if not pw:
        pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
        update_state(contractor_password=pw)
    return CONTRACTOR_USER, pw


def contractor_totp_secret() -> str:
    """TOTP-geheim (base32) voor de tweede factor van de contractor op de jump host."""
    secret = get_state().get("contractor_totp")
    if not secret:
        secret = base64.b32encode(secrets.token_bytes(10)).decode()
        update_state(contractor_totp=secret)
    return secret


def totp(secret: str, at: float | None = None) -> str:
    """Huidige 6-cijferige code volgens RFC 6238 (30 s, SHA-1), zoals een authenticator-app."""
    counter = int((at if at is not None else time.time()) // 30)
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def segment_map(topo: dict) -> dict:
    return {s["id"]: s for s in topo["segments"]}


def prefixlen(subnet: str) -> int:
    return ipaddress.ip_network(subnet).prefixlen


def netmask(subnet: str) -> str:
    return str(ipaddress.ip_network(subnet).netmask)


def probe_ip(seg: dict, suffix: int) -> str:
    net = ipaddress.ip_network(seg["subnet"])
    return str(net.network_address + suffix)


def inventory(topo: dict) -> list[dict]:
    """Alle LXC's (devices + probes) met vaste VMID's."""
    base = topo["lab"]["vmid_base"]
    segs = segment_map(topo)
    items = []
    vmid = base + 10
    for d in topo["devices"]:
        items.append({**d, "kind": "device", "vmid": vmid, "args": d.get("args", {}),
                      "hostname": vm_name(topo, d["name"])})
        vmid += 1
    vmid = base + 80
    for sid in topo["probes"]["segments"]:
        seg = segs[sid]
        items.append({
            "name": f"probe-{sid}",
            "segment": sid,
            "ip": probe_ip(seg, topo["probes"]["ip_suffix"]),
            "role": "probe",
            "kind": "probe",
            "vmid": vmid,
            "args": {},
            "hostname": vm_name(topo, f"probe-{sid}"),
        })
        vmid += 1
    return items


def template_vmid(topo: dict) -> int:
    return topo["lab"]["vmid_base"] + 1
