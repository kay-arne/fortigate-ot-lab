"""Testcenter: validatietests draaien op probes/devices en FortiGate-logs koppelen."""
from __future__ import annotations

import json
import time

import yaml

from . import fortigate
from .config import SCENARIO_FILE, get_state, inventory, load_topology, update_state
from .hostctl import host

PROBE_CMD = "/opt/otsim/venv/bin/python -m otsim.probe"


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
    topo = load_topology()
    log(f"[{sc['id']}] {sc['title']}")
    log(f"  vanaf {sc['from']}: otprobe {sc['run']}")
    started = time.time()
    res = run_command(sc["from"], sc["run"], timeout=sc.get("timeout", 120))
    outcome = "allowed" if res.get("ok") else "blocked"
    passed = outcome == sc["expect"]
    log(f"  resultaat: {outcome} (verwacht {sc['expect']}) -> {'PASS' if passed else 'FAIL'}")
    log(f"  detail: {res.get('detail')}")

    time.sleep(4)  # FortiGate schrijft logs met kleine vertraging
    evidence = []
    if get_state().get("fgt_api_key"):
        for kind in sc.get("logs", []):
            for r in fortigate.query_logs(topo, kind, res["srcip"], started)[:10]:
                evidence.append({
                    "type": kind,
                    "action": r.get("action"),
                    "policy": r.get("policyname") or r.get("policyid"),
                    "dst": f"{r.get('dstip')}:{r.get('dstport')}",
                    "app": r.get("app") or r.get("service"),
                    "attack": r.get("attack"),
                    "msg": r.get("msg"),
                })
        for e in evidence:
            log(f"  FGT {e['type']}: {e['action']} policy={e['policy']} {e['dst']} "
                f"app={e['app'] or '-'}{' attack=' + e['attack'] if e['attack'] else ''}")
        if not evidence:
            log("  (geen FortiGate-logregels gevonden voor dit bron-IP)")

    all_results = results()
    all_results[sc["id"]] = {"passed": passed, "outcome": outcome, "detail": res.get("detail"),
                             "evidence": evidence, "at": started}
    update_state(results=all_results)


def run_all(log):
    for sc in load():
        try:
            run_scenario(log, sc["id"])
        except Exception as e:  # noqa: BLE001 - door met de volgende test
            log(f"  ✖ {e}")


def run_adhoc(log, node_name: str, command: str):
    log(f"{node_name}$ otprobe {command}")
    res = run_command(node_name, command)
    log(json.dumps(res, indent=2))

