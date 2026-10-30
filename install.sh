#!/usr/bin/env bash
# FortiGate OT Lab - installer voor de Proxmox VE host.
#
# Oneliner:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/kay-arne/fortigate-ot-lab/main/install.sh)"
#
# Variabelen (optioneel, als env vóór de oneliner):
#   REPO_URL     git-URL van deze repo
#   REPO_BRANCH  branch (default: main)
#   CTID         VMID van de controller-LXC (default: 9099)
#   CT_STORAGE   storage voor rootfs (default: local-lvm)
#   CT_BRIDGE    bridge naar het fysieke netwerk (default: vmbr0)
#   CT_IP        "dhcp" of CIDR, bijv. 192.168.1.240/24 (default: dhcp)
#   CT_GW        gateway bij statisch IP
#   UI_PORT      poort van de web UI (default: 8080)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/kay-arne/fortigate-ot-lab.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
CTID="${CTID:-9099}"
CT_STORAGE="${CT_STORAGE:-local-lvm}"
CT_BRIDGE="${CT_BRIDGE:-vmbr0}"
CT_IP="${CT_IP:-dhcp}"
CT_GW="${CT_GW:-}"
UI_PORT="${UI_PORT:-8080}"
CT_HOSTNAME="otlab-controller"

c_info() { printf '\033[1;36m[otlab]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[1;31m[otlab]\033[0m %s\n' "$*" >&2; }
die()    { c_err "$*"; exit 1; }

# --- preflight -------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Draai als root op de Proxmox host."
command -v pveversion >/dev/null || die "Dit is geen Proxmox VE host."
command -v pct >/dev/null || die "pct niet gevonden."
pct status "$CTID" >/dev/null 2>&1 && die "CT $CTID bestaat al. Kies een andere CTID= of verwijder hem eerst."
ip link show "$CT_BRIDGE" >/dev/null 2>&1 || die "Bridge $CT_BRIDGE bestaat niet."
pvesm status | awk 'NR>1{print $1}' | grep -qx "$CT_STORAGE" || die "Storage $CT_STORAGE bestaat niet (zet CT_STORAGE=...)."

NODE="$(hostname)"
HOST_IP="$(ip -4 -o addr show "$CT_BRIDGE" | awk '{print $4}' | cut -d/ -f1 | head -n1)"
[[ -n "$HOST_IP" ]] || die "Kon IP van $CT_BRIDGE niet bepalen."
c_info "Proxmox node: $NODE ($HOST_IP)"

# --- Debian template -------------------------------------------------------
c_info "Debian 12 LXC-template ophalen..."
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -n1)"
[[ -n "$TEMPLATE" ]] || die "Geen debian-12-standard template gevonden."
TPL_STORAGE="local"
if ! pveam list "$TPL_STORAGE" | grep -q "$TEMPLATE"; then
  pveam download "$TPL_STORAGE" "$TEMPLATE"
fi
TPL_REF="$TPL_STORAGE:vztmpl/$TEMPLATE"

# --- controller LXC --------------------------------------------------------
NET0="name=eth0,bridge=$CT_BRIDGE,ip=$CT_IP"
[[ "$CT_IP" != "dhcp" && -n "$CT_GW" ]] && NET0="$NET0,gw=$CT_GW"

c_info "Controller-LXC $CTID aanmaken..."
pct create "$CTID" "$TPL_REF" \
  --hostname "$CT_HOSTNAME" \
  --cores 2 --memory 2048 --swap 512 \
  --rootfs "$CT_STORAGE:16" \
  --net0 "$NET0" \
  --unprivileged 1 --features nesting=1 \
  --onboot 1 \
  --description "FortiGate OT Lab controller - web UI op poort $UI_PORT" \
  --tags "otlab"
pct start "$CTID"

c_info "Wachten op netwerk in de controller..."
for _ in $(seq 1 60); do
  CT_ADDR="$(pct exec "$CTID" -- sh -c "ip -4 -o addr show eth0 | awk '{print \$4}' | cut -d/ -f1" 2>/dev/null || true)"
  [[ -n "$CT_ADDR" ]] && pct exec "$CTID" -- sh -c "getent hosts deb.debian.org >/dev/null" && break
  sleep 2
done
[[ -n "${CT_ADDR:-}" ]] || die "Controller kreeg geen IP-adres."

c_info "Pakketten installeren in de controller..."
pct exec "$CTID" -- bash -c "export DEBIAN_FRONTEND=noninteractive; apt-get update -q && apt-get install -yq git python3-venv python3-pip openssh-client genisoimage ca-certificates curl >/dev/null"

c_info "Repo clonen ($REPO_URL @ $REPO_BRANCH)..."
pct exec "$CTID" -- git clone --depth 1 -b "$REPO_BRANCH" "$REPO_URL" /opt/otlab

pct exec "$CTID" -- bash -c "python3 -m venv /opt/otlab/.venv && /opt/otlab/.venv/bin/pip install -q -r /opt/otlab/controller/requirements.txt"

# --- koppeling controller -> Proxmox host ---------------------------------
# De controller beheert de host via SSH (qm/pct/pvesh). De sleutel wordt
# beperkt tot het IP van de controller.
c_info "SSH-sleutel koppelen aan de Proxmox host..."
pct exec "$CTID" -- bash -c "mkdir -p /opt/otlab/data && ssh-keygen -q -t ed25519 -N '' -C otlab-controller -f /opt/otlab/data/id_ed25519"
PUBKEY="$(pct exec "$CTID" -- cat /opt/otlab/data/id_ed25519.pub)"
mkdir -p /root/.ssh && chmod 700 /root/.ssh
sed -i '/otlab-controller$/d' /root/.ssh/authorized_keys 2>/dev/null || true
echo "from=\"$CT_ADDR\" $PUBKEY" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

pct exec "$CTID" -- bash -c "cat > /opt/otlab/data/host.json" <<EOF
{
  "host": "$HOST_IP",
  "node": "$NODE",
  "ssh_user": "root",
  "ssh_key": "/opt/otlab/data/id_ed25519",
  "storage": "$CT_STORAGE",
  "template": "$TPL_REF",
  "controller_ctid": $CTID
}
EOF

UI_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)"
pct exec "$CTID" -- bash -c "umask 077; echo '$UI_PASSWORD' > /opt/otlab/data/ui_password"

pct exec "$CTID" -- bash -c "ssh -o StrictHostKeyChecking=accept-new -i /opt/otlab/data/id_ed25519 root@$HOST_IP true" \
  || die "Controller kan niet via SSH naar de host."

# --- systemd service ------------------------------------------------------
pct exec "$CTID" -- bash -c "cat > /etc/systemd/system/otlab.service" <<EOF
[Unit]
Description=FortiGate OT Lab controller
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/otlab/controller
ExecStart=/opt/otlab/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $UI_PORT
Restart=on-failure
Environment=OTLAB_ROOT=/opt/otlab

[Install]
WantedBy=multi-user.target
EOF
pct exec "$CTID" -- systemctl daemon-reload
pct exec "$CTID" -- systemctl enable --now otlab.service

c_info "Klaar."
echo
echo "  Web UI:  http://$CT_ADDR:$UI_PORT"
echo "  Login:   admin / $UI_PASSWORD   (staat ook in CT $CTID: /opt/otlab/data/ui_password)"
echo "  Volgende stap: upload het FortiGate-VM (KVM) qcow2-image + licentie in de UI en klik 'Deploy lab'."
echo
