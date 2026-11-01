"""Testcenter: validatietests draaien op probes/devices en FortiGate-logs koppelen."""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from collections import defaultdict
from urllib.parse import urlparse

import paramiko
import yaml

from . import deployer, fortigate
from .config import (SCENARIO_FILE, contractor_credentials, contractor_totp_secret, get_state, inventory,
                     load_topology, segment_map, totp, update_state)
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
    started = time.time()
    if sc.get("check"):
        log(f"  controller: {sc['run']}")
        res = CHECKS[sc["check"]](log, topo)
    else:
        log(f"  vanaf {sc['from']}: otprobe {sc['run']}")
        res = run_command(sc["from"], sc["run"], timeout=sc.get("timeout", 120))
    outcome = "allowed" if res.get("ok") else "blocked"
    passed = outcome == sc["expect"]
    log(f"  resultaat: {outcome} (verwacht {sc['expect']}) -> {'PASS' if passed else 'FAIL'}")
    log(f"  detail: {res.get('detail')}")

    evidence = []
    if get_state().get("fgt_api_key") and res.get("srcip") and sc.get("logs"):
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


# --------------------------------------------------------------------------
# checks die de controller zelf uitvoert (scenario's met 'check:')
# --------------------------------------------------------------------------
def check_assets(log, topo) -> dict:
    """Herkent de FortiGate elk device, en in de juiste Purdue-laag?"""
    devices = fortigate.asset_inventory(topo)
    by_mac = {(d.get("mac") or "").lower(): d for d in devices}
    by_ip = {d["ipv4_address"]: d for d in devices if d.get("ipv4_address") and d.get("is_online")}
    segs = segment_map(topo)
    nodes = [n for n in inventory(topo) if n["kind"] == "device"]
    missing, wrong = [], []
    for n in nodes:
        d = by_mac.get(deployer.node_hwaddr(n).lower()) or by_ip.get(n["ip"])
        want = fortigate.purdue_level(segs[n["segment"]])
        if not d:
            missing.append(n["name"])
        elif str(d.get("purdue_level")) != want:
            wrong.append(f"{n['name']} L{d.get('purdue_level')} i.p.v. L{want}")
    detail = (f"{len(nodes) - len(missing)}/{len(nodes)} devices herkend"
              + (f"; niet gezien: {', '.join(missing)}" if missing else "")
              + (f"; verkeerde laag: {', '.join(wrong)}" if wrong else ""))
    return {"ok": not missing and not wrong, "detail": detail, "srcip": None}


def check_contractor_login(log, topo, mfa=True) -> dict:
    """Inloggen als de contractor via de VIP op de WAN-poort, zoals de kandidaat
    doet: wachtwoord én TOTP-code, daarna een commando in de opgenomen shell.
    Met mfa=False klopt het wachtwoord wel, maar de code niet."""
    user, pw = contractor_credentials()
    ip = fortigate.mgmt_ip(topo)
    code = totp(contractor_totp_secret()) if mfa else "000000"

    def answer(title, instructions, prompts):
        return [code if "code" in p.lower() else pw for p, _ in prompts]

    try:
        sock = socket.create_connection((ip, 2222), timeout=10)
    except OSError as e:
        return {"ok": False, "detail": f"{ip}:2222 - {e}", "srcip": None}
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=10)
        transport.auth_interactive(user, answer)
        chan = transport.open_session(timeout=10)
        chan.exec_command("hostname")
        out = chan.makefile().read().decode(errors="replace").strip()
        return {"ok": True, "detail": f"ingelogd als {user} (wachtwoord + TOTP) op {out}",
                "srcip": sock.getsockname()[0]}
    except (paramiko.SSHException, OSError) as e:
        return {"ok": False, "detail": f"{ip}:2222 - {e}", "srcip": None}
    finally:
        transport.close()


BREAK_POLICY = "L3->L1 Modbus read-only"
BREAK_SECONDS = 60


def check_break_readonly(log, topo) -> dict:
    """'Breek het': de write-blokkade even uitzetten en Ghost het setpoint laten
    verzetten. Policy en setpoint worden daarna altijd hersteld."""
    before = fortigate.policy_app_list(topo, BREAK_POLICY, "OT-monitor")
    log(f"  policy '{BREAK_POLICY}': {before} -> OT-monitor, schrijven wordt niet meer geblokkeerd")
    try:
        # De IPS-engine laadt een gewijzigde policy met enkele seconden vertraging.
        for attempt in range(5):
            time.sleep(4)
            res = run_command("probe-ops", "modbus-write 10.10.0.10 1 999")
            if res.get("ok"):
                break
            log(f"  write nog geblokkeerd (poging {attempt + 1}), FortiGate laadt de policy nog")
        if res.get("ok"):
            log(f"  tank-setpoint staat op 999: kijk {BREAK_SECONDS} s in de SCADA HMI hoe de tank overloopt")
            time.sleep(BREAK_SECONDS)
    finally:
        fortigate.policy_app_list(topo, BREAK_POLICY, before or "")
        log(f"  policy hersteld naar {before}")
        hmi = run_command("scada-hmi", "modbus-write 10.10.0.10 1 50")
        log(f"  setpoint via de HMI terug naar 50: {'ok' if hmi.get('ok') else hmi.get('detail')}")
    return res


VPATCH_POLICY = "L3->L1 Modbus read-only"
VPATCH_PLC = "10.10.0.10"       # tank-PLC
VPATCH_ATTACKER = "probe-ops"   # L3, heeft het Modbus-conduit naar de PLC
VPATCH_RECOVER = 24             # > otsim LISTEN_ONLY_SECONDS, zodat de PLC herstelt


def _plc_reachable(node, ip) -> bool:
    return bool(run_command(node, f"modbus-read {ip} 0 1").get("ok"))


def _poll_plc(node, ip, want_up: bool, log, tries=6) -> bool:
    """Wacht tot de PLC de gewenste toestand heeft (bereikbaar of stil)."""
    for _ in range(tries):
        if _plc_reachable(node, ip) == want_up:
            return True
        time.sleep(2)
    return False


def check_plc_dos_vpatch(log, topo) -> dict:
    """Virtual patching: dezelfde Force-Listen-Only-aanval (Modbus FC08) eerst
    zonder, dan met de FortiGuard-signature Modbus.TCP.Force.Listen.Only (ID
    11521) op de L3->L1-policy. We wisselen de IPS-sensor van de policy tussen een
    variant zonder en met de Force-Listen-block. Zonder valt de PLC stil; met
    block wordt het pakket geblokkeerd en blijft de PLC staan."""
    before = fortigate.policy_ips_sensor(topo, VPATCH_POLICY, fortigate.OT_IPS_SENSOR_NOVP)
    log(f"  IPS-sensor op '{VPATCH_POLICY}' tijdelijk naar {fortigate.OT_IPS_SENSOR_NOVP} "
        f"(geen Force-Listen-block); stond op {before}")
    phase1 = phase2 = False
    atk2 = {}
    try:
        time.sleep(5)  # FortiGate laadt de policy opnieuw
        log(f"  PLC vooraf: {'bereikbaar' if _plc_reachable(VPATCH_ATTACKER, VPATCH_PLC) else 'niet bereikbaar'}")

        log("  fase 1 - aanval zonder virtual patch")
        atk1 = run_command(VPATCH_ATTACKER, f"modbus-forcelisten {VPATCH_PLC}")
        log(f"    {atk1.get('detail')}")
        phase1 = _poll_plc(VPATCH_ATTACKER, VPATCH_PLC, want_up=False, log=log)
        log(f"    PLC is {'stilgevallen (DoS geslaagd)' if phase1 else 'nog bereikbaar (onverwacht)'}")
        log(f"  wacht tot de PLC vanzelf herstelt (~{VPATCH_RECOVER} s)")
        time.sleep(VPATCH_RECOVER)

        fortigate.policy_ips_sensor(topo, VPATCH_POLICY, fortigate.OT_IPS_SENSOR)
        log("  fase 2 - Force-Listen-block (virtual patch) weer AAN, zelfde aanval")
        time.sleep(12)  # de IPS-engine heeft enkele seconden nodig voor de nieuwe sensor actief is
        atk2 = run_command(VPATCH_ATTACKER, f"modbus-forcelisten {VPATCH_PLC}")
        log(f"    {atk2.get('detail')}")
        phase2 = _poll_plc(VPATCH_ATTACKER, VPATCH_PLC, want_up=True, log=log)
        log(f"    PLC {'bleef bereikbaar (aanval geblokkeerd)' if phase2 else 'viel alsnog stil (onverwacht)'}")
    finally:
        fortigate.policy_ips_sensor(topo, VPATCH_POLICY, before or fortigate.OT_IPS_SENSOR)
        log(f"  IPS-sensor op '{VPATCH_POLICY}' hersteld naar {before or fortigate.OT_IPS_SENSOR}")
        _poll_plc(VPATCH_ATTACKER, VPATCH_PLC, want_up=True, log=log)

    detail = (("patch uit: PLC viel stil" if phase1 else "patch uit: PLC bleef staan (onverwacht)")
              + "; " + ("patch aan: geblokkeerd, PLC bleef up" if phase2 else "patch aan: PLC viel alsnog stil (onverwacht)"))
    return {"ok": phase1 and phase2, "detail": detail, "srcip": atk2.get("srcip")}


CHECKS = {"assets": check_assets, "contractor-login": check_contractor_login,
          "contractor-no-mfa": lambda log, topo: check_contractor_login(log, topo, mfa=False),
          "break-readonly": check_break_readonly, "plc-dos-vpatch": check_plc_dos_vpatch}


def run_all(log):
    for sc in load():
        if sc.get("manual"):
            continue  # demo's die de FortiGate-config tijdelijk wijzigen
        try:
            run_scenario(log, sc["id"])
        except Exception as e:  # noqa: BLE001 - door met de volgende test
            log(f"  ✖ {e}")


def run_adhoc(log, node_name: str, command: str):
    log(f"{node_name}$ otprobe {command}")
    with _node_locks[node_name]:
        res = run_command(node_name, command)
    log(json.dumps(res, indent=2))

