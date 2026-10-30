"""Paden, topologie en persistente state van de controller."""
from __future__ import annotations

import ipaddress
import json
import os
import threading
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
    # WAN-instellingen uit de UI overschrijven de defaults uit de YAML.
    topo["fortigate"]["wan"].update(get_state().get("wan", {}))
    return topo


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
        items.append({**d, "kind": "device", "vmid": vmid, "args": d.get("args", {})})
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
        })
        vmid += 1
    return items


def template_vmid(topo: dict) -> int:
    return topo["lab"]["vmid_base"] + 1
