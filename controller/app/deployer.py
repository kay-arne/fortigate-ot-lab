"""Uitrol en afbraak van het lab op de Proxmox host."""
from __future__ import annotations

import json
import shlex
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from . import fortigate
from .config import (ROOT, UPLOADS, get_state, inventory, load_host, load_topology,
                     prefixlen, segment_map, template_vmid, update_state)
from .hostctl import HostError, host

REMOTE_DIR = "/var/lib/vz/otlab"
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
    return {
        "fortigate": {"vmid": topo["fortigate"]["vmid"], "status": st(topo["fortigate"]["vmid"]),
                      "wan": topo["fortigate"]["wan"], "api": bool(get_state().get("fgt_api_key"))},
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

    h.run(["mkdir", "-p", REMOTE_DIR])
    remote_img = f"{REMOTE_DIR}/{image}"
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
    h.run("mkdir -p /var/lib/vz/template/iso")
    h.put(iso_local, f"/var/lib/vz/template/iso/{ISO_NAME}")

    storage = hcfg["storage"]
    args = ["qm", "create", str(vmid), "--name", fgt["name"],
            "--memory", str(fgt["memory"]), "--cores", str(fgt["cores"]), "--cpu", "host",
            "--ostype", "l26", "--serial0", "socket", "--vga", "serial0",
            "--tags", "otlab", "--onboot", "1",
            "--net0", f"virtio,bridge={topo['lab']['uplink_bridge']}"]
    for i, seg in enumerate(topo["segments"], start=1):
        args += [f"--net{i}", f"virtio,bridge={seg['bridge']}"]
    log(f"VM {vmid} aanmaken ({len(topo['segments']) + 1} NIC's)")
    h.run(args, log=log)
    h.run(["qm", "set", str(vmid), "--virtio0", f"{storage}:0,import-from={remote_img}"], log=log)
    h.run(["qm", "set", str(vmid), "--virtio1", f"{storage}:4"], log=log)  # log disk
    h.run(["qm", "set", str(vmid), "--ide2", f"local:iso/{ISO_NAME},media=cdrom",
           "--boot", "order=virtio0"], log=log)
    h.run(["qm", "start", str(vmid)], log=log)
    log("FortiGate start; eerste boot duurt enkele minuten")


def build_config_iso(topo, state) -> Path:
    out = Path(tempfile.mkdtemp(prefix="fgtiso-"))
    work = out / "drive"
    latest = work / "openstack" / "latest"
    content = work / "openstack" / "content"
    latest.mkdir(parents=True)
    content.mkdir(parents=True)

    meta = {"uuid": "otlab-fgt", "files": []}
    lic_mode = state.get("license_mode")
    if lic_mode == "file" and state.get("license_file"):
        (content / "0000").write_bytes((UPLOADS / state["license_file"]).read_bytes())
        meta["files"].append({"path": "/license", "content_path": "/content/0000"})
    elif lic_mode == "fortiflex" and state.get("fortiflex_token"):
        (content / "0000").write_text(f"LICENSE-TOKEN:{state['fortiflex_token']}\n")
        meta["files"].append({"path": "/license", "content_path": "/content/0000"})

    (latest / "user_data").write_text(fortigate.bootstrap_config(topo, state))
    (latest / "meta_data.json").write_text(json.dumps(meta))

    iso = out / ISO_NAME
    subprocess.run(["genisoimage", "-quiet", "-V", "config-2", "-R", "-J", "-o", str(iso), str(work)], check=True)
    return iso


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
    h.put(tgz, "/tmp/otsim.tgz")
    h.run(["pct", "push", str(vmid), "/tmp/otsim.tgz", "/tmp/otsim.tgz"])
    h.pct_exec(vmid, "mkdir -p /opt/otsim && rm -rf /opt/otsim/otsim && tar xzf /tmp/otsim.tgz -C /opt/otsim "
                     "&& install -m644 /opt/otsim/otsim/otsim.service /etc/systemd/system/otsim.service "
                     "&& systemctl daemon-reload", check=True)


def build_template(log, topo):
    h = host()
    hcfg = load_host()
    vmid = template_vmid(topo)
    if h.exists(vmid):
        log(f"template {vmid} bestaat al, overslaan")
        return
    tnet = topo["lab"].get("template_net", "ip=dhcp")
    log(f"template-LXC {vmid} bouwen (tijdelijk op {topo['lab']['uplink_bridge']} voor packages)")
    h.run(["pct", "create", str(vmid), hcfg["template"], "--hostname", "otlab-template",
           "--cores", "2", "--memory", "1024", "--rootfs", f"{hcfg['storage']}:4",
           "--unprivileged", "1", "--features", "nesting=1",
           "--net0", f"name=eth0,bridge={topo['lab']['uplink_bridge']},{tnet}",
           "--tags", "otlab"], log=log)
    h.run(["pct", "start", str(vmid)])
    for _ in range(60):
        rc, _ = h.pct_exec(vmid, "getent hosts deb.debian.org")
        if rc == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError("template heeft geen internet (DHCP/DNS op de uplink?)")

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
        log(f"{node['name']} ({vmid}) bestaat al")
        return
    tpl = template_vmid(topo)
    log(f"{node['name']} ({vmid}) → {seg['name']} {node['ip']}")
    try:
        h.run(["pct", "clone", str(tpl), str(vmid), "--hostname", node["name"]])
    except HostError:
        log("  linked clone niet mogelijk op deze storage, full clone")
        h.run(["pct", "clone", str(tpl), str(vmid), "--hostname", node["name"], "--full", "1"])
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
        if node["role"] in ("jumphost", "ews"):
            h.pct_exec(vmid, "systemctl enable --now ssh")


def update_simulators(log):
    """Nieuwe otsim-code naar alle draaiende devices pushen (na git pull)."""
    topo = load_topology()
    for node in inventory(topo):
        if host().status(node["vmid"]) == "running":
            log(f"otsim bijwerken op {node['name']}")
            push_otsim(log, node["vmid"])
            if node["kind"] == "device":
                host().pct_exec(node["vmid"], "systemctl restart otsim")


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
    update_state(deployed=True)


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
    update_state(deployed=False, fgt_api_key=None)
