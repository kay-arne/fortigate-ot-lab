"""Uitrol en afbraak van het lab op de Proxmox host."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from . import fortigate, updates
from .config import (ROOT, UPLOADS, contractor_credentials, get_state, inventory, load_host, load_topology,
                     prefixlen, segment_map, template_vmid, update_state,
                     uplink_tag, vm_name)
from .hostctl import HostError, host

ISO_NAME = "otlab-fgt-config.iso"
PIP_PACKAGES = "pymodbus==3.7.4 python-snap7==1.3 asyncua==1.1.5 flask==3.1.0"
APT_PACKAGES = "python3-venv python3-pip nmap curl netcat-openbsd openssh-server tcpdump iproute2"


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def lab_status() -> dict:
    topo = load_topology()
    h = host()
    out = h.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"])
    present = {r["vmid"]: r for r in json.loads(out)}

    def st(vmid):
        r = present.get(vmid)
        return r.get("status", "unknown") if r else "absent"

    links = set(h.run("ls /sys/class/net").split())
    fgt_status = st(topo["fortigate"]["vmid"])
    api = fortigate.api_status(topo) if fgt_status == "running" else None
    return {
        "fortigate": {"vmid": topo["fortigate"]["vmid"], "status": fgt_status,
                      "name": vm_name(topo, topo["fortigate"]["name"]),
                      "wan": topo["fortigate"]["wan"], "api": bool(get_state().get("fgt_api_key")),
                      "api_ok": bool(api and api["ok"]), "version": (api or {}).get("version", ""),
                      "api_error": (api or {}).get("error", "")},
        "template": {"vmid": template_vmid(topo), "status": st(template_vmid(topo))},
        "segments": [{**s, "present": s["bridge"] in links} for s in topo["segments"]],
        "nodes": [{**i, "status": st(i["vmid"])} for i in inventory(topo)],
    }


# --------------------------------------------------------------------------
# netwerk
# --------------------------------------------------------------------------
def ensure_bridges(log, topo):
    h = host()
    links = set(h.run("ls /sys/class/net").split())
    created = False
    for seg in topo["segments"]:
        if seg["bridge"] in links:
            log(f"bridge {seg['bridge']} bestaat al ({seg['name']})")
            continue
        log(f"bridge {seg['bridge']} aanmaken voor {seg['name']} (geen fysieke poort, geen host-IP)")
        h.run(["pvesh", "create", f"/nodes/{h.node}/network", "--iface", seg["bridge"],
               "--type", "bridge", "--autostart", "1", "--comments", f"otlab {seg['name']}"])
        created = True
    if created:
        log("netwerkconfig toepassen (ifreload)")
        h.run(["pvesh", "set", f"/nodes/{h.node}/network"])


def remove_bridges(log, topo):
    h = host()
    links = set(h.run("ls /sys/class/net").split())
    removed = False
    for seg in topo["segments"]:
        if seg["bridge"] in links:
            log(f"bridge {seg['bridge']} verwijderen")
            h.run(["pvesh", "delete", f"/nodes/{h.node}/network/{seg['bridge']}"], check=False)
            removed = True
    if removed:
        h.run(["pvesh", "set", f"/nodes/{h.node}/network"])


# --------------------------------------------------------------------------
# FortiGate
# --------------------------------------------------------------------------
def deploy_fortigate(log, topo):
    h = host()
    hcfg = load_host()
    state = get_state()
    fgt = topo["fortigate"]
    vmid = fgt["vmid"]

    image = state.get("fgt_image")
    if not image or not (UPLOADS / image).exists():
        raise RuntimeError("Geen FortiGate qcow2-image geüpload.")
    if fgt["wan"]["mode"] == "static" and not fgt["wan"].get("ip"):
        raise RuntimeError("Stel eerst het WAN-IP van de FortiGate in.")
    if not state.get("fgt_admin_password"):
        raise RuntimeError("Geen FortiGate admin-wachtwoord ingesteld.")

    if h.exists(vmid):
        log(f"FortiGate VM {vmid} bestaat al, overslaan")
        return

    h.run(["mkdir", "-p", h.workdir])  # oudere installaties hebben de werkmap nog niet
    remote_img = f"{h.workdir}/{image}"
    if h.run(f"test -f {shlex.quote(remote_img)} && echo yes", check=False).strip() != "yes":
        log(f"image {image} naar de host kopiëren...")
        last = [0]

        def progress(done, total):
            pct = int(done * 100 / total) if total else 100
            if pct >= last[0] + 10:
                last[0] = pct
                log(f"  {pct}%")
        h.put(UPLOADS / image, remote_img, callback=progress)

    log("cloud-init config drive bouwen (bootstrap + licentie)")
    iso_local = build_config_iso(topo, state)
    h.put(iso_local, f"{h.workdir}/{ISO_NAME}")
    h.run(["install", "-m", "0644", f"{h.workdir}/{ISO_NAME}", f"/var/lib/vz/template/iso/{ISO_NAME}"])

    # nieuwe FortiGate: API-key en provisioning van een eventuele vorige zijn ongeldig
    update_state(fgt_api_key=None, fgt_provisioned=False, fgt_password_applied=False)
    try:
        create_fortigate_vm(log, topo, hcfg["storage"], remote_img)
    except Exception:
        log(f"aanmaken mislukt; half-aangemaakte VM {vmid} opruimen")
        h.run(["qm", "destroy", str(vmid), "--purge", "1"], check=False)
        raise
    h.run(["qm", "start", str(vmid)], log=log)
    log("FortiGate start: eerste boot formatteert de logdisk en herstart (met FortiFlex nog een keer)")


def create_fortigate_vm(log, topo, storage, remote_img):
    """VM volgens de Fortinet Proxmox Administration Guide:
    - OS Linux 6.x/2.6, geen standaarddisk, NIC's zonder Proxmox-firewall;
    - fortios.qcow2 met 'qm disk import' als unused disk en daarna als scsi0 koppelen;
    - extra logdisk als scsi1 (FortiOS formatteert hem bij de eerste boot);
    - config drive (cloud-init ISO) als ide2, boot order ide2 -> scsi0 -> scsi1."""
    h = host()
    fgt = topo["fortigate"]
    vmid = fgt["vmid"]
    nics = [topo["lab"]["uplink_bridge"] + uplink_tag(topo)] + [seg["bridge"] for seg in topo["segments"]]
    args = ["qm", "create", str(vmid), "--name", vm_name(topo, fgt["name"]),
            "--ostype", "l26", "--cpu", "host", "--cores", str(fgt["cores"]), "--memory", str(fgt["memory"]),
            "--scsihw", "virtio-scsi-single", "--serial0", "socket",
            "--tags", "otlab", "--onboot", "1",
            "--description", "FortiGate-VM (FortiGate OT Lab). port1 = uplink, port2.. = OT-segmenten"]
    for i, bridge in enumerate(nics):
        args += [f"--net{i}", f"virtio,bridge={bridge},firewall=0"]
    log(f"VM {vmid} aanmaken ({len(nics)} NIC's: port1 uplink, port2-port{len(nics)} OT-segmenten)")
    h.run(args, log=log)

    log("fortios.qcow2 importeren (qm disk import)")
    try:
        h.run(["qm", "disk", "import", str(vmid), remote_img, storage], log=log)
    except HostError:
        h.run(["qm", "importdisk", str(vmid), remote_img, storage], log=log)  # oudere PVE-versies
    cfg = h.run(["qm", "config", str(vmid)])
    unused = re.search(r"^unused0:\s*(\S+)", cfg, re.M)
    if not unused:
        raise RuntimeError(f"geïmporteerde disk niet gevonden in de VM-config:\n{cfg}")
    log(f"boot disk koppelen als scsi0 ({unused.group(1)})")
    h.run(["qm", "set", str(vmid), "--scsi0", unused.group(1)], log=log)
    log(f"logdisk scsi1 ({fgt.get('log_disk_gb', 32)} GB) toevoegen")
    h.run(["qm", "set", str(vmid), "--scsi1", f"{storage}:{fgt.get('log_disk_gb', 32)}"], log=log)
    h.run(["qm", "set", str(vmid), "--ide2", f"local:iso/{ISO_NAME},media=cdrom"], log=log)
    # Apart van --ide2: in één 'qm set' voegt Proxmox de nieuwe cdrom toe aan de
    # oude boot order (net0) en gaat onze order verloren.
    boot = "order=ide2;scsi0;scsi1"
    h.run(["qm", "set", str(vmid), "--boot", boot], log=log)
    actual = re.search(r"^boot:\s*(\S+)", h.run(["qm", "config", str(vmid)]), re.M)
    if not actual or actual.group(1) != boot:
        raise RuntimeError(f"boot order niet gezet: verwacht '{boot}', staat op "
                           f"'{actual.group(1) if actual else '(leeg)'}'")
    log(f"boot order: {boot}")


def build_config_iso(topo, state) -> Path:
    """Config drive zoals in de Fortinet-handleiding (cloud-init met FortiFlex):
    één MIME-multipart user_data in openstack/latest met de configuratie en
    (indien gekozen) de licentie als bijlage; openstack/content blijft leeg."""
    out = Path(tempfile.mkdtemp(prefix="fgtiso-"))
    work = out / "config-drive"
    (work / "openstack" / "latest").mkdir(parents=True)
    (work / "openstack" / "content").mkdir(parents=True)

    parts = [("config.txt", fortigate.bootstrap_config(topo, state))]
    lic_mode = state.get("license_mode")
    if lic_mode == "file" and state.get("license_file"):
        parts.append(("license.txt", (UPLOADS / state["license_file"]).read_text().strip() + "\n"))
    elif lic_mode == "fortiflex" and state.get("fortiflex_token"):
        parts.append(("license.txt", f"LICENSE-TOKEN:{state['fortiflex_token']}\n"))
    (work / "openstack" / "latest" / "user_data").write_text(mime_multipart(parts))

    iso = out / ISO_NAME
    subprocess.run(["genisoimage", "-quiet", "-R", "-r", "-o", str(iso), str(work)], check=True)
    return iso


def mime_multipart(parts: list[tuple[str, str]]) -> str:
    """Zelfde formaat als 'write-mime-multipart -o user_data config.txt license.txt'."""
    msg = MIMEMultipart()
    for filename, text in parts:
        sub = MIMEText(text, "plain", "us-ascii")
        sub.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(sub)
    return msg.as_string()


# --------------------------------------------------------------------------
# LXC template met alle OT-simulators
# --------------------------------------------------------------------------
def _otsim_tarball() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="otsim-")) / "otsim.tgz"
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(ROOT / "otsim", arcname="otsim", filter=lambda t: None if "__pycache__" in t.name else t)
    return tmp


def push_otsim(log, vmid):
    h = host()
    tgz = _otsim_tarball()
    h.put(tgz, f"{h.workdir}/otsim.tgz")
    h.run(["pct", "push", str(vmid), f"{h.workdir}/otsim.tgz", "/tmp/otsim.tgz"])
    h.pct_exec(vmid, "mkdir -p /opt/otsim && rm -rf /opt/otsim/otsim && tar xzf /tmp/otsim.tgz -C /opt/otsim "
                     "&& install -m644 /opt/otsim/otsim/otsim.service /etc/systemd/system/otsim.service "
                     "&& systemctl daemon-reload", check=True)


def _is_template(h, vmid) -> bool:
    return bool(re.search(r"^template:\s*1", h.run(["pct", "config", str(vmid)], check=False), re.M))


def build_template(log, topo):
    h = host()
    hcfg = load_host()
    vmid = template_vmid(topo)
    if h.exists(vmid):
        if _is_template(h, vmid):
            log(f"template {vmid} bestaat al, overslaan")
            return
        log(f"container {vmid} is een onvoltooid template van een eerdere mislukte build; opnieuw bouwen")
        h.run(["pct", "stop", str(vmid)], check=False)
        h.run(["pct", "destroy", str(vmid), "--purge", "1"], log=log)
    tnet = topo["lab"]["template_net"]
    bridge = f"{topo['lab']['uplink_bridge']}{uplink_tag(topo)}"
    log(f"template-LXC {vmid} bouwen (tijdelijk op {bridge.replace(',tag=', ' VLAN ')}, {tnet}, voor packages)")
    h.run(["pct", "create", str(vmid), hcfg["template"], "--hostname", vm_name(topo, "template"),
           "--cores", "2", "--memory", "1024", "--rootfs", f"{hcfg['storage']}:4",
           "--unprivileged", "1", "--features", "nesting=1",
           "--net0", f"name=eth0,bridge={bridge},{tnet}",
           "--tags", "otlab"], log=log)
    try:
        h.run(["pct", "start", str(vmid)], log=log)
        wait_for_internet(log, h, vmid)
        log("apt packages installeren")
        h.pct_exec(vmid, f"export DEBIAN_FRONTEND=noninteractive; apt-get update -q && apt-get install -yq {APT_PACKAGES}",
                   timeout=1200, check=True)
        log("python-omgeving met OT-protocol libraries")
        h.pct_exec(vmid, f"python3 -m venv /opt/otsim/venv && /opt/otsim/venv/bin/pip install -q {PIP_PACKAGES}",
                   timeout=1200, check=True)
        push_otsim(log, vmid)
        h.pct_exec(vmid, "systemctl disable ssh; apt-get clean; rm -f /etc/ssh/ssh_host_*; truncate -s0 /etc/machine-id")
        log("netwerk loskoppelen en converteren naar template")
        h.run(["pct", "stop", str(vmid)])
        h.run(["pct", "set", str(vmid), "--delete", "net0"])
        h.run(["pct", "template", str(vmid)])
    except Exception:
        log(f"template-build mislukt; container {vmid} opruimen zodat een nieuwe poging schoon begint")
        h.run(["pct", "stop", str(vmid)], check=False)
        h.run(["pct", "destroy", str(vmid), "--purge", "1"], check=False)
        raise


def wait_for_internet(log, h, vmid, timeout=180):
    log("wachten op IP-adres en DNS in het template...")
    start = time.time()
    next_note = start + 30
    while time.time() - start < timeout:
        rc, _ = h.pct_exec(vmid, "getent hosts deb.debian.org")
        if rc == 0:
            log(f"internet OK na {int(time.time() - start)}s")
            return
        if time.time() > next_note:
            log(f"  nog geen internet na {int(time.time() - start)}s...")
            next_note += 30
        time.sleep(3)
    log("diagnose van het netwerk in het template:")
    for line in network_diagnosis(h, vmid):
        log(f"  {line}")
    raise RuntimeError("template heeft geen internet; zie de diagnose hierboven")


def network_diagnosis(h, vmid) -> list[str]:
    """Stap voor stap: IP (DHCP) -> default route -> gateway -> internet -> DNS."""
    def run(cmd):
        rc, out = h.pct_exec(vmid, cmd, timeout=30)
        return rc, out.strip()

    out = []
    _, addr = run("ip -4 -o addr show eth0 | awk '{print $4}'")
    _, route = run("ip route | awk '/^default/ {print $3; exit}'")
    _, resolv = run("grep -E '^(nameserver|search)' /etc/resolv.conf")
    out.append(f"IPv4-adres eth0 : {addr or 'GEEN'}")
    out.append(f"default gateway : {route or 'GEEN'}")
    out.append(f"resolv.conf     : {resolv.replace(chr(10), ' | ') or 'leeg'}")
    if not addr:
        out.append("=> geen IP-adres: geen DHCP-antwoord op deze bridge/VLAN (DHCP-pool vol, geen DHCP-server "
                   "in dit VLAN?). Controleer DHCP of kies een tijdelijk statisch IP voor de template-build "
                   "(template_net in data/host.json).")
        return out
    if not route:
        out.append("=> wel IP maar geen default gateway: DHCP geeft geen router mee, of de gateway ontbreekt in template_net.")
        return out
    rc_gw, _ = run(f"ping -c1 -W2 {route} >/dev/null")
    rc_net, _ = run("ping -c1 -W2 1.1.1.1 >/dev/null")
    rc_dns, _ = run("getent hosts deb.debian.org")
    out.append(f"ping gateway    : {'ok' if rc_gw == 0 else 'geen antwoord'}")
    out.append(f"ping 1.1.1.1    : {'ok' if rc_net == 0 else 'geen antwoord'}")
    out.append(f"DNS deb.debian.org: {'ok' if rc_dns == 0 else 'mislukt'}")
    if rc_net == 0 and rc_dns != 0:
        out.append("=> internet werkt, DNS niet: controleer de nameserver hierboven (overgenomen van de Proxmox host).")
    elif rc_net != 0:
        out.append("=> geen internet via de gateway: controleer routing/firewall voor dit IP of deze VLAN.")
    return out


# --------------------------------------------------------------------------
# devices + probes
# --------------------------------------------------------------------------
def deploy_node(log, topo, node):
    h = host()
    segs = segment_map(topo)
    seg = segs[node["segment"]]
    vmid = node["vmid"]
    res = topo["resources"]["probe" if node["kind"] == "probe" else "device"]
    if h.exists(vmid):
        log(f"{node['hostname']} ({vmid}) bestaat al")
        return
    tpl = template_vmid(topo)
    log(f"{node['hostname']} ({vmid}) → {seg['name']} {node['ip']}")
    try:
        h.run(["pct", "clone", str(tpl), str(vmid), "--hostname", node["hostname"]])
    except HostError:
        log("  linked clone niet mogelijk op deze storage, full clone")
        h.run(["pct", "clone", str(tpl), str(vmid), "--hostname", node["hostname"], "--full", "1"])
    h.run(["pct", "set", str(vmid),
           "--net0", f"name=eth0,bridge={seg['bridge']},ip={node['ip']}/{prefixlen(seg['subnet'])},gw={seg['gateway']}",
           "--cores", str(res["cores"]), "--memory", str(res["memory"]),
           "--nameserver", seg["gateway"],
           "--tags", f"otlab;{node['segment']};{node['kind']}",
           "--description", f"otlab {node['kind']} role={node['role']}"])
    h.run(["pct", "start", str(vmid)])
    time.sleep(2)
    cfg = json.dumps({"name": node["name"], "role": node["role"], "ip": node["ip"],
                      "segment": node["segment"], "args": node["args"]})
    h.pct_exec(vmid, f"dpkg-reconfigure -f noninteractive openssh-server >/dev/null 2>&1; "
                     f"systemd-machine-id-setup >/dev/null; echo {shlex.quote(cfg)} > /etc/otsim.json", check=True)
    if node["kind"] == "device":
        h.pct_exec(vmid, "systemctl enable --now otsim", check=True)
    setup_access(log, node)


OTPROBE_WRAPPER = '#!/bin/sh\ncd /opt/otsim && exec /opt/otsim/venv/bin/python -m otsim.probe "$@"\n'
ACCESS_ROLES = ("jumphost", "ews")


def setup_access(log, node):
    """'otprobe' als gewoon commando op elke node; op de jump host en de EWS
    ook het contractor-account met SSH-wachtwoordlogin (pad van Remco)."""
    script = ["set -e",
              f"printf %s {shlex.quote(OTPROBE_WRAPPER)} > /usr/local/bin/otprobe",
              "chmod 755 /usr/local/bin/otprobe"]
    if node["role"] in ACCESS_ROLES:
        user, pw = contractor_credentials()
        log(f"  account '{user}' op {node['hostname']} (SSH)")
        script += [f"id -u {user} >/dev/null 2>&1 || useradd -m -s /bin/bash -c 'Contractor (lab)' {user}",
                   f"echo {shlex.quote(f'{user}:{pw}')} | chpasswd",
                   "mkdir -p /etc/ssh/sshd_config.d",
                   "printf 'PasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/otlab.conf",
                   "systemctl enable ssh >/dev/null 2>&1",
                   "systemctl restart ssh"]
    host().pct_exec(node["vmid"], "; ".join(script), check=True)


# Webinterfaces die het kijkvenster in de controller kan tonen.
def view_port(node: dict) -> int | None:
    role = node.get("role")
    if role == "hmi":
        return 80
    if role == "historian":
        return 8080
    if role == "webservice":
        return int(node.get("args", {}).get("port", 80))
    return None


def fetch_view(node: dict, path: str = "/") -> str:
    """Haal een webpagina op ín de container zelf (via Proxmox, niet via de
    FortiGate): de kandidaat kijkt mee zonder dat er een gat in de firewall komt."""
    port = view_port(node)
    url = f"http://127.0.0.1:{port}{path}"
    rc, out = host().pct_exec(node["vmid"], f"curl -s -m 5 {shlex.quote(url)}", timeout=20)
    if rc != 0:
        raise RuntimeError(f"{node['hostname']} gaf geen antwoord op poort {port} (curl rc={rc})")
    return out


def update_simulators(log):
    """Nieuwe otsim-code naar alle draaiende devices pushen (na git pull)."""
    topo = load_topology()
    for node in inventory(topo):
        if host().status(node["vmid"]) == "running":
            log(f"otsim bijwerken op {node['hostname']}")
            push_otsim(log, node["vmid"])
            if node["kind"] == "device":
                host().pct_exec(node["vmid"], "systemctl restart otsim")
            setup_access(log, node)
    update_state(otsim_tree=updates.otsim_tree())


# --------------------------------------------------------------------------
# orchestratie
# --------------------------------------------------------------------------
def deploy_all(log):
    topo = load_topology()
    log("== 1/5 netwerksegmenten (Purdue) ==")
    ensure_bridges(log, topo)
    log("== 2/5 FortiGate ==")
    deploy_fortigate(log, topo)
    log("== 3/5 LXC-template ==")
    build_template(log, topo)
    log("== 4/5 OT-devices en probes ==")
    for node in inventory(topo):
        deploy_node(log, topo, node)
    log("== 5/5 FortiGate OT-policies ==")
    fortigate.provision(log, topo)
    update_state(deployed=True, otsim_tree=updates.otsim_tree())


def component_ids(topo) -> list[str]:
    """Alle losse componenten, in uitrolvolgorde."""
    return (["network", "fortigate", "template"] + [f"node:{n['name']}" for n in inventory(topo)]
            + ["policies"])


def _destroy_vmid(log, h, vmid, label):
    kind = h.exists(vmid)
    if not kind:
        log(f"{label} ({vmid}) bestaat niet, overslaan")
        return
    tool = "qm" if kind == "qemu" else "pct"
    log(f"{label} ({vmid}) verwijderen")
    h.run([tool, "stop", str(vmid)], check=False)
    h.run([tool, "destroy", str(vmid), "--purge", "1"], log=log)


def destroy_selected(log, items):
    topo = load_topology()
    h = host()
    items = set(items)
    nodes = [n for n in inventory(topo) if f"node:{n['name']}" in items]
    for n in nodes:
        _destroy_vmid(log, h, n["vmid"], n["hostname"])
    if "fortigate" in items:
        _destroy_vmid(log, h, topo["fortigate"]["vmid"], vm_name(topo, topo["fortigate"]["name"]))
        update_state(fgt_api_key=None, fgt_provisioned=False, fgt_password_applied=False)
    if "template" in items:
        try:
            _destroy_vmid(log, h, template_vmid(topo), vm_name(topo, "template"))
        except HostError as e:
            raise RuntimeError(f"template kan niet weg: {e}. Linked clones gebruiken het nog; "
                               "verwijder eerst de containers die ervan gekloond zijn.") from None
    if "network" in items:
        lab_ids = [topo["fortigate"]["vmid"], template_vmid(topo)] + [n["vmid"] for n in inventory(topo)]
        still = [v for v in lab_ids if h.exists(v)]
        if still:
            log(f"bridges niet verwijderd: nog {len(still)} lab-VM's/containers gebruiken ze")
        else:
            remove_bridges(log, topo)


def deploy_selected(log, items, recreate=False):
    """Alleen de gekozen componenten uitrollen. Afhankelijkheden (bridges, template)
    worden zo nodig meegenomen; bestaande componenten worden overgeslagen, tenzij
    recreate: dan worden ze eerst verwijderd."""
    topo = load_topology()
    items = set(items)
    nodes = [n for n in inventory(topo) if f"node:{n['name']}" in items]
    if recreate:
        log("== gekozen componenten eerst verwijderen ==")
        destroy_selected(log, items - {"network", "policies"})
    if items & {"network", "fortigate"} or nodes:
        log("== netwerksegmenten ==")
        ensure_bridges(log, topo)
    if "fortigate" in items:
        log("== FortiGate ==")
        deploy_fortigate(log, topo)
    if "template" in items or nodes:
        log("== LXC-template ==")
        build_template(log, topo)
    if nodes:
        log(f"== {len(nodes)} container(s) ==")
        for node in nodes:
            deploy_node(log, topo, node)
    if items & {"fortigate", "policies"}:
        log("== FortiGate OT-policies ==")
        fortigate.provision(log, topo)


def destroy_all(log, remove_template=False, remove_net=False):
    topo = load_topology()
    h = host()
    ids = [n["vmid"] for n in inventory(topo)] + [topo["fortigate"]["vmid"]]
    if remove_template:
        ids.append(template_vmid(topo))
    for vmid in ids:
        kind = h.exists(vmid)
        if not kind:
            continue
        tool = "qm" if kind == "qemu" else "pct"
        log(f"{tool} destroy {vmid}")
        h.run([tool, "stop", str(vmid)], check=False)
        h.run([tool, "destroy", str(vmid), "--purge", "1"], check=False)
    if remove_net:
        remove_bridges(log, topo)
    update_state(deployed=False, fgt_api_key=None, fgt_password_applied=False)
