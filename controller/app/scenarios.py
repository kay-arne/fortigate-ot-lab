"""Testcenter: validatietests draaien op probes/devices en FortiGate-logs koppelen."""
from __future__ import annotations

import ipaddress
import json
import threading
import time
from collections import defaultdict
from urllib.parse import urlparse

import yaml

from . import fortigate
from .config import SCENARIO_FILE, get_state, inventory, load_topology, update_state
from .hostctl import host

PROBE_CMD = "/opt/otsim/venv/bin/python -m otsim.probe"

# Logregels worden op bron-IP en tijd aan een test gekoppeld; twee tests tegelijk
# vanaf dezelfde node zouden elkaars regels als bewijs krijgen.
_node_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def _target(command: str):
    """Doel van een otprobe-commando als netwerk (IP of subnet), of None."""
    parts = command.split()
    if len(parts) < 2:
        return None
    arg = urlparse(parts[1]).hostname if "://" in parts[1] else parts[1]
    try:
        return ipaddress.ip_network(arg, strict=False)
    except ValueError:
        return None


def load() -> list[dict]:
    with open(SCENARIO_FILE) as f:
        return yaml.safe_load(f)["scenarios"]


def results() -> dict:
    return get_state().get("results", {})


def run_command(node_name: str, command: str, timeout=120) -> dict:
    topo = load_topology()
    nodes = {n["name"]: n for n in inventory(topo)}
    if node_name not in nodes:
        raise ValueError(f"onbekende node {node_name}")
    node = nodes[node_name]
    rc, out = host().pct_exec(node["vmid"], f"cd /opt/otsim && {PROBE_CMD} {command}", timeout=timeout)
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {"ok": False, "detail": out.strip()[-1000:] or f"rc={rc}"}
    res["node"] = node_name
    res["srcip"] = node["ip"]
    return res


def run_scenario(log, scenario_id: str):
    sc = next(s for s in load() if s["id"] == scenario_id)
    lock = _node_locks[sc["from"]]
    if lock.locked():
        log(f"  wacht tot de vorige test op {sc['from']} klaar is")
    with lock:
        _run_scenario(log, sc)


def _run_scenario(log, sc: dict):
    topo = load_topology()
    log(f"[{sc['id']}] {sc['title']}")
    log(f"  vanaf {sc['from']}: otprobe {sc['run']}")
    started = time.time()
    res = run_command(sc["from"], sc["run"], timeout=sc.get("timeout", 120))
    outcome = "allowed" if res.get("ok") else "blocked"
    passed = outcome == sc["expect"]
    log(f"  resultaat: {outcome} (verwacht {sc['expect']}) -> {'PASS' if passed else 'FAIL'}")
    log(f"  detail: {res.get('detail')}")

    evidence = []
    if get_state().get("fgt_api_key"):
        # Een toegestane sessie wordt pas gelogd als hij sluit, soms seconden later.
        deadline = time.time() + 15
        time.sleep(4)
        while True:
            evidence = _collect_evidence(topo, sc, res["srcip"], started)
            if evidence or time.time() > deadline:
                break
            time.sleep(2)
        for e in evidence:
            log(f"  FGT {e['type']}: {e['action']} policy={e['policy']} {e['dst']} "
                f"app={e['app'] or '-'}{' attack=' + e['attack'] if e['attack'] else ''}")
        if not evidence:
            log("  (geen FortiGate-logregels gevonden voor dit bron-IP)")

    all_results = results()
    all_results[sc["id"]] = {"passed": passed, "outcome": outcome, "detail": res.get("detail"),
                             "evidence": evidence, "at": started}
    update_state(results=all_results)


def _collect_evidence(topo, sc: dict, srcip: str, started: float) -> list[dict]:
    evidence = []
    target = _target(sc["run"])
    single = str(target.network_address) if target and target.num_addresses == 1 else None
    for kind in sc.get("logs", []):
        rows = fortigate.query_logs(topo, kind, srcip, started, rows=200 if target and not single else 50,
                                    dstip=single)
        if target and not single:
            rows = [r for r in rows if r.get("dstip") and ipaddress.ip_address(r["dstip"]) in target]
        for r in rows[:10]:
            evidence.append({
                "type": kind,
                "action": r.get("action"),
                "policy": r.get("policyname") or r.get("policyid"),
                "dst": f"{r.get('dstip')}:{r.get('dstport')}",
                "app": r.get("app") or r.get("service"),
                "attack": r.get("attack"),
                "msg": r.get("msg"),
            })
    return evidence


def run_all(log):
    for sc in load():
        try:
            run_scenario(log, sc["id"])
        except Exception as e:  # noqa: BLE001 - door met de volgende test
            log(f"  ✖ {e}")


def run_adhoc(log, node_name: str, command: str):
    log(f"{node_name}$ otprobe {command}")
    with _node_locks[node_name]:
        res = run_command(node_name, command)
    log(json.dumps(res, indent=2))

