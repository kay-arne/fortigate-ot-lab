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
# Tijdelijk admin-wachtwoord voor de eerste boot: voldoet aan complexiteitseisen
# (hoofd-/kleine letter, cijfer, speciaal teken, >= 12 tekens) maar bevat geen
# tekens die in cloud-init of de FortiOS-CLI quoting nodig hebben. Tijdens de
# provisioning vervangt de controller het door het wachtwoord uit de UI.
BOOTSTRAP_PASSWORD = "Deployment_Pass2026"
# Lab: zo lang mogelijk ingelogd blijven in de GUI (FortiOS-maximum, minuten).
ADMIN_TIMEOUT = 480


def port_alias(seg) -> str:
    """Alias van een segmentpoort; FortiOS staat maximaal 25 tekens toe."""
    return (seg.get("alias") or seg["name"])[:25]


def purdue_level(seg) -> str:
    """Purdue-level voor de segmentpoort. FortiOS 7.6 kent op interfaces geen
    level 0, dus L0 valt in L1; het testsegment speelt IT (L4)."""
    level = str(seg.get("purdue", ""))
    if level in ("", "-"):
        return "4"
    return "1" if level == "0" else level


# --------------------------------------------------------------------------
# bootstrap (cloud-init user_data)
# --------------------------------------------------------------------------
def bootstrap_config(topo, state) -> str:
    fgt = topo["fortigate"]
    wan = fgt["wan"]
    lines = [
        "config system global",
        f'    set hostname "{vm_name(topo, fgt["name"])}"',
        f"    set admintimeout {ADMIN_TIMEOUT}",
        f"    set admin-sport {fgt.get('admin_https_port', 443)}",
        "end",
        # geen --More--: de controller leest CLI-output over SSH
        "config system console",
        "    set output standard",
        "end",
        # lab: geen complexiteitseisen, zodat het wachtwoord uit de UI altijd geaccepteerd wordt
        "config system password-policy",
        "    set status disable",
        "end",
        "config system admin",
        "    edit admin",
        f"        set password {BOOTSTRAP_PASSWORD}",
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
            f'        set alias "{port_alias(seg)}"',
            "        set role lan",
            "        set mode static",
            f"        set ip {seg['gateway']} {netmask(seg['subnet'])}",
            "        set allowaccess ping",
            "        set device-identification enable",
            f"        set default-purdue-level {purdue_level(seg)}",
            "    next",
        ]
    lines.append("end")
    lines += route_lines(wan) + dns_lines(wan)
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


def route_lines(wan) -> list[str]:
    if wan["mode"] != "static" or not wan.get("gateway"):
        return []
    return ["config router static", "    edit 1", f"        set gateway {wan['gateway']}",
            "        set device port1", "    next", "end"]


def dns_lines(wan) -> list[str]:
    """Eigen DNS-servers. Zonder deze instelling gebruikt FortiOS de FortiGuard-DNS
    (96.45.45.45) via DNS-over-TLS (tcp/853), wat in veel netwerken geblokkeerd is;
    dan faalt o.a. de FortiFlex-activatie."""
    servers = [d for d in (wan.get("dns1"), wan.get("dns2")) if d]
    if not servers:
        return []
    lines = ["config system dns", f"    set primary {servers[0]}"]
    if len(servers) > 1:
        lines.append(f"    set secondary {servers[1]}")
    lines += ["    set protocol cleartext", "end"]
    return lines


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


def _admin_passwords() -> list[str]:
    """Kandidaat-wachtwoorden voor admin, meest waarschijnlijke eerst (elke
    mislukte poging telt mee voor de admin-lockout van FortiOS)."""
    state = get_state()
    final = state.get("fgt_admin_password")
    order = [final, BOOTSTRAP_PASSWORD] if state.get("fgt_password_applied") else [BOOTSTRAP_PASSWORD, final]
    return [p for i, p in enumerate(order) if p and p not in order[:i]]


def _ssh_connect(topo, passwords: list[str] | None = None) -> paramiko.SSHClient:
    """Log in als admin met het eerste wachtwoord dat werkt."""
    err = None
    for pw in passwords or _admin_passwords():
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(mgmt_ip(topo), username="admin", password=pw,
                           look_for_keys=False, allow_agent=False, timeout=15)
            client.admin_password = pw  # voor 'Please enter current administrator password'
            return client
        except paramiko.AuthenticationException as e:
            err = e
    raise err or paramiko.AuthenticationException("geen admin-wachtwoord bekend")


def wait_for_ssh(log, topo, timeout=900):
    """Wacht tot admin kan inloggen. Vlak na de eerste boot (logdisk formatteren,
    FortiFlex-activatie) herstart de FortiGate nog; dan weigert SSH tijdelijk.
    Per ronde maar één wachtwoord proberen en na een geweigerde login ruim een
    minuut wachten: FortiOS blokkeert het IP na 3 mislukte logins (60 s)."""
    deadline = time.time() + timeout
    last = None
    attempt = 0
    while time.time() < deadline:
        candidates = _admin_passwords()
        pw = candidates[attempt % len(candidates)]
        label = "bootstrap-wachtwoord" if pw == BOOTSTRAP_PASSWORD else "ingesteld wachtwoord"
        try:
            _ssh_connect(topo, [pw]).close()
            if pw != BOOTSTRAP_PASSWORD:
                update_state(fgt_password_applied=True)
            log(f"SSH-login als admin gelukt ({label})")
            return
        except paramiko.AuthenticationException:
            attempt += 1
            log(f"  SSH: {label} geweigerd; 65 s wachten (lockout voorkomen)...")
            time.sleep(65)
        except (paramiko.SSHException, OSError) as e:
            if str(e) != last:
                log(f"  SSH nog niet gelukt ({e}); opnieuw proberen...")
                last = str(e)
            time.sleep(15)
    raise RuntimeError(
        f"Kan niet als admin inloggen op de FortiGate (bootstrap-wachtwoord '{BOOTSTRAP_PASSWORD}' en het "
        "ingestelde wachtwoord geweigerd). Controleer via de Proxmox-console: log in als admin en draai "
        "'diagnose debug cloudinit show' om te zien of de bootstrap-config is toegepast.")


CLI_PROMPT = re.compile(r"[#$] $")
PW_PROMPT = re.compile(r"password:\s*$", re.I)
YN_PROMPT = re.compile(r"\(y/n\)\s*$", re.I)
MORE_PROMPT = re.compile(r"--More--\s*$")


def _read_until_prompt(shell, timeout) -> str:
    """Lees tot de CLI een prompt toont (of de timeout verloopt)."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if shell.recv_ready():
            buf += shell.recv(65535).decode(errors="replace")
            if MORE_PROMPT.search(buf):
                shell.send(" ")
                continue
            tail = buf.rstrip()
            if CLI_PROMPT.search(buf) or PW_PROMPT.search(tail) or YN_PROMPT.search(tail):
                time.sleep(0.3)  # nog meer output onderweg?
                if not shell.recv_ready():
                    return buf
        elif shell.exit_status_ready():
            return buf
        else:
            time.sleep(0.2)
    return buf


def ssh_cli(topo, commands: list[str], timeout=60) -> str:
    """Voer CLI-regels uit over SSH (interactieve shell). Vraagt FortiOS om het
    huidige admin-wachtwoord of om een bevestiging (y/n), dan antwoordt de
    controller zelf."""
    client = _ssh_connect(topo)
    shell = client.invoke_shell(width=250)
    out = _read_until_prompt(shell, 15)
    try:
        for cmd in commands:
            shell.send(cmd + "\n")
            chunk = _read_until_prompt(shell, timeout)
            for _ in range(3):
                tail = chunk.rstrip()
                if PW_PROMPT.search(tail):
                    shell.send(client.admin_password + "\n")
                elif YN_PROMPT.search(tail):
                    shell.send("y\n")
                else:
                    break
                chunk += _read_until_prompt(shell, timeout)
            out += chunk
    finally:
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

    def put(self, path, body):
        r = self.s.put(f"{self.base}/api/v2/{path}", json=body, timeout=30)
        if not r.ok:
            raise RuntimeError(f"{path}: {r.status_code} {r.text[:300]}")
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


def api_status(topo) -> dict | None:
    """Snelle check of de REST API antwoordt (voor de statusweergave).
    None als er nog geen API-key is."""
    if not get_state().get("fgt_api_key"):
        return None
    try:
        r = Api(topo).s.get(f"{base_url(topo)}/api/v2/monitor/system/status", timeout=3)
        r.raise_for_status()
        return {"ok": True, "version": r.json().get("version", "")}
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "error": str(e)[:200]}


# --------------------------------------------------------------------------
# provisioning na de eerste boot
# --------------------------------------------------------------------------
def ensure_license(log, topo):
    """Alleen de licentiestatus loggen. De licentie (bestand of FortiFlex-token)
    gaat bij de uitrol mee via cloud-init; opnieuw activeren herstart de
    FortiGate en blokkeert dan de provisioning, dus dat doen we hier niet."""
    status = ssh_cli(topo, ["get system status"])
    m = re.search(r"License Status:\s*(\S+)", status)
    if not m:
        log("⚠ licentiestatus niet gevonden in 'get system status'")
        return
    lic = m.group(1)
    log(f"licentiestatus: {lic}")
    if lic.lower() not in ("valid", "grace"):
        log("⚠ licentie nog niet geldig; controleer FortiGuard-bereikbaarheid. "
            "Zonder geldige licentie (en OT-licentie) ontbreken industriële signatures.")


def connectivity_test(log, topo):
    out = ssh_cli(topo, ["get router info routing-table static", "get system dns",
                         "execute ping-options repeat-count 2",
                         "execute ping update.fortiguard.net"])
    # FortiOS toont de CNAME, bijv. 'PING fds1.fortinet.com (173.243.138.71)'
    resolved = re.search(r"^PING (\S+) \(([\d.]+)\)", out, re.M)
    replies = re.search(r"(\d+) packets received", out)
    if not resolved:
        log("✖ DNS: update.fortiguard.net wordt niet opgelost. Controleer de DNS-servers.")
    else:
        log(f"✔ DNS: update.fortiguard.net -> {resolved.group(1)} ({resolved.group(2)})")
        if replies and int(replies.group(1)) > 0:
            log("✔ internet: ping-antwoord ontvangen")
        else:
            log("⚠ geen ping-antwoord (ICMP kan geblokkeerd zijn); controleer gateway en uitgaand verkeer")
    if not re.search(r"0\.0\.0\.0/0", out):
        log("✖ geen default route: vul de gateway van port1 in")


def ensure_api_key(log, topo):
    if get_state().get("fgt_api_key"):
        return
    ctl_ip = _controller_ip_towards(mgmt_ip(topo))
    log(f"REST API-user '{API_USER}' aanmaken (trusthost {ctl_ip}/32)")
    out = ssh_cli(topo, [
        "config system api-user",
        f'edit "{API_USER}"',
        "set accprofile super_admin",
        "config vdom",
        "edit root",
        "next",
        "end",
        "config trusthost",
        "edit 1",
        f"set ipv4-trusthost {ctl_ip} 255.255.255.255",
        "next",
        "end",
        "next",
        "end",
        f"execute api-user generate-key {API_USER}",
    ])
    m = re.search(r"New API key:\s*(\S+)", out)
    if not m:
        raise RuntimeError(f"Kon API-key niet genereren. CLI-output:\n{out[-800:]}")
    update_state(fgt_api_key=m.group(1))
    log("API-key opgeslagen")


def ensure_admin_password(log, topo):
    """Vervang het bootstrap-wachtwoord door het wachtwoord uit de UI (via REST,
    dus zonder CLI-quoting) en controleer het met een SSH-login."""
    state = get_state()
    final = state.get("fgt_admin_password")
    if not final or final == BOOTSTRAP_PASSWORD:
        log("⚠ geen eigen admin-wachtwoord ingesteld; bootstrap-wachtwoord blijft actief")
        return
    if state.get("fgt_password_applied"):
        try:
            _ssh_connect(topo, [final]).close()
            log("admin-wachtwoord is al ingesteld")
            return
        except paramiko.AuthenticationException:
            pass
    log("admin-wachtwoord instellen (bootstrap-wachtwoord vervangen)")
    api = Api(topo)
    try:
        api.put("cmdb/system/admin/admin", {"password": final})
    except RuntimeError as e:
        log(f"  zonder oud wachtwoord geweigerd ({e}); opnieuw met old-password")
        api.put("cmdb/system/admin/admin", {"password": final, "old-password": BOOTSTRAP_PASSWORD})
    time.sleep(2)
    try:
        _ssh_connect(topo, [final]).close()
    except paramiko.AuthenticationException:
        raise RuntimeError("Admin-wachtwoord gewijzigd, maar inloggen met het nieuwe wachtwoord lukt niet. "
                           f"Inloggen kan nog met '{BOOTSTRAP_PASSWORD}' als de wijziging niet is doorgevoerd.")
    update_state(fgt_password_applied=True)
    log("✔ admin-wachtwoord ingesteld en getest")


# GUI-wizard bij de eerste login (FortiCare-registratie, FortiConverter, patch-
# upgrades, dashboard). Niet elke optie bestaat in elke 7.x-release; de REST API
# negeert onbekende velden, dus na het zetten lezen we terug wat is blijven staan.
GUI_GLOBAL = {
    "admintimeout": ADMIN_TIMEOUT,
    "gui-forticare-registration-setup-warning": "disable",
    "gui-auto-upgrade-setup-warning": "disable",
    "gui-firmware-upgrade-setup-warning": "disable",
    "gui-firmware-upgrade-warning": "disable",
    "gui-cdn-usage": "disable",
}


def suppress_setup_wizard(log, api):
    """Zet de vragen bij de eerste GUI-login uit (lab: geen FortiCare-registratie)."""
    try:
        api.put("cmdb/system/global", GUI_GLOBAL)
        cur = api.get("cmdb/system/global").get("results", {})
    except (RuntimeError, requests.RequestException) as e:
        log(f"⚠ setup-wizard uitzetten mislukt: {e}")
        return
    applied = [k for k, v in GUI_GLOBAL.items() if str(cur.get(k)) == str(v)]
    unknown = [k for k in GUI_GLOBAL if k not in cur]
    log(f"GUI: logout na {cur.get('admintimeout', '?')} min; setup-wizard: "
        f"{len([k for k in applied if k != 'admintimeout'])} instelling(en) uit"
        + (f"; niet in deze FortiOS-versie: {', '.join(unknown)}" if unknown else ""))
    # 'What's new' en dashboard-keuze zijn per admin; markeer de huidige release als gezien.
    try:
        ver = api.get("monitor/system/status").get("version", "")
        m = re.match(r"v?(\d+\.\d+)", ver)
        if m:
            api.put("cmdb/system/admin/admin", {"gui-ignore-release-overview-version": m.group(1)})
    except (RuntimeError, requests.RequestException) as e:
        log(f"  release-overzicht niet uitgezet: {e}")


# OT-weergave in de GUI: Purdue-levels (assets per laag) en virtual patching.
GUI_OT_FEATURES = {"gui-ot": "enable", "gui-virtual-patch-profile": "enable"}
VPATCH_PROFILE = "OT-vpatch"


def ensure_aliases(log, api, topo):
    """Aliassen ook via de API zetten, zodat een bestaand lab ze krijgt zonder
    nieuwe bootstrap; de bootstrap slaat een geweigerde alias stil over."""
    failed = []
    for seg in topo["segments"]:
        alias = port_alias(seg)
        try:
            api.put(f"cmdb/system/interface/{seg['port']}", {"alias": alias})
            got = api.get(f"cmdb/system/interface/{seg['port']}").get("results", [{}])
            got = (got[0] if isinstance(got, list) and got else got or {}).get("alias")
            if got != alias:
                failed.append(seg["port"])
        except (RuntimeError, requests.RequestException):
            failed.append(seg["port"])
    if failed:
        log(f"⚠ alias niet gezet op {', '.join(failed)}")


def enable_ot_features(log, api, topo) -> str | None:
    """Feature visibility voor OT, Purdue-level per segmentpoort en een virtual
    patching-profiel. Geeft de profielnaam terug als dat profiel bestaat."""
    try:
        api.put("cmdb/system/settings", GUI_OT_FEATURES)
        cur = api.get("cmdb/system/settings").get("results", {})
        on = [k for k, v in GUI_OT_FEATURES.items() if cur.get(k) == v]
        missing = [k for k in GUI_OT_FEATURES if k not in on]
        log(f"feature visibility: {', '.join(on) or 'geen'} aan"
            + (f"; niet gelukt: {', '.join(missing)}" if missing else ""))
    except (RuntimeError, requests.RequestException) as e:
        log(f"⚠ feature visibility niet gezet: {e}")

    # Purdue-level van assets volgt de poort waarop ze ontdekt worden.
    levels = []
    for seg in topo["segments"]:
        level = purdue_level(seg)
        try:
            api.put(f"cmdb/system/interface/{seg['port']}", {"default-purdue-level": level})
            got = api.get(f"cmdb/system/interface/{seg['port']}").get("results", [{}])
            got = (got[0] if isinstance(got, list) and got else got or {}).get("default-purdue-level")
            if got is not None and str(got) == level:
                levels.append(f"{seg['port']}=L{level}")
        except (RuntimeError, requests.RequestException):
            pass
    log(f"Purdue-levels: {', '.join(levels) if levels else 'niet ondersteund door deze FortiOS-versie'}")

    try:
        api.upsert("cmdb/virtual-patch/profile", VPATCH_PROFILE, {
            "name": VPATCH_PROFILE, "comment": "OT virtual patching (FortiGuard OT-signatures)",
            "severity": "info low medium high critical", "action": "block", "log": "enable"})
        log(f"virtual patching-profiel {VPATCH_PROFILE} aangemaakt")
        return VPATCH_PROFILE
    except (RuntimeError, requests.RequestException) as e:
        log(f"⚠ virtual patching-profiel niet aangemaakt (licentie of FortiOS-versie?): {e}")
        return None


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
    wait_for_ssh(log, topo)
    try:
        connectivity_test(log, topo)
    except Exception as e:  # noqa: BLE001 - alleen diagnose, niet fataal
        log(f"⚠ connectiviteitstest overgeslagen: {e}")
    ensure_license(log, topo)
    ensure_api_key(log, topo)
    ensure_admin_password(log, topo)
    api = Api(topo)
    suppress_setup_wizard(log, api)
    ensure_aliases(log, api, topo)
    vpatch = enable_ot_features(log, api, topo)
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
    # info/low op 'default': opt-in-signatures als Modbus.TCP.Unauthorized.Read.Request.PLC
    # gaan anders af op elke read van de HMI en historian over een toegestane conduit.
    api.upsert("cmdb/ips/sensor", "OT-IPS", {
        "name": "OT-IPS", "comment": "IT + OT signatures, block medium+",
        "entries": [{"id": 1, "severity": "medium high critical", "status": "enable",
                     "action": "block", "log": "enable", "log-packet": "enable"},
                    {"id": 2, "severity": "info low", "status": "default", "action": "pass", "log": "enable"}]})

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
        if vpatch and di != "port1":  # virtual patching op alles wat de fabriek in gaat
            body["virtual-patch-profile"] = vpatch
        try:
            api.upsert("cmdb/firewall/policy", pid, body)
        except RuntimeError:
            if "virtual-patch-profile" not in body:
                raise
            log(f"  policy {pid}: virtual-patch-profile geweigerd, zonder opnieuw")
            body.pop("virtual-patch-profile")
            api.upsert("cmdb/firewall/policy", pid, body)
    update_state(fgt_provisioned=True)
    log("FortiGate OT-provisioning klaar")


# --------------------------------------------------------------------------
# logs voor de testcenter-validatie
# --------------------------------------------------------------------------
def asset_inventory(topo) -> list[dict]:
    """Devices die de FortiGate heeft herkend (Asset Identity Center)."""
    return Api(topo).get("monitor/user/device/query").get("results", [])


def policy_app_list(topo, name: str, profile: str | None = None) -> str | None:
    """Application-list-profiel van een policy (op naam); met profile wordt het gezet.
    Geeft het profiel van vóór de wijziging terug."""
    api = Api(topo)
    found = api.get("cmdb/firewall/policy", filter=f"name=={name}").get("results", [])
    if not found:
        raise RuntimeError(f"policy '{name}' niet gevonden")
    if profile is not None:
        api.put(f"cmdb/firewall/policy/{found[0]['policyid']}", {"application-list": profile})
    return found[0].get("application-list") or None


def policy_vpatch(topo, name: str, profile: str | None = None) -> str | None:
    """Virtual-patch-profiel van een policy (op naam); met profile wordt het gezet
    ("" haalt het eraf). Geeft het profiel van vóór de wijziging terug."""
    api = Api(topo)
    found = api.get("cmdb/firewall/policy", filter=f"name=={name}").get("results", [])
    if not found:
        raise RuntimeError(f"policy '{name}' niet gevonden")
    if profile is not None:
        api.put(f"cmdb/firewall/policy/{found[0]['policyid']}", {"virtual-patch-profile": profile})
    return found[0].get("virtual-patch-profile") or None


def query_logs(topo, kind: str, srcip: str, since: float, rows=50, dstip: str | None = None) -> list[dict]:
    """kind: traffic/forward | app-ctrl | ips. Meerdere filters zijn een AND."""
    api = Api(topo)
    path = f"log/memory/{kind}"
    filters = [f"srcip=={srcip}"] + ([f"dstip=={dstip}"] if dstip else [])
    try:
        res = api.get(path, filter=filters, rows=rows)
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
